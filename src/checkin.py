"""Fail-closed attendance client; reviewed against the public H5 on 2026-09-18.

Only ``clockIn.action`` mutates attendance. It is disabled by default, never
retried, and its response alone is never proof of a successful attendance.
Raw init data is private working data: callers must log ``evidence`` only.
See docs/PROTOCOL_REVIEW.md for the verified schema and evidence boundaries.
"""

import base64
import datetime
import json
import math
import re
import time
import urllib.parse
from zoneinfo import ZoneInfo

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

BASE_URL = "https://yzsxg.fzu.edu.cn"
API_PREFIX = "/livecloud/project/fzu/attn"
AES_KEY = b"apexinfoapexinfo"
TZ_CN = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"
READ_RETRIES = 2  # Additional attempts, and only for transient read failures.
MAX_CLOCK_SKEW_SECONDS = 300


class SafeCheckinError(RuntimeError):
    """A static error code/message; never carries a URL or server response."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def beijing_now(ts_ms=None):
    if ts_ms is not None:
        return datetime.datetime.fromtimestamp(ts_ms / 1000, TZ_CN)
    return datetime.datetime.now(TZ_CN)


def encrypt(obj):
    data = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_KEY)
    return base64.b64encode(cipher.encrypt(pad(data.encode(), AES.block_size))).decode()


def decrypt(b64text):
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_KEY)
    return unpad(cipher.decrypt(base64.b64decode(b64text)), AES.block_size).decode()


class AttnClient:
    """Explicit read-only default, fixed-origin endpoints, no redirects/retries on writes."""

    _ENDPOINTS = {
        "init.action": "POST",
        "currentTimestamp.action": "GET",
        "check.action": "GET",
        "clockIn.action": "GET",
    }

    def __init__(self, token, *, read_only=True):
        self._read_only = read_only is not False
        self.http = requests.Session()
        # Do not forward the authentication header to ambient proxy settings.
        self.http.trust_env = False
        self.http.headers.update({"User-Agent": UA, "token": token})
        self.http.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

    @property
    def read_only(self):
        return self._read_only

    def close(self):
        self.http.close()

    def _request(self, method, path, **kwargs):
        if self._ENDPOINTS.get(path) != method:
            raise SafeCheckinError("unsafe_endpoint", "接口不在允许列表内")
        is_write = path == "clockIn.action"
        if is_write and self.read_only:
            raise SafeCheckinError("read_only", "只读模式禁止提交签到")
        attempts = 1 if is_write else READ_RETRIES + 1
        for attempt in range(attempts):
            try:
                response = self.http.request(
                    method,
                    BASE_URL + API_PREFIX + "/" + path,
                    timeout=(5, 15),
                    allow_redirects=False,
                    **kwargs,
                )
            except requests.exceptions.SSLError:
                raise SafeCheckinError("network_unavailable", "学校连接证书验证失败") from None
            except (requests.Timeout, requests.ConnectionError):
                if attempt + 1 < attempts:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise SafeCheckinError("network_unavailable", "学校连接超时或暂不可用") from None
            except requests.RequestException:
                raise SafeCheckinError("network_unavailable", "学校请求未能完成") from None
            status = response.status_code
            if status in {401, 403}:
                raise SafeCheckinError("auth_required", "学校认证失效，需要重新登录")
            if status in {429, 502, 503, 504} and attempt + 1 < attempts:
                response.close()
                time.sleep(0.25 * (attempt + 1))
                continue
            if status != 200:
                raise SafeCheckinError("school_http", "学校接口返回非正常HTTP状态")
            try:
                body = response.json()
            except (ValueError, requests.RequestException):
                raise SafeCheckinError("invalid_response", "学校响应不是可验证的JSON") from None
            if not isinstance(body, dict):
                raise SafeCheckinError("invalid_response", "学校响应结构无法验证")
            if type(body.get("code")) is int and body["code"] in {401, 403}:
                raise SafeCheckinError("auth_required", "学校认证失效，需要重新登录")
            if body.get("success") is not True or type(body.get("code")) is not int or body["code"] != 1:
                raise SafeCheckinError("school_rejected", "学校接口未明确接受请求")
            return body.get("data")
        raise SafeCheckinError("network_unavailable", "学校请求未能完成")

    def _enc_get(self, path, payload):
        # H5: encodeURIComponent(Encrypt(...)), then Axios encodes the query.
        param = urllib.parse.quote(encrypt(payload), safe="")
        return self._request("GET", path, params={"param": param})

    def init(self):
        data = self._request("POST", "init.action", json={})
        if not isinstance(data, dict):
            raise SafeCheckinError("invalid_response", "学校初始化数据结构无法验证")
        return data

    def server_time(self):
        # Current public H5 calls this GET with {}, i.e. no encrypted param.
        data = self._request("GET", "currentTimestamp.action")
        value = data.get("timestamp") if isinstance(data, dict) else None
        if type(value) not in (int, float) or not math.isfinite(value) or not 946684800000 <= value <= 4102444800000:
            raise SafeCheckinError("protocol_schema", "学校时间戳无法验证")
        return value

    def check(self, longitude, latitude, school_position):
        data = self._enc_get("check.action", {
            "mapType": "amap", "longitude": longitude, "latitude": latitude,
            "range": json.dumps(school_position, separators=(",", ":")),
        })
        if not isinstance(data, list):
            raise SafeCheckinError("invalid_response", "学校范围校验结果无法验证")
        return data

    def clock_in(self, *, campus_id, longitude, latitude, start_time, end_time,
                 actual_location, coll_unit, way, is_late):
        return self._enc_get("clockIn.action", {
            "campus": campus_id, "lon": longitude, "lat": latitude,
            "startTime": start_time, "endTime": end_time,
            "actualLocation": actual_location, "caIsNo": "0",
            "collUnit": coll_unit, "way": way, "isLate": 1 if is_late else 0,
        })


POST_SUBMIT_QUERIES = 4
POST_SUBMIT_DELAY_SECONDS = 3


def _task_date(value):
    if not isinstance(value, str):
        return None
    # Live 2026-09-29 sample: "2026-09-29  星期二" (two spaces + weekday); weekday suffix is decorative.
    match = re.fullmatch(
        r"(\d{4})(?:-(\d{1,2})-(\d{1,2})|/(\d{1,2})/(\d{1,2})|年(\d{1,2})月(\d{1,2})日)"
        r"(?:\s+(?:星期|周)[一二三四五六日天])?", value.strip())
    if not match:
        return None
    numbers = [int(v) for v in match.groups() if v is not None]
    try:
        return datetime.date(*numbers)
    except ValueError:
        return None


def _minute(value):
    # Live sample pads "21:00" with trailing spaces; only surrounding whitespace is tolerated.
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value.strip()):
        return None
    hour, minute = map(int, value.strip().split(":"))
    return hour * 60 + minute if hour < 24 and minute < 60 else None


def _attendance_time(value):
    """Parse a dated school timestamp; a time-only/nonempty string is not proof."""
    try:
        if type(value) in (int, float):
            if not math.isfinite(value) or not 946684800000 <= value <= 4102444800000:
                return None
            return beijing_now(value)
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?", value
        ):
            return None
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=TZ_CN) if parsed.tzinfo is None else parsed.astimezone(TZ_CN)
    except (ValueError, OverflowError, OSError):
        return None


def _server_now(client):
    value = client.server_time()
    if type(value) not in (int, float) or not math.isfinite(value) or not 946684800000 <= value <= 4102444800000:
        raise SafeCheckinError("protocol_schema", "学校时间戳无法验证")
    now = beijing_now(value)
    local = beijing_now()
    if now.date() != local.date() or abs((now - local).total_seconds()) > MAX_CLOCK_SKEW_SECONDS:
        raise SafeCheckinError("protocol_schema", "学校时间与本机北京时间不一致")
    return now


def _result(status, reason, *, data=None, window=None, **evidence):
    result = {"status": status, "evidence": {"reason": reason, **evidence}}
    if data is not None:
        result.update(init=data, window=window or {}, need_checkin=status == "ready")
    return result


def _inspect(data, now):
    def result(status, reason, window=None, **evidence):
        return _result(status, reason, data=data, window=window, **evidence)

    if not isinstance(data, dict):
        return result("pending", "unknown_init_schema")
    params, user = data.get("paramsData"), data.get("userData")
    if not isinstance(params, dict) or not isinstance(user, dict):
        return result("pending", "unknown_task_schema")
    identity_fields = [name for name in ("FUser", "UserId") if name in user]
    if not identity_fields or any(type(user[name]) not in (str, int) or not str(user[name]).strip() for name in identity_fields):
        return result("pending", "user_identity_unverified")
    day = _task_date(params.get("FToday"))
    if day is None or day != now.date():
        return result("pending", "task_date_unverified", today_matches=False)
    if "FPlanID" not in user or type(user.get("FWay")) is not int or user["FWay"] not in {1, 2, 3}:
        return result("pending", "unknown_plan_schema", today_matches=True)
    plan = user["FPlanID"]
    if plan is not None and type(plan) not in (str, int):
        return result("pending", "unknown_plan_schema", today_matches=True)
    has_plan = plan is not None and str(plan).strip() not in {"", "0"}
    state_data = data.get("statusData")
    if state_data is None:
        state = "ABSENT"
    elif isinstance(state_data, dict) and isinstance(state_data.get("status"), str) and state_data["status"] in {"SUCCESS", "PROCESSING", "FAILED"}:
        state = state_data["status"]
    else:
        return result("pending", "unknown_status_schema", today_matches=True)
    if not has_plan or user["FWay"] == 3:
        if state != "ABSENT":
            return result("pending", "status_without_current_plan", today_matches=True)
        return result("no_task", "no_current_plan" if not has_plan else "school_exempt", today_matches=True, has_plan=has_plan)
    start, end, late = (_minute(params.get(name)) for name in ("FStartTime", "FEndTime", "FLateTime"))
    if any(value is None for value in (start, end, late)) or not start <= late <= end:
        return result("pending", "school_window_unverified", today_matches=True, has_plan=True)
    minute = now.hour * 60 + now.minute
    window = {
        "date": day.isoformat(), "start": params["FStartTime"],
        "end": params["FEndTime"], "late": params["FLateTime"],
        "in_window": start <= minute <= end,
    }
    if state == "SUCCESS":
        signed = _attendance_time(state_data.get("createTime"))
        if signed is None or signed.date() != day or signed > now + datetime.timedelta(seconds=5) or not start <= signed.hour * 60 + signed.minute <= end:
            return result("pending", "success_time_unverified", window, schema_verified=False, today_matches=True)
        return result("already", "school_success_verified", window, schema_verified=True, today_matches=True, success_verified=True)
    if state in {"PROCESSING", "FAILED"}:
        return result("pending", "school_processing" if state == "PROCESSING" else "school_failed", window, schema_verified=True, today_matches=True)
    if user["FWay"] != 1:
        return result("pending", "outside_mode_not_supported", window, schema_verified=True, today_matches=True)
    return result("ready", "unsigned_current_plan", window, schema_verified=True, today_matches=True, success_verified=False)


def query_today_task(client, cfg):
    """Read today's verified task. Unknown/processing/failed states never mean ready."""
    data = client.init()
    return _inspect(data, _server_now(client))


