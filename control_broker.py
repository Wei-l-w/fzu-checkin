#!/usr/bin/env python3
"""Narrow root control bridge. No client-controlled commands, paths, or environment.

The public web process stays unprivileged. This daemon accepts one bounded JSON
line over a private Unix socket and exposes only the controls listed below.
Command output is never sent to the client or written to the journal.
"""

from __future__ import annotations

import errno
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import stat
import struct
import subprocess
import threading
from dataclasses import dataclass
from zoneinfo import ZoneInfo


RUNTIME_DIR = Path("/run/fzu-checkin-control")
SOCKET_NAME = "control.sock"
CONTROL_COMMAND = "/usr/local/sbin/fzu-checkinctl"
SYSTEMCTL = "/usr/bin/systemctl"
ALLOWED_ACTIONS = frozenset({"status", "preflight", "pause", "resume", "notify-test"})
MAX_REQUEST_BYTES = 4096
ACTION_TIMEOUT = 240
READ_TIMEOUT = 2
MAX_CONNECTIONS = 16
FIXED_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "TZ": "Asia/Shanghai"}
PRIVATE_CONFIG = Path("/var/lib/fzu-checkin/config.yaml")
TIMER_DROPIN = Path("/etc/systemd/system/fzu-checkin.timer.d/schedule.conf")
PROFILES_ROOT = Path("/var/lib/fzu-checkin/profiles")
PROFILE_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,23}\Z")
DEFAULT_PROFILE = "admin"


def valid_profile(value) -> bool:
    return isinstance(value, str) and PROFILE_PATTERN.fullmatch(value) is not None


def profile_config_path(profile: str) -> Path:
    return PROFILES_ROOT / profile / "config.yaml"


def profile_exists(profile: str) -> bool:
    """Root-side check: only profiles provisioned under PROFILES_ROOT may be controlled."""
    return valid_profile(profile) and profile_config_path(profile).is_file()


def timer_unit(profile: str) -> str:
    return f"fzu-checkin@{profile}.timer"


def service_unit(profile: str) -> str:
    return f"fzu-checkin@{profile}.service"


def preflight_unit(profile: str) -> str:
    return f"fzu-checkin-preflight@{profile}.service"


def timer_dropin(profile: str) -> Path:
    return Path(f"/etc/systemd/system/fzu-checkin@{profile}.timer.d/schedule.conf")
DEFAULT_SCHEDULE_TIMES = ("21:35", "21:40", "21:50")
SCHEDULE_EARLIEST, SCHEDULE_LATEST, SCHEDULE_MAX = "21:00", "23:55", 6


