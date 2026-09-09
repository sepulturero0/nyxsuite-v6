import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import bridge_app
from core.macos_dock import hide_macos_dock_icon


class MacDockSuppressionTests(unittest.TestCase):
    def test_hide_macos_dock_uses_activation_policy_and_process_transform(self):
        app = SimpleNamespace(setActivationPolicy_=mock.Mock())
        appkit = SimpleNamespace(
            NSApplication=SimpleNamespace(sharedApplication=mock.Mock(return_value=app)),
            NSApplicationActivationPolicyAccessory=1,
        )
        app_services = SimpleNamespace(
            GetCurrentProcess=mock.Mock(return_value=(0, "psn")),
            TransformProcessType=mock.Mock(return_value=0),
            kProcessTransformToUIElementApplication=4,
        )
        bridge = bridge_app.BridgeApp.__new__(bridge_app.BridgeApp)

        with mock.patch.object(bridge_app.sys, "platform", "darwin"), \
             mock.patch.dict(sys.modules, {
                 "AppKit": appkit,
                 "ApplicationServices": app_services,
             }):
            bridge._hide_macos_dock()

        app.setActivationPolicy_.assert_called_once_with(1)
        app_services.GetCurrentProcess.assert_called_once_with(None)
        app_services.TransformProcessType.assert_called_once_with("psn", 4)

    def test_hide_macos_dock_skips_real_pyobjc_imports_during_pytest_collection(self):
        imported = []
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name in {"ApplicationServices", "AppKit"}:
                imported.append(name)
                raise AssertionError(f"unexpected real import: {name}")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.dict(sys.modules, {"pytest": object()}, clear=False), \
             mock.patch("builtins.__import__", side_effect=fake_import):
            errors = hide_macos_dock_icon()

        self.assertEqual(errors, [])
        self.assertEqual(imported, [])

    def test_tray_run_reapplies_dock_hide_after_pystray_starts(self):
        bridge = bridge_app.BridgeApp.__new__(bridge_app.BridgeApp)
        bridge._tray_icon = None
        bridge._stop = mock.Mock()
        bridge._build_tray_icon = mock.Mock()
        bridge._hide_macos_dock = mock.Mock()
        bridge.open_dashboard = mock.Mock()

        icon = mock.Mock()
        bridge._build_tray_icon.return_value = icon

        with mock.patch.dict(bridge_app.os.environ, {}, clear=True):
            bridge.run()

        bridge._hide_macos_dock.assert_called_once()
        setup = icon.run.call_args.kwargs.get("setup")
        self.assertTrue(callable(setup))

        setup(icon)

        self.assertEqual(bridge._hide_macos_dock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
