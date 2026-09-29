"""Offline protocol fixtures only. These tests never use an account or a network."""

import copy
import datetime
import json
import traceback
import unittest
import urllib.parse
from unittest.mock import Mock, patch

import requests

from src import checkin


FIXED_NOW = datetime.datetime(2026, 9, 18, 21, 35, tzinfo=checkin.TZ_CN)
CFG = {"checkin": {"longitude": 0.1, "latitude": 0.1, "actual_location": "MOCK ONLY"}}


def task(status=None, signed_at=None):
    # Field names come from the public H5. Every value below is synthetic.
    data = {
        "paramsData": {"FToday": "2026/9/18", "FStartTime": "21:30", "FEndTime": "23:59", "FLateTime": "22:30"},
        "userData": {"FPlanID": "MOCK_PLAN", "FWay": 1, "FCollUnit": "MOCK_UNIT", "FUser": "MOCK_USER", "UserId": "MOCK_USER"},
        "schoolData": [{"ID": "MOCK_CAMPUS", "FPosition": json.dumps({"range": [
            {"id": "MOCK_RANGE", "type": "polygon", "paths": [[0, 0], [1, 0], [0, 1]]}
        ]})}],
        "statusData": None,
    }
    if status is not None:
        data["statusData"] = {"status": status, "createTime": signed_at}
    return data


def client_for(*responses, now=FIXED_NOW, read_only=False):
    client = Mock()
    client.read_only = read_only
    client.init.side_effect = [copy.deepcopy(item) for item in responses]
    client.server_time.return_value = int(now.timestamp() * 1000)
    client.check.return_value = [{"id": "MOCK_RANGE", "title": "MOCK ONLY"}]
    client.clock_in.return_value = {"attn": {"msg": "success"}}
    return client


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.local_now = FIXED_NOW
        self.clock = patch.object(checkin, "beijing_now", side_effect=lambda stamp=None: (
            self.local_now if stamp is None else datetime.datetime.fromtimestamp(stamp / 1000, checkin.TZ_CN)
        ))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        network = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Tests must not access network"))
        network.start()
        self.addCleanup(network.stop)
        sleep = patch.object(checkin.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)


