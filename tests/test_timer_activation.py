"""Timer activation regressions. No test touches live units or school APIs."""
import contextlib
import errno
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import control_broker as broker


class TimerActivationTests(unittest.TestCase):
    def setUp(self):
        self.runner = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, b"ActiveState=active\nUnitFileState=enabled\n"))
        for name, value in (("profile_exists", True), ("read_schedule_times", ["21:10", "22:00"]),
                            ("apply_schedule", True)):
            patcher = mock.patch.object(broker, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_new_member_schedule_and_verification_are_profile_scoped(self):
        broker.activate_timer("mate_01", runner=self.runner)
        self.read_schedule_times.assert_called_once_with(broker.profile_config_path("mate_01"))
        self.apply_schedule.assert_called_once_with(["21:10", "22:00"],
            dropin=broker.timer_dropin("mate_01"), runner=self.runner)
        self.assertEqual(self.runner.call_args_list, [
            mock.call([broker.SYSTEMCTL, "enable", "--now", "fzu-checkin@mate_01.timer"], check=True),
            mock.call([broker.SYSTEMCTL, "restart", "fzu-checkin@mate_01.timer"], check=True),
            mock.call([broker.SYSTEMCTL, "show", "fzu-checkin@mate_01.timer", "--no-pager",
                       "--property=ActiveState,UnitFileState"], check=True, capture_output=True),
        ])

    def test_readonly_system_directory_never_reaches_timer_enable(self):
        self.apply_schedule.side_effect = OSError(errno.EROFS, "read-only fixture")
        with self.assertRaises(OSError):
            broker.activate_timer("mate_01", runner=self.runner)
        self.runner.assert_not_called()

    def test_unchanged_file_still_retries_daemon_reload(self):
        self.apply_schedule.return_value = False
        broker.activate_timer("mate_01", runner=self.runner)
        self.assertEqual(self.runner.call_args_list[0], mock.call([broker.SYSTEMCTL, "daemon-reload"], check=True))

    def test_disabled_inactive_and_unknown_timers_are_not_success(self):
        for output in (b"ActiveState=inactive\nUnitFileState=enabled\n",
                       b"ActiveState=active\nUnitFileState=disabled\n", b""):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                self.runner.return_value.stdout = output
                broker.activate_timer("mate_01", runner=self.runner)

    def test_enable_restart_or_verification_failure_is_propagated(self):
        for failed_command in ("enable", "restart", "show"):
            def run(arguments, **_kwargs):
                if arguments[1] == failed_command:
                    raise subprocess.CalledProcessError(1, arguments)
                return subprocess.CompletedProcess(arguments, 0, b"ActiveState=active\nUnitFileState=enabled\n")
            with self.subTest(command=failed_command), self.assertRaises(subprocess.CalledProcessError):
                broker.activate_timer("mate_01", runner=run)

    def test_unknown_profile_cannot_write_or_activate_a_timer(self):
        self.profile_exists.return_value = False
        with self.assertRaises(ValueError):
            broker.activate_timer("missing", runner=self.runner)
        self.apply_schedule.assert_not_called()
        self.runner.assert_not_called()


class CLIScheduleTests(unittest.TestCase):
    def run_script(self, activation_error=None, receipt_error=False):
        source = (Path(broker.__file__).parent / "deploy/fzu-checkinctl").read_text()
        source = source.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        def run(arguments, **_kwargs):
            code = 22 if receipt_error and "finalize_resume(True)" in arguments[-1] else 0
            return subprocess.CompletedProcess(arguments, code)
        runner = mock.Mock(side_effect=run)
        with mock.patch.object(sys, "argv", ["-", "resume", "mate_01"]), \
                mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(broker, "profile_exists", return_value=True), \
                mock.patch.object(broker, "secure_runtime_directory", return_value=100), \
                mock.patch.object(broker, "open_root_lock", return_value=101), \
                mock.patch.object(broker, "activate_timer", side_effect=activation_error), \
                mock.patch("pwd.getpwnam", return_value=mock.Mock(pw_gid=999)), \
                mock.patch("fcntl.flock"), mock.patch("os.close"), \
                mock.patch("os.path.lexists", return_value=False), \
                mock.patch("subprocess.run", runner), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                exec(compile(source, "<offline-cli-schedule>", "exec"), {})
            except SystemExit as exc:
                return exc.code, runner
        return 0, runner

    def test_success_records_receipt_only_after_activation(self):
        code, runner = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn("finalize_resume(True)", runner.call_args_list[0].args[0][-1])
        self.assertFalse(any("disable" in call.args[0] for call in runner.call_args_list))

    def test_activation_failure_repauses_disables_stops_and_records_failure(self):
        for options in ({"activation_error": OSError(errno.EROFS, "offline readonly")},
                        {"receipt_error": True}):
            with self.subTest(options=options):
                code, runner = self.run_script(**options)
                self.assertEqual(code, 26)
                calls = [call.args[0] for call in runner.call_args_list]
                pause = next(index for index, args in enumerate(calls) if args[-1] == "pause")
                disable = calls.index([broker.SYSTEMCTL, "disable", "--now", "fzu-checkin@mate_01.timer"])
                stop = calls.index([broker.SYSTEMCTL, "stop", "fzu-checkin@mate_01.service"])
                self.assertLess(pause, disable)
                self.assertLess(disable, stop)
                self.assertIn("finalize_resume(False)", calls[-1][-1])
                self.assertNotIn("@admin", repr(calls))

    def test_only_root_control_unit_can_write_system_timer_configuration(self):
        deploy = Path(broker.__file__).parent / "deploy"
        control = (deploy / "fzu-checkin-control.service").read_text()
        self.assertIn("User=root\n", control)
        self.assertIn("ProtectSystem=strict\n", control)
        self.assertIn("ReadWritePaths=/etc/systemd/system\n", control)
        for filename in ("fzu-checkin-web.service", "fzu-checkin@.service", "fzu-checkin-preflight@.service"):
            unit = (deploy / filename).read_text()
            self.assertIn("User=fzu-checkin\n", unit)
            self.assertNotIn("ReadWritePaths=/etc", unit)


if __name__ == "__main__":
    unittest.main()
