"""Private, file-backed administrator authentication (unrelated to school login)."""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time


DEFAULT_AUTH_FILE = "/var/lib/fzu-checkin/admin/auth.json"
SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
PASSWORD_MAX_BYTES = 1024
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class AuthConfigurationError(Exception):
    """Deliberately carries no underlying filesystem or credential values."""

    def __init__(self):
        super().__init__("管理员认证文件不可用，请在服务器本地检查权限或重设密码")


class LoginLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after))
        super().__init__("登录尝试过于频繁，请稍后重试")


def _private_directory(directory, create=False):
    try:
        if create:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise AuthConfigurationError()
    except OSError:
        raise AuthConfigurationError() from None


@contextmanager
def private_file_lock(path):
    """Serialize private store writers, including CLI and separate web processes."""
    path = Path(path)
    _private_directory(path.parent, create=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise AuthConfigurationError()
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _password_bytes(password):
    if not isinstance(password, str):
        return None
    try:
        encoded = password.encode("utf-8")
    except UnicodeError:
        return None
    if not encoded or len(encoded) > PASSWORD_MAX_BYTES:
        return None
    return encoded


def _derive(encoded, salt):
    return hashlib.scrypt(encoded, salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                          p=SCRYPT_P, dklen=32, maxmem=SCRYPT_MAXMEM)


def read_password_file(path):
    """Read only a regular, owner-private file; reject configurable KDF costs."""
    path = Path(path)
    _private_directory(path.parent)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 4096):
            raise AuthConfigurationError()
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            raw = source.read(4097)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
                "version", "algorithm", "n", "r", "p", "salt", "digest"}:
            raise AuthConfigurationError()
        if (type(value["version"]) is not int or value["version"] != 1
                or value["algorithm"] != "scrypt"
                or type(value["n"]) is not int or value["n"] != SCRYPT_N
                or type(value["r"]) is not int or value["r"] != SCRYPT_R
                or type(value["p"]) is not int or value["p"] != SCRYPT_P):
            raise AuthConfigurationError()
        salt = base64.b64decode(value["salt"], validate=True)
        digest = base64.b64decode(value["digest"], validate=True)
        if len(salt) != 32 or len(digest) != 32:
            raise AuthConfigurationError()
        return salt, digest
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise AuthConfigurationError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_password_file(path, password):
    """For local provisioning only. Never returns, prints, or persists plaintext."""
    encoded = _password_bytes(password)
    if encoded is None or len(password) < 16:
        raise ValueError("管理员密码至少需要 16 个字符，且不超过 1024 UTF-8 字节")
    path = Path(path)
    _private_directory(path.parent, create=True)
    salt = secrets.token_bytes(32)
    digest = _derive(encoded, salt)
    value = {"version": 1, "algorithm": "scrypt", "n": SCRYPT_N,
             "r": SCRYPT_R, "p": SCRYPT_P,
             "salt": base64.b64encode(salt).decode("ascii"),
             "digest": base64.b64encode(digest).decode("ascii")}
    _write_private_json(path, value)


def _write_private_json(path, value):
    """Atomic, fsynced, owner-private (0600) JSON write; never leaves partial files."""
    path = Path(path)
    _private_directory(path.parent, create=True)
    temporary = None
    try:
        previous = None
        try:
            previous = path.lstat()
            if not stat.S_ISREG(previous.st_mode):
                raise AuthConfigurationError()
        except FileNotFoundError:
            pass
        descriptor, temporary = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            os.fchmod(target.fileno(), 0o600)
            if previous is not None and os.geteuid() == 0:
                os.fchown(target.fileno(), previous.st_uid, previous.st_gid)
            json.dump(value, target, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise AuthConfigurationError() from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


DEFAULT_USERS_FILE = "/var/lib/fzu-checkin/users/users.json"
USER_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,23}\Z")
ROLES = ("owner", "member")
MIN_PASSWORD_CHARS = 10
USERS_FILE_MAX_BYTES = 256 * 1024
MAX_USERS = 64
_DUMMY_SALT = bytes(32)