class QueryTests(OfflineCase):
    def test_today_unsigned_is_ready(self):
        result = checkin.query_today_task(client_for(task()), CFG)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["window"]["date"], "2026-09-18")
        self.assertTrue(result["window"]["in_window"])

    def test_live_school_formats_weekday_suffix_and_padded_times_are_accepted(self):
        # Digit-masked live sample (2026-09-29): FToday "dddd-dd-dd  星期二", times "dd:dd     ".
        data = task()
        data["paramsData"].update(FToday="2026-09-18  星期五", FStartTime="21:00     ",
                                  FLateTime="22:30     ", FEndTime="23:59     ")
        result = checkin.query_today_task(client_for(data), CFG)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["window"]["date"], "2026-09-18")
        self.assertTrue(result["window"]["in_window"])
        for bad in ("2026-09-18 星期五 补", "2026-09-18星期", "星期五 2026-09-18", "2026-09-17  星期四"):
            data = task()
            data["paramsData"]["FToday"] = bad
            with self.subTest(value=bad):
                self.assertEqual(checkin.query_today_task(client_for(data), CFG)["status"], "pending")
        data = task()
        data["paramsData"]["FStartTime"] = "21 :00"
        self.assertEqual(checkin.query_today_task(client_for(data), CFG)["status"], "pending")

    def test_live_range_schema_accepts_rectangle_corners_and_rejects_other_shapes(self):
        # Live 2026-09-29 sample: two "polygon" items (9 vertices) plus one "rectangle" with 2 corners.
        def with_ranges(ranges):
            data = task()
            data["schoolData"][0]["FPosition"] = json.dumps({"range": ranges, "mapType": "amap"})
            return data
        polygon = {"id": "MOCK_RANGE", "type": "polygon", "title": "MOCK", "radius": [], "paths": [[0, 0], [1, 0], [0, 1]]}
        rectangle = {"id": "MOCK_RECT", "type": "rectangle", "title": "MOCK", "radius": [], "paths": [[0, 0], [1, 1]]}
        client = client_for(task())
        result = checkin.validate_location(client, CFG, with_ranges([polygon, rectangle]))
        self.assertEqual(result["status"], "valid")
        sent = client.check.call_args.args[2]["range"]
        self.assertEqual([item["type"] for item in sent], ["polygon", "rectangle"])
        for bad in ([dict(rectangle, paths=[[0, 0], [1, 1], [2, 2]])], [dict(polygon, paths=[[0, 0], [1, 1]])],
                    [dict(rectangle, type="circle")], [dict(rectangle, paths=[[0, 0]])]):
            client = client_for(task())
            with self.subTest(bad=bad[0]["type"], points=len(bad[0]["paths"])):
                self.assertEqual(checkin.validate_location(client, CFG, with_ranges(bad))["status"], "pending")
                client.check.assert_not_called()

    def test_nonempty_legacy_fields_do_not_prove_success(self):
        data = task()
        data["userData"].update(FState="SUCCESS", FCheckInTime="已签到 / random nonempty text")
        self.assertEqual(checkin.query_today_task(client_for(data), CFG)["status"], "ready")

    def test_success_requires_verified_dated_timestamp(self):
        accepted = [int((FIXED_NOW - datetime.timedelta(minutes=1)).timestamp() * 1000),
                    "2026-09-18 21:34:00", "2026-09-18T13:34:00Z", "2026-09-18T21:34:00+08:00"]
        for value in accepted:
            with self.subTest(value=value):
                self.assertEqual(checkin.query_today_task(client_for(task("SUCCESS", value)), CFG)["status"], "already")
        rejected = [None, "", "已签到", "21:34:00", True, "2026-09-17 21:34:00", "2026-09-18 23:30:00", "2026-09-18 10:00:00", float("nan")]
        for value in rejected:
            with self.subTest(value=value):
                self.assertEqual(checkin.query_today_task(client_for(task("SUCCESS", value)), CFG)["status"], "pending")

    def test_processing_failed_and_unknown_states_never_ready(self):
        for state in ["PROCESSING", "FAILED", "success", "已签到", "UNKNOWN", 1, True, [], {}]:
            with self.subTest(state=state):
                self.assertEqual(checkin.query_today_task(client_for(task(state, "2026-09-18 21:34:00")), CFG)["status"], "pending")

    def test_unknown_or_stale_date_plan_window_fail_closed(self):
        for section, key, value in [
            ("paramsData", "FToday", "2026/9/17"), ("paramsData", "FToday", "Friday"),
            ("paramsData", "FToday", None), ("userData", "FWay", "1"),
            ("userData", "FWay", True), ("userData", "FPlanID", {}),
            ("paramsData", "FLateTime", None), ("paramsData", "FEndTime", "01:00"),
            ("paramsData", "FStartTime", "25:00"), ("paramsData", "FLateTime", "20:30"),
        ]:
            data = task()
            data[section][key] = value
            with self.subTest(section=section, key=key, value=value):
                self.assertEqual(checkin.query_today_task(client_for(data), CFG)["status"], "pending")
        data = task()
        del data["userData"]["FPlanID"]
        self.assertEqual(checkin.query_today_task(client_for(data), CFG)["status"], "pending")
        data = task()
        del data["userData"]["FUser"]
        del data["userData"]["UserId"]
        self.assertEqual(checkin.query_today_task(client_for(data), CFG)["status"], "pending")

    def test_no_task_requires_explicit_today_schema(self):
        data = task()
        data["userData"]["FPlanID"] = ""
        self.assertEqual(checkin.query_today_task(client_for(data), CFG)["status"], "no_task")
        data = task()
        data["userData"]["FWay"] = 3
        self.assertEqual(checkin.query_today_task(client_for(data), CFG)["status"], "no_task")
        data["userData"]["FWay"] = 2
        self.assertEqual(checkin.query_today_task(client_for(data), CFG)["status"], "pending")

    def test_beijing_window_boundaries_are_minute_inclusive(self):
        for hour, minute, second, expected in [(21, 29, 59, False), (21, 30, 0, True),
                (21, 34, 0, True), (21, 35, 0, True), (23, 59, 0, True), (23, 59, 59, True)]:
            self.local_now = FIXED_NOW.replace(hour=hour, minute=minute, second=second)
            with self.subTest(time=self.local_now.isoformat()):
                result = checkin.query_today_task(client_for(task(), now=self.local_now), CFG)
                self.assertEqual(result["window"]["in_window"], expected)
        self.local_now = FIXED_NOW.replace(day=19, hour=0, minute=0)
        self.assertEqual(checkin.query_today_task(client_for(task(), now=self.local_now), CFG)["status"], "pending")

    def test_server_clock_is_required_and_skew_fails_closed(self):
        for value in [None, "123", True, float("nan"), 0, int((FIXED_NOW + datetime.timedelta(minutes=6)).timestamp() * 1000)]:
            client = client_for(task())
            client.server_time.return_value = value
            with self.subTest(value=value), self.assertRaises(checkin.SafeCheckinError):
                checkin.query_today_task(client, CFG)