def valid_schedule_times(value) -> bool:
    """Root-side copy of the strict rule: 1-6 zero-padded HH:MM in 21:00-23:55, ascending, unique."""
    if not isinstance(value, list) or not 1 <= len(value) <= SCHEDULE_MAX:
        return False
    previous = None
    for item in value:
        if not isinstance(item, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", item):
            return False
        if not SCHEDULE_EARLIEST <= item <= SCHEDULE_LATEST or (previous is not None and item <= previous):
            return False
        previous = item
    return True


def read_schedule_times(path: Path = PRIVATE_CONFIG) -> list[str]:
    """Read schedule.times from the private config as root. Defaults when absent; ValueError when invalid."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return list(DEFAULT_SCHEDULE_TIMES)
    try:
        data = json.loads(text)
    except ValueError:
        import yaml  # The UI writes JSON; a hand-written YAML config must quote times ('21:35').
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("config is not a mapping")
    schedule = data.get("schedule")
    if schedule is None:
        return list(DEFAULT_SCHEDULE_TIMES)
    if not isinstance(schedule, dict) or not valid_schedule_times(schedule.get("times")):
        raise ValueError("invalid schedule.times")
    return list(schedule["times"])


def schedule_dropin(times) -> str:
    """systemd drop-in that replaces the timer's OnCalendar list with the validated times."""
    times = list(times)
    if not valid_schedule_times(times):
        raise ValueError("invalid schedule times")
    lines = ["# Managed by `fzu-checkinctl resume` from the private config (schedule.times). Do not edit by hand.",
             "[Unit]", "Description=FZU evening attendance at " + ",".join(times) + " Beijing time",
             "[Timer]", "OnCalendar="]
    lines += ["OnCalendar=*-*-* " + item + ":00 Asia/Shanghai" for item in times]
    return "\n".join(lines) + "\n"


def apply_schedule(times, *, dropin: Path = TIMER_DROPIN, runner=subprocess.run) -> bool:
    """Write the drop-in atomically (0644) and daemon-reload; returns True only when it changed."""
    content = schedule_dropin(times)
    try:
        if dropin.read_text(encoding="utf-8") == content:
            return False
    except FileNotFoundError:
        pass
    dropin.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = dropin.with_name(dropin.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, dropin)
    runner([SYSTEMCTL, "daemon-reload"], check=True)
    return True


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    timed_out: bool = False


def error(code: str, returncode: int) -> dict:
    return {"ok": False, "returncode": returncode, "error": code}


def secure_runtime_directory(group_gid: int) -> int:
    """Return a verified directory FD; never follow a replaceable final symlink."""
    created = False
    try:
        os.mkdir(RUNTIME_DIR, 0o750)
        created = True
    except FileExistsError:
        pass
    directory_fd = os.open(RUNTIME_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if created:
            os.fchown(directory_fd, 0, group_gid)
            os.fchmod(directory_fd, 0o750)
        info = os.fstat(directory_fd)
        if info.st_uid != 0 or info.st_gid != group_gid or stat.S_IMODE(info.st_mode) != 0o750:
            raise PermissionError("unsafe_runtime_directory")
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def open_root_lock(directory_fd: int, name: str) -> int:
    """Open a root-only regular lock without truncating or following symlinks."""
    if name not in {"broker.lock", "schedule.lock"}:
        raise ValueError("invalid_lock_name")
    descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise PermissionError("unsafe_lock")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError("unsafe_lock_mode")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class CommandRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen] = set()

    @staticmethod
    def kill_group(process: subprocess.Popen) -> None:
        # Every process registered here was launched with start_new_session=True.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def __call__(self, arguments: tuple[str, ...], timeout: int = ACTION_TIMEOUT) -> CommandResult:
        process = None
        try:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       cwd="/", env=FIXED_ENV.copy(), close_fds=True,
                                       start_new_session=True)
            with self._lock:
                self._processes.add(process)
            try:
                output, _ = process.communicate(timeout=timeout)
                return CommandResult(process.returncode, output)
            except subprocess.TimeoutExpired:
                self.kill_group(process)
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    # A descendant that escaped the group must not keep a pipe
                    # reader blocked forever. The fixed CLI does not daemonize.
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                return CommandResult(124, timed_out=True)
        except (OSError, ValueError):
            return CommandResult(70)
        finally:
            if process is not None:
                with self._lock:
                    self._processes.discard(process)

    def cancel_all(self) -> None:
        with self._lock:
            for process in self._processes:
                self.kill_group(process)


def parse_request(raw: bytes) -> tuple[str, str]:
    """One bounded JSON line: {"action": ..., "profile"?: ...}. Anything else is invalid."""
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ValueError("invalid_request")

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_key")
            value[key] = item
        return value

    request = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    if not isinstance(request, dict) or not set(request) <= {"action", "profile"} or "action" not in request:
        raise ValueError("invalid_request")
    action = request["action"]
    if type(action) is not str or action not in ALLOWED_ACTIONS:
        raise ValueError("invalid_action")
    profile = request.get("profile", DEFAULT_PROFILE)
    if not valid_profile(profile):
        raise ValueError("invalid_profile")
    return action, profile


