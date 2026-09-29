"""Private configuration and state; errors never contain configuration values."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

CONFIG_PATH = "/var/lib/fzu-checkin/config.yaml"
_ERROR_MESSAGES = {
    "CONFIG_MISSING": "私有配置尚未创建",
    "CONFIG_INVALID": "私有配置格式无效",
    "CONFIG_READ_FAILED": "无法读取私有配置或状态",
    "CONFIG_PERMISSIONS": "私有目录或文件权限不安全",
    "CONFIG_PATH_INVALID": "私有配置或状态路径无效",
    "CONFIG_SESSION_INVALID": "私有登录态格式无效",
    "CONFIG_WRITE_FAILED": "无法安全保存私有状态",
    "CONFIG_DATE_INVALID": "跳过日期或假期区间无效",
    "state_invalid": "私有运行状态无效，不能安全继续",
    "state_permissions": "私有状态目录或文件权限不安全",
}


class ConfigError(RuntimeError):
    """A safe message and stable code, with no path, value, or raw exception."""

    def __init__(self, code: str = "CONFIG_INVALID"):
        self.code = code if code in _ERROR_MESSAGES else "CONFIG_INVALID"
        super().__init__(_ERROR_MESSAGES[self.code])


def _absolute_path(value) -> Path:
    try:
        value = os.fspath(value)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError
        return Path(os.path.abspath(value))
    except (TypeError, ValueError, OSError):
        raise ConfigError("CONFIG_PATH_INVALID") from None


def state_dir(cfg: dict) -> Path:
    """No home-directory/repository fallback: only this project's private path."""
    if cfg.get("_state_dir"):
        return _absolute_path(cfg["_state_dir"])
    config_path = _absolute_path(cfg.get("_config_path", CONFIG_PATH))
    return _absolute_path(os.environ.get("FZU_CHECKIN_STATE_DIR", str(config_path.parent / "state")))


def _check_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise ConfigError("CONFIG_PERMISSIONS")
    except ConfigError:
        raise
    except OSError:
        raise ConfigError("CONFIG_READ_FAILED") from None