class LocationTests(OfflineCase):
    def test_preflight_only_checks_range_and_does_not_leak_values(self):
        client = client_for(task())
        result = checkin.validate_location(client, CFG, task())
        self.assertEqual(result["status"], "valid")
        client.clock_in.assert_not_called()
        self.assertNotIn("MOCK", json.dumps(result))
        self.assertNotIn("longitude", json.dumps(result))

    def test_malformed_school_ranges_fail_closed(self):
        for value in [None, [], {}, [{"ID": "x", "FPosition": "bad json"}],
                      [{"ID": "x", "FPosition": '{"range":[{"id":"x"}]}'}]]:
            data = task()
            data["schoolData"] = value
            client = client_for(data)
            with self.subTest(value=value):
                self.assertEqual(checkin.validate_location(client, CFG, data)["status"], "pending")
                client.check.assert_not_called()

    def test_unknown_match_and_empty_match_are_not_accepted(self):
        for match, status in [([], "out_of_range"), ([{"id": "UNREVIEWED"}], "pending"),
                              ([{}], "pending"), ([None], "pending"), (None, "pending")]:
            client = client_for(task())
            client.check.return_value = match
            with self.subTest(match=match):
                self.assertEqual(checkin.validate_location(client, CFG, task())["status"], status)

    def test_nan_boolean_and_missing_coordinates_fail_closed(self):
        for value in [float("nan"), float("inf"), True, None, ""]:
            cfg = copy.deepcopy(CFG)
            cfg["checkin"]["longitude"] = value
            with self.subTest(value=value):
                self.assertEqual(checkin.validate_location(client_for(task()), cfg, task())["status"], "pending")


