"""Optional notifications; no response, secret URL, or raw exception is logged."""
import re

import requests

from src.config import valid_notification_url


def notify(cfg: dict, title: str, content: str) -> bool:
    n = cfg.get("notify", {})
    if not isinstance(n, dict):
        return False
    ntype = n.get("type", "none")
    if ntype == "none":
        return False
    try:
        if ntype == "serverchan":
            ok = _serverchan(n.get("serverchan_key", ""), title, content)
        elif ntype == "bark":
            ok = _bark(n.get("bark_url", ""), title, content)
        elif ntype == "wecom":
            ok = _wecom(n.get("wecom_webhook", ""), title, content)
        else:
            ok = False
    except Exception:
        # requests exceptions can contain full token-bearing URLs.
        print("[notify] NOTIFY_FAILED: 推送失败，请检查渠道配置或网络")
        return False
    print("[notify] NOTIFY_ACCEPTED" if ok else "[notify] NOTIFY_FAILED")
    return ok


def _business_ok(response, field: str, expected: int) -> bool:
    if response.status_code != 200:
        return False
    data = response.json()
    return isinstance(data, dict) and type(data.get(field)) is int and data[field] == expected


def _post(url, **kwargs):
    # No implicit ~/.netrc credentials and no secret-bearing redirects.
    with requests.Session() as session:
        session.trust_env = False
        return session.post(url, timeout=15, allow_redirects=False, **kwargs)


def _serverchan(key: str, title: str, content: str) -> bool:
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_-]{8,256}", key) is None:
        return False
    response = _post(
        f"https://sctapi.ftqq.com/{key}.send",
        data={"title": title, "desp": content},
    )
    return _business_ok(response, "code", 0)


def _bark(url: str, title: str, content: str) -> bool:
    if not valid_notification_url(url, "bark"):
        return False
    response = _post(
        url, json={"title": title, "body": content, "level": "timeSensitive"},
    )
    return _business_ok(response, "code", 200)


def _wecom(webhook: str, title: str, content: str) -> bool:
    if not valid_notification_url(webhook, "wecom"):
        return False
    response = _post(
        webhook, json={"msgtype": "text", "text": {"content": f"{title}\n{content}"}},
    )
    return _business_ok(response, "errcode", 0)
