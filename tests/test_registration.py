"""Offline signup, approval, isolation and crash-recovery regressions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from unittest import mock

import test_admin_http as fixture
from admin_server import AdminHTTPServer
from src.admin_auth import AuthConfigurationError, AuthManager
from src.admin_backend import BackendRegistry
from src.registration import RegistrationError, RegistrationManager

MEMBER_PASSWORD = "OFFLINE-member-signup-password"


class RegistrationTests(fixture.AuthFixture):
    def setUp(self):
        super().setUp()
        self.network = mock.patch("requests.sessions.Session.request", side_effect=AssertionError("no school/notification network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.auth = AuthManager(self.auth_path, login_limit=100)
        self.controls = []
        def controller_factory(username):
            def control(action):
                self.controls.append((username, action))
                return {"ok": True, "timer": {"active": "inactive", "enabled": "disabled", "next_run": ""}}
            return control
        self.registry = BackendRegistry(self.directory / "profiles", controller_factory=controller_factory)
        self.registry.create_profile("admin")
        self.now = [1801281600.0]
        self.manager = RegistrationManager(self.auth, self.registry, clock=lambda: self.now[0])

    def apply(self, username="mate", password=MEMBER_PASSWORD):
        self.manager.set_enabled(True)
        self.manager.apply(username, password, "OFFLINE-申请说明")
        return next(item["id"] for item in self.manager.overview()["requests"] if item["username"] == username and item["status"] == "pending")

    def test_default_closed_and_pending_does_not_create_a_login_or_profile(self):
        before = self.auth_path.read_bytes()
        self.assertEqual(self.manager.public_settings(), {"enabled": False})
        with self.assertRaises(RegistrationError) as error:
            self.manager.apply("mate", MEMBER_PASSWORD, "")
        self.assertEqual(error.exception.code, "REGISTRATION_CLOSED")
        self.apply()
        self.assertEqual(self.auth_path.read_bytes(), before)
        self.assertIsNone(self.auth.login("mate", MEMBER_PASSWORD))
        self.assertFalse(self.registry.profile_exists("mate"))
        self.assertEqual(self.controls, [])
        self.assertEqual(self.manager.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(MEMBER_PASSWORD.encode(), self.manager.path.read_bytes())
        self.assertNotIn('"auth"', json.dumps(self.manager.overview()))
        self.assertNotIn('"digest"', json.dumps(self.manager.overview()))

    def test_status_requires_correct_password_and_works_when_registration_closes(self):
        self.apply()
        self.manager.set_enabled(False)
        self.assertEqual(self.manager.applicant_status("mate", MEMBER_PASSWORD)["status"], "pending")
        for username, password in (("mate", "wrong-password"), ("unknown", MEMBER_PASSWORD)):
            with self.assertRaises(RegistrationError) as error:
                self.manager.applicant_status(username, password)
            self.assertEqual((error.exception.status, error.exception.message), (401, "申请用户名或密码不正确"))

    def test_approval_creates_only_a_blank_paused_member_and_is_idempotent(self):
        original = (self.registry.root / "admin" / "config.yaml").read_bytes()
        request_id = self.apply()
        result = self.manager.review(request_id, "approve", "admin")
        self.assertEqual(result["pending_count"], 0)
        token, session = self.auth.login("mate", MEMBER_PASSWORD)
        self.assertEqual(session.role, "member")
        backend = self.registry.for_user("mate")
        cfg = json.loads(backend.path.read_bytes())
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["user"]["token"], "")
        self.assertTrue((backend.state_path / "paused").is_file())
        self.assertEqual(self.controls, [])
        # A double click must not clear a subsequently edited config.
        cfg["checkin"]["actual_location"] = "OFFLINE-PRIVATE-MEMBER-LOCATION"
        from src.config import atomic_write_json
        atomic_write_json(backend.path, cfg)
        self.manager.review(request_id, "approve", "admin")
        self.assertEqual(json.loads(backend.path.read_bytes()), cfg)
        self.assertIsNotNone(self.auth.authenticate(token))
        self.assertEqual((self.registry.root / "admin" / "config.yaml").read_bytes(), original)
        self.auth.remove_user("mate")
        self.manager.review(request_id, "approve", "admin")
        self.assertIsNone(self.auth.login("mate", MEMBER_PASSWORD), "old approvals cannot resurrect a removed account")

    def test_rejection_keeps_login_closed_and_reapplication_has_a_new_review_id(self):
        first = self.apply()
        self.manager.review(first, "reject", "admin", "请说明与管理员的关系")
        self.assertEqual(self.manager.applicant_status("mate", MEMBER_PASSWORD)["reason"], "请说明与管理员的关系")
        self.assertIsNone(self.auth.login("mate", MEMBER_PASSWORD))
        second = self.apply(password=MEMBER_PASSWORD + "-new")
        self.assertNotEqual(first, second)
        self.assertEqual(self.manager.applicant_status("mate", MEMBER_PASSWORD + "-new")["status"], "pending")
        with self.assertRaises(RegistrationError):
            self.manager.applicant_status("mate", MEMBER_PASSWORD)
        with self.assertRaises(RegistrationError):
            self.manager.review(first, "approve", "admin")

    def test_duplicate_and_existing_owner_names_cannot_be_overwritten(self):
        self.apply()
        for username in ("mate", "admin"):
            with self.assertRaises(RegistrationError):
                self.manager.apply(username, MEMBER_PASSWORD, "")
        self.assertEqual(len(self.manager.overview()["requests"]), 1)
        self.assertEqual(self.auth.list_users()[0]["role"], "owner")

    def test_two_simultaneous_approvals_create_one_account(self):
        request_id = self.apply()
        other = RegistrationManager(AuthManager(self.auth_path), self.registry)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda manager: manager.review(request_id, "approve", "admin"), [self.manager, other]))
        self.assertEqual([item["pending_count"] for item in results], [0, 0])
        self.assertEqual([user["id"] for user in self.auth.list_users()], ["admin", "mate"])

    def test_failed_profile_provision_never_creates_login_and_can_be_retried(self):
        request_id = self.apply()
        with mock.patch.object(self.registry, "create_registration_profile", side_effect=OSError("OFFLINE-private-path")):
            with self.assertRaises(OSError):
                self.manager.review(request_id, "approve", "admin")
        self.assertIsNone(self.auth.login("mate", MEMBER_PASSWORD))
        self.assertEqual(self.manager.overview()["requests"][0]["status"], "pending")
        self.manager.review(request_id, "approve", "admin")
        self.assertIsNotNone(self.auth.login("mate", MEMBER_PASSWORD))

    def test_crash_after_account_commit_recovers_without_resetting_profile(self):
        request_id = self.apply()
        original_write = self.manager._write
        def fail_final(data):
            if data["requests"][request_id]["status"] == "approved":
                raise OSError("OFFLINE-receipt-failure")
            return original_write(data)
        with mock.patch.object(self.manager, "_write", side_effect=fail_final):
            with self.assertRaises(OSError):
                self.manager.review(request_id, "approve", "admin")
        before = self.registry.for_user("mate").path.read_bytes()
        self.assertIsNotNone(self.auth.login("mate", MEMBER_PASSWORD))
        self.assertEqual(self.manager.overview()["requests"][0]["status"], "approving")
        self.manager.review(request_id, "approve", "admin")
        self.assertEqual(self.manager.overview()["requests"][0]["status"], "approved")
        self.assertEqual(self.registry.for_user("mate").path.read_bytes(), before)

    def test_existing_unrelated_profile_or_user_is_not_overwritten(self):
        request_id = self.apply()
        self.registry.create_profile("mate")
        before = self.registry.for_user("mate").path.read_bytes()
        with self.assertRaises(Exception):
            self.manager.review(request_id, "approve", "admin")
        self.assertEqual(self.registry.for_user("mate").path.read_bytes(), before)
        self.assertIsNone(self.auth.login("mate", MEMBER_PASSWORD))
        self.auth.add_user("mate", MEMBER_PASSWORD + "-different")
        with self.assertRaises(RegistrationError):
            self.manager.review(request_id, "approve", "admin")
        self.assertIsNone(self.auth.login("mate", MEMBER_PASSWORD))

    def test_rate_limits_are_persistent_and_separate_from_logins(self):
        self.apply()
        with mock.patch("src.registration.RATE_LIMITS", {"apply": 2, "status": 2}):
            for _ in range(1):
                with self.assertRaises(RegistrationError):
                    self.manager.apply("mate", MEMBER_PASSWORD, "")
            restarted = RegistrationManager(self.auth, self.registry, clock=lambda: self.now[0])
            with self.assertRaises(RegistrationError) as error:
                restarted.apply("another", MEMBER_PASSWORD, "")
            self.assertEqual(error.exception.status, 429)
            for _ in range(2):
                with self.assertRaises(RegistrationError):
                    restarted.applicant_status("mate", "wrong")
            with self.assertRaises(RegistrationError) as error:
                restarted.applicant_status("mate", MEMBER_PASSWORD)
            self.assertEqual(error.exception.status, 429)
            self.assertIsNotNone(self.auth.login("admin", fixture.PASSWORD))
            self.now[0] += 601
            restarted.apply("another", MEMBER_PASSWORD, "")

    def test_invalid_values_private_permissions_and_capacity_fail_closed(self):
        self.manager.set_enabled(True)
        for args in (("../admin", MEMBER_PASSWORD, ""), ("admin2", "short", ""), ("admin2", MEMBER_PASSWORD, "x" * 201)):
            with self.assertRaises(RegistrationError):
                self.manager.apply(*args)
        self.apply()
        with mock.patch("src.registration.MAX_PENDING", 1):
            with self.assertRaises(RegistrationError) as error:
                self.manager.apply("another", MEMBER_PASSWORD, "")
            self.assertEqual(error.exception.code, "REGISTRATION_CAPACITY")
        self.manager.path.chmod(0o644)
        with self.assertRaises(AuthConfigurationError):
            self.manager.public_settings()


class RegistrationHTTPTests(fixture.AuthFixture):
    request = fixture.AdminHTTPTests.request
    login = fixture.AdminHTTPTests.login
    login_as = fixture.AdminHTTPTests.login_as
    close_server = fixture.AdminHTTPTests.close_server

    def setUp(self):
        super().setUp()
        self.auth = AuthManager(self.auth_path, login_limit=100)
        self.registry = BackendRegistry(self.directory / "profiles", controller_factory=lambda _: lambda action: {"ok": True})
        self.registry.create_profile("admin")
        self.server = AdminHTTPServer(("127.0.0.1", 0), backend=self.registry, auth_manager=self.auth,
                                      origin=fixture.ORIGIN, base_path="/fzu")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.cookie = self.csrf = None
        self.network = mock.patch("requests.sessions.Session.request", side_effect=AssertionError("school network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def apply(self):
        self.server.registrations.set_enabled(True)
        return self.request("POST", "/fzu/api/registration/apply",
                            {"username": "mate", "password": MEMBER_PASSWORD, "note": "offline-note"})

    def test_public_pending_then_owner_approval_and_member_isolation(self):
        self.assertEqual(json.loads(self.request("GET", "/fzu/api/registration")[2]), {"enabled": False})
        status, headers, body = self.apply()
        self.assertEqual(status, 202, body)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.request("POST", "/fzu/api/login", {"username": "mate", "password": MEMBER_PASSWORD})[0], 401)
        for endpoint in ("config", "status", "registrations", "users"):
            self.assertEqual(self.request("GET", "/fzu/api/" + endpoint)[0], 401)
        self.login()
        result = json.loads(self.request("GET", "/fzu/api/registrations")[2])
        request_id = result["requests"][0]["id"]
        self.assertNotIn("auth", repr(result))
        self.assertEqual(self.request("POST", f"/fzu/api/registrations/{request_id}/approve", {"confirmed": True})[0], 200)
        self.login_as("mate", MEMBER_PASSWORD)
        config = json.loads(self.request("GET", "/fzu/api/config")[2])
        self.assertFalse(config["config"]["enabled"])
        self.assertFalse(config["configured"]["token"])
        self.assertEqual(self.request("GET", "/fzu/api/registrations")[0], 403)
        self.assertEqual(self.request("PUT", "/fzu/api/registration/settings", {"enabled": True})[0], 403)
        self.assertEqual(self.request("POST", f"/fzu/api/registrations/{request_id}/reject", {"confirmed": True, "reason": ""})[0], 403)

    def test_origins_csrf_and_strict_payloads_are_enforced(self):
        self.server.registrations.set_enabled(True)
        payload = {"username": "mate", "password": MEMBER_PASSWORD, "note": ""}
        for origin in (None, "https://evil.invalid"):
            self.assertEqual(self.request("POST", "/fzu/api/registration/apply", payload, {"Origin": origin})[0], 403)
        for extra in ({"role": "owner"}, {"status": "approved"}, {"profile": "admin"}):
            self.assertEqual(self.request("POST", "/fzu/api/registration/apply", {**payload, **extra})[0], 400)
        self.apply()
        request_id = self.server.registrations.overview()["requests"][0]["id"]
        self.login()
        for route, method, body in (("registration/settings", "PUT", {"enabled": True}),
                                    (f"registrations/{request_id}/approve", "POST", {"confirmed": True})):
            self.assertEqual(self.request(method, "/fzu/api/" + route, body, {"X-CSRF-Token": None})[0], 403)
        self.assertEqual(self.request("POST", f"/fzu/api/registrations/{request_id}/approve", {"confirmed": True, "role": "owner"})[0], 400)

    def test_closed_registration_still_allows_authenticated_status_and_rejection_reason(self):
        self.apply()
        self.login()
        request_id = self.server.registrations.overview()["requests"][0]["id"]
        self.assertEqual(self.request("POST", f"/fzu/api/registrations/{request_id}/reject", {"confirmed": True, "reason": "请补充说明"})[0], 200)
        self.assertEqual(self.request("PUT", "/fzu/api/registration/settings", {"enabled": False})[0], 200)
        self.cookie = self.csrf = None
        query = {"username": "mate", "password": MEMBER_PASSWORD}
        code, headers, body = self.request("POST", "/fzu/api/registration/status", query)
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["reason"], "请补充说明")
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.request("POST", "/fzu/api/registration/status", {**query, "password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/fzu/api/registration/apply", {**query, "note": ""})[0], 403)

    def test_global_application_limit_cannot_be_bypassed_with_forwarded_ips(self):
        self.apply()
        with mock.patch("src.registration.RATE_LIMITS", {"apply": 1, "status": 30}):
            code, headers, body = self.request("POST", "/fzu/api/registration/apply",
                {"username": "another", "password": MEMBER_PASSWORD, "note": ""}, {"X-Forwarded-For": "203.0.113.8"})
            self.assertEqual(code, 429, body)
            self.assertGreater(int(headers["Retry-After"]), 0)
        self.login()  # Signup throttling does not exhaust the administrator's login budget.