class SubmitTests(OfflineCase):
    def test_submit_response_alone_is_not_success(self):
        client = client_for(task(), task(), task(), task(), task())
        result = checkin.do_checkin(client, CFG, task(), before_submit=lambda: True)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["evidence"]["reason"], "post_submit_unconfirmed")
        client.clock_in.assert_called_once()
        self.assertEqual(client.init.call_count, 1 + checkin.POST_SUBMIT_QUERIES)
        self.assertEqual(self.sleep.call_count, checkin.POST_SUBMIT_QUERIES - 1)

    def test_async_school_insert_is_confirmed_by_a_later_requery_without_resubmitting(self):
        client = client_for(task(), task("PROCESSING", None), task("SUCCESS", "2026-09-18 21:35:00"))
        result = checkin.do_checkin(client, CFG, task(), before_submit=lambda: True)
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["evidence"]["success_verified"])
        client.clock_in.assert_called_once()
        self.assertEqual(client.init.call_count, 3)
        self.assertEqual(self.sleep.call_count, 1)
        client = client_for(task(), task("FAILED", None), task("SUCCESS", "2026-09-18 21:35:00"))
        result = checkin.do_checkin(client, CFG, task(), before_submit=lambda: True)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(client.init.call_count, 2)

    def test_live_integer_coll_unit_is_forwarded_and_other_types_block_submission(self):
        data = task()
        data["userData"]["FCollUnit"] = 12
        client = client_for(data, task("SUCCESS", "2026-09-18 21:35:00"))
        result = checkin.do_checkin(client, CFG, data, before_submit=lambda: True)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(client.clock_in.call_args.kwargs["coll_unit"], 12)
        for bad in (True, None, 1.5, {}, " "):
            data = task()
            data["userData"]["FCollUnit"] = bad
            client = client_for(data, data)
            with self.subTest(bad=bad):
                self.assertEqual(checkin.do_checkin(client, CFG, data, before_submit=lambda: True)["evidence"]["reason"], "submission_schema_unverified")
                client.clock_in.assert_not_called()

    def test_success_requires_requery_and_same_today_plan(self):
        client = client_for(task(), task("SUCCESS", "2026-09-18 21:35:00"))
        result = checkin.do_checkin(client, CFG, task(), before_submit=lambda: True)
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["evidence"]["success_verified"])
        client.clock_in.assert_called_once()
        changed = task("SUCCESS", "2026-09-18 21:35:00")
        changed["userData"]["FPlanID"] = "NEW_PLAN"
        self.assertEqual(checkin.do_checkin(client_for(task(), changed), CFG, task(), before_submit=lambda: True)["status"], "pending")

    def test_timeout_always_requeries_and_never_resubmits(self):
        for after, expected in [(task(), "pending"), (task("SUCCESS", "2026-09-18 21:35:00"), "confirmed")]:
            client = client_for(task(), after)
            client.clock_in.side_effect = requests.Timeout("SECRET_URL_WITH_TOKEN")
            with self.subTest(expected=expected):
                result = checkin.do_checkin(client, CFG, task(), before_submit=lambda: True)
                self.assertEqual(result["status"], expected)
                client.clock_in.assert_called_once()
                self.assertGreaterEqual(client.init.call_count, 2)  # re-queried (with async retries), never re-submitted
                self.assertNotIn("SECRET", json.dumps(result))

    def test_processing_and_failed_after_submit_stay_pending(self):
        for state in ["PROCESSING", "FAILED"]:
            client = client_for(task(), task(state, "2026-09-18 21:35:00"))
            self.assertEqual(checkin.do_checkin(client, CFG, task(), before_submit=lambda: True)["status"], "pending")
            client.clock_in.assert_called_once()

    def test_query_failure_after_write_is_pending_without_retrying_write(self):
        client = client_for(task())
        client.init.side_effect = [task(), checkin.SafeCheckinError("school_http", "学校接口暂不可用")]
        result = checkin.do_checkin(client, CFG, task(), before_submit=lambda: True)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["evidence"]["reason"], "post_submit_query_unavailable")
        client.clock_in.assert_called_once()

    def test_guard_mandatory_readonly_pause_and_changed_plan_block(self):
        for readonly, guard, expected in [(True, lambda: True, "pending"), (False, None, "pending"),
                                           (False, lambda: False, "paused"), (False, lambda: 1, "paused")]:
            client = client_for(task(), read_only=readonly)
            with self.subTest(readonly=readonly, expected=expected):
                self.assertEqual(checkin.do_checkin(client, CFG, task(), before_submit=guard)["status"], expected)
                client.clock_in.assert_not_called()
        old = task()
        old["userData"]["FPlanID"] = "OLD_PLAN"
        client = client_for(task())
        self.assertEqual(checkin.do_checkin(client, CFG, old, before_submit=lambda: True)["status"], "pending")
        client.clock_in.assert_not_called()

    def test_final_guard_is_immediately_before_single_write(self):
        events = []
        client = client_for(task(), task("SUCCESS", "2026-09-18 21:35:00"))
        client.clock_in.side_effect = lambda **kwargs: events.append("clock")
        client.server_time.side_effect = lambda: events.append("server_time") or int(FIXED_NOW.timestamp() * 1000)
        def guard():
            events.append("guard")
            return True
        checkin.do_checkin(client, CFG, task(), before_submit=guard)
        self.assertEqual(events[events.index("guard") + 1], "clock")

    def test_late_line_uses_server_beijing_minutes(self):
        for minute, second, late in [(30, 59, False), (31, 0, True)]:
            self.local_now = FIXED_NOW.replace(hour=22, minute=minute, second=second)
            client = client_for(task(), task(), now=self.local_now)
            checkin.do_checkin(client, CFG, task(), before_submit=lambda: True)
            self.assertEqual(client.clock_in.call_args.kwargs["is_late"], late)

    def test_outside_window_and_midnight_never_write(self):
        self.local_now = FIXED_NOW.replace(hour=21, minute=29)
        client = client_for(task(), now=self.local_now)
        self.assertEqual(checkin.do_checkin(client, CFG, task(), before_submit=lambda: True)["status"], "outside_window")
        client.clock_in.assert_not_called()
        self.local_now = FIXED_NOW.replace(day=19, hour=0, minute=0)
        client = client_for(task(), now=self.local_now)
        self.assertEqual(checkin.do_checkin(client, CFG, task(), before_submit=lambda: True)["status"], "pending")
        client.clock_in.assert_not_called()