def _location(client, cfg, data):
    """Private result contains the matched campus; never log it or return it publicly."""
    try:
        configured = cfg["checkin"]
        if type(configured["longitude"]) is bool or type(configured["latitude"]) is bool:
            raise ValueError
        longitude, latitude = float(configured["longitude"]), float(configured["latitude"])
        if not math.isfinite(longitude) or not math.isfinite(latitude) or not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError
        user, schools = data["userData"], data["schoolData"]
        if not isinstance(user, dict):
            raise ValueError
        if type(user.get("FWay")) is not int or user["FWay"] != 1:
            return _result("pending", "outside_mode_not_supported", location_valid=False, range_valid=False), None
        if not isinstance(schools, list) or not schools:
            raise ValueError
        polygons = []
        allowed_ids = set()
        for school in schools:
            if not isinstance(school, dict) or type(school.get("ID")) not in (str, int) or not str(school["ID"]).strip():
                raise ValueError
            position = json.loads(school["FPosition"])
            if not isinstance(position, dict):
                raise ValueError
            ranges = position["range"]
            if not isinstance(ranges, list) or not ranges:
                raise ValueError
            for item in ranges:
                if not isinstance(item, dict) or type(item.get("id")) not in (str, int) or not str(item["id"]).strip():
                    raise ValueError
                paths = item.get("paths")
                # Live 2026-09-29 sample: campus ranges mix "polygon" (>=3 vertices) with
                # "rectangle" (exactly 2 corner points); the school's check.action does the geometry.
                minimum = {"polygon": 3, "rectangle": 2}.get(item.get("type"))
                if minimum is None or not isinstance(paths, list) or len(paths) < minimum \
                        or (item["type"] == "rectangle" and len(paths) != 2):
                    raise ValueError
                for point in paths:
                    if not isinstance(point, list) or len(point) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in point):
                        raise ValueError
                    if not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90:
                        raise ValueError
                allowed_ids.add(str(item["id"]))
            allowed_ids.add(str(school["ID"]))
            polygons.extend(ranges)
    except (KeyError, TypeError, ValueError, OverflowError):
        return _result("pending", "school_range_unverified", location_valid=False, range_valid=False), None
    matched = client.check(longitude, latitude, {"range": polygons})
    if not isinstance(matched, list):
        return _result("pending", "range_response_unverified", location_valid=False, range_valid=True), None
    if not matched:
        return _result("out_of_range", "outside_school_range", location_valid=False, range_valid=True), None
    first = matched[0]
    if not isinstance(first, dict) or type(first.get("id")) not in (str, int) or str(first["id"]) not in allowed_ids:
        return _result("pending", "matched_range_unverified", location_valid=False, range_valid=True), None
    return _result("valid", "school_range_verified", location_valid=True, range_valid=True), (first, longitude, latitude)


