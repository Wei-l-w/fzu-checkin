"""Owner-approved registration. Pending requests never enter the login store."""
from contextlib import contextmanager
import base64
import datetime as dt
import hmac
import math
from pathlib import Path
import re
import secrets
import threading
import time

from src.admin_auth import (AuthConfigurationError, SCRYPT_N, SCRYPT_R, SCRYPT_P,
                            _derive, _parse_record, _password_bytes, _read_private_json,
                            _write_private_json, new_user_record, private_file_lock,
                            valid_user_id)

MAX_REQUESTS = 256
MAX_PENDING = 64
RATE_WINDOW = 600
RATE_LIMITS = {"apply": 10, "status": 30}
REQUEST_PATTERN = re.compile(r"[a-f0-9]{32}\Z")
STATES = {"pending", "approving", "approved", "rejected"}


class RegistrationError(Exception):
    def __init__(self, code, message, status=400, retry_after=None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


def clean_note(value):
    if (not isinstance(value, str) or len(value) > 200
            or any(ord(char) < 32 and char not in "\n\t" for char in value)):
        raise RegistrationError("REGISTRATION_TEXT_INVALID", "说明或审核原因最多 200 个字符，不可含控制字符")
    return value.strip()


def _stamp(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="microseconds")


def _auth_json(record):
    return {"algorithm": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": base64.b64encode(record["salt"]).decode("ascii"),
            "digest": base64.b64encode(record["digest"]).decode("ascii")}


class RegistrationManager:
    def __init__(self, auth, profiles, *, path=None, clock=time.time):
        self.auth, self.profiles, self.clock = auth, profiles, clock
        self.path = Path(path) if path is not None else auth.auth_file.with_name("registrations.json")
        self._mutex = threading.RLock()

    @staticmethod
    def _empty():
        return {"version": 1, "enabled": False, "requests": {}, "attempts": {"apply": [], "status": []}}

    def _read(self):
        try:
            self.path.lstat()
        except FileNotFoundError:
            return self._empty()
        value = _read_private_json(self.path, 1024 * 1024)
        if (not isinstance(value, dict) or set(value) != {"version", "enabled", "requests", "attempts"}
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["enabled"]) is not bool or not isinstance(value["requests"], dict)
                or len(value["requests"]) > MAX_REQUESTS or not isinstance(value["attempts"], dict)
                or set(value["attempts"]) != set(RATE_LIMITS)):
            raise AuthConfigurationError()
        fields = {"username", "note", "status", "created_at", "reviewed_at", "reviewed_by", "reason", "auth"}
        for request_id, item in value["requests"].items():
            if (not REQUEST_PATTERN.fullmatch(request_id) or not isinstance(item, dict) or set(item) != fields
                    or not valid_user_id(item["username"]) or not isinstance(item["status"], str) or item["status"] not in STATES
                    or any(not isinstance(item[key], str) or len(item[key]) > 200
                           for key in ("note", "reason", "created_at", "reviewed_at", "reviewed_by"))):
                raise AuthConfigurationError()
            _parse_record(item["auth"])
        for action, attempts in value["attempts"].items():
            if (not isinstance(attempts, list) or len(attempts) > RATE_LIMITS[action]
                    or any(type(stamp) not in (int, float) or not math.isfinite(stamp) for stamp in attempts)):
                raise AuthConfigurationError()
        return value

    @contextmanager
    def _locked(self):
        with self._mutex, private_file_lock(self.path.with_suffix(".lock")):
            yield self._read()

    def _write(self, data):
        _write_private_json(self.path, data)

    def _limit(self, data, action):
        now = self.clock()
        attempts = [stamp for stamp in data["attempts"][action] if now - stamp < RATE_WINDOW]
        if len(attempts) >= RATE_LIMITS[action]:
            retry = max(1, min(RATE_WINDOW, math.ceil(RATE_WINDOW - (now - min(attempts)))))
            raise RegistrationError("REGISTRATION_LIMITED", "申请或查询过于频繁，请稍后再试", 429, retry)
        attempts.append(now)
        data["attempts"][action] = attempts
        self._write(data)  # Persist before password hashing, even on failed requests.

    def _latest(self, data, username):
        matches = [(key, item) for key, item in data["requests"].items() if item["username"] == username]
        return max(matches, key=lambda pair: pair[1]["created_at"], default=(None, None))

    def public_settings(self):
        return {"enabled": self._read()["enabled"]}

    @staticmethod
    def _public_request(request_id, item):
        return {"id": request_id, **{key: item[key] for key in
                ("username", "note", "status", "created_at", "reviewed_at", "reviewed_by", "reason")}}

    def _overview(self, data):
        rows = [self._public_request(key, item) for key, item in data["requests"].items()]
        rows.sort(key=lambda item: (item["status"] in {"pending", "approving"}, item["created_at"]), reverse=True)
        return {"enabled": data["enabled"], "pending_count": sum(item["status"] in {"pending", "approving"} for item in rows),
                "requests": rows}

    def overview(self):
        return self._overview(self._read())

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise RegistrationError("REGISTRATION_SETTINGS_INVALID", "注册开关必须为开启或关闭")
        with self._locked() as data:
            data["enabled"] = enabled
            self._write(data)
            return self._overview(data)

    def apply(self, username, password, note):
        if not valid_user_id(username):
            raise RegistrationError("REGISTRATION_INVALID", "用户名需为 2–24 位小写字母、数字或下划线，且以字母开头")
        if _password_bytes(password) is None or len(password) < 10:
            raise RegistrationError("REGISTRATION_INVALID", "密码至少 10 个字符，且不超过 1024 UTF-8 字节")
        note = clean_note(note)
        with self._locked() as data:
            if not data["enabled"]:
                raise RegistrationError("REGISTRATION_CLOSED", "暂未开放新注册；已提交的申请仍可查询结果", 403)
            self._limit(data, "apply")
            _, previous = self._latest(data, username)
            if any(user["id"] == username for user in self.auth.list_users()) or previous and previous["status"] != "rejected":
                raise RegistrationError("REGISTRATION_UNAVAILABLE", "该用户名不可申请或已有申请；已有申请请查询审核结果", 409)
            if (len(data["requests"]) >= MAX_REQUESTS
                    or sum(item["status"] in {"pending", "approving"} for item in data["requests"].values()) >= MAX_PENDING):
                raise RegistrationError("REGISTRATION_CAPACITY", "申请数量已达上限，请联系管理员", 409)
            if not self.auth._kdf_slots.acquire(blocking=False):
                raise RegistrationError("REGISTRATION_LIMITED", "服务繁忙，请稍后再试", 429, 2)
            try:
                record = new_user_record(password, "member")
            finally:
                self.auth._kdf_slots.release()
            request_id = secrets.token_hex(16)
            created = self.clock()
            if previous:
                created = max(created, dt.datetime.fromisoformat(previous["created_at"]).timestamp() + 0.000001)
            data["requests"][request_id] = {"username": username, "note": note, "status": "pending",
                "created_at": _stamp(created), "reviewed_at": "", "reviewed_by": "", "reason": "", "auth": _auth_json(record)}
            self._write(data)
            return {"ok": True, "status": "pending", "message": "申请已提交，等待管理员审批；获批前不能登录签到管理"}

    def applicant_status(self, username, password):
        encoded = _password_bytes(password)
        if not valid_user_id(username) or encoded is None:
            raise RegistrationError("REGISTRATION_QUERY_FAILED", "申请用户名或密码不正确", 401)
        with self._locked() as data:
            self._limit(data, "status")
            _, item = self._latest(data, username)
            salt, digest = _parse_record(item["auth"]) if item else (bytes(32), bytes(32))
            if not self.auth._kdf_slots.acquire(blocking=False):
                raise RegistrationError("REGISTRATION_LIMITED", "服务繁忙，请稍后再试", 429, 2)
            try:
                matched = hmac.compare_digest(_derive(encoded, salt), digest)
            finally:
                self.auth._kdf_slots.release()
            if item is None or not matched:
                raise RegistrationError("REGISTRATION_QUERY_FAILED", "申请用户名或密码不正确", 401)
            return {key: item[key] for key in ("status", "created_at", "reviewed_at", "reason")}

    def review(self, request_id, decision, reviewer, reason=""):
        if not REQUEST_PATTERN.fullmatch(request_id) or decision not in {"approve", "reject"}:
            raise RegistrationError("REGISTRATION_INVALID", "审批请求无效")
        reason = clean_note(reason)
        with self._locked() as data:
            item = data["requests"].get(request_id)
            if item is None:
                raise RegistrationError("REGISTRATION_NOT_FOUND", "申请不存在，请刷新列表", 404)
            target_status = "approved" if decision == "approve" else "rejected"
            if item["status"] == target_status:
                return self._overview(data)  # Idempotent; never resurrect a removed member.
            if item["status"] not in {"pending", "approving"} or item["status"] == "approving" and decision != "approve":
                raise RegistrationError("REGISTRATION_ALREADY_REVIEWED", "审批状态已变化，请刷新；处理中申请请重试通过", 409)
            if decision == "approve":
                item.update(status="approving", reviewed_by=reviewer, reviewed_at=_stamp(self.clock()))
                self._write(data)  # Durable explicit approval intent before account provisioning.
                salt, digest = _parse_record(item["auth"])
                record = {"role": "member", "created_at": item["created_at"], "salt": salt, "digest": digest}
                def provision():
                    try:
                        self.profiles.create_registration_profile(item["username"], request_id)
                    except Exception:
                        # This callback runs before the account commit. A failed
                        # provision is still pending and can be rejected/retried.
                        item["status"] = "pending"
                        self._write(data)
                        raise
                try:
                    self.auth.add_approved_member(item["username"], record, provision)
                except ValueError as error:
                    item["status"] = "pending"
                    self._write(data)
                    raise RegistrationError("REGISTRATION_APPROVAL_BLOCKED", str(error), 409) from None
            item.update(status=target_status, reviewed_by=reviewer, reviewed_at=_stamp(self.clock()), reason=reason)
            self._write(data)
            return self._overview(data)
