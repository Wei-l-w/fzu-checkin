"""Offline privilege-boundary tests. All service/CLI processes are mocked."""

import concurrent.futures
import errno
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import control_broker as broker


class ScheduleTests(unittest.TestCase):
    def test_dropin_text_and_validation(self):
        text = broker.schedule_dropin(["21:35", "21:40", "21:50"])
        self.assertIn("\nOnCalendar=\n", text)
        self.assertEqual(text.count("OnCalendar=*-*-*"), 3)
        self.assertIn("OnCalendar=*-*-* 21:40:00 Asia/Shanghai\n", text)
        for bad in (["20:00"], ["21:50", "21:35"], ["21:35;rm -rf"], [], ["21:0%d" % i for i in range(7)], ["21:35\n"], "21:35"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                broker.schedule_dropin(bad)

    def test_read_schedule_times_defaults_json_yaml_and_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            self.assertEqual(broker.read_schedule_times(path), list(broker.DEFAULT_SCHEDULE_TIMES))
            path.write_text(json.dumps({"enabled": True, "user": {"token": "TEST_SECRET"}}))
            self.assertEqual(broker.read_schedule_times(path), list(broker.DEFAULT_SCHEDULE_TIMES))
            path.write_text(json.dumps({"schedule": {"times": ["21:05", "23:00"]}}))
            self.assertEqual(broker.read_schedule_times(path), ["21:05", "23:00"])
            path.write_text("schedule:\n  times:\n    - '21:15'\n    - '22:45'\n")
            self.assertEqual(broker.read_schedule_times(path), ["21:15", "22:45"])
            for bad in ('{"schedule": {"times": ["20:00"]}}', '{"schedule": []}', '[]', 'schedule: {times: ["21:00", "21:00"]}'):
                path.write_text(bad)
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    broker.read_schedule_times(path)

    def test_apply_schedule_writes_atomically_and_reloads_only_on_change(self):
        with tempfile.TemporaryDirectory() as directory:
            dropin = Path(directory) / "fzu-checkin.timer.d" / "schedule.conf"
            runner = mock.Mock()
            self.assertTrue(broker.apply_schedule(["21:35", "21:40"], dropin=dropin, runner=runner))
            runner.assert_called_once_with([broker.SYSTEMCTL, "daemon-reload"], check=True)
            self.assertEqual(dropin.read_text(), broker.schedule_dropin(["21:35", "21:40"]))
            self.assertEqual(dropin.stat().st_mode & 0o777, 0o644)
            self.assertFalse(broker.apply_schedule(["21:35", "21:40"], dropin=dropin, runner=runner))
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(sorted(item.name for item in dropin.parent.iterdir()), ["schedule.conf"])
            with self.assertRaises(ValueError):
                broker.apply_schedule(["25:00"], dropin=dropin, runner=runner)
            runner.assert_called_once()


class RequestTests(unittest.TestCase):
    def test_allowlist(self):
        for action in ("status", "preflight", "pause", "resume", "notify-test"):
            self.assertEqual(broker.parse_request(json.dumps({"action": action}).encode() + b"\n"), (action, "admin"))
        self.assertEqual(broker.parse_request(b'{"action":"pause","profile":"mate_01"}\n'), ("pause", "mate_01"))

    def test_invalid_schema_and_injection(self):
        for raw in (
            b'{"action":"run"}\n', b'{"action":"logs"}\n',
            b'{"action":"resume;id"}\n', b'{"action":"status","argv":[]}\n',
            b'{"action":"status","env":{"PATH":"/tmp"}}\n',
            b'{"action":"status","path":"/etc/shadow"}\n',
            b'{"action":"status","action":"pause"}\n',
            b'{"action":[]}\n', b'[]\n', b'null\n', b'{"action":null}\n',
            b'{"action":"status"}', b'{"action":"status"}\n{}\n',
            b'\xff\n', b'\n', b'{}\n', b' ' * 4097 + b'\n',
            b'{"action":"status","profile":"Admin"}\n', b'{"action":"status","profile":"../x"}\n',
            b'{"action":"status","profile":""}\n', b'{"action":"status","profile":1}\n',
            b'{"action":"status","profile":"a"}\n', b'{"profile":"admin"}\n',
        ):
            with self.subTest(raw=raw[:70]):
                with self.assertRaises((ValueError, UnicodeError)):
                    broker.parse_request(raw)

    def test_limit_includes_newline(self):
        payload = b'{"action":"status"}'
        maximum = payload + b" " * (4095 - len(payload)) + b"\n"
        self.assertEqual(broker.parse_request(maximum), ("status", "admin"))
        with self.assertRaises(ValueError):
            broker.parse_request(maximum + b"\n")


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.runner = mock.Mock(return_value=broker.CommandResult(0, b"SECRET password=not-for-client"))
        self.control = broker.ControlBroker(12345, runner=self.runner, profile_exists=lambda _: True)

    def test_only_fixed_command_and_no_output(self):
        for action in ("preflight", "pause", "resume", "notify-test"):
            response = self.control.dispatch(action)
            self.assertEqual(response, {"ok": True, "returncode": 0})
            self.runner.assert_called_with((broker.CONTROL_COMMAND, action, "admin"), timeout=240)

    def test_profile_is_forwarded_only_when_provisioned(self):
        self.assertEqual(self.control.dispatch("preflight", "mate_01"), {"ok": True, "returncode": 0})
        self.runner.assert_called_with((broker.CONTROL_COMMAND, "preflight", "mate_01"), timeout=240)
        strict = broker.ControlBroker(12345, runner=self.runner, profile_exists=lambda profile: profile == "admin")
        self.runner.reset_mock()
        self.assertEqual(strict.dispatch("preflight", "ghost"), broker.error("invalid_request", 64))
        self.assertEqual(strict.dispatch("status", "ghost"), broker.error("invalid_request", 64))
        self.runner.assert_not_called()
        self.assertTrue(broker.profile_exists.__doc__)
        self.assertFalse(broker.profile_exists("../etc"))
        self.assertEqual(broker.timer_unit("mate_01"), "fzu-checkin@mate_01.timer")
        self.assertEqual(str(broker.timer_dropin("mate_01")), "/etc/systemd/system/fzu-checkin@mate_01.timer.d/schedule.conf")

    def test_unknown_action_never_calls_process(self):
        self.assertEqual(self.control.dispatch("run"), broker.error("invalid_request", 64))
        self.runner.assert_not_called()

    def test_failure_never_returns_command_output(self):
        self.runner.return_value = broker.CommandResult(20, b"SECRET upstream details")
        self.assertEqual(self.control.dispatch("preflight"), {"ok": False, "returncode": 20})

    def test_static_exception_response_and_pause(self):
        self.runner.side_effect = [RuntimeError("SECRET"), broker.CommandResult(0)]
        self.assertEqual(self.control.dispatch("resume"), broker.error("internal_error", 70))
        self.assertEqual(self.runner.call_args.args[0], (broker.CONTROL_COMMAND, "pause", "admin"))

    def test_timeout_pauses_and_returns_static_error(self):
        self.runner.side_effect = [broker.CommandResult(124, timed_out=True), broker.CommandResult(0)]
        self.assertEqual(self.control.dispatch("resume"), broker.error("timeout", 124))
        self.assertEqual(self.runner.call_args_list, [
            mock.call((broker.CONTROL_COMMAND, "resume", "admin"), timeout=240),
            mock.call((broker.CONTROL_COMMAND, "pause", "admin"), timeout=30),
        ])

    def test_pause_timeout_has_fixed_disable_stop_fallback(self):
        self.runner.side_effect = [broker.CommandResult(124, timed_out=True),
                                   broker.CommandResult(124, timed_out=True),
                                   broker.CommandResult(0), broker.CommandResult(0)]
        self.assertEqual(self.control.dispatch("pause"), broker.error("timeout", 124))
        self.assertEqual(self.runner.call_args_list[-2:], [
            mock.call((broker.SYSTEMCTL, "disable", "--now", "fzu-checkin@admin.timer"), timeout=15),
            mock.call((broker.SYSTEMCTL, "stop", "fzu-checkin@admin.service"), timeout=15),
        ])

    def test_status_only_returns_allowlisted_properties(self):
        self.runner.side_effect = [
            broker.CommandResult(0, b"ActiveState=inactive\nUnitFileState=disabled\nNextElapseUSecRealtime=n/a\nEnvironment=SECRET\n"),
            broker.CommandResult(0, b"ActiveState=inactive\nResult=success\nExecMainStatus=20\nExecStart=SECRET\n"),
        ]
        response = self.control.dispatch("status")
        self.assertEqual(response, {"ok": True,
                         "timer": {"active": "inactive", "enabled": "disabled", "next_run": ""},
                         "service": {"active": "inactive", "result": "success", "exit_code": 20}})
        for call in self.runner.call_args_list:
            self.assertEqual(call.args[0][0:2], (broker.SYSTEMCTL, "show"))
        self.assertNotIn("SECRET", json.dumps(response))

    def test_status_failure_and_malformed_properties_are_static(self):
        self.runner.side_effect = [broker.CommandResult(1, b"SECRET"), broker.CommandResult(0)]
        self.assertEqual(self.control.dispatch("status"), broker.error("status_unavailable", 70))
        self.runner.side_effect = [broker.CommandResult(0, b"ActiveState=<SECRET>\nNextElapseUSecRealtime=<SECRET>\n"),
                                   broker.CommandResult(0, b"ExecMainStatus=<SECRET>\n")]
        response = self.control.dispatch("status")
        self.assertEqual(response["timer"]["active"], "unknown")
        self.assertEqual(response["timer"]["next_run"], "")
        self.assertIsNone(response["service"]["exit_code"])
        self.assertNotIn("SECRET", json.dumps(response))

    def test_next_run_uses_unambiguous_iso_timezone(self):
        self.runner.side_effect = [
            broker.CommandResult(0, b"ActiveState=active\nUnitFileState=enabled\nNextElapseUSecRealtime=Sat 2026-09-19 21:35:00 CST\n"),
            broker.CommandResult(0, b"ActiveState=inactive\nResult=success\nExecMainStatus=0\n"),
        ]
        self.assertEqual(self.control.dispatch("status")["timer"]["next_run"], "2026-09-19T21:35:00+08:00")
        self.assertEqual(broker._next_run_iso("Sat 2026-09-19 13:35:00 UTC"), "2026-09-19T13:35:00+00:00")
        self.assertEqual(broker._next_run_iso("n/a"), "")
        self.assertEqual(broker._next_run_iso("Sat 2026-99-99 21:35:00 CST"), "")

    def test_pause_bypasses_running_resume_and_ordinary_work_is_bounded(self):
        entered, release = threading.Event(), threading.Event()

        def run(arguments, timeout):
            if arguments == (broker.CONTROL_COMMAND, "resume", "admin"):
                entered.set()
                self.assertTrue(release.wait(3))
            return broker.CommandResult(0)

        self.runner.side_effect = run
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            resumed = pool.submit(self.control.dispatch, "resume")
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.control.dispatch("preflight"), broker.error("busy", 75))
                self.assertEqual(self.control.dispatch("pause"), {"ok": True, "returncode": 0})
            finally:
                release.set()
            self.assertTrue(resumed.result(timeout=1)["ok"])

    def test_second_pause_and_excess_status_are_bounded(self):
        self.control._pause_slot.acquire()
        self.assertEqual(self.control.dispatch("pause"), broker.error("busy", 75))
        self.control._pause_slot.release()
        self.control._status_slots.acquire()
        self.control._status_slots.acquire()
        self.assertEqual(self.control.dispatch("status"), broker.error("busy", 75))
        self.control._status_slots.release()
        self.control._status_slots.release()
        self.runner.assert_not_called()


