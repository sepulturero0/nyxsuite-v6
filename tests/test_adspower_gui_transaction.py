import threading
import sys
import types
import unittest
from unittest import mock

# The source-tree test environment may not have optional runtime dependencies.
# AdsPowerManager only needs the request surface during import for these unit
# tests; no network behavior is exercised here.
if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.Session = lambda: None
    requests_stub.exceptions = types.SimpleNamespace(
        ConnectionError=ConnectionError,
        Timeout=TimeoutError,
        RequestException=Exception,
    )
    sys.modules["requests"] = requests_stub

from core.adspower import AdsPowerManager


class AdsPowerGuiTransactionTests(unittest.TestCase):
    def test_gui_launch_transaction_holds_foreground_until_cdp_confirmation(self):
        manager = AdsPowerManager.__new__(AdsPowerManager)
        manager._is_gui_control_mode = lambda: True
        events = []
        controller = mock.Mock()
        controller._a11y_enter.side_effect = lambda: events.append("foreground")
        controller._a11y_exit.side_effect = lambda: events.append("restore")
        manager._ui_controller = lambda: controller
        manager.create_profile = mock.Mock(side_effect=lambda *_args, **_kwargs: events.append("create") or {"profile_id": "p1"})
        manager.open_profile = mock.Mock(side_effect=lambda *_args: events.append("cdp_confirmed") or "ws://p1")

        with mock.patch("core.adspower_ui._GUI_LOCK", threading.RLock()):
            created, endpoint = manager.create_and_open_profile_transaction("Snapchat:", "127.0.0.1:1")

        self.assertEqual(created["profile_id"], "p1")
        self.assertEqual(endpoint, "ws://p1")
        self.assertEqual(events, ["foreground", "create", "cdp_confirmed", "restore"])

    def test_non_gui_launch_transaction_keeps_existing_create_path(self):
        manager = AdsPowerManager.__new__(AdsPowerManager)
        manager._is_gui_control_mode = lambda: False
        manager.create_profile = mock.Mock(return_value={"profile_id": "api1"})

        created, endpoint = manager.create_and_open_profile_transaction("Snapchat:", "127.0.0.1:1")

        self.assertEqual(created["profile_id"], "api1")
        self.assertEqual(endpoint, "")
        manager.create_profile.assert_called_once()
