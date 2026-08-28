"""Cached live-state + STARTING/STOPPING overlay in core/runner_supervisor.

Verifies the fast/accurate runner controls for Phase 2:
  * the (pid -> running) verdict is cached so the 0.5s watcher doesn't shell out
  * the latched starting/stopping overlay drives logical_state()
  * start() is idempotent (never double-spawns) under the per-runner lock
  * stop() always performs a complete process-tree termination (with force-kill
    escalation for a survivor)
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import runner_supervisor
from core.runner_supervisor import ManagedRunner, RunnerSpec, RunnerSupervisor


def _spec(tmp: Path) -> RunnerSpec:
    return RunnerSpec(
        name="nyx",
        script_path=Path("/app/main.py"),
        pid_file=tmp / "nyx.pid",
        stdout_path=tmp / "out.log",
        stderr_path=tmp / "err.log",
        script_match="/app/main.py",
        process_names=["NyxBot 6.0.6.exe", "NyxBot 6.0.6"],
    )


class RunningVerdictCachingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runner = ManagedRunner(_spec(self.tmp))

    def test_running_verdict_is_cached_within_ttl(self):
        with mock.patch("core.runner_supervisor.is_pid_running", return_value=True) as irp:
            self.assertTrue(self.runner._running_verdict(555))
            self.assertTrue(self.runner._running_verdict(555))
            irp.assert_called_once()

    def test_running_verdict_refreshes_after_ttl(self):
        with mock.patch(
            "core.runner_supervisor.is_pid_running", side_effect=[True, False]
        ) as irp:
            runner_supervisor.LIVE_STATE_TTL_SECONDS = 0.0
            try:
                self.assertTrue(self.runner._running_verdict(555))
                self.assertFalse(self.runner._running_verdict(555))
                self.assertEqual(irp.call_count, 2)
            finally:
                runner_supervisor.LIVE_STATE_TTL_SECONDS = 0.75

    def test_no_pid_never_caches_as_running(self):
        self.assertFalse(self.runner._running_verdict(None))
        self.assertFalse(self.runner._running_verdict(0))


class TransitionOverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runner = ManagedRunner(_spec(self.tmp))

    def test_logical_state_reflects_transition_overlay(self):
        # No pid file -> is_running() False without shelling out.
        self.assertEqual(self.runner.logical_state(), "stopped")

        self.runner.mark_starting()
        self.assertEqual(self.runner.transition(), "starting")
        self.assertEqual(self.runner.logical_state(), "starting")

        self.runner.clear_transition()
        self.assertEqual(self.runner.logical_state(), "stopped")

        self.runner.mark_stopping()
        self.assertEqual(self.runner.transition(), "stopping")
        self.assertEqual(self.runner.logical_state(), "stopping")

        self.runner.clear_transition()
        self.assertEqual(self.runner.logical_state(), "stopped")

    def test_logical_state_running_when_pid_alive(self):
        self.runner.resolve_pid = lambda: 999
        with mock.patch("core.runner_supervisor.is_pid_running", return_value=True):
            self.runner.clear_transition()
            self.assertEqual(self.runner.logical_state(), "running")


class StartIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runner = ManagedRunner(_spec(self.tmp))
        self.runner._find_pids = lambda: []
        self.pid_file = self.tmp / "nyx.pid"

    def test_start_is_idempotent_after_first_spawn(self):
        fake_proc = SimpleNamespace(pid=4321)
        with mock.patch(
            "core.runner_supervisor.start_background_process", return_value=fake_proc
        ) as sbg, mock.patch("core.runner_supervisor.is_pid_running", return_value=True):
            pid, started = self.runner.start()
            self.assertTrue(started)
            self.assertEqual(pid, 4321)
            # Second start must adopt the live pid file, never re-spawn.
            pid2, started2 = self.runner.start()
            self.assertFalse(started2)
            self.assertEqual(pid2, 4321)
            sbg.assert_called_once()


class StopTerminationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runner = ManagedRunner(_spec(self.tmp))
        self.runner._find_pids = lambda: []
        self.runner._spawned_pids.add(7777)
        self.pid_file = self.tmp / "nyx.pid"

    def test_stop_kills_the_process_tree(self):
        self.pid_file.write_text("7777")
        with mock.patch("core.runner_supervisor.stop_process_tree") as spt, \
             mock.patch("core.runner_supervisor.force_kill_process_tree") as fkt, \
             mock.patch("core.runner_supervisor.is_pid_running", side_effect=[True, False]):
            stopped = self.runner.stop()
            self.assertTrue(stopped)
            spt.assert_called_once_with(7777)
            # No survivor -> no force-kill escalation.
            fkt.assert_not_called()
        self.assertFalse(self.pid_file.exists())

    def test_stop_escalates_to_force_kill_for_survivor(self):
        self.pid_file.write_text("7777")
        with mock.patch(
            "core.runner_supervisor.STOP_CONFIRM_TIMEOUT_SECONDS", 0.05
        ), mock.patch("core.runner_supervisor.stop_process_tree"), \
             mock.patch("core.runner_supervisor.force_kill_process_tree") as fkt, \
             mock.patch("core.runner_supervisor.is_pid_running", return_value=True):
            self.runner.stop()
            # A survivor that ignores SIGTERM/taskkill is force-killed.
            fkt.assert_called_with(7777)


class SupervisorStatusTests(unittest.TestCase):
    def test_status_includes_transition_and_state(self):
        tmp = Path(tempfile.mkdtemp())
        sup = RunnerSupervisor()
        runner = sup.register(_spec(tmp))
        runner._find_pids = lambda: []
        status = sup.status()
        self.assertIn("transition", status["nyx"])
        self.assertIn("state", status["nyx"])
        self.assertEqual(status["nyx"]["state"], "stopped")
        self.assertEqual(status["nyx"]["transition"], "idle")
        self.assertEqual(sup.transition("nyx"), "idle")
        self.assertEqual(sup.logical_state("nyx"), "stopped")


if __name__ == "__main__":
    unittest.main()
