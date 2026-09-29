"""Optional notifications; no response, secret URL, or raw exception is logged."""
import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import re
import smtplib
import ssl

import requests

from src.config import email_smtp_endpoint, valid_email_address, valid_notification_url

BEIJING = datetime.timezone(datetime.timedelta(hours=8))


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
        elif ntype == "email":
            ok = _email(n, title, content)
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


def _email(n: dict, title: str, content: str) -> bool:
    """SMTP over TLS only: implicit TLS on 465, STARTTLS on 587; certificates are verified."""
    address = n.get("email_address", "")
    password = n.get("email_password", "")
    recipient = n.get("email_to", "") or address
    endpoint = email_smtp_endpoint(n)
    if (endpoint is None or not valid_email_address(address) or not valid_email_address(recipient)
            or not isinstance(password, str) or not password):
        return False
    host, port = endpoint
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = address
    message["To"] = recipient
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=address.rpartition("@")[2])
    sent_at = datetime.datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    message.set_content(f"{content}\n\n发送时间：{sent_at}（北京时间）\n此邮件由 fzu-checkin 自动签到服务发送。")
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as server:
            server.login(address, password)
            refused = server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(address, password)
            refused = server.send_message(message)
    return not refused
