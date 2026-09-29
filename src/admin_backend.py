"""Authenticated admin business operations; no API can submit attendance now."""
import copy
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading

import yaml

from main import LABELS, control_lock, read_state, run_lock
from src.checkin import SafeCheckinError, beijing_now
from src.config import (CONFIG_PATH, ConfigError, _read_private, atomic_write_json,
                        load_config, state_dir, validation_errors)
from src.config import DEFAULT_SCHEDULE_TIMES, normalize_token, schedule_times
from src.config import EMAIL_ADDRESS_MESSAGE, EMAIL_PASSWORD_MESSAGE
from src.admin_auth import valid_user_id
from src.history import read_history, record_event, safe_result

SECRET_PATHS = frozenset({"user.password", "user.token", "notify.bark_url",
                          "notify.serverchan_key", "notify.wecom_webhook", "notify.email_password"})
EMAIL_TEXT_PATHS = frozenset({"notify.email_address", "notify.email_to", "notify.email_smtp"})
ACTIONS = frozenset({"pause", "resume", "preflight", "notify-test"})


class BackendError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def _json_dates(value):
    if type(value) is dt.date:
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_dates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_dates(item) for item in value]
    return value


PROFILES_ROOT = Path("/var/lib/fzu-checkin/profiles")


def default_profile_config():
    """Blank private config for a newly created member profile (valid YAML as JSON)."""
    return {"enabled": False, "user": {"token": "", "username": "", "password": ""},
            "checkin": {"coordinate_system": "GCJ-02", "confirmed": False, "longitude": "",
                        "latitude": "", "actual_location": ""},
            "skip_dates": [], "vacation": {"notify": False, "skip_ranges": []},
            "notify": {"type": "none", "bark_url": "", "serverchan_key": "", "wecom_webhook": "",
                       "email_address": "", "email_password": "", "email_to": "", "email_smtp": "", "daily_confirm": False},
            "schedule": {"times": list(DEFAULT_SCHEDULE_TIMES)}}


