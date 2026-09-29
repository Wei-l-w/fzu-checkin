"""Offline orchestration regressions: school I/O and notifications are mocked."""
import contextlib
import copy
import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import main


class MainSafetyTests(unittest.TestCase):
    def setUp(self):
        original_umask = os.umask(0o077)
        self.addCleanup(os.umask, original_umask)
        temporary = tempfile.TemporaryDirectory(prefix="fzu-main-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "state"
        self.directory.mkdir(mode=0o700)
        self.now = datetime.datetime(2026, 9, 18, 21, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.cfg = {
            "enabled": True,
            "paused": False,
            "skip_dates": [],
            "user": {"token": "offline-test-token"},
            "checkin": {"longitude": 1, "latitude": 1, "confirmed": True},
            "notify": {"type": "none"},
        }
        self.current = {
            "status": "ready",
            "init": {"offline": True},
            "evidence": {"success_verified": False},
            "window": {
                "date": "2026-09-18", "start": "21:30", "end": "23:59",
                "late": "23:00", "in_window": True,
            },
        }
        self.client = mock.Mock(name="offline_client")
        self.environment = self.start(mock.patch.dict(os.environ))
        os.environ["FZU_CHECKIN_STATE_DIR"] = str(self.directory)
        os.environ.pop("FZU_CHECKIN_FORCE", None)
        self.load = self.start(mock.patch.object(main, "load_config", side_effect=lambda: copy.deepcopy(self.cfg)))
        self.start(mock.patch.object(main, "state_dir", return_value=self.directory))
        self.validation = self.start(mock.patch.object(main, "validation_errors", return_value=[]))
        self.clock = self.start(mock.patch.object(main, "beijing_now", return_value=self.now))
        self.original_authenticated_query = main.authenticated_query
        self.query = self.start(mock.patch.object(main, "authenticated_query", side_effect=lambda *_: (self.client, copy.deepcopy(self.current))))
        self.location = self.start(mock.patch.object(main, "validate_location", return_value={"status": "valid"}))
        self.submit = self.start(mock.patch.object(main, "do_checkin", return_value={"status": "pending"}))
        self.notification = self.start(mock.patch.object(main, "notify", return_value=True))
        self.start(mock.patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden in offline tests")))

    def start(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def invoke(self, mode):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main.execute(mode)
        self.output = output.getvalue()
        messages = [json.loads(line) for line in self.output.splitlines() if line.startswith("{")]
        self.assertTrue(messages, self.output)
        return code, messages[-1]

    def read(self, name):
        return main.read_state(self.directory / name)

    def test_paused_run_does_not_authenticate_or_submit(self):
        self.cfg["paused"] = True
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "paused"))
        self.query.assert_not_called()
        self.submit.assert_not_called()

    def test_disabled_run_does_not_require_credentials(self):
        self.cfg["enabled"] = False
        self.cfg["user"] = {}
        self.validation.return_value = ["user.credentials"]
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "paused"))
        self.query.assert_not_called()

    def test_pause_marker_blocks_every_run_entry(self):
        main.atomic_write_json(self.directory / "paused", {"paused_at": self.now.isoformat()})
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "paused"))
        self.query.assert_not_called()
        self.submit.assert_not_called()

    def test_pause_works_without_loading_valid_config(self):
        self.load.side_effect = main.ConfigError("CONFIG_INVALID")
        code, result = self.invoke("pause")
        self.assertEqual((code, result["status"]), (0, "paused"))
        self.assertTrue((self.directory / "paused").is_file())
        self.load.assert_not_called()

    def test_personal_skip_date_precedes_authentication(self):
        self.cfg["skip_dates"] = ["2026-09-18"]
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "skipped"))
        self.query.assert_not_called()

    def test_personal_skip_range_precedes_authentication(self):
        self.cfg["vacation"] = {"skip_ranges": [{"start": "2026-09-17", "end": "2026-09-20"}]}
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "skipped"))
        self.query.assert_not_called()

    def test_missing_credentials_fail_closed(self):
        self.validation.return_value = ["user.credentials"]
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (20, "config_error"))
        self.query.assert_not_called()
        self.submit.assert_not_called()

    def test_invalid_config_has_static_exit_and_no_exception_text(self):
        self.load.side_effect = main.ConfigError("CONFIG_INVALID")
        code, result = self.invoke("preflight")
        self.assertEqual((code, result["status"], result["code"]), (20, "config_error", "config_invalid"))
        self.submit.assert_not_called()

    def test_concurrent_lock_returns_busy_without_school_io(self):
        with main.run_lock(self.cfg):
            code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (24, "busy"))
        self.query.assert_not_called()
        self.submit.assert_not_called()

    def test_preflight_validates_location_but_never_submits(self):
        code, result = self.invoke("preflight")
        self.assertEqual((code, result["status"]), (0, "ready"))
        self.location.assert_called_once()
        self.submit.assert_not_called()
        self.notification.assert_not_called()
        self.assertFalse((self.directory / "submission.json").exists())

    def test_preflight_does_not_remove_pause(self):
        main.atomic_write_json(self.directory / "paused", {"paused_at": "before"})
        code, result = self.invoke("preflight")
        self.assertEqual((code, result["status"]), (0, "ready"))
        self.assertTrue((self.directory / "paused").exists())
        self.submit.assert_not_called()

    def test_default_cli_is_read_only(self):
        with mock.patch.object(main, "execute", return_value=0) as execute:
            self.assertEqual(main.main([]), 0)
        execute.assert_called_once_with("preflight")

    def test_force_can_only_reduce_run_to_read_only(self):
        with mock.patch.dict(os.environ, {"FZU_CHECKIN_FORCE": "1"}):
            with mock.patch.object(main, "execute", return_value=0) as execute:
                self.assertEqual(main.main(["run"]), 0)
        execute.assert_called_once_with("preflight")

    def test_already_signed_skips_even_with_pending_intent(self):
        self.current["status"] = "already"
        main.atomic_write_json(self.directory / "submission.json", {"date": "2026-09-18", "status": "pending"})
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "already"))
        self.submit.assert_not_called()
        self.notification.assert_not_called()

    def test_school_no_task_is_not_a_failure(self):
        self.current["status"] = "no_task"
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "no_task"))
        self.submit.assert_not_called()

    def test_outside_window_does_not_create_submission_intent(self):
        self.current["window"]["in_window"] = False
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "outside_window"))
        self.submit.assert_not_called()
        self.assertFalse((self.directory / "submission.json").exists())

    def test_submission_intent_is_durable_before_submission(self):
        def guarded_submission(*_args, before_submit):
            self.assertIs(before_submit(), True)
            intent = self.read("submission.json")
            self.assertEqual((intent["date"], intent["status"]), ("2026-09-18", "pending"))
            self.assertEqual((self.directory / "submission.json").stat().st_mode & 0o777, 0o600)
            return {"status": "confirmed", "evidence": {"success_verified": True}}

        self.submit.side_effect = guarded_submission
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "confirmed"))
        self.assertEqual(self.read("submission.json")["status"], "confirmed")

    def test_same_day_uncertain_intent_prevents_repeat_submission(self):
        main.atomic_write_json(self.directory / "submission.json", {"date": "2026-09-18", "status": "pending"})
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"], result["code"]), (23, "pending", "previous_submission_unconfirmed"))
        self.query.assert_called_once()
        self.submit.assert_not_called()

    def test_crash_leaves_durable_intent_and_next_run_does_not_submit(self):
        def interrupted_submission(*_args, before_submit):
            self.assertIs(before_submit(), True)
            raise KeyboardInterrupt("simulated process interruption")

        self.submit.side_effect = interrupted_submission
        with self.assertRaises(KeyboardInterrupt):
            self.invoke("run")
        self.assertEqual(self.read("submission.json")["status"], "pending")
        self.submit.reset_mock()
        self.submit.side_effect = None
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (23, "pending"))
        self.submit.assert_not_called()

    def test_changed_config_cancels_final_submission_guard(self):
        def guarded_submission(*_args, before_submit):
            self.cfg["checkin"]["longitude"] = 2
            self.assertIs(before_submit(), False)
            return {"status": "paused"}

        self.submit.side_effect = guarded_submission
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "paused"))
        self.assertFalse((self.directory / "submission.json").exists())

    def test_pause_during_work_cancels_final_submission_guard(self):
        def guarded_submission(*_args, before_submit):
            main.atomic_write_json(self.directory / "paused", {"paused_at": self.now.isoformat()})
            self.assertIs(before_submit(), False)
            return {"status": "paused"}

        self.submit.side_effect = guarded_submission
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "paused"))
        self.assertFalse((self.directory / "submission.json").exists())

    def test_midnight_cancels_final_submission_guard(self):
        def guarded_submission(*_args, before_submit):
            self.clock.return_value = self.now + datetime.timedelta(days=1)
            self.assertIs(before_submit(), False)
            return {"status": "paused"}

        self.submit.side_effect = guarded_submission
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (0, "paused"))
        self.assertFalse((self.directory / "submission.json").exists())

    def test_corrupt_intent_fails_closed_instead_of_being_discarded(self):
        main.atomic_write_json(self.directory / "submission.json", ["not", "a", "state", "mapping"])
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (20, "config_error"))
        self.submit.assert_not_called()

    def test_auth_error_is_not_misreported_as_no_task(self):
        self.query.side_effect = main.AuthError("AUTH_REJECTED")
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (21, "auth_error"))
        self.submit.assert_not_called()

    def test_network_error_is_not_misreported_as_no_task(self):
        self.query.side_effect = main.SafeCheckinError("network_unavailable", "network unavailable")
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"]), (22, "failed"))
        self.submit.assert_not_called()

    def test_unknown_exception_does_not_leak_secret_url(self):
        self.query.side_effect = RuntimeError("https://example.invalid/?token=DO_NOT_LEAK_TEST&password=DO_NOT_LEAK_TEST")
        code, result = self.invoke("run")
        self.assertEqual((code, result["status"], result["code"]), (22, "failed", "internal_error"))
        self.assertNotIn("DO_NOT_LEAK_TEST", self.output)
        self.assertNotIn("example.invalid", self.output)
        self.assertNotIn("DO_NOT_LEAK_TEST", json.dumps(self.read("last_run.json")))

    def test_resume_requires_enabled_confirmation(self):
        self.cfg["enabled"] = False
        main.atomic_write_json(self.directory / "paused", {"paused_at": "before"})
        code, result = self.invoke("resume")
        self.assertEqual((code, result["status"]), (20, "config_error"))
        self.assertTrue((self.directory / "paused").exists())
        self.query.assert_not_called()

    def test_resume_requires_verified_location_and_never_submits(self):
        main.atomic_write_json(self.directory / "paused", {"paused_at": "before"})
        self.location.return_value = {"status": "pending"}
        code, result = self.invoke("resume")
        self.assertEqual((code, result["status"]), (23, "pending"))
        self.assertTrue((self.directory / "paused").exists())
        self.submit.assert_not_called()

    def test_resume_only_removes_existing_unchanged_pause_after_read_only_checks(self):
        main.atomic_write_json(self.directory / "paused", {"paused_at": "before"})
        code, result = self.invoke("resume")
        self.assertEqual((code, result["status"]), (0, "resumed"))
        self.assertFalse((self.directory / "paused").exists())
        self.submit.assert_not_called()

    def test_resume_does_not_clear_pause_created_during_preflight(self):
        def pause_during_query(*_args):
            main.atomic_write_json(self.directory / "paused", {"paused_at": "new-user-pause"})
            return self.client, copy.deepcopy(self.current)

        self.query.side_effect = pause_during_query
        _code, result = self.invoke("resume")
        self.assertNotEqual(result["status"], "resumed")
        self.assertEqual(self.read("paused")["paused_at"], "new-user-pause")
        self.submit.assert_not_called()

    def test_resume_does_not_clear_pause_replaced_during_preflight(self):
        main.atomic_write_json(self.directory / "paused", {"paused_at": "older-pause"})

        def pause_during_query(*_args):
            main.atomic_write_json(self.directory / "paused", {"paused_at": "new-user-pause"})
            return self.client, copy.deepcopy(self.current)

        self.query.side_effect = pause_during_query
        _code, result = self.invoke("resume")
        self.assertNotEqual(result["status"], "resumed")
        self.assertEqual(self.read("paused")["paused_at"], "new-user-pause")
        self.submit.assert_not_called()

    def test_resume_cannot_enable_changed_configuration(self):
        main.atomic_write_json(self.directory / "paused", {"paused_at": "before"})

        def change_config_during_query(*_args):
            self.cfg["enabled"] = False
            return self.client, copy.deepcopy(self.current)

        self.query.side_effect = change_config_during_query
        _code, result = self.invoke("resume")
        self.assertNotEqual(result["status"], "resumed")
        self.assertTrue((self.directory / "paused").exists())
        self.submit.assert_not_called()

    def test_preflight_and_resume_create_read_only_school_clients(self):
        with mock.patch.object(main, "AttnClient", return_value=self.client) as constructor:
            with mock.patch.object(main, "query_today_task", return_value=self.current):
                for mode, expected_read_only in (("preflight", True), ("resume", True), ("run", False)):
                    with self.subTest(mode=mode):
                        self.original_authenticated_query(copy.deepcopy(self.cfg), mode)
                        constructor.assert_called_with("offline-test-token", read_only=expected_read_only)

    def test_generic_school_http_error_does_not_resubmit_password(self):
        self.cfg["user"].update({"username": "offline-user", "password": "offline-password"})
        with mock.patch.object(main, "AttnClient", return_value=self.client):
            with mock.patch.object(main, "query_today_task", side_effect=main.SafeCheckinError("school_http", "school unavailable")):
                with mock.patch.object(main, "new_login") as login:
                    with self.assertRaises(main.SafeCheckinError) as failure:
                        self.original_authenticated_query(copy.deepcopy(self.cfg), "preflight")
                    self.assertEqual(failure.exception.code, "school_http")
                    login.assert_not_called()

    def test_explicit_expired_token_allows_only_one_read_only_relogin(self):
        self.cfg["user"].update({"username": "offline-user", "password": "offline-password"})
        with mock.patch.object(main, "AttnClient", return_value=self.client) as constructor:
            with mock.patch.object(main, "query_today_task", side_effect=[main.SafeCheckinError("auth_required", "authentication required"), self.current]) as query:
                with mock.patch.object(main, "new_login", return_value="offline-refreshed-token") as login:
                    _client, current = self.original_authenticated_query(copy.deepcopy(self.cfg), "preflight")
                    self.assertEqual(current["status"], "ready")
                    login.assert_called_once()
                    self.assertEqual(query.call_count, 2)
                    self.assertEqual(constructor.call_args_list, [
                        mock.call("offline-test-token", read_only=True),
                        mock.call("offline-refreshed-token", read_only=True),
                    ])

    def test_rejected_password_is_not_repeated_with_same_credentials(self):
        self.cfg["user"] = {"username": "offline-user", "password": "offline-password"}
        with mock.patch.object(main, "login", side_effect=main.AuthError("AUTH_REJECTED")) as login:
            with mock.patch.object(main, "save_token") as save:
                with self.assertRaises(main.AuthError) as first:
                    main.new_login(self.cfg)
                self.assertEqual(first.exception.code, "AUTH_REJECTED")
                with self.assertRaises(main.AuthError) as second:
                    main.new_login(self.cfg)
                self.assertEqual(second.exception.code, "AUTH_MANUAL_REQUIRED")
                login.assert_called_once()
                save.assert_not_called()
        saved = json.dumps(self.read("auth_failure.json"))
        self.assertNotIn("offline-user", saved)
        self.assertNotIn("offline-password", saved)

    def test_transient_auth_failure_is_not_a_permanent_credential_block(self):
        self.cfg["user"] = {"username": "offline-user", "password": "offline-password"}
        for error_code in ("AUTH_NETWORK", "AUTH_RATE_LIMITED"):
            with self.subTest(code=error_code):
                with mock.patch.object(main, "login", side_effect=main.AuthError(error_code)) as login:
                    for _attempt in range(2):
                        with self.assertRaises(main.AuthError):
                            main.new_login(self.cfg)
                    self.assertEqual(login.call_count, 2)
                self.assertFalse((self.directory / "auth_failure.json").exists())

    def test_notification_acceptance_is_deduplicated_for_the_day(self):
        self.cfg["notify"] = {"type": "bark"}
        result = {"status": "pending", "code": "post_submit_unconfirmed"}
        main.announce(self.cfg, result)
        main.announce(self.cfg, result)
        self.notification.assert_called_once()
        self.assertTrue(self.read("notifications.json")["events"]["pending:post_submit_unconfirmed"]["accepted"])

    def test_unaccepted_notification_has_ten_minute_retry_floor(self):
        self.cfg["notify"] = {"type": "bark"}
        self.notification.return_value = False
        result = {"status": "failed", "code": "network_unavailable"}
        with contextlib.redirect_stdout(io.StringIO()):
            main.announce(self.cfg, result)
            self.clock.return_value = self.now + datetime.timedelta(minutes=9)
            main.announce(self.cfg, result)
            self.assertEqual(self.notification.call_count, 1)
            self.clock.return_value = self.now + datetime.timedelta(minutes=10)
            main.announce(self.cfg, result)
        self.assertEqual(self.notification.call_count, 2)

    def test_read_only_failure_does_not_send_runtime_notification(self):
        self.cfg["notify"] = {"type": "bark"}
        self.query.side_effect = main.AuthError("AUTH_REJECTED")
        code, result = self.invoke("preflight")
        self.assertEqual((code, result["status"]), (21, "auth_error"))
        self.notification.assert_not_called()

    def test_safe_evidence_drops_response_payload_and_identifiers(self):
        evidence = main.safe_evidence({
            "success_verified": True, "record_state": "missing", "raw": "DO_NOT_LEAK_TEST",
            "token": "DO_NOT_LEAK_TEST", "reason": "https://example.invalid/?secret=DO_NOT_LEAK_TEST",
            "school_id": 123, "schoolDate": "2026-09-18",
        })
        self.assertEqual(evidence, {"success_verified": True, "record_state": "missing"})


if __name__ == "__main__":
    unittest.main()