class PeerTests(unittest.TestCase):
    def connection(self, uid=12345, request=b'{"action":"pause"}\n'):
        connection = mock.Mock()
        connection.getsockopt.return_value = struct.pack("3i", 999, uid, 111)
        connection.recv.side_effect = [request, b""]
        return connection

    def response(self, connection):
        return json.loads(connection.sendall.call_args.args[0])

    def test_root_and_service_uid_allowed(self):
        for uid in (0, 12345):
            runner = mock.Mock(return_value=broker.CommandResult(0))
            connection = self.connection(uid)
            broker.ControlBroker(12345, runner, profile_exists=lambda _: True).handle_connection(connection)
            self.assertTrue(self.response(connection)["ok"])
            connection.getsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            connection.close.assert_called_once()

    def test_other_uid_denied_without_reading_request(self):
        runner = mock.Mock()
        connection = self.connection(12346)
        broker.ControlBroker(12345, runner, profile_exists=lambda _: True).handle_connection(connection)
        self.assertEqual(self.response(connection), broker.error("forbidden", 77))
        connection.recv.assert_not_called()
        runner.assert_not_called()

    def test_oversize_and_unknown_fields_do_not_run(self):
        for request in (b" " * 4097, b'{"action":"pause","path":"/tmp/secret"}\n'):
            runner = mock.Mock()
            connection = self.connection(request=request)
            broker.ControlBroker(12345, runner, profile_exists=lambda _: True).handle_connection(connection)
            self.assertEqual(self.response(connection), broker.error("invalid_request", 64))
            runner.assert_not_called()

    def test_slow_request_times_out(self):
        runner = mock.Mock()
        connection = self.connection()
        connection.recv.side_effect = socket.timeout()
        broker.ControlBroker(12345, runner, profile_exists=lambda _: True).handle_connection(connection)
        self.assertEqual(self.response(connection), broker.error("invalid_request", 64))
        runner.assert_not_called()


