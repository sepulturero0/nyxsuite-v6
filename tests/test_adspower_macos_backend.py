import unittest
from types import SimpleNamespace
from unittest import mock


class MacBackendSelectionTests(unittest.TestCase):
    def test_controller_uses_macos_backend_on_darwin(self):
        import core.adspower_ui as aui

        fake_backend = mock.Mock()
        fake_backend.connect.return_value = mock.Mock()

        with mock.patch.object(aui.sys, "platform", "darwin"), \
             mock.patch("core.adspower_ui_backend_macos.MacOSAdsPowerBackend",
                        return_value=fake_backend):
            ctrl = aui.AdsPowerUIController()

        self.assertIs(ctrl._backend, fake_backend)
        fake_backend.assert_not_called()

    def test_missing_accessibility_permission_is_actionable(self):
        from core.adspower_ui_backend_macos import MacOSAccessibilityPermissionError

        message = str(MacOSAccessibilityPermissionError("Codex"))
        self.assertIn("Accessibility", message)
        self.assertIn("Privacy & Security", message)
        self.assertIn("Codex", message)

    def test_missing_accessibility_permission_mentions_launcher_case(self):
        from core.adspower_ui_backend_macos import MacOSAccessibilityPermissionError

        message = str(MacOSAccessibilityPermissionError("Python"))
        self.assertIn("Python", message)
        self.assertIn("Google Chrome", message)
        self.assertIn("restart Nyx Suite", message)

    def test_backend_prompts_for_accessibility_before_raising(self):
        import core.adspower_ui_backend_macos as macos_backend
        from core.adspower_ui_backend_macos import (
            MacOSAccessibilityPermissionError,
            MacOSAdsPowerBackend,
        )

        fake_as = mock.Mock()
        fake_as.kAXTrustedCheckOptionPrompt = "AXTrustedCheckOptionPrompt"
        fake_as.AXIsProcessTrustedWithOptions.return_value = False

        with mock.patch.object(MacOSAdsPowerBackend, "_load_frameworks", lambda self: setattr(self, "_as", fake_as)), \
             mock.patch.object(MacOSAdsPowerBackend, "_accessibility_app_name", return_value="Python"), \
             mock.patch.object(macos_backend.sys, "platform", "darwin"):
            with self.assertRaises(MacOSAccessibilityPermissionError):
                MacOSAdsPowerBackend()

        fake_as.AXIsProcessTrustedWithOptions.assert_called_once_with(
            {"AXTrustedCheckOptionPrompt": True}
        )

    def test_accessibility_app_name_uses_current_python_bundle(self):
        from core.adspower_ui_backend_macos import MacOSAdsPowerBackend

        backend = MacOSAdsPowerBackend.__new__(MacOSAdsPowerBackend)

        with mock.patch(
            "core.adspower_ui_backend_macos.sys.executable",
            "/opt/homebrew/Cellar/python@3.14/3.14.5/Frameworks/"
            "Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python",
        ):
            self.assertEqual(backend._accessibility_app_name(), "Python")


class MacSmokePreflightTests(unittest.TestCase):
    def test_non_macos_has_no_preflight_message(self):
        from tools.test_adspower_ui_profile import _macos_preflight_message

        self.assertIsNone(
            _macos_preflight_message(
                platform="linux",
                import_check=lambda _name: False,
                ax_trusted=lambda: False,
            )
        )

    def test_missing_pyobjc_is_reported_before_live_test(self):
        from tools.test_adspower_ui_profile import _macos_preflight_message

        message = _macos_preflight_message(
            platform="darwin",
            import_check=lambda _name: False,
            ax_trusted=lambda: True,
        )

        self.assertIn("PyObjC", message)
        self.assertIn("requirements.txt", message)

    def test_missing_accessibility_is_reported_before_live_test(self):
        from tools.test_adspower_ui_profile import _macos_preflight_message

        message = _macos_preflight_message(
            platform="darwin",
            import_check=lambda _name: True,
            ax_trusted=lambda: False,
        )

        self.assertIn("Accessibility", message)
        self.assertIn("Privacy & Security", message)