def valid_user_id(value):
    return isinstance(value, str) and USER_ID_PATTERN.fullmatch(value) is not None


def new_user_record(password, role="member", created_at=""):
    """Fresh scrypt record for one user. Raises ValueError with a user-facing message."""
    encoded = _password_bytes(password)
    if encoded is None or len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(f"密码至少需要 {MIN_PASSWORD_CHARS} 个字符，且不超过 1024 UTF-8 字节")
    if role not in ROLES:
        raise ValueError("角色无效")
    salt = secrets.token_bytes(32)
    return {"role": role, "created_at": str(created_at)[:64], "salt": salt, "digest": _derive(encoded, salt)}


def _parse_record(value):
    if (not isinstance(value, dict) or set(value) != {"algorithm", "n", "r", "p", "salt", "digest"}
            or value["algorithm"] != "scrypt"
            or type(value["n"]) is not int or value["n"] != SCRYPT_N
            or type(value["r"]) is not int or value["r"] != SCRYPT_R
            or type(value["p"]) is not int or value["p"] != SCRYPT_P):
        raise AuthConfigurationError()
    try:
        salt = base64.b64decode(value["salt"], validate=True)
        digest = base64.b64decode(value["digest"], validate=True)
    except (ValueError, TypeError):
        raise AuthConfigurationError() from None
    if len(salt) != 32 or len(digest) != 32:
        raise AuthConfigurationError()
    return salt, digest


def _read_private_json(path, limit):
    path = Path(path)
    _private_directory(path.parent)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit:
            raise AuthConfigurationError()
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            return json.loads(source.read(limit + 1))
    except (OSError, ValueError, TypeError, UnicodeError):
        raise AuthConfigurationError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_users_file(path):
    """{user_id: {"role", "created_at", "salt", "digest"}} from the v2 users file.

    A legacy single-owner ``auth.json`` (v1) is accepted transparently as owner ``admin``.
    """
    value = _read_private_json(path, USERS_FILE_MAX_BYTES)
    if isinstance(value, dict) and value.get("version") == 1 and "digest" in value:
        salt, digest = read_password_file(path)
        return {"admin": {"role": "owner", "created_at": "", "salt": salt, "digest": digest}}
    if (not isinstance(value, dict) or set(value) != {"version", "users"} or value["version"] != 2
            or not isinstance(value["users"], dict) or not 1 <= len(value["users"]) <= MAX_USERS):
        raise AuthConfigurationError()
    users = {}
    for user_id, item in value["users"].items():
        if (not valid_user_id(user_id) or not isinstance(item, dict)
                or set(item) != {"role", "created_at", "auth"} or item["role"] not in ROLES
                or not isinstance(item["created_at"], str) or len(item["created_at"]) > 64):
            raise AuthConfigurationError()
        salt, digest = _parse_record(item["auth"])
        users[user_id] = {"role": item["role"], "created_at": item["created_at"], "salt": salt, "digest": digest}
    if not any(item["role"] == "owner" for item in users.values()):
        raise AuthConfigurationError()
    return users


def write_users_file(path, users):
    """Atomic 0600 write of the v2 users file (``users`` in read_users_file shape)."""
    if not isinstance(users, dict) or not 1 <= len(users) <= MAX_USERS:
        raise ValueError("用户数量无效")
    serialised = {}
    for user_id, item in users.items():
        if not valid_user_id(user_id) or item.get("role") not in ROLES:
            raise ValueError("用户名或角色无效")
        serialised[user_id] = {"role": item["role"], "created_at": str(item.get("created_at", ""))[:64],
                               "auth": {"algorithm": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                                        "salt": base64.b64encode(item["salt"]).decode("ascii"),
                                        "digest": base64.b64encode(item["digest"]).decode("ascii")}}
    if not any(item["role"] == "owner" for item in serialised.values()):
        raise ValueError("至少保留一个管理员账户")
    _write_private_json(path, {"version": 2, "users": serialised})