def _properties(output: bytes) -> dict[str, str]:
    result = {}
    for line in output[:8192].decode("ascii", errors="ignore").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            result[key] = value[:160]
    return result


def _safe_word(value: str, default: str = "unknown") -> str:
    return value if re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value) else default


def _next_run_iso(value: str) -> str:
    # systemctl inherits the fixed Asia/Shanghai environment. Never expose CST
    # to browsers: JavaScript can interpret that abbreviation as US Central.
    match = re.fullmatch(r"(?:[A-Za-z]{3} )?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)? (CST|UTC)", value)
    if not match:
        return ""
    try:
        zone = timezone.utc if match.group(2) == "UTC" else ZoneInfo("Asia/Shanghai")
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone).isoformat()
    except ValueError:
        return ""


class ControlBroker:
    def __init__(self, allowed_uid: int, runner=None, profile_exists=profile_exists):
        self.allowed_uid = allowed_uid
        self.runner = runner if runner is not None else CommandRunner()
        self.profile_exists = profile_exists
        self._regular_slot = threading.BoundedSemaphore(1)
        self._pause_slot = threading.BoundedSemaphore(1)
        self._status_slots = threading.BoundedSemaphore(2)

    def status(self, profile: str = DEFAULT_PROFILE) -> dict:
        timer = self.runner((SYSTEMCTL, "show", timer_unit(profile), "--no-pager",
                             "--property=ActiveState,UnitFileState,NextElapseUSecRealtime"), timeout=10)
        service = self.runner((SYSTEMCTL, "show", service_unit(profile), "--no-pager",
                               "--property=ActiveState,Result,ExecMainStatus"), timeout=10)
        if timer.returncode or service.returncode:
            return error("status_unavailable", 70)
        timer_values, service_values = _properties(timer.stdout), _properties(service.stdout)
        next_run = _next_run_iso(timer_values.get("NextElapseUSecRealtime", ""))
        exit_text = service_values.get("ExecMainStatus", "")
        exit_code = int(exit_text) if re.fullmatch(r"[0-9]{1,6}", exit_text) else None
        return {"ok": True,
                "timer": {"active": _safe_word(timer_values.get("ActiveState", "")),
                          "enabled": _safe_word(timer_values.get("UnitFileState", "")),
                          "next_run": next_run},
                "service": {"active": _safe_word(service_values.get("ActiveState", "")),
                            "result": _safe_word(service_values.get("Result", "")),
                            "exit_code": exit_code}}

    def fail_closed(self, profile: str = DEFAULT_PROFILE) -> None:
        # This runs outside the normal-action gate: a stalled resume must never
        # prevent a pause. The CLI first writes the unprivileged pause marker.
        result = self.runner((CONTROL_COMMAND, "pause", profile), timeout=30)
        if result.returncode:
            self.runner((SYSTEMCTL, "disable", "--now", timer_unit(profile)), timeout=15)
            self.runner((SYSTEMCTL, "stop", service_unit(profile)), timeout=15)

    def dispatch(self, action: str, profile: str = DEFAULT_PROFILE) -> dict:
        if action not in ALLOWED_ACTIONS or not self.profile_exists(profile):
            return error("invalid_request", 64)
        slot = self._pause_slot if action == "pause" else self._status_slots if action == "status" else self._regular_slot
        if not slot.acquire(blocking=False):
            return error("busy", 75)
        try:
            if action == "status":
                return self.status(profile)
            result = self.runner((CONTROL_COMMAND, action, profile), timeout=ACTION_TIMEOUT)
            if result.timed_out:
                self.fail_closed(profile)
                return error("timeout", 124)
            return {"ok": result.returncode == 0, "returncode": result.returncode}
        except Exception:
            if action != "status":
                try:
                    self.fail_closed(profile)
                except Exception:
                    pass
            return error("internal_error", 70)
        finally:
            slot.release()

    def handle_connection(self, connection: socket.socket) -> None:
        response = error("invalid_request", 64)
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _, uid, _ = struct.unpack("3i", credentials)
            if uid not in {0, self.allowed_uid}:
                response = error("forbidden", 77)
            else:
                connection.settimeout(READ_TIMEOUT)
                raw = bytearray()
                while len(raw) <= MAX_REQUEST_BYTES:
                    part = connection.recv(MAX_REQUEST_BYTES + 1 - len(raw))
                    if not part:
                        break
                    raw.extend(part)
                    if b"\n" in part:
                        break
                action, profile = parse_request(bytes(raw))
                response = self.dispatch(action, profile)
        except (OSError, ValueError, UnicodeError, RecursionError):
            response = error("invalid_request", 64)
        except Exception:
            response = error("internal_error", 70)
        try:
            connection.settimeout(READ_TIMEOUT)
            connection.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        except OSError:
            pass
        finally:
            connection.close()


