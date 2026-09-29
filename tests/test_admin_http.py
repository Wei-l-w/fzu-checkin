"""Offline HTTP/auth regressions. Fake backend; no school network or submission."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from admin_server import (AdminHTTPServer, BODY_LIMIT, COOKIE_NAME,
                          validate_base_path, validate_origin)
from src.admin_auth import (AuthConfigurationError, AuthManager, LoginLimited,
                            read_password_file, write_password_file)


PASSWORD = "offline-administrator-password-ONLY"
ORIGIN = "https://admin.example.invalid"
HOST = "admin.example.invalid"


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.failure = None

    def get_config(self):
        if self.failure is not None:
            raise self.failure
        self.calls.append(("get_config",))
        return {"config": {"user": {"username": "offline-user"}},
                "configured": {"password": True}, "revision": "offline-revision"}

    def save_config(self, payload):
        self.calls.append(("save_config", payload))
        return {"ok": True, "saved": True}

    def get_status(self):
        self.calls.append(("get_status",))
        return {"today": {"status": "paused"}, "history": []}

    def start_action(self, action, payload):
        self.calls.append(("start_action", action, payload))
        return {"accepted": True, "action": action}


class AuthFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory(prefix="fzu-http-auth-template-")
        cls.template_path = Path(cls.template.name) / "auth.json"
        write_password_file(cls.template_path, PASSWORD)
        cls.template_bytes = cls.template_path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fzu-http-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.auth_path = self.directory / "auth.json"
        self.auth_path.write_bytes(self.template_bytes)
        self.auth_path.chmod(0o600)


class AdminAuthenticationTests(AuthFixture):
    def test_only_scrypt_hash_is_persisted_with_private_permissions(self):
        self.assertNotIn(PASSWORD.encode(), self.auth_path.read_bytes())
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.auth_path.stat().st_mode & 0o777, 0o600)
        salt, digest = read_password_file(self.auth_path)
        self.assertEqual((len(salt), len(digest)), (32, 32))
        self.assertEqual(json.loads(self.auth_path.read_bytes())["algorithm"], "scrypt")

    def test_public_auth_file_or_directory_are_refused(self):
        self.auth_path.chmod(0o640)
        with self.assertRaises(AuthConfigurationError):
            AuthManager(self.auth_path)
        self.auth_path.chmod(0o600)
        self.directory.chmod(0o750)
        with self.assertRaises(AuthConfigurationError):
            AuthManager(self.auth_path)
        self.directory.chmod(0o700)

    def test_symlinks_and_cost_parameter_changes_are_refused(self):
        link = self.directory / "link.json"
        link.symlink_to(self.auth_path)
        with self.assertRaises(AuthConfigurationError):
            read_password_file(link)
        with self.assertRaises(AuthConfigurationError):
            write_password_file(link, PASSWORD)
        value = json.loads(self.auth_path.read_bytes())
        value["n"] = 2 ** 24
        self.auth_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(AuthConfigurationError):
            read_password_file(self.auth_path)

    def test_password_file_errors_are_sanitized_and_atomic(self):
        before = self.auth_path.read_bytes()
        with patch("src.admin_auth.os.replace", side_effect=OSError("OFFLINE-SECRET-PATH")):
            with self.assertRaises(AuthConfigurationError) as caught:
                write_password_file(self.auth_path, PASSWORD + "-new")
        self.assertNotIn("OFFLINE-SECRET-PATH", str(caught.exception))
        self.assertEqual(self.auth_path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob(".auth-*")), [])

    def test_short_or_oversized_new_password_is_refused(self):
        for password in ("short", "x" * 1025, None):
            with self.subTest(kind=type(password).__name__):
                with self.assertRaises(ValueError):
                    write_password_file(self.auth_path, password)

    def test_sessions_store_hash_only_and_enforce_idle_expiry(self):
        now = [10.0]
        manager = AuthManager(self.auth_path, clock=lambda: now[0], idle_ttl=30)
        token, session = manager.login("admin", PASSWORD)
        self.assertNotIn(token, repr(manager._sessions))
        self.assertEqual(list(manager._sessions), [session.token_hash])
        self.assertEqual(manager.authenticate(token).csrf, session.csrf)
        now[0] = 39.0
        self.assertIsNotNone(manager.authenticate(token))
        now[0] = 69.0
        self.assertIsNone(manager.authenticate(token))

    def test_absolute_expiry_is_not_extended_by_activity(self):
        now = [0.0]
        manager = AuthManager(self.auth_path, clock=lambda: now[0], absolute_ttl=100, idle_ttl=30)
        token, _ = manager.login("admin", PASSWORD)
        for moment in (20, 40, 60, 80, 99):
            now[0] = float(moment)
            self.assertIsNotNone(manager.authenticate(token))
        now[0] = 100.0
        self.assertIsNone(manager.authenticate(token))

    def test_password_rotation_revokes_existing_sessions(self):
        manager = AuthManager(self.auth_path)
        token, _ = manager.login("admin", PASSWORD)
        write_password_file(self.auth_path, PASSWORD + "-rotated")
        self.assertIsNone(manager.authenticate(token))
        self.assertIsNone(manager.login("admin", PASSWORD))
        self.assertIsNotNone(manager.login("admin", PASSWORD + "-rotated"))

    def test_unavailable_credentials_fail_closed_for_existing_session(self):
        manager = AuthManager(self.auth_path)
        token, _ = manager.login("admin", PASSWORD)
        self.auth_path.chmod(0o644)
        with self.assertRaises(AuthConfigurationError):
            manager.authenticate(token)
        self.assertEqual(manager._sessions, {})

    def test_global_limiter_expires_and_is_concurrency_safe(self):
        now = [10.0]
        manager = AuthManager(self.auth_path, clock=lambda: now[0], login_limit=4, login_window=60)
        def attempt(_):
            try:
                manager.login("admin", "wrong-offline-password")
                return "failed"
            except LoginLimited:
                return "limited"
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(attempt, range(12)))
        self.assertGreaterEqual(results.count("limited"), 8)
        self.assertEqual(len(manager._attempts), 4)
        with self.assertRaises(LoginLimited):
            manager.login("admin", PASSWORD)
        now[0] = 70.0
        self.assertIsNotNone(manager.login("admin", PASSWORD))

    def test_session_count_is_bounded(self):
        manager = AuthManager(self.auth_path, max_sessions=2)
        first, _ = manager.login("admin", PASSWORD)
        second, _ = manager.login("admin", PASSWORD)
        third, _ = manager.login("admin", PASSWORD)
        self.assertEqual(len(manager._sessions), 2)
        self.assertIsNone(manager.authenticate(first))
        self.assertIsNotNone(manager.authenticate(second))
        self.assertIsNotNone(manager.authenticate(third))


class AdminHTTPTests(AuthFixture):
    def setUp(self):
        super().setUp()
        self.backend = FakeBackend()
        self.auth = AuthManager(self.auth_path)
        self.static_dir = self.directory / "admin_static"
        self.static_dir.mkdir()
        for filename, contents in (("index.html", "<html>offline-login-only</html>"),
                                   ("admin.js", "'use strict';"), ("location.js", "'use strict';"),
                                   ("admin.css", "body{margin:0}")):
            (self.static_dir / filename).write_text(contents, encoding="utf-8")
        self.server = AdminHTTPServer(("127.0.0.1", 0), backend=self.backend,
                                      auth_manager=self.auth, origin=ORIGIN,
                                      base_path="/fzu", static_dir=self.static_dir)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.cookie = None
        self.csrf = None

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, payload=None, headers=None, raw=None):
        request_headers = {"Host": HOST}
        if method in {"POST", "PUT"}:
            request_headers.update({"Origin": ORIGIN, "Content-Type": "application/json"})
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        if self.csrf and method in {"POST", "PUT"}:
            request_headers["X-CSRF-Token"] = self.csrf
        for key, value in (headers or {}).items():
            if value is None:
                request_headers.pop(key, None)
            else:
                request_headers[key] = value
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def login(self):
        return self.login_as("admin", PASSWORD)

    def login_as(self, username, password):
        self.cookie, self.csrf = None, None
        status, headers, body = self.request("POST", "/fzu/api/login", {"username": username, "password": password})
        self.assertEqual(status, 200, body)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = json.loads(body)["csrf"]
        return headers

    def test_login_requires_a_valid_username_and_session_reports_the_user(self):
        for payload in ({"password": PASSWORD}, {"username": "Admin", "password": PASSWORD},
                        {"username": "a", "password": PASSWORD}, {"username": "admin"}):
            self.assertEqual(self.request("POST", "/fzu/api/login", payload)[0], 400, payload)
        status, headers, body = self.request("POST", "/fzu/api/login", {"username": "nobody", "password": PASSWORD})
        self.assertEqual((status, "Set-Cookie" in headers), (401, False))
        self.login()
        self.assertEqual(json.loads(self.request("GET", "/fzu/api/session")[2])["user"], {"id": "admin", "role": "owner"})

    def test_owner_manages_members_and_members_only_manage_themselves(self):
        self.login()
        status, _, body = self.request("GET", "/fzu/api/users")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in json.loads(body)["users"]], ["admin"])
        status, _, body = self.request("POST", "/fzu/api/users", {"username": "mate", "password": "mate-initial-pw"})
        self.assertEqual(status, 201, body)
        self.assertEqual([item["id"] for item in json.loads(body)["users"]], ["admin", "mate"])
        for bad in ({"username": "mate", "password": "mate-initial-pw"}, {"username": "x", "password": "mate-initial-pw"},
                    {"username": "mate2", "password": "short"}, {"username": "mate3"}):
            self.assertEqual(self.request("POST", "/fzu/api/users", bad)[0], 400, bad)
        self.assertEqual(self.request("POST", "/fzu/api/users/admin/remove", {"confirmed": True})[0], 400)
        self.assertEqual(self.request("POST", "/fzu/api/users/mate/remove", {})[0], 400)
        owner = (self.cookie, self.csrf)
        self.login_as("mate", "mate-initial-pw")
        self.assertEqual(json.loads(self.request("GET", "/fzu/api/session")[2])["user"], {"id": "mate", "role": "member"})
        self.assertEqual(self.request("GET", "/fzu/api/users")[0], 403)
        self.assertEqual(self.request("POST", "/fzu/api/users", {"username": "z1", "password": "zz-password-01"})[0], 403)
        self.assertEqual(self.request("POST", "/fzu/api/users/admin/password", {"password": "zz-password-01"})[0], 403)
        self.assertEqual(self.request("GET", "/fzu/api/config")[0], 200)
        status, _, body = self.request("PUT", "/fzu/api/password", {"current_password": "wrong-pw-000", "new_password": "mate-new-pw-01"})
        self.assertEqual(status, 401)
        self.assertEqual(self.request("PUT", "/fzu/api/password", {"current_password": "mate-initial-pw", "new_password": "short"})[0], 400)
        status, _, body = self.request("PUT", "/fzu/api/password", {"current_password": "mate-initial-pw", "new_password": "mate-new-pw-01"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.request("GET", "/fzu/api/session")[0], 200, "own password change keeps this session")
        member = (self.cookie, self.csrf)
        self.cookie, self.csrf = owner
        self.assertEqual(self.request("POST", "/fzu/api/users/mate/password", {"password": "mate-reset-pw-02"})[0], 200)
        self.cookie, self.csrf = member
        self.assertEqual(self.request("GET", "/fzu/api/session")[0], 401, "owner reset revokes the member session")
        self.login_as("mate", "mate-reset-pw-02")
        self.cookie, self.csrf = owner
        status, _, body = self.request("POST", "/fzu/api/users/mate/remove", {"confirmed": True})
        self.assertEqual(status, 200, body)
        self.assertEqual([item["id"] for item in json.loads(body)["users"]], ["admin"])
        self.cookie, self.csrf = None, None
        self.assertEqual(self.request("POST", "/fzu/api/login", {"username": "mate", "password": "mate-reset-pw-02"})[0], 401)

    def test_anonymous_static_access_has_full_security_headers(self):
        for path in ("/fzu/", "/fzu/index.html", "/fzu/admin.js", "/fzu/admin.css", "/fzu/location.js"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertNotIn("'unsafe-inline'", headers["Content-Security-Policy"])
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.backend.calls, [])

    def test_geolocation_is_limited_to_same_origin_admin_documents(self):
        for path in ("/fzu/", "/fzu/index.html"):
            status, headers, _ = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Permissions-Policy"], "camera=(), microphone=(), geolocation=(self)")
        for path in ("/fzu", "/fzu/admin.js", "/fzu/location.js", "/fzu/api/session", "/fzu/other.html"):
            _, headers, _ = self.request("GET", path)
            self.assertEqual(headers["Permissions-Policy"], "camera=(), microphone=(), geolocation=()")
        self.assertEqual(self.backend.calls, [])

    def test_prefix_redirect_is_relative_and_uppercase_prefix_is_supported(self):
        status, headers, _ = self.request("GET", "/fzu")
        self.assertEqual((status, headers["Location"]), (308, "/fzu/"))
        self.server.base_path = "/FZU"
        self.assertEqual(self.request("GET", "/FZU/")[0], 200)
        self.assertEqual(self.request("GET", "/fzu/")[0], 404)

    def test_static_whitelist_refuses_traversal_queries_and_unlisted_files(self):
        for path in ("/fzu/../config.yaml", "/fzu/%2e%2e/config.yaml", "/fzu/admin.js?secret=NO",
                     "/fzu/src/admin_auth.py", "/config.yaml", "/fzu/admin_static/index.html",
                     "/fzu//admin.js", "/fzu/.env"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        (self.static_dir / "admin.css").unlink()
        (self.static_dir / "admin.css").symlink_to(self.auth_path)
        status, _, body = self.request("GET", "/fzu/admin.css")
        self.assertEqual(status, 503)
        self.assertNotIn(b"scrypt", body)

    def test_unauthenticated_apis_are_401_and_never_call_backend(self):
        for method, path, payload in (("GET", "/fzu/api/config", None),
                                      ("GET", "/fzu/api/status", None),
                                      ("PUT", "/fzu/api/config", {}),
                                      ("POST", "/fzu/api/actions/resume", {}),
                                      ("POST", "/fzu/api/logout", {})):
            with self.subTest(path=path):
                status, headers, _ = self.request(method, path, payload)
                self.assertEqual(status, 401)
                self.assertEqual(headers["Cache-Control"], "no-store")
        status, _, body = self.request("GET", "/fzu/api/session")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"authenticated": False, "csrf": None})
        self.assertEqual(self.backend.calls, [])

    def test_login_session_cookie_and_logout(self):
        headers = self.login()
        cookie = headers["Set-Cookie"]
        for flag in (COOKIE_NAME + "=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/fzu/", "Max-Age=28800"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Domain=", cookie)
        status, _, body = self.request("GET", "/fzu/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"authenticated": True, "csrf": self.csrf, "user": {"id": "admin", "role": "owner"}})
        status, headers, _ = self.request("POST", "/fzu/api/logout", {})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/fzu/api/config")[0], 401)

    def test_wrong_password_has_no_cookie_and_cannot_echo_secret(self):
        status, headers, body = self.request("POST", "/fzu/api/login", {"username": "admin", "password": "OFFLINE-SECRET-WRONG"})
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn(b"OFFLINE-SECRET-WRONG", body)

    def test_login_origin_is_required_and_host_cannot_be_forwarded_or_forged(self):
        variants = ({"Origin": None}, {"Origin": "null"}, {"Origin": "https://evil.invalid"},
                    {"Origin": ORIGIN + "/"}, {"Host": "evil.invalid", "X-Forwarded-Host": HOST},
                    {"Host": "127.0.0.1:18779", "X-Forwarded-Host": HOST},
                    {"Host": HOST + ":443"})
        for headers in variants:
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.request("POST", "/fzu/api/login", {"username": "admin", "password": PASSWORD}, headers)[0], 403)
        self.assertEqual(len(self.auth._attempts), 0)

    def test_csrf_is_required_for_every_authenticated_write(self):
        self.login()
        routes = (("PUT", "/fzu/api/config"), ("POST", "/fzu/api/logout"),
                  *(("POST", "/fzu/api/actions/" + action)
                    for action in ("preflight", "resume", "pause", "notify-test")))
        for method, path in routes:
            for value in (None, "wrong"):
                with self.subTest(path=path, csrf=value):
                    self.assertEqual(self.request(method, path, {}, {"X-CSRF-Token": value})[0], 403)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.request("GET", "/fzu/api/session")[0], 200)

    def test_valid_csrf_cannot_override_wrong_origin(self):
        self.login()
        self.assertEqual(self.request("PUT", "/fzu/api/config", {}, {"Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/fzu/api/config", headers={"Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(self.backend.calls, [])

    def test_routes_call_only_allowed_backend_methods(self):
        self.login()
        self.assertEqual(self.request("GET", "/fzu/api/config")[0], 200)
        self.assertEqual(self.request("GET", "/fzu/api/status")[0], 200)
        payload = {"config": {"user": {"password": "OFFLINE-NEW-SCHOOL-SECRET"}}}
        status, _, body = self.request("PUT", "/fzu/api/config", payload)
        self.assertEqual(status, 200)
        self.assertNotIn(b"OFFLINE-NEW-SCHOOL-SECRET", body)
        for action in ("preflight", "resume", "pause", "notify-test"):
            self.assertEqual(self.request("POST", "/fzu/api/actions/" + action, {"confirmed": True})[0], 202)
        self.assertIn(("save_config", payload), self.backend.calls)
        self.assertEqual(len(self.backend.calls), 7)
        for action in ("run", "checkin", "submit", "preflight/../run"):
            self.assertEqual(self.request("POST", "/fzu/api/actions/" + action, {})[0], 404)
        self.assertEqual(len(self.backend.calls), 7)

    def test_login_limit_is_global_despite_forged_forwarded_ips(self):
        self.auth.login_limit = 3
        for index in range(3):
            self.assertEqual(self.request("POST", "/fzu/api/login", {"username": "admin", "password": "wrong-offline"},
                                          {"X-Forwarded-For": f"198.51.100.{index}"})[0], 401)
        status, headers, _ = self.request("POST", "/fzu/api/login", {"username": "admin", "password": PASSWORD},
                                          {"X-Forwarded-For": "203.0.113.99"})
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 0)

    def test_body_limit_and_json_content_type_are_enforced_before_login(self):
        status, _, _ = self.request("POST", "/fzu/api/login", raw=b"",
                                    headers={"Content-Length": str(BODY_LIMIT + 1)})
        self.assertEqual(status, 413)
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "application/json; charset=gbk"):
            self.assertEqual(self.request("POST", "/fzu/api/login", {"username": "admin", "password": PASSWORD},
                                          {"Content-Type": content_type})[0], 415)
        self.assertEqual(len(self.auth._attempts), 0)

    def test_total_header_size_is_bounded(self):
        for headers in ({"X-Padding": "x" * 17000},
                        {f"X-Padding-{index}": "x" * 16000 for index in range(8)}):
            status, _, _ = self.request("GET", "/fzu/", headers=headers)
            self.assertEqual(status, 431)

    def test_bad_json_and_encoding_do_not_echo_values_or_call_backend(self):
        self.login()
        for raw in (b'{"password":"OFFLINE-SECRET-BODY"', b"[]", b'{"x":NaN}',
                    b'{"x":1,"x":2}', b'\xff', b'[' * 1200 + b']' * 1200):
            status, _, body = self.request("PUT", "/fzu/api/config", raw=raw)
            self.assertEqual(status, 400)
            self.assertNotIn(b"OFFLINE-SECRET-BODY", body)
        for header in ({"Content-Encoding": "gzip"}, {"Transfer-Encoding": "chunked"}):
            self.assertEqual(self.request("PUT", "/fzu/api/config", {}, header)[0], 400)
        self.assertEqual(self.backend.calls, [])

    def test_duplicate_host_content_length_origin_and_csrf_are_refused(self):
        self.login()
        for key, value in (("Host", HOST), ("Origin", ORIGIN), ("Content-Length", "2"),
                           ("X-CSRF-Token", self.csrf)):
            lines = ["PUT /fzu/api/config HTTP/1.1", "Host: " + HOST,
                     "Origin: " + ORIGIN, "Cookie: " + self.cookie,
                     "X-CSRF-Token: " + self.csrf, "Content-Type: application/json",
                     "Content-Length: 2", key + ": " + value, "", "{}"]
            with socket.create_connection(self.server.server_address, timeout=2) as connection:
                connection.sendall("\r\n".join(lines).encode())
                response = connection.recv(8192)
            self.assertNotIn(b" 200 ", response.split(b"\r\n", 1)[0])
        self.assertEqual(self.backend.calls, [])

    def test_wrong_methods_and_url_passwords_never_reach_backend_or_logs(self):
        self.login()
        with patch("sys.stderr", new=io.StringIO()) as captured:
            for method in ("DELETE", "PATCH", "OPTIONS", "TRACE", "SECRET-METHOD"):
                status, headers, body = self.request(method, "/fzu/api/config?password=OFFLINE-URL-SECRET")
                self.assertIn(status, (405, 501))
                self.assertNotIn(b"OFFLINE-URL-SECRET", body)
                self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertNotIn("OFFLINE-URL-SECRET", captured.getvalue())
        self.assertEqual(self.backend.calls, [])

    def test_unexpected_backend_exception_is_not_exposed(self):
        self.login()
        self.backend.failure = RuntimeError("OFFLINE-PRIVATE-EXCEPTION https://secret.invalid/token")
        with patch("sys.stderr", new=io.StringIO()) as captured:
            status, _, body = self.request("GET", "/fzu/api/config")
            self.assertEqual(status, 500)
            self.assertNotIn(b"OFFLINE-PRIVATE-EXCEPTION", body)
            self.assertNotIn("secret.invalid", captured.getvalue())

    def test_backend_explicit_safe_errors_preserve_status_and_code(self):
        from src.admin_backend import BackendError
        self.login()
        self.backend.failure = BackendError("revision_conflict", "配置已改变，请刷新", 409)
        status, _, body = self.request("GET", "/fzu/api/config")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["code"], "revision_conflict")

    def test_read_requests_cannot_carry_bodies(self):
        self.login()
        self.assertEqual(self.request("GET", "/fzu/api/status", raw=b"SECRET")[0], 400)
        self.assertEqual(self.backend.calls, [])

    def test_total_body_deadline_stops_slow_drip(self):
        self.server.request_seconds = 0.18
        connection = socket.create_connection(self.server.server_address, timeout=2)
        self.addCleanup(connection.close)
        lines = ["POST /fzu/api/login HTTP/1.1", "Host: " + HOST, "Origin: " + ORIGIN,
                 "Content-Type: application/json", "Content-Length: 100", "", "{"]
        start = time.monotonic()
        connection.sendall("\r\n".join(lines).encode())
        for _ in range(4):
            time.sleep(0.05)
            try:
                connection.sendall(b" ")
            except OSError:
                break
        response = connection.recv(8192)
        self.assertIn(b" 408 ", response.split(b"\r\n", 1)[0])
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(len(self.auth._attempts), 0)


class AdminSettingsTests(unittest.TestCase):
    def test_origin_requires_exact_https_origin_without_credentials_or_path(self):
        self.assertEqual(validate_origin(ORIGIN), (ORIGIN, HOST))
        self.assertEqual(validate_origin("https://example.invalid:8443")[1], "example.invalid:8443")
        for value in ("", "http://example.invalid", "https://example.invalid/", "https://user:pass@example.invalid",
                      "https://@example.invalid", "https://example%2einvalid",
                      "https://example.invalid/path", "https://example.invalid?x=y", "https://example.invalid#x",
                      "https://example.invalid:bad", "https://example.invalid\n", "//example.invalid"):
            with self.subTest(origin=value):
                with self.assertRaises(ValueError):
                    validate_origin(value)

    def test_base_path_is_single_literal_segment(self):
        self.assertEqual(validate_base_path("/FZU"), "/FZU")
        for value in ("/", "fzu", "/fzu/", "/fzu/other", "/fzu?x", "/../", "//fzu"):
            with self.assertRaises(ValueError):
                validate_base_path(value)

    def test_service_rejects_public_bind(self):
        with self.assertRaises(ValueError):
            AdminHTTPServer(("0.0.0.0", 18779), origin=ORIGIN, backend=FakeBackend())


if __name__ == "__main__":
    unittest.main()