@dataclass(frozen=True)
class AuthenticatedSession:
    token_hash: bytes
    csrf: str
    user_id: str = "admin"
    role: str = "owner"


@dataclass
class _Session:
    created_at: float
    last_seen: float
    user_id: str
    fingerprint: bytes


class AuthManager:
    """Single-process sessions, fixed+idle expiration, globally bounded logins.

    Multi-user: every session is bound to one user id and to a fingerprint of that
    user's credential record, so changing or removing a user revokes only their
    sessions. No forwarded IP header is trusted; the login limit stays global.
    """

    def __init__(self, auth_file=DEFAULT_USERS_FILE, *, clock=time.monotonic,
                 login_limit=10, login_window=600, absolute_ttl=8 * 3600,
                 idle_ttl=30 * 60, max_sessions=64):
        self.auth_file = Path(auth_file)
        self.clock = clock
        self.login_limit = login_limit
        self.login_window = login_window
        self.absolute_ttl = absolute_ttl
        self.idle_ttl = idle_ttl
        self.max_sessions = max_sessions
        self._users = read_users_file(self.auth_file)
        self._lock = threading.RLock()
        self._kdf_slots = threading.BoundedSemaphore(2)
        self._attempts = deque()
        self._sessions = {}
        self._csrf_key = secrets.token_bytes(32)

    @staticmethod
    def _fingerprint(record):
        return record["salt"] + record["digest"] + record["role"].encode("ascii")

    def _refresh_credentials(self):
        try:
            current = read_users_file(self.auth_file)
        except AuthConfigurationError:
            self._sessions.clear()
            raise
        if current != self._users:
            self._users = current
            for key, item in list(self._sessions.items()):
                record = current.get(item.user_id)
                if record is None or self._fingerprint(record) != item.fingerprint:
                    del self._sessions[key]

    def _prune_sessions(self, now):
        expired = [key for key, item in self._sessions.items()
                   if now - item.created_at >= self.absolute_ttl
                   or now - item.last_seen >= self.idle_ttl]
        for key in expired:
            del self._sessions[key]

    def _csrf(self, token_hash):
        return hmac.new(self._csrf_key, token_hash, hashlib.sha256).hexdigest()

    @staticmethod
    def _token_hash(token):
        if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            return None
        return hashlib.sha256(token.encode("ascii")).digest()

    def _count_attempt(self):
        now = self.clock()
        while self._attempts and now - self._attempts[0] >= self.login_window:
            self._attempts.popleft()
        if len(self._attempts) >= self.login_limit:
            raise LoginLimited(self.login_window - (now - self._attempts[0]) + 1)
        self._attempts.append(now)

    def _check(self, user_id, password):
        """Rate-limited credential check; returns the matching record or None."""
        with self._lock:
            self._count_attempt()
            self._refresh_credentials()
            record = self._users.get(user_id) if valid_user_id(user_id) else None
        encoded = _password_bytes(password)
        if encoded is None:
            return None
        if not self._kdf_slots.acquire(blocking=False):
            raise LoginLimited(1)
        try:
            # Always run the KDF, with a dummy salt for unknown ids, so timing does not reveal ids.
            derived = _derive(encoded, record["salt"] if record else _DUMMY_SALT)
        finally:
            self._kdf_slots.release()
        if record is None or not hmac.compare_digest(derived, record["digest"]):
            return None
        return record

    def verify_password(self, user_id, password):
        return self._check(user_id, password) is not None

    def login(self, user_id, password):
        record = self._check(user_id, password)
        if record is None:
            return None
        token = secrets.token_urlsafe(32)
        token_hash = self._token_hash(token)
        with self._lock:
            self._refresh_credentials()
            fresh = self._users.get(user_id)
            if fresh is None or self._fingerprint(fresh) != self._fingerprint(record):
                return None
            now = self.clock()
            self._prune_sessions(now)
            if len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions, key=lambda key: self._sessions[key].created_at)
                del self._sessions[oldest]
            self._sessions[token_hash] = _Session(now, now, user_id, self._fingerprint(fresh))
            return token, AuthenticatedSession(token_hash, self._csrf(token_hash), user_id, fresh["role"])

    def authenticate(self, token):
        token_hash = self._token_hash(token)
        if token_hash is None:
            return None
        with self._lock:
            self._refresh_credentials()
            now = self.clock()
            self._prune_sessions(now)
            session = self._sessions.get(token_hash)
            if session is None:
                return None
            record = self._users.get(session.user_id)
            if record is None:
                del self._sessions[token_hash]
                return None
            session.last_seen = now
            return AuthenticatedSession(token_hash, self._csrf(token_hash), session.user_id, record["role"])

    def logout(self, session):
        with self._lock:
            self._sessions.pop(session.token_hash, None)

    # ---- user administration (owner-only at the HTTP layer) ----
    def list_users(self):
        with self._lock:
            self._refresh_credentials()
            return [{"id": user_id, "role": item["role"], "created_at": item["created_at"]}
                    for user_id, item in sorted(self._users.items())]

    def add_user(self, user_id, password, role="member", created_at=""):
        if not valid_user_id(user_id):
            raise ValueError("用户名需为 2–24 位小写字母、数字或下划线，且以字母开头")
        with self._lock, private_file_lock(self.auth_file.with_suffix(".lock")):
            self._refresh_credentials()
            if user_id in self._users:
                raise ValueError("该用户名已存在")
            if len(self._users) >= MAX_USERS:
                raise ValueError("用户数量已达上限")
            users = dict(self._users)
            users[user_id] = new_user_record(password, role, created_at)
            write_users_file(self.auth_file, users)
            self._refresh_credentials()

    def set_password(self, user_id, password, *, keep=None):
        with self._lock, private_file_lock(self.auth_file.with_suffix(".lock")):
            self._refresh_credentials()
            if user_id not in self._users:
                raise ValueError("用户不存在")
            users = dict(self._users)
            current = users[user_id]
            users[user_id] = new_user_record(password, current["role"], current["created_at"])
            kept = self._sessions.get(keep) if keep is not None else None
            write_users_file(self.auth_file, users)
            self._refresh_credentials()
            if kept is not None and kept.user_id == user_id and keep not in self._sessions:
                # The caller changed their own password: keep exactly that session alive.
                self._sessions[keep] = _Session(kept.created_at, kept.last_seen, user_id,
                                                self._fingerprint(self._users[user_id]))

    def remove_user(self, user_id):
        with self._lock, private_file_lock(self.auth_file.with_suffix(".lock")):
            self._refresh_credentials()
            if user_id not in self._users:
                raise ValueError("用户不存在")
            if self._users[user_id]["role"] == "owner":
                raise ValueError("不能移除管理员账户")
            users = dict(self._users)
            del users[user_id]
            write_users_file(self.auth_file, users)
            self._refresh_credentials()

    def add_approved_member(self, user_id, record, provision):
        """Install an already-hashed approved member, with a retry-safe profile first.

        Only the owner approval route calls this. No role or hash is accepted from
        an HTTP request. Existing unrelated accounts must never be overwritten.
        """
        if (not valid_user_id(user_id) or record.get("role") != "member"
                or any(not isinstance(record.get(key), bytes) or len(record[key]) != 32
                       for key in ("salt", "digest"))):
            raise ValueError("审批账户记录无效")
        with self._lock, private_file_lock(self.auth_file.with_suffix(".lock")):
            self._refresh_credentials()
            existing = self._users.get(user_id)
            if existing is not None:
                if existing != record:
                    raise ValueError("用户名已被其他账户使用，不能覆盖，请联系申请人")
                return  # Recovery after account commit but before the approval receipt.
            if len(self._users) >= MAX_USERS:
                raise ValueError("成员数量已达上限，请先整理成员")
            provision()
            users = dict(self._users)
            users[user_id] = record
            write_users_file(self.auth_file, users)
            self._refresh_credentials()
