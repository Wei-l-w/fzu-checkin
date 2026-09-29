import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from src.admin_backend import Backend, BackendError
from src.config import atomic_write_json, load_config, save_token
from src.history import read_history, record_event


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        (self.base / "state").mkdir(mode=0o700)
        (self.base / "backups").mkdir(mode=0o700)
        self.path = self.base / "config.yaml"
        self.raw = {"enabled": False, "user": {"username": "test-user", "password": "TEST_PASSWORD_PRIVATE", "token": ""},
                    "checkin": {"coordinate_system": "GCJ-02", "confirmed": False, "longitude": "", "latitude": "", "actual_location": ""},
                    "notify": {"type": "bark", "bark_url": "https://push.example.invalid/TEST_PUSH_PRIVATE"},
                    "skip_dates": [], "vacation": {"skip_ranges": []}}
        atomic_write_json(self.path, self.raw)
        self.env = patch.dict(os.environ, {"FZU_CHECKIN_CONFIG": str(self.path), "FZU_CHECKIN_STATE_DIR": str(self.base / "state")})
        self.env.start()
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Tests must never contact school/notification services"))
        self.network.start()
        self.calls = []

        def controller(action):
            self.calls.append(action)
            if action == "pause":
                atomic_write_json(self.base / "state" / "paused", {"paused": True})
            return {"ok": True, "timer": {"active": "inactive", "enabled": "disabled", "next_run": ""}}

        self.backend = Backend(controller=controller)

    def tearDown(self):
        self.network.stop()
        self.env.stop()
        self.temp.cleanup()

    def request(self, config=None, clears=None):
        return {"revision": self.backend.get_config()["revision"], "config": config or {}, "clear_secrets": clears or []}

    def test_config_never_returns_password_token_or_push_secret(self):
        save_token(load_config(), "TEST_SESSION_TOKEN_PRIVATE_123456")
        result = self.backend.get_config()
        body = json.dumps(result)
        for secret in ("TEST_PASSWORD_PRIVATE", "TEST_PUSH_PRIVATE", "TEST_SESSION_TOKEN_PRIVATE_123456"):
            self.assertNotIn(secret, body)
        self.assertTrue(result["configured"]["password"])
        self.assertTrue(result["configured"]["token"])
        self.assertNotEqual(result["revision"], Backend(controller=self.backend.controller).get_config()["revision"])

    def test_draft_save_preserves_blank_secrets_and_forces_pause(self):
        result = self.backend.save_config(self.request({"user": {"username": "test-new", "password": "", "token": ""}}))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["user"]["password"], "TEST_PASSWORD_PRIVATE")
        self.assertEqual(self.calls, ["pause"])
        self.assertTrue((self.base / "state" / "paused").exists())
        self.assertTrue(result["paused"])
        self.assertIsNotNone(self.backend.get_status()["config_changed_at"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.base / "backups" / "config-before-ui-save.json").read_text()), self.raw)

    def test_pasted_login_url_is_reduced_to_its_token_before_saving(self):
        url = "https://yzsxg.fzu.edu.cn/livecloud/project/fzu/attn/index.action?token=TEST-URL-TOKEN-0123456789&contextPath="
        self.backend.save_config(self.request({"user": {"username": "test-user", "password": "", "token": url}}))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["user"]["token"], "TEST-URL-TOKEN-0123456789")
        self.assertEqual(saved["user"]["password"], "TEST_PASSWORD_PRIVATE")

    def test_schedule_times_are_saved_and_invalid_times_are_rejected(self):
        self.backend.save_config(self.request({"schedule": {"times": ["21:10", "22:00"]}}))
        self.assertEqual(json.loads(self.path.read_text())["schedule"], {"times": ["21:10", "22:00"]})
        self.assertEqual(self.backend.get_status()["schedule_times"], ["21:10", "22:00"])
        for bad in (["20:00"], ["22:00", "21:10"], "21:10", ["21:10", "21:10"], ["21:10", "x"]):
            with self.subTest(bad=bad), self.assertRaises(BackendError):
                self.backend.save_config(self.request({"schedule": {"times": bad}}))
        self.assertEqual(json.loads(self.path.read_text())["schedule"], {"times": ["21:10", "22:00"]})
        with self.assertRaises(BackendError):
            self.backend.save_config(self.request({"schedule": {"times": ["21:10"], "other": 1}}))

    def test_clear_is_explicit_and_invalidates_cached_token(self):
        save_token(load_config(), "TEST_SESSION_TOKEN_PRIVATE_123456")
        self.backend.save_config(self.request(clears=["user.password", "user.token", "notify.bark_url"]))
        cfg = load_config()
        self.assertEqual(cfg["user"]["password"], "")
        self.assertEqual(cfg["user"]["token"], "")
        self.assertFalse(self.backend.get_config()["configured"]["bark_url"])

    def test_stale_revision_does_not_write(self):
        request = self.request()
        changed = copy.deepcopy(self.raw)
        changed["enabled"] = True
        atomic_write_json(self.path, changed)
        with self.assertRaises(BackendError) as error:
            self.backend.save_config(request)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.calls, [])

    def test_unknown_fields_paths_and_secret_clear_rejected(self):
        for request in (self.request({"_config_path": "/etc/passwd"}), self.request({"user": {"is_admin": True}}),
                        self.request(clears=["admin.password"]), self.request({"user": {"password": "new"}}, ["user.password"])):
            with self.subTest(request=request):
                with self.assertRaises(BackendError):
                    self.backend.save_config(request)
        self.assertEqual(self.calls, [])

    def test_bad_dates_and_nonfinite_coordinates_rejected(self):
        for config in ({"skip_dates": ["2026-99-88"]}, {"checkin": {"longitude": "nan"}},
                       {"checkin": {"coordinate_system": "WGS84"}},
                       {"vacation": {"skip_ranges": [{"start": "2026-10-10", "end": "2026-10-01"}]}}):
            with self.subTest(config=config):
                with self.assertRaises(BackendError):
                    self.backend.save_config(self.request(config))

    def test_pause_failure_keeps_original_config(self):
        self.backend.controller = lambda action: {"ok": False}
        with self.assertRaises(BackendError):
            self.backend.save_config(self.request({"enabled": True}))
        self.assertEqual(json.loads(self.path.read_text()), self.raw)

    def test_never_provides_run_action(self):
        for action, data in (("run", {"confirmed": True}), ("resume", {}), ("resume", {"confirmed": True, "force": True})):
            with self.assertRaises(BackendError):
                self.backend.start_action(action, data)
        self.assertEqual(self.calls, [])

    def test_job_pause_is_not_a_school_success(self):
        self.backend.start_action("pause", {"confirmed": True})
        for _ in range(100):
            status = self.backend.get_status()
            if status["job"]["state"] == "done":
                break
            time.sleep(0.001)
        self.assertEqual(status["job"]["result"]["status"], "paused")
        self.assertEqual(status["history"][0]["status"], "paused")
        self.assertTrue(status["paused"])

    def test_status_history_sanitizes_all_record_payloads(self):
        cfg = load_config()
        event = {"status": "pending", "mode": "run", "checked_at": "2026-09-18T21:35:00+08:00",
                 "message": "TEST_PASSWORD_PRIVATE", "token": "TEST_SESSION_TOKEN_PRIVATE",
                 "evidence": {"password": "TEST_PASSWORD_PRIVATE", "success_verified": False}}
        atomic_write_json(self.base / "state" / "last_run.json", event)
        for _ in range(130):
            event["checked_at"] = (dt.datetime(2026, 9, 18, 21, 35, tzinfo=dt.timezone(dt.timedelta(hours=8))) + dt.timedelta(seconds=_)).isoformat()
            record_event(cfg, event)
        history = read_history(cfg)
        self.assertEqual(len(history), 120)
        self.assertNotIn("TEST_PASSWORD_PRIVATE", json.dumps(self.backend.get_status()))
        self.assertNotIn("TEST_SESSION_TOKEN_PRIVATE", json.dumps(self.backend.get_status()))