class RunnerTests(unittest.TestCase):
    @mock.patch.object(broker.subprocess, "Popen")
    def test_process_has_fixed_environment_new_group_and_hidden_output(self, popen):
        process = popen.return_value
        process.communicate.return_value = (b"only internally captured", b"SECRET")
        process.returncode = 0
        result = broker.CommandRunner()((broker.CONTROL_COMMAND, "preflight"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(popen.call_args.args, ((broker.CONTROL_COMMAND, "preflight"),))
        options = popen.call_args.kwargs
        self.assertTrue(options["start_new_session"])
        self.assertTrue(options["close_fds"])
        self.assertEqual(options["cwd"], "/")
        self.assertEqual(options["env"], broker.FIXED_ENV)
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.PIPE)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)

    @mock.patch.object(broker.os, "killpg")
    @mock.patch.object(broker.subprocess, "Popen")
    def test_timeout_kills_only_new_child_group(self, popen, killpg):
        process = popen.return_value
        process.pid = 54321
        process.communicate.side_effect = [subprocess.TimeoutExpired("not leaked", 240), (b"SECRET", b"SECRET")]
        result = broker.CommandRunner()((broker.CONTROL_COMMAND, "resume", "admin"))
        self.assertEqual(result, broker.CommandResult(124, timed_out=True))
        killpg.assert_called_once_with(54321, signal.SIGKILL)
        self.assertEqual(process.communicate.call_args_list, [mock.call(timeout=240), mock.call(timeout=2)])

    @mock.patch.object(broker.os, "killpg")
    @mock.patch.object(broker.subprocess, "Popen")
    def test_timeout_pipe_cleanup_cannot_wait_forever(self, popen, killpg):
        process = popen.return_value
        process.pid = 54322
        process.communicate.side_effect = subprocess.TimeoutExpired("not leaked", 240)
        self.assertTrue(broker.CommandRunner()((broker.CONTROL_COMMAND, "resume", "admin")).timed_out)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)

    @mock.patch.object(broker.subprocess, "Popen", side_effect=OSError("SECRET"))
    def test_launch_error_is_static(self, popen):
        self.assertEqual(broker.CommandRunner()((broker.CONTROL_COMMAND, "pause", "admin")), broker.CommandResult(70))