def bind_socket(directory_fd: int, group_gid: int) -> socket.socket:
    address = str(RUNTIME_DIR / SOCKET_NAME)
    # The caller holds broker.lock, so another daemon cannot replace this socket
    # between our stale-socket ownership check and unlink.
    try:
        info = os.stat(SOCKET_NAME, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_gid != group_gid:
            raise PermissionError("unsafe_socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(address)
            except OSError as exc:
                if exc.errno != errno.ECONNREFUSED:
                    raise
            else:
                raise RuntimeError("socket_in_use")
        os.unlink(SOCKET_NAME, dir_fd=directory_fd)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(address)
        os.chown(SOCKET_NAME, 0, group_gid, dir_fd=directory_fd, follow_symlinks=False)
        # Linux chmod cannot combine dir_fd with follow_symlinks=False. The
        # verified root-only directory plus broker.lock prevent replacement.
        os.chmod(SOCKET_NAME, 0o660, dir_fd=directory_fd)
        listener.listen(MAX_CONNECTIONS)
        listener.settimeout(0.5)
        return listener
    except BaseException:
        listener.close()
        raise


def main() -> int:
    if os.geteuid() != 0:
        return 1
    os.umask(0o077)
    directory_fd = lock_fd = None
    listener = None
    socket_identity = None
    stopped = threading.Event()
    connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    runner = CommandRunner()
    threads: list[threading.Thread] = []
    try:
        account = pwd.getpwnam("fzu-checkin")
        directory_fd = secure_runtime_directory(account.pw_gid)
        lock_fd = open_root_lock(directory_fd, "broker.lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        listener = bind_socket(directory_fd, account.pw_gid)
        info = os.stat(SOCKET_NAME, dir_fd=directory_fd, follow_symlinks=False)
        socket_identity = (info.st_dev, info.st_ino)
        broker = ControlBroker(account.pw_uid, runner)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopped.set())

        def worker(connection):
            try:
                broker.handle_connection(connection)
            finally:
                connection_slots.release()

        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            if not connection_slots.acquire(blocking=False):
                connection.close()
                continue
            thread = threading.Thread(target=worker, args=(connection,), daemon=True)
            thread.start()
            threads = [previous for previous in threads if previous.is_alive()]
            threads.append(thread)
        return 0
    except Exception:
        # No exception details: they can include filesystem paths or data.
        return 1
    finally:
        if listener is not None:
            listener.close()
        runner.cancel_all()
        for thread in threads:
            thread.join(timeout=0.1)
        if socket_identity is not None:
            try:
                info = os.stat(SOCKET_NAME, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISSOCK(info.st_mode) and info.st_uid == 0 and (info.st_dev, info.st_ino) == socket_identity:
                    os.unlink(SOCKET_NAME, dir_fd=directory_fd)
            except OSError:
                pass
        if lock_fd is not None:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)


if __name__ == "__main__":
    raise SystemExit(main())
