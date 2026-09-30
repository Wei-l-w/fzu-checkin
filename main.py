"""私有服务器入口。默认只读；只有 run 命令能够进入提交路径。"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from src.checkin import (AttnClient, SafeCheckinError, beijing_now,
                         do_checkin, query_today_task, validate_location)
from src.config import (ConfigError, atomic_write_json, load_config,
                        save_token, state_dir, validation_errors)
from src.login import AuthError, login
from src.notify import notify
from src.vacation import matched_range
from src.history import record_event

EXIT = {"ready": 0, "already": 0, "no_task": 0, "confirmed": 0,
        "paused": 0, "skipped": 0, "outside_window": 0, "resumed": 0,
        "notification_accepted": 0, "config_error": 20, "auth_error": 21,
        "failed": 22, "pending": 23, "busy": 24, "notification_failed": 25}
LABELS = {"ready": "只读预检通过（未提交）", "already": "学校记录确认当天已签到",
          "no_task": "学校明确返回无需签到", "confirmed": "提交后学校记录复核确认签到成功",
          "paused": "自动提交已暂停", "skipped": "命中个人跳过日期",
          "outside_window": "不在学校当日允许时段，未提交", "resumed": "定时器已启用（本次未提交签到）",
          "config_error": "配置缺失或无效", "auth_error": "认证失败，需要检查登录或人工认证",
          "failed": "执行失败，请在手机学校 App 检查", "pending": "结果待确认，禁止重复提交",
          "busy": "已有实例运行，本次未执行", "notification_accepted": "部署测试通知获渠道受理，手机送达需本人确认",
          "notification_failed": "部署测试通知未获渠道确认"}
CODE_LABELS = {
    "timer_not_enabled": "学校预检已通过，但服务器定时器未能启用，当前不会自动签到；请联系管理员检查定时配置及权限",
    "resume_preflight_passed": "学校预检已通过，正在启用定时器；尚不能视为恢复成功",
    "resume_changed_before_activation": "恢复过程中出现了新的暂停或预检状态变化，未确认恢复定时",
}


def read_state(path):
    """状态损坏必须报错，不能静默丢掉提交意图而重签。"""
    path = Path(path)
    if not path.exists():
        if path.is_symlink():
            raise ConfigError("state_invalid")
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ConfigError("state_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ConfigError("state_invalid") from None


@contextmanager
def run_lock(cfg):
    directory = state_dir(cfg)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ConfigError("state_permissions")
    fd = os.open(directory / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafeCheckinError("busy", "已有实例运行") from None
        yield
    finally:
        os.close(fd)


@contextmanager
def control_lock(directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / "control.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _fingerprint(cfg):
    values = {key: value for key, value in cfg.items() if not key.startswith("_")}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _auth_identity(cfg):
    user = cfg.get("user") or {}
    return hashlib.sha256(json.dumps([user.get("username"), user.get("password")]).encode()).hexdigest()


def blocked_status(cfg):
    if cfg.get("enabled") is not True or cfg.get("paused", False) or cfg.get("pause", False) or (state_dir(cfg) / "paused").exists():
        return "paused"
    today = beijing_now().date()
    if today.isoformat() in [str(value) for value in cfg.get("skip_dates", [])] or matched_range(cfg, today):
        return "skipped"
    return None


def safe_evidence(value):
    """仅接受受控布尔和枚举；绝不持久化学校原始响应/ID/位置。"""
    result = {}
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,48}", key):
            continue
        if type(item) is bool or item is None:
            result[key] = item
        elif key in {"reason", "record_state"} and isinstance(item, str) and re.fullmatch(r"[A-Za-z_]{1,64}", item):
            result[key] = item
    return result


def announce(cfg, result):
    status = result["status"]
    if status not in {"confirmed", "failed", "pending", "auth_error", "config_error"}:
        return
    if (cfg.get("notify") or {}).get("type", "none") == "none":
        return
    path = state_dir(cfg) / "notifications.json"
    cache = read_state(path)
    today = beijing_now().date().isoformat()
    if cache.get("date") != today:
        cache = {"date": today, "events": {}}
    events = cache.setdefault("events", {})
    key = status + ":" + str(result.get("code", ""))
    previous = events.get(key, {})
    now = beijing_now().timestamp()
    if previous.get("accepted") or now - previous.get("attempt_at", 0) < 600:
        return
    events[key] = {"attempt_at": now, "accepted": False}
    atomic_write_json(path, cache)
    content = ("学校当天记录已复核。" if status == "confirmed" else
               "请打开智汇福大 App 检查，必要时手动处理。请勿把程序状态当成学校签到记录。")
    accepted = notify(cfg, "智汇福大晚点名：" + LABELS[status], content)
    events[key]["accepted"] = accepted
    atomic_write_json(path, cache)
    if not accepted:
        print("通知未获渠道确认；签到结果不受影响。", flush=True)


def finish(cfg, mode, status, *, code=None, evidence=None, missing=None, window=None):
    if status not in EXIT:
        status, code = "pending", "unknown_result"
    result = {"status": status, "message": LABELS[status], "mode": mode,
              "checked_at": beijing_now().isoformat(), "evidence": safe_evidence(evidence)}
    if code and re.fullmatch(r"[A-Za-z_0-9]{1,64}", str(code)):
        result["code"] = code.lower()
        result["message"] = CODE_LABELS.get(result["code"], result["message"])
    if missing:
        result["missing"] = missing
    if window:
        result["window"] = {k: v for k, v in window.items() if k in {"date", "start", "end", "late", "in_window"}}
    if cfg is not None and status in {"already", "confirmed"}:
        # Record that the school already shows a signed record today so later timer
        # runs can skip entirely instead of re-querying (they never resubmit anyway).
        try:
            atomic_write_json(state_dir(cfg) / "confirmed.json", {"date": beijing_now().date().isoformat()})
        except Exception:
            pass
    if cfg is not None and mode in {"run", "preflight", "resume"}:
        filename = "last_run.json" if mode == "run" else "last_preflight.json"
        atomic_write_json(state_dir(cfg) / filename, result)
        try:
            record_event(cfg, result)
        except Exception:
            print("本地历史记录保存失败，核心签到状态不受影响。", flush=True)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if cfg is not None and mode == "run":
        try:
            announce(cfg, result)
        except Exception:
            print("通知处理失败（详情已脱敏），请查看本机运行状态。", flush=True)
    return EXIT[status]


def new_login(cfg):
    user = cfg.get("user") or {}
    failure_path = state_dir(cfg) / "auth_failure.json"
    failure = read_state(failure_path)
    identity = _auth_identity(cfg)
    if failure.get("identity") == identity:
        raise AuthError("AUTH_MANUAL_REQUIRED")
    try:
        token = login(user["username"], user["password"])
    except AuthError as exc:
        if exc.code not in {"AUTH_NETWORK", "AUTH_RATE_LIMITED"}:
            atomic_write_json(failure_path, {"identity": identity, "code": exc.code})
        raise
    save_token(cfg, token)
    cfg["user"]["token"] = token
    return token


def authenticated_query(cfg, mode):
    user = cfg.get("user") or {}
    existing = bool(user.get("token"))
    token = user.get("token") or new_login(cfg)
    client = AttnClient(token, read_only=mode != "run")
    try:
        return client, query_today_task(client, cfg)
    except SafeCheckinError as exc:
        if exc.code == "auth_required" and user.get("username") and user.get("password") and existing:
            # 只在明确认证失效时换一次token，仍失败则失败，不报无任务。
            token = new_login(cfg)
            client = AttnClient(token, read_only=mode != "run")
            return client, query_today_task(client, cfg)
        raise


def inspect_status(cfg):
    user = cfg.get("user") or {}
    checkin = cfg.get("checkin") or {}
    result = {"enabled": cfg.get("enabled") is True,
              "pause_marker": (state_dir(cfg) / "paused").exists(),
              "credentials": "已配置" if user.get("token") or (user.get("username") and user.get("password")) else "未配置",
              "coordinates": "已配置" if checkin.get("longitude") not in (None, "") and checkin.get("latitude") not in (None, "") else "未配置",
              "location_confirmed": checkin.get("confirmed") is True,
              "notification": "未配置" if (cfg.get("notify") or {}).get("type", "none") == "none" else "已配置",
              "validation": validation_errors(cfg)}
    for filename in ("last_run.json", "last_preflight.json"):
        value = read_state(state_dir(cfg) / filename)
        result[filename.removesuffix(".json")] = {k: value[k] for k in ("status", "checked_at", "code") if k in value}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def finalize_resume(enabled):
    """Internal CLI receipt, after systemd verification; never performs school I/O."""
    cfg = None
    try:
        cfg = load_config()
        directory = state_dir(cfg)
        with control_lock(directory):
            previous = read_state(directory / "last_preflight.json")
            if enabled:
                if (blocked_status(cfg) == "paused" or previous.get("mode") != "resume"
                        or previous.get("status") != "ready"
                        or previous.get("code") != "resume_preflight_passed"):
                    return finish(cfg, "resume", "config_error", code="resume_changed_before_activation")
                return finish(cfg, "resume", "resumed", code="timer_enabled",
                              evidence=previous.get("evidence"), window=previous.get("window"))
            if not (directory / "paused").exists():
                atomic_write_json(directory / "paused", {"paused_at": beijing_now().isoformat()})
            return finish(cfg, "resume", "failed", code="timer_not_enabled")
    except Exception:
        # The root caller must disable the timer if persisting the receipt fails.
        print('{"status":"failed","code":"resume_receipt_failed"}', flush=True)
        return EXIT["failed"]


def execute(mode):
    cfg = None
    try:
        if mode == "pause":
            # 配置损坏也必须能暂停；只使用本服务指定的状态路径。
            directory = Path(os.environ.get("FZU_CHECKIN_STATE_DIR", "/var/lib/fzu-checkin/state"))
            with control_lock(directory):
                atomic_write_json(directory / "paused", {"paused_at": beijing_now().isoformat()})
            return finish(None, mode, "paused")
        cfg = load_config()
        if mode == "status":
            return inspect_status(cfg)
        with run_lock(cfg):
            if mode == "notify-test":
                accepted = notify(cfg, "智汇福大晚点名：部署测试", "仅测试通知，不执行签到。请在手机确认收到。")
                return finish(cfg, mode, "notification_accepted" if accepted else "notification_failed")
            if mode == "run":
                blocked = blocked_status(cfg)
                if blocked:
                    return finish(cfg, mode, blocked)
            missing = validation_errors(cfg)
            if missing:
                return finish(cfg, mode, "config_error", missing=missing)
            if mode == "run" and read_state(state_dir(cfg) / "confirmed.json").get("date") == beijing_now().date().isoformat():
                # 学校当天已确认签到：后续档次不再连接学校，也不再提交。
                return finish(cfg, mode, "already", code="already_confirmed_today")
            if mode == "resume" and (cfg.get("enabled") is not True or cfg.get("paused", False) or cfg.get("pause", False)):
                return finish(cfg, mode, "config_error", code="enable_confirmation_required")
            pause_before = read_state(state_dir(cfg) / "paused")
            client, current = authenticated_query(cfg, mode)
            status = current["status"]
            if mode in {"preflight", "resume"}:
                if status not in {"ready", "already"}:
                    return finish(cfg, mode, "pending", code="preflight_incomplete", evidence=current.get("evidence"), window=current.get("window"))
                location = validate_location(client, cfg, current["init"])
                if location["status"] != "valid":
                    return finish(cfg, mode, "pending", code="location_unverified", evidence=location.get("evidence"))
                if mode == "resume":
                    with control_lock(state_dir(cfg)):
                        # 用户在预检期间发起的新暂停优先；不删除比本次恢复更晚的暂停。
                        if read_state(state_dir(cfg) / "paused") != pause_before:
                            return finish(cfg, mode, "config_error", code="pause_changed_during_preflight")
                        latest = load_config()
                        if validation_errors(latest) or _fingerprint(latest) != _fingerprint(cfg):
                            return finish(cfg, mode, "config_error", code="config_changed_during_preflight")
                        (state_dir(cfg) / "paused").unlink(missing_ok=True)
                    # This is only phase 1. The root CLI still has to install,
                    # start and verify the timer before finalize_resume(True).
                    return finish(cfg, mode, "ready", code="resume_preflight_passed",
                                  evidence=current.get("evidence"), window=current.get("window"))
                return finish(cfg, mode, status, evidence=current.get("evidence"), window=current.get("window"))
            if status != "ready":
                return finish(cfg, mode, status, evidence=current.get("evidence"), window=current.get("window"))
            if not current.get("window", {}).get("in_window"):
                return finish(cfg, mode, "outside_window", window=current.get("window"))
            # 原版21:30-21:34的naive/aware等待分支已删除：timer在21:35触发，
            # 应用层仍按学校当日计划检查时段，不把本地排档当学校许可。
            today = beijing_now().date().isoformat()
            intent_path = state_dir(cfg) / "submission.json"
            if read_state(intent_path).get("date") == today:
                return finish(cfg, mode, "pending", code="previous_submission_unconfirmed", evidence=current.get("evidence"))
            fingerprint = _fingerprint(cfg)

            def before_submit():
                latest = load_config()
                if blocked_status(latest) or validation_errors(latest) or _fingerprint(latest) != fingerprint:
                    return False
                if beijing_now().date().isoformat() != today:
                    return False
                atomic_write_json(intent_path, {"date": today, "intent_at": beijing_now().isoformat(), "status": "pending"})
                return blocked_status(load_config()) is None

            result = do_checkin(client, cfg, current["init"], before_submit=before_submit)
            intent = read_state(intent_path)
            if intent.get("date") == today:
                intent["status"] = result["status"]
                atomic_write_json(intent_path, intent)
            return finish(cfg, mode, result["status"], evidence=result.get("evidence"), window=current.get("window"))
    except ConfigError as exc:
        return finish(None, mode, "config_error", code=exc.code)
    except AuthError as exc:
        return finish(cfg, mode, "auth_error", code=exc.code)
    except SafeCheckinError as exc:
        status = "busy" if exc.code == "busy" else "auth_error" if exc.code == "auth_required" else "failed"
        return finish(cfg, mode, status, code=exc.code)
    except Exception:
        try:
            return finish(cfg, mode, "failed", code="internal_error")
        except Exception:
            print('{"status":"failed","code":"state_or_internal_error"}', flush=True)
            return EXIT["failed"]


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="默认只读预检，run会按条件真实提交")
    parser.add_argument("mode", nargs="?", default="preflight",
                        choices=["preflight", "run", "status", "pause", "resume", "notify-test"])
    mode = parser.parse_args(argv).mode
    if os.environ.get("FZU_CHECKIN_FORCE") and mode == "run":
        mode = "preflight"
    return execute(mode)


if __name__ == "__main__":
    sys.exit(main())