class LockTests(unittest.TestCase):
    def test_existing_runtime_directory_must_be_root_owned_exact_mode(self):
        for uid, gid, mode in ((1, 500, 0o750), (0, 501, 0o750), (0, 500, 0o770)):
            metadata = mock.Mock(st_uid=uid, st_gid=gid, st_mode=stat.S_IFDIR | mode)
            with mock.patch.object(broker.os, "mkdir", side_effect=FileExistsError), \
                    mock.patch.object(broker.os, "open", return_value=9) as opened, \
                    mock.patch.object(broker.os, "fstat", return_value=metadata), \
                    mock.patch.object(broker.os, "close") as closed:
                with self.assertRaises(PermissionError):
                    broker.secure_runtime_directory(500)
                self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)
                closed.assert_called_once_with(9)

    def test_new_directory_sets_root_group_mode(self):
        metadata = mock.Mock(st_uid=0, st_gid=500, st_mode=stat.S_IFDIR | 0o750)
        with mock.patch.object(broker.os, "mkdir"), mock.patch.object(broker.os, "open", return_value=9), \
                mock.patch.object(broker.os, "fstat", return_value=metadata), \
                mock.patch.object(broker.os, "fchown") as chown, \
                mock.patch.object(broker.os, "fchmod") as chmod:
            self.assertEqual(broker.secure_runtime_directory(500), 9)
            chown.assert_called_once_with(9, 0, 500)
            chmod.assert_called_once_with(9, 0o750)

    def test_lock_uses_nofollow_and_never_truncates(self):
        metadata = mock.Mock(st_uid=0, st_mode=stat.S_IFREG | 0o600, st_nlink=1)
        with mock.patch.object(broker.os, "open", return_value=9) as opened, \
                mock.patch.object(broker.os, "fstat", return_value=metadata):
            self.assertEqual(broker.open_root_lock(8, "schedule.lock"), 9)
        flags = opened.call_args.args[1]
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertFalse(flags & os.O_TRUNC)
        self.assertEqual(opened.call_args.kwargs, {"dir_fd": 8})

    def test_unsafe_owner_type_permissions_and_hardlinks_rejected(self):
        for uid, mode, links in ((1, stat.S_IFREG | 0o600, 1), (0, stat.S_IFIFO | 0o600, 1),
                                 (0, stat.S_IFREG | 0o660, 1), (0, stat.S_IFREG | 0o600, 2)):
            metadata = mock.Mock(st_uid=uid, st_mode=mode, st_nlink=links)
            with mock.patch.object(broker.os, "open", return_value=9), \
                    mock.patch.object(broker.os, "fstat", return_value=metadata), \
                    mock.patch.object(broker.os, "close"):
                with self.assertRaises(PermissionError):
                    broker.open_root_lock(8, "schedule.lock")

    def test_real_symlink_lock_cannot_truncate_target(self):
        with tempfile.TemporaryDirectory(prefix="fzu-lock-test-") as temporary:
            directory = Path(temporary)
            protected = directory / "protected"
            # Only disposable fixtures are ever targeted by this safety test.
            original = b"must-not-be-truncated"
            protected.write_bytes(original)
            (directory / "schedule.lock").symlink_to(protected)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError) as raised:
                    broker.open_root_lock(descriptor, "schedule.lock")
                self.assertEqual(raised.exception.errno, errno.ELOOP)
                self.assertTrue((directory / "schedule.lock").is_symlink())
                self.assertTrue(protected.is_file())
                self.assertEqual(protected.read_bytes(), original)
            finally:
                os.close(descriptor)

    def test_no_arbitrary_lock_names(self):
        with self.assertRaises(ValueError):
            broker.open_root_lock(8, "../../etc/shadow")

    def test_cli_does_not_use_service_user_writable_schedule_lock(self):
        source = (Path(broker.__file__).parent / "deploy" / "fzu-checkinctl").read_text()
        self.assertNotIn("/var/lib/fzu-checkin/state/schedule.lock", source)
        self.assertIn('open_root_lock(directory_fd, "schedule.lock")', source)
        self.assertIn('secure_runtime_directory(pwd.getpwnam("fzu-checkin").pw_gid)', source)