class HttpTests(OfflineCase):
    def http_client(self, read_only=True):
        client = checkin.AttnClient("MOCK_SECRET_TOKEN", read_only=read_only)
        self.addCleanup(client.close)
        client.http.request = Mock()
        return client

    @staticmethod
    def response(data=None, status=200, **body):
        response = Mock(status_code=status)
        response.json.return_value = body or {"success": True, "code": 1, "data": data}
        return response

    def test_readonly_guard_covers_direct_encrypted_endpoint(self):
        for explicit in [True, None, 0, "false"]:
            client = self.http_client(explicit)
            with self.assertRaises(checkin.SafeCheckinError) as context:
                client._enc_get("clockIn.action", {})
            self.assertEqual(context.exception.code, "read_only")
            client.http.request.assert_not_called()

    def test_transient_reads_have_at_most_two_retries(self):
        client = self.http_client()
        client.http.request.side_effect = requests.Timeout("MOCK_SECRET_TOKEN in URL")
        with patch.object(checkin.time, "sleep"), self.assertRaises(checkin.SafeCheckinError) as context:
            client.init()
        self.assertEqual(client.http.request.call_count, 3)
        self.assertNotIn("MOCK_SECRET_TOKEN", "".join(traceback.format_exception(context.exception)))

    def test_write_has_no_network_or_http_retry(self):
        for failure in [requests.Timeout("PRIVATE_URL"), self.response(status=503)]:
            client = self.http_client(False)
            if isinstance(failure, Exception):
                client.http.request.side_effect = failure
            else:
                client.http.request.return_value = failure
            with self.subTest(failure=type(failure).__name__), self.assertRaises(checkin.SafeCheckinError):
                client._enc_get("clockIn.action", {})
            client.http.request.assert_called_once()
            self.assertFalse(client.http.request.call_args.kwargs["allow_redirects"])
            self.assertEqual(client.http.adapters["https://"].max_retries.total, 0)

    def test_http_errors_rejections_and_invalid_json_are_sanitized(self):
        for response, code in [(self.response(status=500), "school_http"), (self.response(status=401), "auth_required"),
                               (self.response(success=False, code=0, msg="PRIVATE_SERVER_RESPONSE"), "school_rejected"),
                               (self.response(success="false", code=1, data={}), "school_rejected"),
                               (self.response(success=True, code=True, data={}), "school_rejected")]:
            client = self.http_client()
            client.http.request.return_value = response
            with self.subTest(code=code), self.assertRaises(checkin.SafeCheckinError) as context:
                client.init()
            self.assertEqual(context.exception.code, code)
            self.assertNotIn("PRIVATE", str(context.exception))
            client.http.request.assert_called_once()

    def test_timestamp_get_has_no_param_and_encrypted_calls_double_encode(self):
        client = self.http_client()
        client.http.request.return_value = self.response({"timestamp": int(FIXED_NOW.timestamp() * 1000)})
        client.server_time()
        self.assertNotIn("params", client.http.request.call_args.kwargs)
        client.http.request.return_value = self.response([])
        client.check(0.1, 0.1, {"range": []})
        params = client.http.request.call_args.kwargs["params"]
        prepared = requests.Request("GET", "https://example.invalid/", params=params).prepare()
        once_decoded = urllib.parse.parse_qs(urllib.parse.urlsplit(prepared.url).query)["param"][0]
        payload = json.loads(checkin.decrypt(urllib.parse.unquote(once_decoded)))
        self.assertEqual(payload["mapType"], "amap")
        self.assertEqual(payload["longitude"], 0.1)

    def test_invalid_json_and_redirects_are_not_followed_or_leaked(self):
        client = self.http_client()
        response = self.response()
        response.json.side_effect = ValueError("PRIVATE_RESPONSE")
        client.http.request.return_value = response
        with self.assertRaises(checkin.SafeCheckinError) as context:
            client.init()
        self.assertEqual(context.exception.code, "invalid_response")
        self.assertNotIn("PRIVATE", "".join(traceback.format_exception(context.exception)))
        client.http.request.return_value = self.response(status=302)
        with self.assertRaises(checkin.SafeCheckinError) as context:
            client.init()
        self.assertEqual(context.exception.code, "school_http")
        self.assertFalse(client.http.request.call_args.kwargs["allow_redirects"])

    def test_transient_http_reads_retry_twice_then_succeed(self):
        client = self.http_client()
        client.http.request.side_effect = [self.response(status=503), self.response(status=502), self.response(task())]
        with patch.object(checkin.time, "sleep"):
            self.assertEqual(client.init()["userData"]["FPlanID"], "MOCK_PLAN")
        self.assertEqual(client.http.request.call_count, 3)


if __name__ == "__main__":
    unittest.main()