class Backend:
    def __init__(self, *, path=None, state_dir=None, controller=None, profile=None):
        self.path = Path(path) if path is not None else Path(os.environ.get("FZU_CHECKIN_CONFIG", CONFIG_PATH))
        self.state_path = (Path(state_dir) if state_dir is not None
                           else Path(os.environ.get("FZU_CHECKIN_STATE_DIR", str(self.path.parent / "state"))))
        self.profile = profile
        self.controller = controller or self._control
        self._revision_key = secrets.token_bytes(32)
        self._mutex = threading.RLock()
        self._job = None
        self._saving = False

    def _raw(self):
        try:
            contents = _read_private(self.path)
            value = yaml.safe_load(contents)
            if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
                raise ValueError
            return _json_dates({key: item for key, item in value.items() if not key.startswith("_")})
        except (ConfigError, ValueError, TypeError, yaml.YAMLError):
            raise BackendError("config_unavailable", "私有配置无法安全读取，请在服务器检查格式与权限。", 503) from None

    def _cfg(self, raw=None):
        cfg = copy.deepcopy(self._raw() if raw is None else raw)
        cfg["_config_path"] = str(self.path)
        cfg["_state_dir"] = str(self.state_path)
        return cfg

    def _revision(self, raw):
        body = json.dumps(raw, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        return hmac.new(self._revision_key, body, hashlib.sha256).hexdigest()

    def get_config(self):
        raw = self._raw()
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        checkin = raw.get("checkin") if isinstance(raw.get("checkin"), dict) else {}
        notification = raw.get("notify") if isinstance(raw.get("notify"), dict) else {}
        safe = {"enabled": raw.get("enabled") is True,
                "user": {},
                "checkin": {key: checkin.get(key, default) for key, default in
                            {"coordinate_system": "GCJ-02", "confirmed": False, "longitude": "",
                             "latitude": "", "actual_location": ""}.items()},
                "notify": {"type": notification.get("type", "none"),
                           **{key: notification.get(key, "") if isinstance(notification.get(key, ""), str) else ""
                              for key in ("email_address", "email_to", "email_smtp")}},
                "skip_dates": raw.get("skip_dates", []),
                "vacation": {"skip_ranges": (raw.get("vacation") or {}).get("skip_ranges", [])}}
        flags = {"password": bool(user.get("password")), "token": bool(user.get("token")),
                 **{key: bool(notification.get(key)) for key in ("bark_url", "serverchan_key", "wecom_webhook", "email_password")}}
        try:
            effective = load_config(self.path)
            flags["token"] = bool((effective.get("user") or {}).get("token"))
        except ConfigError:
            pass
        return {"config": safe, "configured": flags, "revision": self._revision(raw),
                "validation": validation_errors(self._cfg(raw))}

    def _merge(self, raw, payload):
        if not isinstance(payload, dict) or set(payload) - {"config", "clear_secrets", "revision"}:
            raise BackendError("invalid_fields", "配置请求含不支持的字段。")
        fields = payload.get("config")
        if not isinstance(fields, dict) or set(fields) - {"enabled", "user", "checkin", "notify", "skip_dates", "vacation", "schedule"}:
            raise BackendError("invalid_fields", "请只填写页面支持的配置字段。")
        clears = payload.get("clear_secrets", [])
        if not isinstance(clears, list) or any(not isinstance(item, str) or item not in SECRET_PATHS for item in clears):
            raise BackendError("invalid_clear", "清除凭据的选择无效。")
        result = copy.deepcopy(raw)
        allowed = {"user": {"username", "password", "token"},
                   "checkin": {"longitude", "latitude", "actual_location", "coordinate_system", "confirmed"},
                   "notify": {"type", "bark_url", "serverchan_key", "wecom_webhook",
                              "email_address", "email_password", "email_to", "email_smtp"},
                   "vacation": {"skip_ranges"}, "schedule": {"times"}}
        for section, value in fields.items():
            if section in allowed:
                if not isinstance(value, dict) or set(value) - allowed[section]:
                    raise BackendError("invalid_fields", "配置分组或字段格式不正确。")
                if not isinstance(result.get(section), dict):
                    result[section] = {}
                for key, item in value.items():
                    path = section + "." + key
                    if path in SECRET_PATHS:
                        if not isinstance(item, str):
                            raise BackendError("invalid_secret", "凭据必须为文本。")
                        if path == "notify.email_password":
                            # Providers show codes in groups ("abcd efgh ..."); SMTP needs them joined.
                            item = re.sub(r"\s+", "", item)
                        if item and path in clears:
                            raise BackendError("invalid_clear", "同一凭据不能同时填写和清除。")
                        if item == "":
                            continue
                        if path == "user.token":
                            # A stale page may send the whole post-login URL; keep only the token.
                            item = normalize_token(item)
                    elif path in EMAIL_TEXT_PATHS and isinstance(item, str):
                        item = item.strip()
                    result[section][key] = item
            else:
                result[section] = value
        for path in clears:
            section, key = path.split(".")
            result.setdefault(section, {})[key] = ""
        errors = validation_errors(self._cfg(result))
        # Drafts may be incomplete, but malformed dates/types/coordinates cannot be stored.
        missing = {"user: 需要 token 或完整的 username/password",
                   "user.username/password: 必须同时填写或同时留空",
                   "checkin.confirmed: 必须由本人核对位置后明确设为 true"}
        checkin = result.get("checkin", {})
        for field in ("longitude", "latitude"):
            if checkin.get(field) in ("", None):
                missing.add(f"checkin.{field}: 必须为有效的有限经纬度")
        if checkin.get("actual_location", "") == "":
            missing.add("checkin.actual_location: 需要本人核对的有效地址文字")
        notification = result.get("notify", {})
        for key, message in {"serverchan_key": "需要有效的 Server酱 SendKey",
                             "bark_url": "需要有效的 HTTPS 推送地址",
                             "wecom_webhook": "需要有效的 HTTPS 推送地址"}.items():
            if notification.get(key, "") == "":
                missing.add(f"notify.{key}: {message}")
        # Drafts may leave the email channel half-filled; malformed values are still rejected.
        if notification.get("email_password", "") == "":
            missing.add(EMAIL_PASSWORD_MESSAGE)
        if notification.get("email_address", "") == "":
            missing.add(EMAIL_ADDRESS_MESSAGE)
        invalid = [error for error in errors if error not in missing]
        if invalid:
            # These are fixed validation strings, never supplied values.
            raise BackendError("invalid_config", "；".join(invalid))
        # JSON is a valid YAML subset and uses the already-tested atomic 0600 writer.
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError):
            raise BackendError("invalid_config", "配置包含不支持的值。") from None
        return result

    def save_config(self, payload):
        with self._mutex:
            if self._saving or self._job and self._job["state"] == "running":
                raise BackendError("busy", "请等当前操作结束，或先暂停后再保存。", 409)
            self._saving = True
        try:
            raw = self._raw()
            candidate = self._merge(raw, payload)
            revision = payload.get("revision")
            if not isinstance(revision, str) or not hmac.compare_digest(revision, self._revision(raw)):
                raise BackendError("stale_config", "配置已被其他操作更新，请重新加载后再保存。", 409)
            cfg = self._cfg(raw)
            response = self.controller("pause")
            if not response.get("ok"):
                raise BackendError("pause_failed", "未能确认暂停，配置未保存。", 503)
            with run_lock(cfg), control_lock(state_dir(cfg)):
                if not hmac.compare_digest(revision, self._revision(self._raw())):
                    raise BackendError("stale_config", "配置已更新；当前保持暂停，请重新加载后保存。", 409)
                backup = self.path.parent / "backups" / "config-before-ui-save.json"
                atomic_write_json(backup, raw)
                atomic_write_json(self.path, candidate)
                # Cleared/replaced credentials invalidate the session cache, including token-only use.
                if candidate.get("user") != raw.get("user") or "user.token" in payload.get("clear_secrets", []):
                    session = state_dir(cfg) / "session.json"
                    if session.exists():
                        atomic_write_json(session, {"version": 1, "credential_fingerprint": "invalidated", "token": ""})
            record_event(cfg, {"status": "paused", "mode": "config-save", "checked_at": beijing_now().isoformat()})
            return {"ok": True, "paused": True, **self.get_config()}
        except (ConfigError, SafeCheckinError):
            raise BackendError("save_blocked", "保存被权限或运行锁阻止；请查看状态后重试。", 409) from None
        finally:
            with self._mutex:
                self._saving = False

    def _control(self, action):
        path = "/run/fzu-checkin-control/control.sock"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(320 if action != "status" else 8)
                connection.connect(path)
                request = {"action": action}
                if self.profile:
                    request["profile"] = self.profile
                connection.sendall(json.dumps(request).encode() + b"\n")
                response = bytearray()
                while not response.endswith(b"\n"):
                    data = connection.recv(4096)
                    if not data or len(response) + len(data) > 16384:
                        raise ValueError
                    response.extend(data)
                parsed = json.loads(response)
                if not isinstance(parsed, dict):
                    raise ValueError
                return parsed
        except (OSError, ValueError):
            raise BackendError("control_unavailable", "控制服务暂不可用；未绕过暂停或执行签到。", 503) from None

    def _decorate(self, value):
        result = safe_result(value)
        if result is not None:
            result["message"] = LABELS[result["status"]]
        return result

    def get_status(self):
        cfg = self._cfg()
        history = read_history(cfg)
        last_run = self._decorate(read_state(state_dir(cfg) / "last_run.json"))
        last_preflight = self._decorate(read_state(state_dir(cfg) / "last_preflight.json"))
        try:
            unit = self.controller("status")
        except BackendError:
            unit = {"timer": {"active": "unknown", "enabled": "unknown", "next_run": ""}}
        for value in (last_run, last_preflight):
            clean = safe_result(value)
            if clean and clean not in history:
                history.append(clean)
        history.sort(key=lambda item: item.get("checked_at", ""), reverse=True)
        changed_at = next((item.get("checked_at") for item in history if item.get("mode") == "config-save"), None)
        with self._mutex:
            job = copy.deepcopy(self._job)
            busy = self._saving or bool(job and job["state"] == "running")
        return {"today": beijing_now().date().isoformat(), "timezone": "Asia/Shanghai",
                "enabled": cfg.get("enabled") is True,
                "paused": cfg.get("enabled") is not True or bool(cfg.get("pause") or cfg.get("paused")) or (state_dir(cfg) / "paused").exists(),
                "timer": unit.get("timer", {}), "last_run": last_run, "last_preflight": last_preflight,
                "config_changed_at": changed_at,
                "history": [self._decorate(item) for item in history[:120]],
                "validation": validation_errors(cfg), "busy": busy, "job": job,
                "schedule_times": schedule_times(cfg)}

    def start_action(self, action, payload):
        if action not in ACTIONS or not isinstance(payload, dict) or payload.get("confirmed") is not True or set(payload) != {"confirmed"}:
            raise BackendError("invalid_action", "请明确确认页面支持的操作。")
        with self._mutex:
            if self._saving or self._job and self._job["state"] == "running":
                if action != "pause":
                    raise BackendError("busy", "已有操作进行中；仍可随时暂停。", 409)
            job = {"id": secrets.token_hex(12), "action": action, "state": "running",
                   "started_at": beijing_now().isoformat(), "result": None}
            self._job = job
            thread = threading.Thread(target=self._work, args=(job["id"], action, job["started_at"]), daemon=True)
            thread.start()
            return {"ok": True, "job": copy.deepcopy(job)}

    def _work(self, job_id, action, started_at):
        result = {"status": "failed", "mode": action, "checked_at": beijing_now().isoformat()}
        try:
            outcome = self.controller(action)
            cfg = self._cfg()
            if action == "pause":
                result["status"] = "paused" if outcome.get("ok") else "failed"
            elif action == "notify-test":
                result["status"] = "notification_accepted" if outcome.get("ok") else "notification_failed"
            else:
                saved = self._decorate(read_state(state_dir(cfg) / "last_preflight.json"))
                if saved and saved.get("checked_at", "") >= started_at:
                    result = saved
                    if action == "resume" and not outcome.get("ok") and result["status"] == "resumed":
                        result = {"status": "failed", "mode": action, "code": "timer_not_enabled", "checked_at": beijing_now().isoformat()}
            record_event(cfg, result)
        except Exception:
            result["code"] = "action_failed"
        result = self._decorate(result)
        with self._mutex:
            if self._job and self._job["id"] == job_id:
                self._job.update(state="done", result=result)


