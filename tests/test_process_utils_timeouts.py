"""Bounded subprocess behaviour in core/process_utils.

Every PowerShell / tasklist / ps call must carry a timeout so a hung Windows
process manager (or a slow machine) can never block the bridge watcher or a
Start/Stop action indefinitely. A timed-out probe must fail **closed** — return
empty/False, never a false-positive "running"/match.
"""
import subprocess
import unittest
from unittest import mock

from core import process_utils
from core.process_utils import SUBPROCESS_TIMEOUT_SECONDS


class WindowsProcessCallsCarryTimeout(unittest.TestCase):
    """The costly CIM/tasklist calls must all be time-bounded."""

    def _assert_timeout(self, fn, **call_kwargs):
        with mock.patch.object(
            process_utils.os, "name", "nt"
        ), mock.patch.object(process_utils, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )
            fn(**call_kwargs)
            self.assertEqual(
                mock_subprocess.run.call_args.kwargs.get("timeout"),
                SUBPROCESS_TIMEOUT_SECONDS,
            )

    def test_find_python_process_ids_windows_is_bounded(self):
        self._assert_timeout(process_utils.find_python_process_ids, script_name="main.py")

    def test_find_process_ids_by_names_windows_is_bounded(self):
        self._assert_timeout(
            process_utils.find_process_ids_by_names, process_names=["NyxBot 6.0.6.exe"]
        )

    def test_is_pid_running_windows_is_bounded(self):
        with mock.patch.object(
            process_utils.os, "name", "nt"
        ), mock.patch.object(process_utils, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="INFO: 1234 nyx.exe", stderr=""
            )
            process_utils.is_pid_running(1234)
            self.assertEqual(
                mock_subprocess.run.call_args.kwargs.get("timeout"),
                SUBPROCESS_TIMEOUT_SECONDS,
            )

    def test_stop_process_tree_windows_is_bounded(self):
        with mock.patch.object(
            process_utils.os, "name", "nt"
        ), mock.patch.object(process_utils, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            process_utils.stop_process_tree(1234)
            self.assertEqual(
                mock_subprocess.run.call_args.kwargs.get("timeout"),
                SUBPROCESS_TIMEOUT_SECONDS,
            )

    def test_process_identity_windows_is_bounded(self):
        self._assert_timeout(process_utils._process_identity, pid=1234)


class PosixProcessCallsCarryTimeout(unittest.TestCase):
    def test_find_python_process_ids_posix_is_bounded(self):
        with mock.patch.object(process_utils, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            process_utils.find_python_process_ids("main.py")
            self.assertEqual(
                mock_subprocess.run.call_args.kwargs.get("timeout"),
                SUBPROCESS_TIMEOUT_SECONDS,
            )

    def test_process_identity_posix_is_bounded(self):
        with mock.patch.object(process_utils, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            process_utils._process_identity(1234)
            self.assertEqual(
                mock_subprocess.run.call_args.kwargs.get("timeout"),
                SUBPROCESS_TIMEOUT_SECONDS,
            )


class TimedOutProbeFailsClosed(unittest.TestCase):
    def test_is_pid_running_times_out_to_false(self):
        with mock.patch.object(
            process_utils.os, "name", "nt"
        ), mock.patch.object(process_utils, "subprocess") as mock_subprocess:
            mock_subprocess.run.side_effect = subprocess.TimeoutExpired(cmd="ps", timeout=8)
            self.assertFalse(process_utils.is_pid_running(1234))

    def test_find_python_process_ids_times_out_to_empty(self):
        with mock.patch.object(process_utils, "subprocess") as mock_subprocess:
            mock_subprocess.run.side_effect = subprocess.TimeoutExpired(cmd="ps", timeout=8)
            self.assertEqual(process_utils.find_python_process_ids("main.py"), [])

    def test_process_identity_times_out_closed(self):
        with mock.patch.object(
            process_utils.os, "name", "nt"
        ), mock.patch.object(process_utils, "subprocess") as mock_subprocess:
            mock_subprocess.run.side_effect = subprocess.TimeoutExpired(cmd="ps", timeout=8)
            self.assertEqual(process_utils._process_identity(1234), ("", ""))

    def test_stop_process_tree_times_out_reports_unchanged(self):
        with mock.patch.object(
            process_utils.os, "name", "nt"
        ), mock.patch.object(process_utils, "subprocess") as mock_subprocess:
            mock_subprocess.run.side_effect = subprocess.TimeoutExpired(cmd="taskkill", timeout=8)
            # Fails closed: a taskkill that never returns is reported as "did not
            # stop", so the supervisor escalates to force_kill.
            self.assertFalse(process_utils.stop_process_tree(1234))


if __name__ == "__main__":
    unittest.main()
