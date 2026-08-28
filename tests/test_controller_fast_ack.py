"""Fast action acknowledgements for Phase 2.

A Start/Stop must acknowledge quickly. The HTTP ack must NOT build the expensive
full snapshot (500 rows + AdsPower annotations) — it returns a light status (bot
+ counts) with the latched STARTING/STOPPING state, and the SSE watcher confirms
the final state. A per-product lock serialises the action so a double click can't
race a spawn against a kill.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.nyx_controller import NyxController
from core.task_store import TaskStore


class _FakeRunner:
    def __init__(self):
        self._transition = "idle"

    def mark_starting(self):
        self._transition = "starting"

    def mark_stopping(self):
        self._transition = "stopping"

    def clear_transition(self):
        self._transition = "idle"

    def transition(self):
        return self._transition

    def resolve_pid(self):
        return 999 if self._transition in ("starting", "stopping") else 999

    def is_running(self):
        return True

    def logical_state(self):
        return self._transition if self._transition in ("starting", "stopping") else "running"


class _FakeSupervisor:
    def __init__(self, runner):
        self._runner = runner

    def register(self, _spec):
        return self._runner

    def start(self, _name, **_kwargs):
        return 999, True

    def stop(self, _name):
        return True

    def is_running(self, _name):
        return self._runner.is_running()

    def transition(self, _name):
        return self._runner.transition()

    def logical_state(self, _name):
        return self._runner.logical_state()


class ControllerFastAckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(db_path=str(Path(self.tmp.name) / "tasks.db"))
        self.runner = _FakeRunner()
        self.supervisor = _FakeSupervisor(self.runner)
        self.controller = NyxController(
            self.supervisor, store=self.store, adspower=object()
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_ack_is_light_and_shows_starting(self):
        result = self.controller.start({})
        self.assertTrue(result["ok"])
        self.assertTrue(result["started"])
        status = result["status"]
        # The ack does not ship the full row table.
        self.assertNotIn("rows", status)
        self.assertIn("bot", status)
        self.assertIn("counts", status)
        # The latched transition is reported immediately.
        self.assertEqual(status["bot"]["state"], "starting")

    def test_stop_ack_is_light_and_shows_stopping(self):
        result = self.controller.stop({})
        self.assertTrue(result["ok"])
        self.assertTrue(result["stopped"])
        status = result["status"]
        self.assertNotIn("rows", status)
        self.assertEqual(status["bot"]["state"], "stopping")

    def test_action_lock_is_reentrant_for_finish_remaining(self):
        # finish_remaining() calls start(); the RLock must not deadlock.
        result = self.controller.finish_remaining({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"]["bot"]["state"], "starting")

    def test_reset_stuck_ack_is_light(self):
        result = self.controller.action_handlers()["reset_stuck"]({})
        self.assertNotIn("rows", result["status"])
        self.assertIn("bot", result["status"])

    def test_clear_completed_ack_is_light(self):
        result = self.controller.action_handlers()["clear_completed"]({})
        self.assertNotIn("rows", result["status"])


if __name__ == "__main__":
    unittest.main()
