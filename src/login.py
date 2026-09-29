"""One password submission to FZU SSO, with a strict TLS redirect allowlist."""
import base64
import html
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

SSO_BASE = "https://sso.fzu.edu.cn/login"
SERVICE = "https://yzsxg.fzu.edu.cn/livecloud/project/fzu/attn/oauth2/callback.action"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_ALLOWED_HOSTS = frozenset(("sso.fzu.edu.cn", "yzsxg.fzu.edu.cn"))
_MESSAGES = {
    "AUTH_CREDENTIALS_MISSING": "认证账号密码未完整配置",
    "AUTH_INTERACTIVE_REQUIRED": "学校认证需要人工验证码、人脸或其他验证，请在官方应用处理",
    "AUTH_REJECTED": "学校拒绝了认证凭据，请人工核对，不会自动重试密码",
    "AUTH_PROTOCOL_CHANGED": "认证响应与已知协议不符，需要人工检查",
    "AUTH_REDIRECT_BLOCKED": "认证重定向目标不在受信任的 HTTPS 学校域名内",
    "AUTH_NETWORK": "学校认证网络或服务暂时异常",
    "AUTH_RATE_LIMITED": "学校认证限制访问，请稍后人工检查",
    "AUTH_MANUAL_REQUIRED": "同一凭据的失败认证已停止重试，请完成人工验证或修正私有配置后按维护说明解除认证锁定",
}


class AuthError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in _MESSAGES else "AUTH_PROTOCOL_CHANGED"
        super().__init__(_MESSAGES[self.code])


def _trusted_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.hostname in _ALLOWED_HOSTS
                 and parts.port in (None, 443) and parts.username is None
                 and parts.password is None and not parts.fragment
                 and not any(char.isspace() or ord(char) < 32 for char in url))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise AuthError("AUTH_REDIRECT_BLOCKED")
    return url


def _optional_field(page: str, element_id: str) -> str:
    match = re.search(
        r'<[^>]+\bid\s*=\s*[\"\']' + re.escape(element_id)
        + r'[\"\'][^>]*>(.*?)</[^>]+>', page, re.I | re.S,
    )
    return html.unescape(re.sub(r"<[^>]*>", "", match.group(1))).strip() if match else ""


def _parse_field(page: str, element_id: str) -> str:
    value = _optional_field(page, element_id)
    if not value:
        raise AuthError("AUTH_PROTOCOL_CHANGED")
    return value


def _encrypt_password(croypto: str, password: str) -> str:
    try:
        key = base64.b64decode(croypto, validate=True)
        if len(key) != 16:
            raise ValueError
        cipher = AES.new(key, AES.MODE_ECB)
        return base64.b64encode(cipher.encrypt(pad(password.encode("utf-8"), AES.block_size))).decode("ascii")
    except (ValueError, TypeError, UnicodeError):
        raise AuthError("AUTH_PROTOCOL_CHANGED") from None


def _extract_token(url: str):
    _trusted_url(url)
    parts = urlsplit(url)
    tokens = parse_qs(parts.query).get("token", [])
    if (parts.hostname == "yzsxg.fzu.edu.cn" and len(tokens) == 1
            and re.fullmatch(r"[A-Za-z0-9._~-]{20,4096}", tokens[0])):
        return tokens[0]
    return None


def _interactive(page: str) -> bool:
    error = _optional_field(page, "login-error-msg")
    if re.search(r"验证码|人脸|二次验证|短信验证|captcha|face.?verif|multi.?factor", error, re.I):
        return True
    # Active controls / explicit requirements, not arbitrary JS references to
    # optional captcha features that may exist on every normal login page.
    for control in re.findall(r"<input\b[^>]*>", page, re.I):
        if (re.search(r"\b(?:name|id)\s*=\s*[\"\'][^\"\']*(?:captcha|verifycode)", control, re.I)
                and not re.search(r"\btype\s*=\s*[\"\']hidden[\"\']", control, re.I)):
            return True
    return bool(re.search(r"(?:请完成|需要|请进行|必须进行).{0,12}(?:验证码|人脸|二次验证|短信验证)", page))


def _check_http(response, *, credentials_sent=False):
    _trusted_url(response.url)
    if response.status_code == 429:
        raise AuthError("AUTH_RATE_LIMITED")
    if _interactive(response.text):
        raise AuthError("AUTH_INTERACTIVE_REQUIRED")
    if credentials_sent and response.status_code in (401, 403):
        raise AuthError("AUTH_REJECTED")
    if response.status_code >= 400 or response.status_code < 200:
        raise AuthError("AUTH_NETWORK")


def _follow_get_redirects(session, response, *, credentials_sent=False):
    for hop in range(9):
        _check_http(response, credentials_sent=credentials_sent)
        token = _extract_token(response.url)
        if token:
            return response, token
        if response.status_code not in (301, 302, 303, 307, 308):
            return response, None
        if hop == 8 or (credentials_sent and response.status_code in (307, 308)):
            # Never replay a password POST, even to an otherwise allowed host.
            raise AuthError("AUTH_PROTOCOL_CHANGED")
        target = response.headers.get("Location")
        if not isinstance(target, str) or not target:
            raise AuthError("AUTH_PROTOCOL_CHANGED")
        target = _trusted_url(urljoin(response.url, target))
        token = _extract_token(target)
        if token:
            return response, token
        response = session.get(target, timeout=15, allow_redirects=False)
        credentials_sent = False
    raise AuthError("AUTH_PROTOCOL_CHANGED")


def login(username: str, password: str) -> str:
    """Return a token or a sanitized AuthError; never retry a password POST."""
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise AuthError("AUTH_CREDENTIALS_MISSING")
    session = None
    try:
        session = requests.Session()
        # Do not discover ~/.netrc credentials or unrelated process proxy data.
        session.trust_env = False
        session.headers.update({"User-Agent": UA})
        response = session.get(SSO_BASE, params={"service": SERVICE}, timeout=15, allow_redirects=False)
        response, token = _follow_get_redirects(session, response)
        if token:
            return token
        # Credentials only go to the original SSO endpoint, not a form action.
        if urlsplit(response.url).hostname != "sso.fzu.edu.cn":
            raise AuthError("AUTH_PROTOCOL_CHANGED")
        croypto = _parse_field(response.text, "login-croypto")
        execution = _parse_field(response.text, "login-page-flowkey")
        data = {
            "type": "UsernamePassword", "_eventId": "submit", "geolocation": "",
            "execution": execution, "croypto": croypto, "username": username,
            "password": _encrypt_password(croypto, password),
        }
        response = session.post(
            SSO_BASE, params={"service": SERVICE}, data=data,
            timeout=20, allow_redirects=False,
        )
        response, token = _follow_get_redirects(session, response, credentials_sent=True)
        if token:
            return token
        page = response.text
        if ("login-page-flowkey" in page and "login-croypto" not in page
                and re.search(r"验证码|短信|人脸|二次验证|captcha", page, re.I)):
            # The password step was accepted and the SSO rendered a follow-up verification
            # step (SMS/captcha/face); it cannot and must not be completed automatically.
            raise AuthError("AUTH_INTERACTIVE_REQUIRED")
        error = _optional_field(page, "login-error-msg")
        if re.search(r"密码|用户名|账号|账户|学号|认证失败|password|credential|invalid.login", error, re.I):
            raise AuthError("AUTH_REJECTED")
        raise AuthError("AUTH_PROTOCOL_CHANGED")
    except AuthError:
        raise
    except requests.RequestException:
        raise AuthError("AUTH_NETWORK") from None
    except Exception:
        raise AuthError("AUTH_PROTOCOL_CHANGED") from None
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