class BackendRegistry:
    """One isolated Backend per user profile under PROFILES_ROOT/<user_id>/."""

    def __init__(self, root=PROFILES_ROOT, *, controller_factory=None):
        self.root = Path(root)
        self._controller_factory = controller_factory
        self._lock = threading.Lock()
        self._backends = {}

    def profile_exists(self, user_id):
        return valid_user_id(user_id) and (self.root / user_id / "config.yaml").is_file()

    def for_user(self, user_id):
        if not valid_user_id(user_id):
            raise BackendError("profile_invalid", "用户档案无效。", 400)
        with self._lock:
            backend = self._backends.get(user_id)
            if backend is None:
                if not self.profile_exists(user_id):
                    raise BackendError("profile_missing", "该用户的配置档案不存在，请联系管理员。", 404)
                controller = self._controller_factory(user_id) if self._controller_factory else None
                backend = Backend(path=self.root / user_id / "config.yaml",
                                  state_dir=self.root / user_id / "state",
                                  controller=controller, profile=user_id)
                self._backends[user_id] = backend
            return backend

    def create_profile(self, user_id):
        if not valid_user_id(user_id):
            raise BackendError("profile_invalid", "用户档案无效。", 400)
        directory = self.root / user_id
        if directory.exists():
            raise BackendError("profile_exists", "该用户的配置档案已存在。", 409)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700)
        (directory / "state").mkdir(mode=0o700)
        (directory / "backups").mkdir(mode=0o700)
        atomic_write_json(directory / "config.yaml", default_profile_config())

    def archive_profile(self, user_id):
        """Keep the data (renamed, still private) instead of deleting a member's history."""
        if not valid_user_id(user_id):
            return
        directory = self.root / user_id
        with self._lock:
            self._backends.pop(user_id, None)
        if directory.exists():
            stamp = beijing_now().strftime("%Y%m%dT%H%M%S")
            directory.rename(self.root / f".removed-{user_id}-{stamp}")