if __name__ == "__main__":
    unittest.main()


class RegistryTests(unittest.TestCase):
    def test_profiles_are_isolated_and_archived_not_deleted(self):
        from src.admin_backend import BackendRegistry
        with tempfile.TemporaryDirectory() as directory, patch("requests.sessions.Session.request",
                                                                side_effect=AssertionError("no network")):
            root = Path(directory) / "profiles"
            calls = []

            def factory(profile):
                def controller(action):
                    calls.append((profile, action))
                    return {"ok": True, "timer": {"active": "inactive", "enabled": "disabled", "next_run": ""}}
                return controller

            registry = BackendRegistry(root, controller_factory=factory)
            for bad in ("nobody", "Bad Name", "../etc", ""):
                with self.subTest(bad=bad), self.assertRaises(BackendError):
                    registry.for_user(bad)
            registry.create_profile("alice")
            registry.create_profile("bob")
            with self.assertRaises(BackendError):
                registry.create_profile("alice")
            self.assertEqual((root / "alice").stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "alice" / "config.yaml").stat().st_mode & 0o777, 0o600)
            alice, bob = registry.for_user("alice"), registry.for_user("bob")
            self.assertIs(alice, registry.for_user("alice"))
            alice.save_config({"revision": alice.get_config()["revision"], "clear_secrets": [],
                               "config": {"checkin": {"actual_location": "ALICE-ONLY-ADDRESS"}}})
            self.assertEqual(alice.get_config()["config"]["checkin"]["actual_location"], "ALICE-ONLY-ADDRESS")
            self.assertEqual(bob.get_config()["config"]["checkin"]["actual_location"], "")
            self.assertEqual(calls, [("alice", "pause")])
            self.assertEqual(alice.get_status()["schedule_times"], ["21:35", "21:40", "21:50"])
            self.assertNotIn("username", alice.get_config()["config"]["user"])
            registry.archive_profile("bob")
            with self.assertRaises(BackendError):
                registry.for_user("bob")
            archived = [item.name for item in root.iterdir() if item.name.startswith(".removed-bob-")]
            self.assertEqual(len(archived), 1)
            self.assertTrue((root / archived[0] / "config.yaml").is_file())