class MacBackendAdapterTests(unittest.TestCase):
    def _backend(self):
        from core.adspower_ui_backend_macos import MacOSAdsPowerBackend

        backend = MacOSAdsPowerBackend.__new__(MacOSAdsPowerBackend)
        backend._ax = None
        return backend

    def test_control_type_mapping_matches_existing_controller_names(self):
        from core.adspower_ui_backend_macos import ax_role_to_control_type

        self.assertEqual(ax_role_to_control_type("AXButton"), "Button")
        self.assertEqual(ax_role_to_control_type("AXStaticText"), "Text")
        self.assertEqual(ax_role_to_control_type("AXTextField"), "Edit")
        self.assertEqual(ax_role_to_control_type("AXCheckBox"), "CheckBox")
        self.assertEqual(ax_role_to_control_type("AXGroup"), "Pane")

    def test_rect_adapter_exposes_pywinauto_shape(self):
        from core.adspower_ui_backend_macos import Rect

        rect = Rect(10, 20, 110, 70)

        self.assertEqual((rect.left, rect.top, rect.right, rect.bottom), (10, 20, 110, 70))
        self.assertEqual(rect.width(), 100)
        self.assertEqual(rect.height(), 50)

    def test_descendants_filters_by_existing_control_type_names(self):
        from core.adspower_ui_backend_macos import MacOSControl

        backend = self._backend()
        root = MacOSControl(
            backend,
            element="root",
            role="AXWindow",
            title="AdsPower Global",
            rect=SimpleNamespace(left=0, top=0, right=500, bottom=500),
            children=[
                MacOSControl(backend, "button", "AXButton", "Open"),
                MacOSControl(backend, "text", "AXStaticText", "No./ID"),
                MacOSControl(backend, "edit", "AXTextField", "Search or new search criteria"),
            ],
        )

        self.assertEqual([c.window_text() for c in root.descendants("Button")], ["Open"])
        self.assertEqual([c.window_text() for c in root.descendants("Text")], ["No./ID"])
        self.assertEqual(
            [c.window_text() for c in root.descendants("Edit")],
            ["Search or new search criteria"],
        )

    def test_descendants_ignores_cycles_in_accessibility_tree(self):
        from core.adspower_ui_backend_macos import MacOSControl

        backend = self._backend()
        root = MacOSControl(backend, "root", "AXWindow", "AdsPower Global")
        child = MacOSControl(backend, "child", "AXButton", "Open")
        root._static_children = [child]
        child._static_children = [root]

        self.assertEqual([c.window_text() for c in root.descendants("Button")], ["Open"])

    def test_child_window_returns_spec_with_exists_and_control(self):
        from core.adspower_ui_backend_macos import MacOSControl

        backend = self._backend()
        root = MacOSControl(
            backend,
            element="root",
            role="AXWindow",
            title="AdsPower Global",
            children=[
                MacOSControl(backend, "button", "AXButton", "Open"),
            ],
        )

        spec = root.child_window(title="Open", control_type="Button")

        self.assertTrue(spec.exists(timeout=0))
        self.assertEqual(spec.window_text(), "Open")
        self.assertEqual(spec.rectangle().width(), 1)

    def test_invoke_uses_backend_perform_action(self):
        from core.adspower_ui_backend_macos import MacOSControl

        backend = self._backend()
        backend.perform_action = mock.Mock(return_value=True)
        ctrl = MacOSControl(backend, "button", "AXButton", "Open")

        ctrl.invoke()

        backend.perform_action.assert_called_once_with("button", "AXPress")

    def test_children_include_navigation_order_nodes(self):
        from core.adspower_ui_backend_macos import MacOSControl

        backend = self._backend()
        child = MacOSControl(backend, "nav-child", "AXButton", "OK")

        def fake_attr(element, name):
            if element == "root" and name == "AXChildren":
                return []
            if element == "root" and name == "AXChildrenInNavigationOrder":
                return ["nav-child"]
            return None

        backend.attr = mock.Mock(side_effect=fake_attr)
        backend.wrap = mock.Mock(return_value=child)
        root = MacOSControl(backend, "root", "AXWindow", "AdsPower")

        self.assertEqual([c.window_text() for c in root.descendants("Button")], ["OK"])

    def test_placeholder_value_is_used_as_edit_text(self):
        from core.adspower_ui_backend_macos import MacOSControl

        backend = self._backend()

        def fake_attr(_element, name):
            if name == "AXPlaceholderValue":
                return "Optional: profile name"
            return None

        backend.attr = mock.Mock(side_effect=fake_attr)
        ctrl = MacOSControl(backend, "edit", "AXTextField", "")

        self.assertEqual(ctrl.window_text(), "Optional: profile name")

    def test_wrap_does_not_eagerly_read_text_attributes(self):
        from core.adspower_ui_backend_macos import MacOSAdsPowerBackend

        backend = self._backend()

        def fake_attr(_element, name):
            if name == "AXRole":
                return "AXButton"
            if name in ("AXTitle", "AXValue", "AXDescription", "AXHelp", "AXPlaceholderValue"):
                raise AssertionError("wrap must not read text eagerly")
            return None

        backend.attr = mock.Mock(side_effect=fake_attr)

        ctrl = MacOSAdsPowerBackend.wrap(backend, "button")

        self.assertEqual(ctrl.control_type(), "Button")