def _read_private(path: Path, *, missing_ok=False) -> str | None:
    """Refuse symlinks, public files, and oversized data before parsing."""
    fd = None
    try:
        _check_private_directory(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
            raise ConfigError("CONFIG_PERMISSIONS")
        if info.st_size > 1024 * 1024:
            raise ConfigError("CONFIG_INVALID")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = None
            value = handle.read(1024 * 1024 + 1)
        if len(value) > 1024 * 1024:
            raise ConfigError("CONFIG_INVALID")
        return value
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ConfigError("CONFIG_MISSING") from None
    except ConfigError:
        raise
    except (OSError, UnicodeError):
        raise ConfigError("CONFIG_READ_FAILED") from None
    finally:
        if fd is not None:
            os.close(fd)


def atomic_write_json(path, value) -> None:
    """Atomic replace + fsync; new files 0600, immediate directory 0700.

    Existing public directories are refused, not chmod'ed. This deliberately
    cannot turn /tmp or another caller-supplied shared directory private.
    """
    path = _absolute_path(path)
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
        _check_private_directory(path.parent)
        if path.is_symlink():
            raise ConfigError("CONFIG_PERMISSIONS")
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except ConfigError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError):
        raise ConfigError("CONFIG_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _credentials_fingerprint(user: dict) -> str:
    # This secret-derived hash is only written beside the token in private
    # session.json. It must never be logged or copied into public code/reports.
    identity = {key: user.get(key, "") for key in ("username", "password", "token")}
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _credential_shape_valid(user) -> bool:
    return (isinstance(user, dict)
            and all(isinstance(user.get(key, ""), str) for key in ("username", "password", "token"))
            and bool(user.get("token") or (user.get("username") and user.get("password"))))


def load_config(path=None) -> dict:
    """Parse even an unfilled template; readiness is validation_errors' job."""
    path = _absolute_path(path if path is not None else os.environ.get("FZU_CHECKIN_CONFIG", CONFIG_PATH))
    try:
        parsed = yaml.safe_load(_read_private(path))
    except (yaml.YAMLError, ValueError, TypeError):
        raise ConfigError("CONFIG_INVALID") from None
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
        raise ConfigError("CONFIG_INVALID")
    # Internal metadata cannot be forged by entries in YAML.
    cfg = {key: value for key, value in parsed.items() if not key.startswith("_")}
    cfg["_config_path"] = str(path)
    cfg["_state_dir"] = str(_absolute_path(os.environ.get("FZU_CHECKIN_STATE_DIR", str(path.parent / "state"))))
    user = cfg.get("user", {})
    if _credential_shape_valid(user):
        fingerprint = _credentials_fingerprint(user)
        cfg["_credentials_fingerprint"] = fingerprint
        session_path = state_dir(cfg) / "session.json"
        # An absent state directory is normal on the first configuration read.
        if state_dir(cfg).exists():
            text = _read_private(session_path, missing_ok=True)
            if text is not None:
                try:
                    session = json.loads(text)
                except (ValueError, TypeError):
                    raise ConfigError("CONFIG_SESSION_INVALID") from None
                if not isinstance(session, dict):
                    raise ConfigError("CONFIG_SESSION_INVALID")
                if session.get("credential_fingerprint") == fingerprint:
                    if (session.get("version") != 1 or not isinstance(session.get("token"), str)
                            or not session["token"] or len(session["token"]) > 4096
                            or any(char.isspace() for char in session["token"])):
                        raise ConfigError("CONFIG_SESSION_INVALID") from None
                    user["token"] = session["token"]
                    cfg["_token_from_session"] = True
    return cfg


def save_token(cfg: dict, token: str) -> None:
    """Do not rewrite user-edited YAML; bind the cache to its credential source."""
    if (not isinstance(token, str) or not token or len(token) > 4096
            or any(char.isspace() for char in token)):
        raise ConfigError("CONFIG_SESSION_INVALID")
    fingerprint = cfg.get("_credentials_fingerprint")
    if not fingerprint:
        user = cfg.get("user", {})
        if not _credential_shape_valid(user):
            raise ConfigError("CONFIG_INVALID")
        fingerprint = _credentials_fingerprint(user)
    atomic_write_json(state_dir(cfg) / "session.json", {
        "version": 1,
        "credential_fingerprint": fingerprint,
        "token": token,
        "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    cfg.setdefault("user", {})["token"] = token


def parse_date(value) -> dt.date:
    """Accept ISO date strings or YAML-native dates, never timestamps/shortcuts."""
    if type(value) is dt.date:
        return value
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ConfigError("CONFIG_DATE_INVALID")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ConfigError("CONFIG_DATE_INVALID") from None


def validated_ranges(cfg: dict) -> list[tuple[dt.date, dt.date, str]]:
    vacation = cfg.get("vacation", {})
    if not isinstance(vacation, dict) or not isinstance(vacation.get("skip_ranges", []), list):
        raise ConfigError("CONFIG_DATE_INVALID")
    parsed = []
    for item in vacation.get("skip_ranges", []):
        if not isinstance(item, dict):
            raise ConfigError("CONFIG_DATE_INVALID")
        start = parse_date(item.get("start"))
        end = parse_date(item.get("end"))
        if start > end or not isinstance(item.get("name", ""), str):
            raise ConfigError("CONFIG_DATE_INVALID")
        name = item.get("name", "")
        if len(name) > 120 or any(ord(char) < 32 for char in name):
            raise ConfigError("CONFIG_DATE_INVALID")
        parsed.append((start, end, name or "自定义跳过日期区间"))
    return parsed


def valid_notification_url(value: str, provider: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return False
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.fragment
                or any(char.isspace() or ord(char) < 32 for char in value)):
            return False
        if url.port is not None and not 1 <= url.port <= 65535:
            return False
        if provider == "wecom":
            return (url.hostname == "qyapi.weixin.qq.com" and url.port in (None, 443)
                    and url.path == "/cgi-bin/webhook/send"
                    and bool(parse_qs(url.query).get("key", [""])[0]))
        return provider == "bark" and bool(url.path.strip("/"))
    except (ValueError, TypeError):
        return False


def normalize_token(value: str) -> str:
    """Reduce a pasted post-login URL or address-bar fragment to the bare token value.

    "https://.../index.action?token=ABC&contextPath=" -> "ABC"; "=ABC&contextPath=" -> "ABC";
    a plain token passes through unchanged (percent-decoded, surrounding whitespace removed)."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    match = re.search(r"(?:^|[?&#])token=([^&#\s]+)", text)
    token = match.group(1) if match else re.sub(r"^[?=]+", "", text).split("&", 1)[0].split("#", 1)[0]
    return unquote(token).strip()


DEFAULT_SCHEDULE_TIMES = ("21:35", "21:40", "21:50")
SCHEDULE_EARLIEST, SCHEDULE_LATEST, SCHEDULE_MAX = "21:00", "23:55", 6
SCHEDULE_MESSAGE = "schedule.times: 需要 1–6 个北京时间 HH:MM（21:00–23:55），按先后顺序且不重复"


def valid_schedule_times(value) -> bool:
    """Strict zero-padded HH:MM list inside the evening window, ascending and unique."""
    if not isinstance(value, list) or not 1 <= len(value) <= SCHEDULE_MAX:
        return False
    previous = None
    for item in value:
        if not isinstance(item, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item):
            return False
        if not SCHEDULE_EARLIEST <= item <= SCHEDULE_LATEST or (previous is not None and item <= previous):
            return False
        previous = item
    return True


def schedule_times(cfg: dict) -> list[str]:
    """Configured evening run times (Beijing), or the built-in defaults when unset/invalid."""
    schedule = cfg.get("schedule") if isinstance(cfg, dict) else None
    times = schedule.get("times") if isinstance(schedule, dict) else None
    return list(times) if valid_schedule_times(times) else list(DEFAULT_SCHEDULE_TIMES)


def validation_errors(cfg: dict) -> list[str]:
    """Safe fixed field descriptions, never interpolation of the supplied value."""
    errors = []
    if not isinstance(cfg, dict):
        return ["config: 必须为映射结构"]
    for section in ("user", "checkin", "vacation", "notify", "campus", "schedule"):
        if section in cfg and not isinstance(cfg[section], dict):
            errors.append(f"{section}: 必须为映射结构")
    user = cfg.get("user") if isinstance(cfg.get("user"), dict) else {}
    checkin = cfg.get("checkin") if isinstance(cfg.get("checkin"), dict) else {}
    notification = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    for field in ("username", "password", "token"):
        value = user.get(field, "")
        if (not isinstance(value, str) or len(value) > 4096
                or (isinstance(value, str) and any(ord(char) < 32 for char in value))):
            errors.append(f"user.{field}: 必须为有效字符串")
    if not _credential_shape_valid(user):
        errors.append("user: 需要 token 或完整的 username/password")
    if bool(user.get("username")) != bool(user.get("password")):
        errors.append("user.username/password: 必须同时填写或同时留空")
    if isinstance(user.get("token"), str) and any(char.isspace() for char in user["token"]):
        errors.append("user.token: 不得包含空白字符")
    elif isinstance(user.get("token"), str) and user["token"] and not re.fullmatch(r"[A-Za-z0-9._~-]{20,4096}", user["token"]):
        # Same token alphabet the SSO extractor accepts; a URL, "&contextPath=" tail or short
        # fragment pasted from an address bar is rejected here instead of failing at the school API.
        errors.append("user.token: 格式不符，请复制登录后地址栏的完整链接（含 token=）粘贴，不要只复制末尾片段")
    if type(cfg.get("enabled")) is not bool:
        errors.append("enabled: 必须为布尔值，初始为 false")
    if "paused" in cfg and type(cfg["paused"]) is not bool:
        errors.append("paused: 必须为布尔值")
    if "pause" in cfg:
        errors.append("pause: 请使用 paused 布尔字段或本机暂停命令")
    if checkin.get("coordinate_system") != "GCJ-02":
        errors.append("checkin.coordinate_system: 必须明确为 GCJ-02")
    if checkin.get("confirmed") is not True:
        errors.append("checkin.confirmed: 必须由本人核对位置后明确设为 true")
    for field, limit in (("longitude", 180), ("latitude", 90)):
        value = checkin.get(field)
        try:
            number = float(value)
            valid = not isinstance(value, bool) and math.isfinite(number) and -limit <= number <= limit
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            errors.append(f"checkin.{field}: 必须为有效的有限经纬度")
    address = checkin.get("actual_location")
    if (not isinstance(address, str) or not address.strip() or len(address) > 300
            or (isinstance(address, str) and any(ord(char) < 32 for char in address))):
        errors.append("checkin.actual_location: 需要本人核对的有效地址文字")
    campus = cfg.get("campus", {})
    if isinstance(campus, dict) and "bounds" in campus:
        errors.append("campus.bounds: 不允许自定义或扩大内置范围")
    dates = cfg.get("skip_dates", [])
    if not isinstance(dates, list):
        errors.append("skip_dates: 必须为 YYYY-MM-DD 日期列表")
    else:
        try:
            for date in dates:
                parse_date(date)
        except ConfigError:
            errors.append("skip_dates: 必须全部为有效的 YYYY-MM-DD 日期")
    try:
        validated_ranges(cfg)
    except ConfigError:
        errors.append("vacation.skip_ranges: 每个区间必须具有有效日期且 start 不晚于 end")
    vacation = cfg.get("vacation", {})
    if isinstance(vacation, dict) and "notify" in vacation and type(vacation["notify"]) is not bool:
        errors.append("vacation.notify: 必须为布尔值")
    schedule = cfg.get("schedule")
    if isinstance(schedule, dict) and (set(schedule) - {"times"} or not valid_schedule_times(schedule.get("times"))):
        errors.append(SCHEDULE_MESSAGE)
    ntype = notification.get("type", "none")
    if ntype not in ("none", "serverchan", "bark", "wecom"):
        errors.append("notify.type: 只允许 none/serverchan/bark/wecom")
    fields = {"serverchan": "serverchan_key", "bark": "bark_url", "wecom": "wecom_webhook"}
    for provider, field in fields.items():
        value = notification.get(field, "")
        if not isinstance(value, str):
            errors.append(f"notify.{field}: 必须为字符串")
        elif value and provider != ntype:
            errors.append(f"notify.{field}: 仅能填写当前选中的一种渠道")
    if ntype == "serverchan":
        key = notification.get("serverchan_key", "")
        if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_-]{8,256}", key) is None:
            errors.append("notify.serverchan_key: 需要有效的 Server酱 SendKey")
    elif ntype in ("bark", "wecom"):
        field = fields[ntype]
        if not valid_notification_url(notification.get(field, ""), ntype):
            errors.append(f"notify.{field}: 需要有效的 HTTPS 推送地址")
    if "daily_confirm" in notification and type(notification["daily_confirm"]) is not bool:
        errors.append("notify.daily_confirm: 必须为布尔值")
    return errors