def validate_location(client, cfg, init_data):
    """Read-only preflight. Public return contains no coordinate, identity or campus ID."""
    return _location(client, cfg, init_data)[0]


def _same_plan(before, after):
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if any(not isinstance(data.get(key), dict) for data in (before, after) for key in ("paramsData", "userData")):
        return False
    before_user, after_user = before["userData"], after["userData"]
    identity_fields = [name for name in ("FUser", "UserId") if name in before_user or name in after_user]
    if not identity_fields or any(type(before_user.get(name)) not in (str, int) or not str(before_user[name]).strip()
                                  or before_user.get(name) != after_user.get(name) for name in identity_fields):
        return False
    return (
        before.get("paramsData", {}).get("FToday") == after.get("paramsData", {}).get("FToday")
        and before.get("userData", {}).get("FPlanID") == after.get("userData", {}).get("FPlanID")
        and before.get("userData", {}).get("FUser") == after.get("userData", {}).get("FUser")
        and before.get("userData", {}).get("UserId") == after.get("userData", {}).get("UserId")
    )


def do_checkin(client, cfg, init_data, before_submit=None):
    """One guarded submission, then independent re-query even after a timeout.

    ``before_submit`` must atomically persist a daily attempt intent and re-read
    pause/skip/config state. Only its exact ``True`` return authorizes the write.
    Caller must refuse further writes on later runs when that intent exists.
    """
    fresh = query_today_task(client, cfg)
    if fresh["status"] != "ready":
        return _result(fresh["status"], fresh["evidence"]["reason"])
    data = fresh["init"]
    if not _same_plan(init_data, data):
        return _result("pending", "task_changed_before_submit")
    if not fresh["window"]["in_window"]:
        return _result("outside_window", "outside_school_window")
    if client.read_only:
        return _result("pending", "read_only")
    if before_submit is None:
        return _result("pending", "submit_guard_required")
    location_result, location = _location(client, cfg, data)
    if location_result["status"] != "valid":
        return _result("pending", location_result["evidence"]["reason"])
    match, longitude, latitude = location
    user, params = data["userData"], data["paramsData"]
    # Live 2026-09-29 sample: FCollUnit is an int (the H5 forwards it unchanged); str or int only.
    if type(user.get("FCollUnit")) not in (str, int) or not str(user["FCollUnit"]).strip():
        return _result("pending", "submission_schema_unverified")
    actual_location = cfg.get("checkin", {}).get("actual_location")
    if not isinstance(actual_location, str) or not actual_location.strip():
        return _result("pending", "location_description_required")
    now = _server_now(client)
    checked = _inspect(data, now)
    if checked["status"] != "ready" or not checked["window"]["in_window"]:
        return _result("outside_window", "window_changed_before_submit")
    kwargs = {
        "campus_id": match["id"], "longitude": longitude, "latitude": latitude,
        "start_time": params["FStartTime"], "end_time": params["FEndTime"],
        "actual_location": actual_location, "coll_unit": user["FCollUnit"],
        "way": user["FWay"], "is_late": now.hour * 60 + now.minute > _minute(params["FLateTime"]),
    }
    if before_submit() is not True:
        return _result("paused", "submission_cancelled_by_guard")
    # Nothing that contacts the school, sleeps, or reads config belongs between
    # the final persisted guard and this single write request.
    submitted_cleanly = True
    try:
        client.clock_in(**kwargs)
    except Exception:
        # The write may have reached the school. Never retry or assume failure.
        submitted_cleanly = False
    # Live 2026-09-29: the school inserts attendance asynchronously ("批量插入成功"), so the
    # record can lag the response by seconds. Re-query a few times before giving up; this
    # loop only reads and never submits again.
    for attempt in range(POST_SUBMIT_QUERIES):
        if attempt:
            time.sleep(POST_SUBMIT_DELAY_SECONDS)
        try:
            verified = query_today_task(client, cfg)
        except Exception:
            if attempt + 1 < POST_SUBMIT_QUERIES:
                continue
            return _result("pending", "post_submit_query_unavailable", submit_attempted=True, success_verified=False)
        if verified["status"] == "already" and _same_plan(data, verified["init"]):
            return _result("confirmed", "school_success_requeried", submit_attempted=True, success_verified=True)
        if verified.get("evidence", {}).get("reason") == "school_failed":
            break
    return _result(
        "pending", "post_submit_unconfirmed" if submitted_cleanly else "submission_outcome_uncertain",
        submit_attempted=True, success_verified=False,
    )