class MacBackendConnectionSpeedTests(unittest.TestCase):
    def test_connect_reuses_valid_cached_window_without_foreground_sleep(self):
        from core.adspower_ui_backend_macos import MacOSAdsPowerBackend, Rect

        backend = MacOSAdsPowerBackend.__new__(MacOSAdsPowerBackend)
        backend._attr_cache = {"stale": "value"}
        backend._app = object()
        backend._app_ref = object()
        backend._window = object()
        backend.window_id = 123
        backend.attr = mock.Mock(return_value=False)
        backend.element_rect = mock.Mock(return_value=Rect(0, 0, 1200, 800))
        backend._frontmost_app_name = mock.Mock(return_value="AdsPower Global")
        backend._find_adspower_app = mock.Mock(
            side_effect=AssertionError("cached connect should not rediscover app")
        )
        backend.foreground = mock.Mock(
            side_effect=AssertionError("cached frontmost connect should not foreground")
        )
        backend.wrap = mock.Mock(return_value="wrapped-window")

        self.assertEqual(backend.connect(), "wrapped-window")

        self.assertEqual(backend._attr_cache, {})
        backend.wrap.assert_called_once_with(backend._window)
        backend._find_adspower_app.assert_not_called()
        backend.foreground.assert_not_called()


class MacBackendFocusTests(unittest.TestCase):
    def _backend(self):
        from core.adspower_ui_backend_macos import MacOSAdsPowerBackend

        return MacOSAdsPowerBackend.__new__(MacOSAdsPowerBackend)

    def test_profile_browser_does_not_count_as_global_dashboard(self):
        backend = self._backend()

        backend._frontmost_app_name = mock.Mock(return_value="AdsPower Browser | k1profile")
        self.assertFalse(backend._frontmost_is_adspower())

        backend._frontmost_app_name.return_value = "AdsPower Global"
        self.assertTrue(backend._frontmost_is_adspower())

    def test_cached_minimized_window_is_not_reused(self):
        from core.adspower_ui_backend_macos import Rect

        backend = self._backend()
        backend._app = object()
        backend._app_ref = object()
        backend._window = object()
        backend.attr = mock.Mock(side_effect=lambda _element, name: name == "AXMinimized")
        backend.element_rect = mock.Mock(return_value=Rect(0, 0, 1200, 800))

        self.assertFalse(backend._cached_window_is_usable())

    def test_cached_window_is_not_reused_when_process_identifier_changes(self):
        from core.adspower_ui_backend_macos import MacOSAdsPowerBackend, Rect

        backend = MacOSAdsPowerBackend.__new__(MacOSAdsPowerBackend)
        backend._app = mock.Mock()
        backend._app.processIdentifier.return_value = 111
        backend._app_ref = object()
        backend._window = object()
        backend._app_pid = 222
        backend.attr = mock.Mock(return_value=False)
        backend.element_rect = mock.Mock(return_value=Rect(0, 0, 1200, 800))

        self.assertFalse(backend._cached_window_is_usable())


if __name__ == "__main__":
    unittest.main()
