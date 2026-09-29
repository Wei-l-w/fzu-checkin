"""Offline-only private configuration/authentication/notification regression tests."""
import base64
from copy import deepcopy
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests
import yaml

from src.config import (ConfigError, atomic_write_json, load_config, parse_date,
                        save_token, state_dir, validation_errors)
from src.login import AuthError, SERVICE, SSO_BASE, login
from src.notify import notify
from src.vacation import matched_range


def valid_config():
    return {
        "enabled": False,
        "user": {"username": "offline-user", "password": "offline-password", "token": ""},
        "checkin": {"coordinate_system": "GCJ-02", "confirmed": True,
                    "longitude": 119.2, "latitude": 26.05, "actual_location": "OFFLINE TEST ONLY"},
        "skip_dates": [], "vacation": {"skip_ranges": []}, "notify": {"type": "none"},
    }


class PrivateConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fzu-offline-config-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "config.yaml"
        self.env = patch.dict(os.environ, {"FZU_CHECKIN_CONFIG": str(self.path)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_config(self, cfg):
        self.path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        self.path.chmod(0o600)

    def test_blank_template_loads_without_credentials_or_activation(self):
        template = Path(__file__).resolve().parents[1] / "config.example.yaml"
        self.path.write_bytes(template.read_bytes())
        self.path.chmod(0o600)
        cfg = load_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["vacation"]["skip_ranges"], [])
        self.assertEqual(cfg["notify"]["type"], "none")
        self.assertTrue(validation_errors(cfg))
        self.assertFalse(state_dir(cfg).exists())

    def test_private_paths_and_atomic_identity_bound_token_cache(self):
        original = valid_config()
        self.write_config(original)
        before = self.path.read_bytes()
        cfg = load_config()
        self.assertEqual(validation_errors(cfg), [])
        self.assertEqual(cfg["_config_path"], str(self.path))
        self.assertEqual(state_dir(cfg), self.directory / "state")
        save_token(cfg, "offline-new-token-0123456789")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(load_config()["user"]["token"], "offline-new-token-0123456789")
        self.assertEqual((state_dir(cfg).stat().st_mode & 0o777), 0o700)
        self.assertEqual(((state_dir(cfg) / "session.json").stat().st_mode & 0o777), 0o600)
        for field in ("username", "password", "token"):
            changed = deepcopy(original)
            changed["user"][field] = "different-offline-value"
            self.write_config(changed)
            self.assertEqual(load_config()["user"]["token"], changed["user"]["token"])

    def test_explicit_state_path_and_untrusted_metadata_are_not_overridden_by_yaml(self):
        cfg = valid_config()
        cfg["_state_dir"] = "/not-an-authorized-configuration-source"
        cfg["_config_path"] = "/not-an-authorized-configuration-source"
        self.write_config(cfg)
        override = self.directory / "private-override"
        with patch.dict(os.environ, {"FZU_CHECKIN_STATE_DIR": str(override)}):
            loaded = load_config()
        self.assertEqual(state_dir(loaded), override)
        self.assertEqual(loaded["_config_path"], str(self.path))

    def test_public_config_and_symlinks_are_refused(self):
        self.write_config(valid_config())
        self.path.chmod(0o644)
        with self.assertRaises(ConfigError):
            load_config()
        self.path.chmod(0o600)
        link = self.directory / "alias.yaml"
        link.symlink_to(self.path)
        with self.assertRaises(ConfigError):
            load_config(link)

    def test_atomic_failure_preserves_original_and_sanitizes_error(self):
        destination = self.directory / "state.json"
        atomic_write_json(destination, {"previous": True})
        with patch("src.config.os.replace", side_effect=OSError("OFFLINE-SECRET-URL")):
            with self.assertRaises(ConfigError) as caught:
                atomic_write_json(destination, {"new": True})
        self.assertNotIn("OFFLINE-SECRET-URL", str(caught.exception))
        self.assertEqual(json.loads(destination.read_text()), {"previous": True})
        self.assertEqual(list(self.directory.glob(".state-*.tmp")), [])

    def test_malformed_yaml_and_session_never_echo_values(self):
        self.path.write_text('secret: [OFFLINE-SECRET-URL\n', encoding="utf-8")
        self.path.chmod(0o600)
        with self.assertRaises(ConfigError) as caught:
            load_config()
        self.assertNotIn("OFFLINE-SECRET-URL", str(caught.exception))
        self.write_config(valid_config())
        cfg = load_config()
        atomic_write_json(state_dir(cfg) / "session.json", ["OFFLINE-SECRET-URL"])
        with self.assertRaises(ConfigError) as caught:
            load_config()
        self.assertEqual(caught.exception.code, "CONFIG_SESSION_INVALID")
        self.assertNotIn("OFFLINE-SECRET-URL", str(caught.exception))

    def test_invalid_controls_coordinates_dates_and_channels_fail_closed(self):
        mutations = [
            lambda c: c.update(enabled="true"),
            lambda c: c.update(paused="false"),
            lambda c: c.update(pause=True),
            lambda c: c["checkin"].update(confirmed=1),
            lambda c: c["checkin"].update(coordinate_system="WGS84"),
            lambda c: c["checkin"].update(longitude=float("nan")),
            lambda c: c["checkin"].update(latitude=float("inf")),
            lambda c: c["checkin"].update(actual_location=""),
            lambda c: c.update(campus={"bounds": {}}),
            lambda c: c.update(skip_dates=["2026-02-30"]),
            lambda c: c.update(skip_dates=None),
            lambda c: c["vacation"].update(skip_ranges=[{"start": "2026-09-19", "end": "2026-09-18"}]),
            lambda c: c["notify"].update(type="pushplus"),
            lambda c: c["notify"].update(type="bark", bark_url="http://example.invalid/OFFLINE-SECRET-URL"),
            lambda c: c["notify"].update(type="bark", bark_url="https://example.invalid/OFFLINE-SECRET-URL", serverchan_key="another-offline-channel"),
            lambda c: c["notify"].update(type="wecom", wecom_webhook="https://example.invalid/OFFLINE-SECRET-URL"),
        ]
        for mutate in mutations:
            cfg = valid_config()
            mutate(cfg)
            with self.subTest(cfg_keys=list(cfg)):
                errors = validation_errors(cfg)
                self.assertTrue(errors)
                self.assertNotIn("OFFLINE-SECRET-URL", " ".join(errors))

    def test_token_must_be_a_clean_token_not_an_address_bar_fragment(self):
        for bad in ("OFFLINEFRAGMENT0123456789ABCDEFGH&contextPath=", "short-token",
                    "https://yzsxg.fzu.edu.cn/x/index.action?token=OFFLINE-TOKEN-0123456789"):
            cfg = valid_config()
            cfg["user"] = {"username": "", "password": "", "token": bad}
            errors = validation_errors(cfg)
            self.assertTrue(any(error.startswith("user.token: 格式不符") for error in errors), bad)
            self.assertNotIn("OFFLINEFRAGMENT", " ".join(errors))
        cfg = valid_config()
        cfg["user"] = {"username": "", "password": "", "token": "OFFLINE-TOKEN-0123456789.~_"}
        self.assertEqual(validation_errors(cfg), [])
        cfg["user"]["token"] = "OFFLINE TOKEN 0123456789"
        self.assertEqual([error for error in validation_errors(cfg) if error.startswith("user.token")],
                         ["user.token: 不得包含空白字符"])

    def test_normalize_token_reduces_urls_and_fragments_to_the_token(self):
        from src.config import normalize_token
        token = "OFFLINE-TOKEN-0123456789abcdef"
        cases = {
            f"https://yzsxg.fzu.edu.cn/livecloud/project/fzu/attn/index.action?token={token}&contextPath=": token,
            f"  ?token={token}#/home\n": token,
            f"token={token}": token,
            f"={token}&contextPath=": token,
            f"{token}&contextPath=": token,
            token: token,
            "OFFLINE%2DTOKEN-0123456789abcdef": "OFFLINE-TOKEN-0123456789abcdef",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_token(value), expected)
        self.assertEqual(normalize_token(""), "")
        self.assertIsNone(normalize_token(None))

    def test_schedule_times_validation_and_defaults(self):
        from src.config import DEFAULT_SCHEDULE_TIMES, schedule_times, valid_schedule_times
        cfg = valid_config()
        self.assertEqual(schedule_times(cfg), list(DEFAULT_SCHEDULE_TIMES))
        self.assertEqual(validation_errors(cfg), [])
        cfg["schedule"] = {"times": ["21:00", "22:30", "23:55"]}
        self.assertEqual(validation_errors(cfg), [])
        self.assertEqual(schedule_times(cfg), ["21:00", "22:30", "23:55"])
        for bad in (["20:59"], ["23:56"], ["21:40", "21:35"], ["21:35", "21:35"], ["9:30"], ["21:35:00"], [],
                    ["21:0%d" % i for i in range(7)], "21:35", [2135], ["24:00"], ["21:35 "], [None]):
            cfg["schedule"] = {"times": bad}
            with self.subTest(bad=bad):
                self.assertFalse(valid_schedule_times(bad))
                self.assertTrue(any(error.startswith("schedule.times") for error in validation_errors(cfg)))
                self.assertEqual(schedule_times(cfg), list(DEFAULT_SCHEDULE_TIMES))
        cfg["schedule"] = {"times": ["21:35"], "extra": 1}
        self.assertTrue(any(error.startswith("schedule.times") for error in validation_errors(cfg)))
        cfg["schedule"] = "21:35"
        self.assertTrue(any(error.startswith("schedule:") for error in validation_errors(cfg)))

    def test_dates_are_strict_and_all_ranges_validate_before_matching(self):
        self.assertEqual(parse_date("2026-09-18"), dt.date(2026, 9, 18))
        self.assertEqual(parse_date(dt.date(2026, 9, 18)), dt.date(2026, 9, 18))
        for value in ("20260918", "2026-9-18", "2026-09-18T00:00:00", dt.datetime(2026, 9, 18), None):
            with self.assertRaises(ConfigError):
                parse_date(value)
        cfg = {"vacation": {"skip_ranges": [{"name": "offline-range", "start": "2026-09-18", "end": "2026-09-18"}]}}
        self.assertEqual(matched_range(cfg, dt.date(2026, 9, 18)), "offline-range")
        cfg["vacation"]["skip_ranges"].append({"start": "bad", "end": "2026-09-18"})
        with self.assertRaises(ConfigError):
            matched_range(cfg, dt.date(2026, 9, 18))


def response(url=SSO_BASE, status=200, text="", location=None, body=None):
    value = MagicMock()
    value.url = url
    value.status_code = status
    value.text = text
    value.headers = {"Location": location} if location else {}
    value.json.return_value = body
    return value


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.session.headers = {}
        self.patch = patch("src.login.requests.Session", return_value=self.session)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        key = base64.b64encode(b"0123456789abcdef").decode("ascii")
        self.page = response(text=f"<p class='hidden' id='login-croypto'>{key}</p><p id='login-page-flowkey'>offline-execution</p>")
        self.session.get.return_value = self.page

    def test_single_post_and_only_allowed_tls_redirect_chain(self):
        callback = response(SERVICE, status=302, location="https://yzsxg.fzu.edu.cn/authorize.action?token=offline-token-0123456789")
        self.session.get.side_effect = [self.page, callback]
        self.session.post.return_value = response(status=302, location=SERVICE + "?ticket=offline-ticket")
        self.assertEqual(login("offline-user", "offline-password"), "offline-token-0123456789")
        self.assertEqual(self.session.post.call_count, 1)
        self.assertFalse(self.session.trust_env)
        for method in (self.session.get, self.session.post):
            for call in method.call_args_list:
                self.assertIs(call.kwargs["allow_redirects"], False)
                self.assertNotEqual(call.kwargs.get("verify"), False)
        submitted = self.session.post.call_args.kwargs["data"]
        self.assertNotEqual(submitted["password"], "offline-password")

    def test_evil_plaintext_userinfo_redirects_and_post_replay_are_blocked(self):
        cases = [
            (302, "https://example.invalid/?token=OFFLINE-SECRET", "AUTH_REDIRECT_BLOCKED"),
            (302, "http://yzsxg.fzu.edu.cn/?token=OFFLINE-SECRET", "AUTH_REDIRECT_BLOCKED"),
            (302, "https://offline-user@yzsxg.fzu.edu.cn/", "AUTH_REDIRECT_BLOCKED"),
            (307, SSO_BASE, "AUTH_PROTOCOL_CHANGED"),
        ]
        for status, target, code in cases:
            self.session.post.reset_mock()
            self.session.post.return_value = response(status=status, location=target)
            with self.assertRaises(AuthError) as caught:
                login("offline-user", "offline-password")
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("OFFLINE-SECRET", str(caught.exception))
            self.assertEqual(self.session.post.call_count, 1)

    def test_authentication_failure_classes_and_no_password_retries(self):
        cases = [
            ("<p id='login-error-msg'>账号或密码错误 OFFLINE-SECRET</p>", "AUTH_REJECTED"),
            ("<p id='login-error-msg'>需要人脸验证 OFFLINE-SECRET</p>", "AUTH_INTERACTIVE_REQUIRED"),
            ("<p id='login-error-msg'>验证码错误 OFFLINE-SECRET</p>", "AUTH_INTERACTIVE_REQUIRED"),
            # Password accepted, but the SSO rendered a follow-up step (flow key kept, no new
            # password key) asking for SMS/captcha verification: interactive, not a protocol change.
            ("<p id='login-page-flowkey'>offline-step OFFLINE-SECRET</p><div>请输入短信验证码</div>",
             "AUTH_INTERACTIVE_REQUIRED"),
            ("<p id='login-page-flowkey'>offline-step OFFLINE-SECRET</p><p>no extra step words</p>",
             "AUTH_PROTOCOL_CHANGED"),
            ("<p>unknown protocol OFFLINE-SECRET</p>", "AUTH_PROTOCOL_CHANGED"),
        ]
        for text, code in cases:
            self.session.post.reset_mock()
            self.session.post.return_value = response(text=text)
            with self.assertRaises(AuthError) as caught:
                login("offline-user", "offline-password")
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("OFFLINE-SECRET", str(caught.exception))
            self.assertEqual(self.session.post.call_count, 1)

    def test_initial_challenge_and_network_failure_do_not_submit(self):
        self.session.get.return_value = response(text='<input name="captcha" type="text">')
        with self.assertRaises(AuthError) as caught:
            login("offline-user", "offline-password")
        self.assertEqual(caught.exception.code, "AUTH_INTERACTIVE_REQUIRED")
        self.session.post.assert_not_called()
        self.session.get.side_effect = requests.Timeout("https://example.invalid/OFFLINE-SECRET")
        with self.assertRaises(AuthError) as caught:
            login("offline-user", "offline-password")
        self.assertEqual(caught.exception.code, "AUTH_NETWORK")
        self.assertNotIn("OFFLINE-SECRET", str(caught.exception))
        self.session.post.assert_not_called()

    def test_post_timeout_is_not_replayed_and_message_is_sanitized(self):
        self.session.post.side_effect = requests.Timeout("OFFLINE-SECRET-PASSWORD")
        with self.assertRaises(AuthError) as caught:
            login("offline-user", "offline-password")
        self.assertEqual(caught.exception.code, "AUTH_NETWORK")
        self.assertNotIn("OFFLINE-SECRET", str(caught.exception))
        self.assertEqual(self.session.post.call_count, 1)


class NotificationTests(unittest.TestCase):
    def test_none_is_silent_and_performs_no_network(self):
        with patch("src.notify.requests.Session") as session, patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertFalse(notify({"notify": {"type": "none"}}, "offline", "offline"))
            session.assert_not_called()
            self.assertEqual(out.getvalue(), "")

    def test_business_codes_are_required_and_requests_are_private(self):
        cases = [
            ({"type": "bark", "bark_url": "https://example.invalid/offline-key"}, "code", 200),
            ({"type": "serverchan", "serverchan_key": "offline-serverchan-key"}, "code", 0),
            ({"type": "wecom", "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=offline-key"}, "errcode", 0),
        ]
        for settings, field, accepted in cases:
            with patch("src.notify.requests.Session") as session_class, patch("sys.stdout", new_callable=io.StringIO):
                session = session_class.return_value.__enter__.return_value
                for status, body, expected in [(200, {field: accepted}, True), (200, {field: 9999}, False), (200, {}, False), (302, {field: accepted}, False), (200, {field: False}, False)]:
                    session.post.return_value = response(status=status, body=body)
                    self.assertEqual(notify({"notify": settings}, "offline", "offline"), expected)
                self.assertFalse(session.trust_env)
                self.assertIs(session.post.call_args.kwargs["allow_redirects"], False)

    def test_raw_notification_exception_is_not_logged(self):
        cfg = {"notify": {"type": "bark", "bark_url": "https://example.invalid/OFFLINE-SECRET"}}
        with patch("src.notify.requests.Session") as session_class, patch("sys.stdout", new_callable=io.StringIO) as out:
            session_class.return_value.__enter__.return_value.post.side_effect = requests.Timeout("https://example.invalid/OFFLINE-SECRET")
            self.assertFalse(notify(cfg, "offline", "offline"))
            self.assertNotIn("OFFLINE-SECRET", out.getvalue())


if __name__ == "__main__":
    unittest.main()
