"""Loopback-only HTTPS-proxied administrator UI. No check-in submission route."""
import argparse
import getpass
from http.client import LineTooLong
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

from src.admin_auth import (AuthConfigurationError, AuthManager, DEFAULT_AUTH_FILE,
                            DEFAULT_USERS_FILE, LoginLimited, valid_user_id, write_password_file)
from src.registration import RegistrationError, RegistrationManager


COOKIE_NAME = "__Secure-fzu_session"
BODY_LIMIT = 64 * 1024
HEADER_LIMIT = 16 * 1024
REQUEST_SECONDS = 15
STATIC_FILES = {"": ("index.html", "text/html; charset=utf-8"),
                "index.html": ("index.html", "text/html; charset=utf-8"),
                "admin.js": ("admin.js", "application/javascript; charset=utf-8"),
                "location.js": ("location.js", "application/javascript; charset=utf-8"),
                "admin.css": ("admin.css", "text/css; charset=utf-8")}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'; object-src 'none'")


class RequestError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def validate_origin(origin):
    if not isinstance(origin, str) or re.search(r"[\s\\]", origin):
        raise ValueError("FZU_ADMIN_ORIGIN 必须是准确的 HTTPS origin，不含路径")
    try:
        parsed = urlsplit(origin)
        valid = (parsed.scheme == "https" and parsed.netloc and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and origin == "https://" + parsed.netloc
                 and (parsed.port is None or 1 <= parsed.port <= 65535)
                 and re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[A-Fa-f0-9:]+\])(?::[0-9]+)?", parsed.netloc) is not None)
        origin.encode("ascii")
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise ValueError("FZU_ADMIN_ORIGIN 必须是准确的 HTTPS origin，不含路径")
    return origin, parsed.netloc


def validate_base_path(base_path):
    if not isinstance(base_path, str) or re.fullmatch(r"/[A-Za-z0-9_-]{1,48}", base_path) is None:
        raise ValueError("FZU_ADMIN_BASE_PATH 必须是单段路径，例如 /fzu")
    return base_path


class _DeadlineReader(io.RawIOBase):
    """Apply one total deadline to headers and body, including bytewise drips."""

    def __init__(self, connection, deadline):
        super().__init__()
        self.connection = connection
        self.deadline = deadline
        self.remaining_bytes = BODY_LIMIT + HEADER_LIMIT + 2048

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request deadline")
        self.connection.settimeout(remaining)
        count = self.connection.recv_into(buffer, min(len(buffer), self.remaining_bytes + 1))
        self.remaining_bytes -= count
        if self.remaining_bytes < 0:
            raise LineTooLong("request headers")
        return count


class _FixedRegistry:
    """Adapter for a single backend object (tests, legacy single-profile deployments)."""

    def __init__(self, backend):
        self.backend = backend

    def for_user(self, user_id):
        return self.backend

    def profile_exists(self, user_id):
        return True

    def create_profile(self, user_id):
        return None

    def archive_profile(self, user_id):
        return None


class AdminHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, server_address=("127.0.0.1", 18779), *, backend=None,
                 auth_manager=None, origin=None, base_path=None, static_dir=None,
                 max_connections=32, request_seconds=REQUEST_SECONDS):
        if server_address[0] != "127.0.0.1":
            raise ValueError("管理员服务只能监听 127.0.0.1")
        self.origin, self.expected_host = validate_origin(
            origin if origin is not None else os.environ.get("FZU_ADMIN_ORIGIN", ""))
        self.base_path = validate_base_path(
            base_path if base_path is not None else os.environ.get("FZU_ADMIN_BASE_PATH", "/fzu"))
        self.static_dir = Path(static_dir) if static_dir is not None else Path(__file__).resolve().parent / "admin_static"
        if backend is None:
            from src.admin_backend import BackendRegistry
            backend = BackendRegistry()
        self.backends = backend if hasattr(backend, "for_user") else _FixedRegistry(backend)
        self.backend = backend
        self.auth = auth_manager if auth_manager is not None else AuthManager(
            os.environ.get("FZU_ADMIN_USERS_FILE") or os.environ.get("FZU_ADMIN_AUTH_FILE", DEFAULT_AUTH_FILE))
        self.registrations = RegistrationManager(self.auth, self.backends)
        self.request_seconds = request_seconds
        self._connection_slots = threading.BoundedSemaphore(max_connections)
        super().__init__(server_address, AdminHandler)

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def handle_error(self, request, client_address):
        # The stdlib default logs tracebacks with request and filesystem detail.
        # The backend owns sanitized operation records; HTTP never logs secrets.
        pass


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "FZUAdmin"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.request_seconds)
        self.rfile.close()
        self.rfile = io.BufferedReader(_DeadlineReader(
            self.connection, time.monotonic() + self.server.request_seconds))

    def log_message(self, format, *args):
        # In particular, do not log the raw request target or passwords in URLs.
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        location_document = self.command == "GET" and self.path in {
            self.server.base_path + "/", self.server.base_path + "/index.html"}
        geolocation = "(self)" if location_document else "()"
        self.send_header("Permissions-Policy", f"camera=(), microphone=(), geolocation={geolocation}")
        self.send_header("Connection", "close")
        self.close_connection = True
        super().end_headers()

    def _json(self, status, data, headers=None):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, code, message, headers=None):
        self._json(status, {"ok": False, "error": {"code": code, "message": message}}, headers)

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler can otherwise reflect arbitrary methods/URLs.
        self._error(code, "HTTP_ERROR", "请求不受支持或格式无效")

    def handle_expect_100(self):
        self._error(417, "EXPECT_UNSUPPORTED", "不支持 Expect 请求头")
        return False

    def _headers_safe(self):
        if sum(len(key) + len(value) + 4 for key, value in self.headers.items()) > HEADER_LIMIT:
            raise RequestError(431, "HEADERS_TOO_LARGE", "请求头过大")
        host = self.headers.get_all("Host", [])
        if len(host) != 1 or host[0] != self.server.expected_host:
            raise RequestError(403, "HOST_REJECTED", "请求主机不匹配")
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0] != self.server.origin):
            raise RequestError(403, "ORIGIN_REJECTED", "仅允许管理页面同源请求")
        if self.command in {"POST", "PUT"} and origins != [self.server.origin]:
            raise RequestError(403, "ORIGIN_REQUIRED", "写入请求需要同源验证")
        if (self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Content-Encoding")
                or self.headers.get_all("Expect")):
            raise RequestError(400, "ENCODING_REJECTED", "不支持请求传输编码")
        if len(self.path) > 2048 or not self.path.startswith("/") or self.path.startswith("//"):
            raise RequestError(400, "PATH_REJECTED", "请求路径无效")
        if self.command == "GET" and self.headers.get_all("Content-Length", []) not in ([], ["0"]):
            raise RequestError(400, "BODY_UNEXPECTED", "读取请求不能附带内容")

    def _read_json(self):
        content_types = self.headers.get_all("Content-Type", [])
        if (len(content_types) != 1
                or re.fullmatch(r"application/json(?:;\s*charset=utf-8)?", content_types[0], re.IGNORECASE) is None):
            raise RequestError(415, "JSON_REQUIRED", "请使用 application/json 请求")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or re.fullmatch(r"[0-9]{1,10}", lengths[0]) is None:
            raise RequestError(411, "LENGTH_REQUIRED", "请求需要准确的内容长度")
        length = int(lengths[0])
        if length > BODY_LIMIT:
            raise RequestError(413, "BODY_TOO_LARGE", "请求内容过大")
        if length == 0:
            raise RequestError(400, "JSON_INVALID", "请求必须是 JSON 对象")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            def no_constants(value):
                raise ValueError
            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError
                    value[key] = item
                return value
            payload = json.loads(raw.decode("utf-8"), parse_constant=no_constants,
                                 object_pairs_hook=unique_object)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, UnicodeError, RecursionError):
            raise RequestError(400, "JSON_INVALID", "请求必须是有效的 JSON 对象") from None

    def _session(self):
        cookies = self.headers.get_all("Cookie", [])
        if len(cookies) != 1 or len(cookies[0]) > 4096:
            return None
        try:
            cookie = SimpleCookie()
            cookie.load(cookies[0])
            token = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else None
        except CookieError:
            return None
        return self.server.auth.authenticate(token)

    def _require_session(self):
        session = self._session()
        if session is None:
            raise RequestError(401, "AUTH_REQUIRED", "请先登录管理页面")
        if self.command in {"POST", "PUT"}:
            tokens = self.headers.get_all("X-CSRF-Token", [])
            if len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode("utf-8"), session.csrf.encode("ascii")):
                raise RequestError(403, "CSRF_REJECTED", "页面验证已失效，请刷新后重试")
        return session

    def _cookie(self, token, clear=False):
        age = 0 if clear else int(self.server.auth.absolute_ttl)
        value = (f"{COOKIE_NAME}={token}; Path={self.server.base_path}/; "
                 f"Max-Age={age}; Secure; HttpOnly; SameSite=Strict")
        if clear:
            value += "; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
        return value

    def _dispatch(self):
        self._headers_safe()
        prefix = self.server.base_path
        path = self.path
        if path == prefix and self.command == "GET":
            self._json(308, {"ok": True}, {"Location": prefix + "/"})
            return
        if path.startswith(prefix + "/") and not path.startswith(prefix + "/api/"):
            relative = path[len(prefix) + 1:]
            if relative not in STATIC_FILES:
                raise RequestError(404, "NOT_FOUND", "页面不存在")
            if self.command != "GET":
                raise RequestError(405, "METHOD_REJECTED", "不支持此请求方法")
            filename, mime = STATIC_FILES[relative]
            try:
                target = self.server.static_dir / filename
                if target.is_symlink() or not target.is_file():
                    raise OSError
                content = target.read_bytes()
            except OSError:
                raise RequestError(503, "STATIC_UNAVAILABLE", "管理页面资源暂不可用") from None
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if not path.startswith(prefix + "/api/"):
            raise RequestError(404, "NOT_FOUND", "页面不存在")
        endpoint = path[len(prefix + "/api/"):]
        if endpoint == "session" and self.command == "GET":
            session = self._session()
            if session is None:
                self._json(401, {"authenticated": False, "csrf": None})
            else:
                self._json(200, {"authenticated": True, "csrf": session.csrf,
                                 "user": {"id": session.user_id, "role": session.role}})
            return
        if endpoint == "login" and self.command == "POST":
            payload = self._read_json()
            if (set(payload) != {"username", "password"} or not isinstance(payload["username"], str)
                    or not isinstance(payload["password"], str) or not valid_user_id(payload["username"])):
                raise RequestError(400, "LOGIN_INVALID", "请输入用户名和密码")
            logged_in = self.server.auth.login(payload["username"], payload["password"])
            if logged_in is None:
                raise RequestError(401, "LOGIN_FAILED", "用户名或密码错误")
            token, session = logged_in
            self._json(200, {"ok": True, "csrf": session.csrf,
                             "user": {"id": session.user_id, "role": session.role}},
                       {"Set-Cookie": self._cookie(token)})
            return
        if endpoint == "registration" and self.command == "GET":
            self._json(200, self.server.registrations.public_settings())
            return
        if endpoint in {"registration/apply", "registration/status"} and self.command == "POST":
            payload = self._read_json()
            expected = {"username", "password", "note"} if endpoint.endswith("/apply") else {"username", "password"}
            if set(payload) != expected or any(not isinstance(value, str) for value in payload.values()):
                raise RequestError(400, "REGISTRATION_INVALID", "请填写页面支持的注册或查询字段")
            if endpoint.endswith("/apply"):
                result = self.server.registrations.apply(payload["username"], payload["password"], payload["note"])
                self._json(202, result)
            else:
                self._json(200, self.server.registrations.applicant_status(payload["username"], payload["password"]))
            return
        session = self._require_session()

        def backend():
            return self.server.backends.for_user(session.user_id)

        def require_owner():
            if session.role != "owner":
                raise RequestError(403, "OWNER_REQUIRED", "仅管理员可执行此操作")

        if endpoint == "registrations" and self.command == "GET":
            require_owner()
            self._json(200, self.server.registrations.overview())
        elif endpoint == "registration/settings" and self.command == "PUT":
            require_owner()
            payload = self._read_json()
            if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
                raise RequestError(400, "REGISTRATION_SETTINGS_INVALID", "请明确选择开启或关闭注册")
            self._json(200, self.server.registrations.set_enabled(payload["enabled"]))
        elif endpoint.startswith("registrations/") and self.command == "POST":
            require_owner()
            parts = endpoint.split("/")
            if len(parts) != 3 or not re.fullmatch(r"[a-f0-9]{32}", parts[1]) or parts[2] not in {"approve", "reject"}:
                raise RequestError(404, "NOT_FOUND", "审批接口不存在")
            payload = self._read_json()
            expected = {"confirmed"} if parts[2] == "approve" else {"confirmed", "reason"}
            if set(payload) != expected or payload.get("confirmed") is not True:
                raise RequestError(400, "CONFIRM_REQUIRED", "请明确确认本次审批")
            result = self.server.registrations.review(parts[1], parts[2], session.user_id, payload.get("reason", ""))
            self._json(200, result)
        elif endpoint == "logout" and self.command == "POST":
            payload = self._read_json()
            if payload:
                raise RequestError(400, "PAYLOAD_UNEXPECTED", "此操作不需要参数")
            self.server.auth.logout(session)
            self._json(200, {"ok": True}, {"Set-Cookie": self._cookie("", clear=True)})
        elif endpoint == "password" and self.command == "PUT":
            payload = self._read_json()
            if (set(payload) != {"current_password", "new_password"}
                    or any(not isinstance(payload[key], str) for key in payload)):
                raise RequestError(400, "PASSWORD_INVALID", "请填写当前密码和新密码")
            if not self.server.auth.verify_password(session.user_id, payload["current_password"]):
                raise RequestError(401, "PASSWORD_WRONG", "当前密码不正确")
            try:
                self.server.auth.set_password(session.user_id, payload["new_password"], keep=session.token_hash)
            except ValueError as error:
                raise RequestError(400, "PASSWORD_REJECTED", str(error)) from None
            self._json(200, {"ok": True})
        elif endpoint == "users" and self.command == "GET":
            require_owner()
            self._json(200, {"users": self.server.auth.list_users()})
        elif endpoint == "users" and self.command == "POST":
            require_owner()
            payload = self._read_json()
            if (set(payload) != {"username", "password"}
                    or any(not isinstance(payload[key], str) for key in payload)):
                raise RequestError(400, "USER_INVALID", "请填写用户名和初始密码")
            username = payload["username"]
            try:
                self.server.auth.add_user(username, payload["password"], "member",
                                          created_at=time.strftime("%Y-%m-%d"))
            except ValueError as error:
                raise RequestError(400, "USER_REJECTED", str(error)) from None
            try:
                self.server.backends.create_profile(username)
            except Exception:
                try:
                    self.server.auth.remove_user(username)
                except ValueError:
                    pass
                raise
            self._json(201, {"ok": True, "users": self.server.auth.list_users()})
        elif endpoint.startswith("users/") and self.command == "POST":
            require_owner()
            parts = endpoint.split("/")
            if len(parts) != 3 or not valid_user_id(parts[1]) or parts[2] not in {"password", "remove"}:
                raise RequestError(404, "NOT_FOUND", "接口不存在")
            target, operation = parts[1], parts[2]
            payload = self._read_json()
            if target == session.user_id:
                raise RequestError(400, "SELF_TARGET", "自己的账户请在“修改密码”中处理")
            if operation == "password":
                if set(payload) != {"password"} or not isinstance(payload["password"], str):
                    raise RequestError(400, "USER_INVALID", "请填写新密码")
                try:
                    self.server.auth.set_password(target, payload["password"])
                except ValueError as error:
                    raise RequestError(400, "USER_REJECTED", str(error)) from None
                self._json(200, {"ok": True})
            else:
                if payload != {"confirmed": True}:
                    raise RequestError(400, "CONFIRM_REQUIRED", "请确认移除该成员")
                try:
                    self.server.backends.for_user(target).controller("pause")
                except Exception:
                    pass
                try:
                    self.server.auth.remove_user(target)
                except ValueError as error:
                    raise RequestError(400, "USER_REJECTED", str(error)) from None
                self.server.backends.archive_profile(target)
                self._json(200, {"ok": True, "users": self.server.auth.list_users()})
        elif endpoint == "config" and self.command == "GET":
            self._json(200, backend().get_config())
        elif endpoint == "config" and self.command == "PUT":
            self._json(200, backend().save_config(self._read_json()))
        elif endpoint == "status" and self.command == "GET":
            self._json(200, backend().get_status())
        elif endpoint.startswith("actions/") and self.command == "POST":
            action = endpoint[len("actions/"):]
            if action not in {"preflight", "resume", "pause", "notify-test"}:
                raise RequestError(404, "ACTION_REJECTED", "此操作不受支持")
            self._json(202, backend().start_action(action, self._read_json()))
        elif endpoint in {"session", "login", "logout", "config", "status", "password", "users"}:
            raise RequestError(405, "METHOD_REJECTED", "不支持此请求方法")
        else:
            raise RequestError(404, "NOT_FOUND", "接口不存在")

    def _handle(self):
        try:
            self._dispatch()
        except RequestError as error:
            self._error(error.status, error.code, error.message)
        except RegistrationError as error:
            headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
            self._error(error.status, error.code, error.message, headers)
        except LoginLimited as error:
            self._error(429, "LOGIN_LIMITED", "登录尝试过于频繁，请稍后重试",
                        {"Retry-After": str(error.retry_after)})
        except AuthConfigurationError:
            self._error(503, "AUTH_UNAVAILABLE", "管理员认证暂不可用，请在服务器本地检查")
        except (TimeoutError, socket.timeout):
            self._error(408, "REQUEST_TIMEOUT", "请求超时")
        except LineTooLong:
            self._error(431, "HEADERS_TOO_LARGE", "请求头过大")
        except (BrokenPipeError, ConnectionError):
            self.close_connection = True
        except Exception as error:
            # Only the backend's explicit, sanitized error class is publishable.
            try:
                from src.admin_backend import BackendError
            except ImportError:
                BackendError = ()
            if isinstance(error, BackendError):
                self._error(error.status, error.code, error.message)
            else:
                self._error(500, "INTERNAL_ERROR", "操作失败，请稍后重试或检查服务状态")

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle


def main(argv=None):
    parser = argparse.ArgumentParser(description="福大签到私有管理员页面")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="启动仅回环地址的管理服务")
    initialize = subparsers.add_parser("init-auth", help="通过本地隐藏输入初始化或重设管理员密码")
    initialize.add_argument("--auth-file", default=os.environ.get("FZU_ADMIN_AUTH_FILE", DEFAULT_AUTH_FILE))
    users_file = os.environ.get("FZU_ADMIN_USERS_FILE", DEFAULT_USERS_FILE)
    init_users = subparsers.add_parser("init-users", help="首次部署：创建账户文件与管理员账户（本地隐藏输入密码）")
    init_users.add_argument("--users-file", default=users_file)
    init_users.add_argument("--owner", default="admin", help="管理员用户名（缺省 admin）")
    for name, text in (("user-add", "新增成员账户（本地隐藏输入初始密码）"),
                       ("user-password", "重设某个账户的密码（本地隐藏输入）"),
                       ("user-remove", "移除成员账户（不删除其配置档案）"),
                       ("user-list", "列出账户")):
        command = subparsers.add_parser(name, help=text)
        command.add_argument("--users-file", default=users_file)
        if name != "user-list":
            command.add_argument("user_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "init-users":
            from src.admin_auth import new_user_record, write_users_file
            from src.admin_backend import BackendRegistry
            if not valid_user_id(args.owner):
                parser.error("管理员用户名需为 2–24 位小写字母、数字或下划线，且以字母开头")
            if Path(args.users_file).exists():
                parser.error("账户文件已存在；如需重设密码请用 user-password")
            if not sys.stdin.isatty():
                parser.error("请在本地交互式终端输入密码；不接受管道或命令行明文")
            password = getpass.getpass(f"管理员 {args.owner} 的登录密码（至少10字符）: ")
            confirmation = getpass.getpass("再次输入密码: ")
            if not hmac.compare_digest(password.encode("utf-8"), confirmation.encode("utf-8")):
                parser.error("两次输入的密码不一致")
            write_users_file(args.users_file, {args.owner: new_user_record(password, "owner", time.strftime("%Y-%m-%d"))})
            registry = BackendRegistry()
            if not registry.profile_exists(args.owner):
                registry.create_profile(args.owner)
            print(f"已创建管理员 {args.owner} 与其空白配置档案；现在可以打开管理页登录。")
            return 0
        if args.command in {"user-add", "user-password", "user-remove", "user-list"}:
            manager = AuthManager(args.users_file)
            if args.command == "user-list":
                for item in manager.list_users():
                    print(f"{item['id']}\t{item['role']}\t{item['created_at']}")
                return 0
            if not valid_user_id(args.user_id):
                parser.error("用户名需为 2–24 位小写字母、数字或下划线，且以字母开头")
            if args.command == "user-remove":
                manager.remove_user(args.user_id)
                print("账户已移除；其配置档案未删除。")
                return 0
            if not sys.stdin.isatty():
                parser.error("请在本地交互式终端输入密码；不接受管道或命令行明文")
            password = getpass.getpass("密码（至少10字符）: ")
            confirmation = getpass.getpass("再次输入密码: ")
            if not hmac.compare_digest(password.encode("utf-8"), confirmation.encode("utf-8")):
                parser.error("两次输入的密码不一致")
            if args.command == "user-add":
                manager.add_user(args.user_id, password, "member", created_at=time.strftime("%Y-%m-%d"))
                from src.admin_backend import BackendRegistry
                BackendRegistry().create_profile(args.user_id)
                print("成员账户与空白配置档案已创建。")
            else:
                manager.set_password(args.user_id, password)
                print("密码已更新；该账户旧会话将失效。")
            return 0
        if args.command == "init-auth":
            if not sys.stdin.isatty():
                parser.error("请在本地交互式终端输入管理员密码；不接受管道或命令行明文")
            password = getpass.getpass("新的管理员密码（至少16字符）: ")
            confirmation = getpass.getpass("再次输入管理员密码: ")
            if not hmac.compare_digest(password.encode("utf-8"), confirmation.encode("utf-8")):
                parser.error("两次输入的密码不一致")
            write_password_file(args.auth_file, password)
            print("管理员密码已更新；旧会话将在下一次请求时失效。")
            return 0
        if os.geteuid() == 0:
            parser.error("管理服务必须以 fzu-checkin 等非 root 专用账号运行")
        port = int(os.environ.get("FZU_ADMIN_PORT", "18779"))
        if not 1024 <= port <= 65535:
            parser.error("FZU_ADMIN_PORT 必须是 1024 到 65535")
        server = AdminHTTPServer(("127.0.0.1", port))
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
        return 0
    except ValueError as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 2
    except AuthConfigurationError:
        print("管理服务初始化失败；请检查 HTTPS origin、路径、私有认证文件或密码格式。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