class SocketOwnershipTests(unittest.TestCase):
    def test_fresh_socket_binds_and_has_restricted_mode(self):
        with tempfile.TemporaryDirectory(prefix="fzu-socket-test-") as temporary:
            directory = Path(temporary)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with mock.patch.object(broker, "RUNTIME_DIR", directory), \
                        mock.patch.object(broker.os, "chown"):
                    listener = broker.bind_socket(descriptor, 500)
                    try:
                        self.assertEqual(stat.S_IMODE((directory / "control.sock").stat().st_mode), 0o660)
                        self.assertEqual(listener.getsockname(), str(directory / "control.sock"))
                    finally:
                        listener.close()
            finally:
                os.close(descriptor)

    def test_foreign_or_regular_socket_path_never_unlinked(self):
        for mode, uid, gid in ((stat.S_IFREG, 0, 500), (stat.S_IFSOCK, 2, 500), (stat.S_IFSOCK, 0, 501)):
            info = mock.Mock(st_mode=mode, st_uid=uid, st_gid=gid)
            with mock.patch.object(broker.os, "stat", return_value=info), \
                    mock.patch.object(broker.os, "unlink") as unlink:
                with self.assertRaises(PermissionError):
                    broker.bind_socket(8, 500)
                unlink.assert_not_called()

    def test_live_owned_socket_is_never_unlinked(self):
        info = mock.Mock(st_mode=stat.S_IFSOCK, st_uid=0, st_gid=500)
        with mock.patch.object(broker.os, "stat", return_value=info), \
                mock.patch.object(broker.socket, "socket") as socket_factory, \
                mock.patch.object(broker.os, "unlink") as unlink:
            socket_factory.return_value.__enter__.return_value.connect.return_value = None
            with self.assertRaises(RuntimeError):
                broker.bind_socket(8, 500)
            unlink.assert_not_called()


if __name__ == "__main__":
    unittest.main()
