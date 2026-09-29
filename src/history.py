"""Bounded local execution history, never a copy of school/private responses."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re

from src.config import atomic_write_json, state_dir

STATUSES = frozenset({"ready", "already", "no_task", "confirmed", "paused", "skipped",
                      "outside_window", "resumed", "config_error", "auth_error", "failed",
                      "pending", "busy", "notification_accepted", "notification_failed"})


def safe_result(result):
    if not isinstance(result, dict) or result.get("status") not in STATUSES:
        return None
    clean = {"status": result["status"]}
    if result.get("mode") in {"run", "preflight", "resume", "pause", "notify-test", "config-save"}:
        clean["mode"] = result["mode"]
    stamp = result.get("checked_at")
    if isinstance(stamp, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})", stamp):
        clean["checked_at"] = stamp
    code = result.get("code")
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z_0-9]{1,64}", code):
        clean["code"] = code
    evidence = result.get("evidence", {})
    if isinstance(evidence, dict):
        clean["evidence"] = {key: value for key, value in evidence.items()
                             if key in {"today_matches", "success_verified", "submit_attempted",
                                        "schema_verified", "location_valid", "range_valid", "has_plan"}
                             and type(value) is bool}
    window = result.get("window", {})
    if isinstance(window, dict):
        clean["window"] = {}
        if isinstance(window.get("date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", window["date"]):
            clean["window"]["date"] = window["date"]
        for key in ("start", "end", "late"):
            if isinstance(window.get(key), str) and re.fullmatch(r"\d{2}:\d{2}", window[key]):
                clean["window"][key] = window[key]
        if type(window.get("in_window")) is bool:
            clean["window"]["in_window"] = window["in_window"]
    return clean


@contextmanager
def _lock(cfg):
    directory = state_dir(cfg)
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory / "history.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(path):
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 262144:
            return []
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, list):
            return []
        return [clean for item in values[-120:] if (clean := safe_result(item)) is not None]
    except (OSError, ValueError, TypeError):
        # History is informational only; never used to decide whether to submit.
        return []


def read_history(cfg):
    with _lock(cfg):
        return _read(state_dir(cfg) / "history.json")


def record_event(cfg, result):
    clean = safe_result(result)
    if clean is None:
        return
    with _lock(cfg):
        path = state_dir(cfg) / "history.json"
        previous = _read(path)
        if previous and previous[-1] == clean:
            return
        atomic_write_json(path, (previous + [clean])[-120:])
