"""Bulk-open / bulk-close coalescing + bulk-search query formatting for the
no-API GUI path.

These cover the pure logic of ``core.adspower_ui._GuiBatcher`` and
``_search_by_ids`` without driving a real AdsPower window:

* ``_search_by_ids`` joins the ids with spaces and applies the ``Profile ID``/
  ``is`` filter (the native bulk-ID search).
* concurrent opens/closes coalesce into ONE bulk search (the leader drains every
  other waiting caller) and each caller's result resolves independently, with
  the action's return value (close's bool) carried back to that caller.

``_GUI_LOCK`` is patched to a plain re-entrant lock so the test never contends
with a real running suite's named mutex.
"""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import core.adspower_ui as aui
from core.adspower_ui import (
    AdsPowerUIController,
    AdsPowerUIError,
    _BatchResult,
    _GuiBatcher,
)


class _FakeController:
    """Stands in for AdsPowerUIController: records each bulk batch and resolves
    every id's result the way the real ``_bulk_*_locked`` methods do."""

    def __init__(self, fail_ids=()):
        self.batches = []
        self._fail = set(fail_ids)
        self._lock = threading.Lock()

    def _bulk_open_locked(self, ids, results):
        self._record(ids, results, value=None)

    def _bulk_close_locked(self, ids, results):
        self._record(ids, results, value=True)   # close returns a bool

    def _record(self, ids, results, value):
        with self._lock:
            self.batches.append(list(ids))
        for pid in ids:
            res = results[pid]
            if pid in self._fail:
                res.ok, res.error = False, RuntimeError(f"boom {pid}")
            else:
                res.ok, res.value = True, value
            res.event.set()


class SearchByIdsTests(unittest.TestCase):
    def _bare_controller(self):
        # __new__ bypasses __init__ (which requires pywinauto) — we only exercise
        # _search_by_ids, stubbing the _search_by it delegates to.
        return AdsPowerUIController.__new__(AdsPowerUIController)

    def _no_chip(self):
        return None

    def _noop_reset(self, *a, **kw):
        pass

    def test_builds_space_joined_profile_id_query(self):
        ctrl = self._bare_controller()
        ctrl._profile_id_chip = self._no_chip       # no chip present
        ctrl._search_by_ids_via_chip = lambda query: False   # no chip -> full search
        ctrl._reset_search = self._noop_reset
        calls = []
        ctrl._search_by = lambda value, field, operator, **kwargs: calls.append(
            (value, field, operator, kwargs))

        ctrl._search_by_ids(["k1a", " k1b ", "", "k1c"])

        self.assertEqual(calls, [("k1a k1b k1c", "Profile ID", "is", {"verify": True})])

    def test_single_id_is_a_one_element_bulk_search(self):
        ctrl = self._bare_controller()
        ctrl._profile_id_chip = self._no_chip
        ctrl._search_by_ids_via_chip = lambda query: False
        ctrl._reset_search = self._noop_reset
        calls = []
        ctrl._search_by = lambda value, field, operator, **kwargs: calls.append(
            (value, field, operator, kwargs))

        ctrl._search_by_ids(["k1solo"])

        self.assertEqual(calls, [("k1solo", "Profile ID", "is", {"verify": True})])

    def test_requires_at_least_one_id(self):
        ctrl = self._bare_controller()
        ctrl._search_by = lambda *a, **k: None
        with self.assertRaises(AdsPowerUIError):
            ctrl._search_by_ids(["", "   "])

    def test_chip_fast_path_skips_full_search(self):
        # When the inline chip edit succeeds, the slow Reset + dropdown search is
        # never run.
        ctrl = self._bare_controller()
        ctrl._profile_id_chip = self._no_chip
        ctrl._search_by_ids_via_chip = lambda query: True
        ctrl._search_by = lambda *a, **k: self.fail("full search should be skipped")

        ctrl._search_by_ids(["k1a", "k1b"])

    def test_chip_failure_falls_back_to_full_search(self):
        # If the chip edit raises, _search_by_ids must still complete via the full
        # search rather than propagating the error.
        ctrl = self._bare_controller()
        ctrl._profile_id_chip = self._no_chip
        ctrl._reset_search = self._noop_reset

        def boom(query):
            raise RuntimeError("editor not found")

        ctrl._search_by_ids_via_chip = boom
        calls = []
        ctrl._search_by = lambda value, field, operator, **kwargs: calls.append(
            (value, field, operator, kwargs))

        ctrl._search_by_ids(["k1a", "k1b"])

        self.assertEqual(calls, [("k1a k1b", "Profile ID", "is", {"verify": True})])

    def test_append_mode_merges_with_existing_chip(self):
        ctrl = self._bare_controller()
        # Simulate an existing chip with 'old1 old2'
        ctrl._profile_id_chip = lambda: (_Rect(460, 178, 720, 200), "Profile ID is old1 old2")
        ctrl._search_by_ids_via_chip = lambda query: True
        ctrl._search_by = lambda *a, **k: self.fail("full search should be skipped")
        # Call with new IDs — append mode should merge them
        ctrl._search_by_ids(["new1"], append=True)

    def test_append_mode_skips_merge_when_append_false(self):
        ctrl = self._bare_controller()
        ctrl._profile_id_chip = lambda: (_Rect(460, 178, 720, 200), "Profile ID is old1 old2")
        ctrl._search_by_ids_via_chip = lambda query: True
        ctrl._search_by = lambda *a, **k: self.fail("full search should be skipped")
        # With append=False, should NOT merge — just pass new IDs directly
        ctrl._search_by_ids(["new1"], append=False)


class DashboardRefreshTests(unittest.TestCase):
    def test_windows_refresh_falls_back_to_control_shift_r(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._backend = None
        ctrl._hwnd = None
        ctrl._app = None
        ctrl._connect = mock.Mock()
        ctrl._minimize_overlapping_browsers = mock.Mock()
        events = []
        fake_pg = SimpleNamespace(
            hotkey=lambda *args: events.append(("hotkey", args)),
        )

        with mock.patch.object(aui.sys, "platform", "win32"), \
             mock.patch.object(aui, "_pg", lambda: fake_pg), \
             mock.patch.object(aui.win_focus, "ensure_foreground", side_effect=lambda *_args, **_kwargs: events.append("foreground") or 1234), \
             mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
            self.assertTrue(ctrl._refresh_dashboard())

        self.assertIn("foreground", events)
        self.assertIn(("hotkey", ("ctrl", "shift", "r")), events)
        ctrl._connect.assert_called()

    def test_macos_refresh_uses_command_shift_r_if_backend_menu_fails(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._backend = SimpleNamespace(
            refresh_window=lambda: False,
            foreground=lambda **_kwargs: None,
        )
        ctrl._connect = mock.Mock()
        events = []

        with mock.patch.object(aui.sys, "platform", "darwin"), \
             mock.patch.object(aui, "_send_macos_system_events", side_effect=lambda action: events.append(action) or True), \
             mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
            self.assertTrue(ctrl._refresh_dashboard())

        self.assertIn('keystroke "r" using {command down, shift down}', events)
        ctrl._connect.assert_called()


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class _FakeControl:
    def __init__(self, text, rect, visible=True):
        self._text = text
        self._rect = rect
        self._visible = visible

    def window_text(self):
        return self._text

    def is_visible(self):
        return self._visible

    def rectangle(self):
        return self._rect


class _FakeWindow:
    def __init__(self, texts=(), buttons=(), checkboxes=(), edits=()):
        self._texts = list(texts)
        self._buttons = list(buttons)
        self._checkboxes = list(checkboxes)
        self._edits = list(edits)

    def descendants(self, control_type=None):
        if control_type == "Text":
            return list(self._texts)
        if control_type == "Button":
            return list(self._buttons)
        if control_type == "CheckBox":
            return list(self._checkboxes)
        if control_type == "Edit":
            return list(self._edits)
        return [*self._texts, *self._buttons, *self._checkboxes, *self._edits]


class RowActionClickTests(unittest.TestCase):
    def _controller(self, win):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._win = win
        ctrl._connect = lambda: None
        ctrl.clicked = []
        ctrl._click_rect = lambda rect, template_name="": ctrl.clicked.append((rect, template_name))
        ctrl._click_xy = lambda x, y: ctrl.clicked.append(((x, y), "xy"))
        return ctrl

    def _headers(self):
        return [
            _FakeControl("No./ID", _Rect(80, 170, 150, 194)),
            _FakeControl("Name", _Rect(260, 170, 320, 194)),
            _FakeControl("Action", _Rect(900, 170, 980, 194)),
        ]

    def test_click_row_action_clicks_button_aligned_to_exact_profile_id(self):
        target_button = _Rect(910, 231, 970, 257)
        other_button = _Rect(910, 271, 970, 297)
        win = _FakeWindow(
            texts=[
                *self._headers(),
                _FakeControl("k1target", _Rect(90, 232, 155, 252)),
                _FakeControl("k1other", _Rect(90, 272, 155, 292)),
            ],
            buttons=[
                _FakeControl("Open", _Rect(420, 120, 480, 146)),  # toolbar, ignored
                _FakeControl("Open", other_button),
                _FakeControl("Open", target_button),
            ],
        )
        ctrl = self._controller(win)

        ctrl._click_row_action("k1target", "Open", template_name="open_btn")

        self.assertEqual(ctrl.clicked, [(target_button, "open_btn")])

    def test_click_row_action_refuses_unaligned_visible_button_by_default(self):
        visible_button = _Rect(910, 231, 970, 257)
        win = _FakeWindow(
            texts=[*self._headers(), _FakeControl("k1other", _Rect(90, 232, 155, 252))],
            buttons=[_FakeControl("Open", visible_button)],
        )
        ctrl = self._controller(win)

        with mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
            with self.assertRaises(AdsPowerUIError):
                ctrl._click_row_action("k1missing", "Open")

        self.assertEqual(ctrl.clicked, [])

    def test_dropdown_row_accepts_profile_no_id_alias(self):
        win = _FakeWindow(
            texts=[
                _FakeControl("Profile No./ID", _Rect(500, 260, 610, 282)),
                _FakeControl("is", _Rect(640, 260, 660, 282)),
                _FakeControl("k1target", _Rect(690, 260, 760, 282)),
            ]
        )
        ctrl = self._controller(win)

        self.assertTrue(ctrl._click_dropdown_row("Profile ID", "is", below_top=200, left_min=450))

    def test_scan_rows_supports_new_separate_id_and_order_columns(self):
        win = _FakeWindow(
            texts=[
                _FakeControl("ID", _Rect(90, 170, 150, 194)),
                _FakeControl("Group", _Rect(220, 170, 290, 194)),
                _FakeControl("Name", _Rect(360, 170, 430, 194)),
                _FakeControl("IP", _Rect(520, 170, 590, 194)),
                _FakeControl("#", _Rect(700, 170, 730, 194)),
                _FakeControl("k1target", _Rect(90, 232, 155, 252)),
                _FakeControl("Snapchat19", _Rect(220, 230, 300, 252)),
                _FakeControl("Snapchat: Olivia", _Rect(360, 230, 470, 252)),
                _FakeControl("78.105.159.107", _Rect(520, 230, 620, 252)),
                _FakeControl("1", _Rect(700, 230, 715, 252)),
            ]
        )
        ctrl = self._controller(win)

        self.assertEqual(ctrl._scan_rows(), [(0, "k1target", "Snapchat: Olivia")])

    def test_scan_rows_ignores_reordered_optional_chronology_columns(self):
        win = _FakeWindow(
            texts=[
                _FakeControl("#", _Rect(90, 170, 120, 194)),
                _FakeControl("Date created", _Rect(160, 170, 250, 194)),
                _FakeControl("IP", _Rect(300, 170, 360, 194)),
                _FakeControl("ID", _Rect(420, 170, 480, 194)),
                _FakeControl("Name", _Rect(560, 170, 620, 194)),
                _FakeControl("7", _Rect(90, 232, 105, 252)),
                _FakeControl("07-15 17:00:46", _Rect(160, 230, 260, 252)),
                _FakeControl("78.105.159.107", _Rect(300, 230, 400, 252)),
                _FakeControl("k1target", _Rect(420, 232, 485, 252)),
                _FakeControl("Snapchat: Olivia", _Rect(560, 230, 670, 252)),
            ]
        )
        ctrl = self._controller(win)

        self.assertEqual(ctrl._scan_rows(), [(0, "k1target", "Snapchat: Olivia")])

    def test_scan_rows_supports_legacy_no_id_without_trusting_number(self):
        win = _FakeWindow(
            texts=[
                _FakeControl("No./ID", _Rect(90, 170, 150, 194)),
                _FakeControl("Group", _Rect(220, 170, 290, 194)),
                _FakeControl("Name", _Rect(360, 170, 430, 194)),
                _FakeControl("703704", _Rect(90, 230, 150, 250)),
                _FakeControl("k1target", _Rect(90, 249, 155, 269)),
                _FakeControl("Snapchat19", _Rect(220, 240, 300, 262)),
                _FakeControl("Snapchat: Olivia", _Rect(360, 240, 470, 262)),
            ]
        )
        ctrl = self._controller(win)

        self.assertEqual(ctrl._scan_rows(), [(0, "k1target", "Snapchat: Olivia")])

    def test_missing_visible_id_error_mentions_enabling_id_column(self):
        win = _FakeWindow(
            texts=[
                _FakeControl("Group", _Rect(220, 170, 290, 194)),
                _FakeControl("Name", _Rect(360, 170, 430, 194)),
                _FakeControl("IP", _Rect(520, 170, 590, 194)),
                _FakeControl("Snapchat19", _Rect(220, 230, 300, 252)),
                _FakeControl("Snapchat: Olivia", _Rect(360, 230, 470, 252)),
                _FakeControl("78.105.159.107", _Rect(520, 230, 620, 252)),
            ],
            buttons=[_FakeControl("Open", _Rect(900, 231, 970, 257))],
        )
        ctrl = self._controller(win)

        with mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
            with self.assertRaises(aui.AdsPowerUIError) as captured:
                ctrl._click_row_action("k1target", "Open")

        self.assertIn("enable the ID column", str(captured.exception))

    def test_dropdown_row_rejects_stale_value_when_expected_value_given(self):
        win = _FakeWindow(
            texts=[
                _FakeControl("Name", _Rect(500, 260, 540, 282)),
                _FakeControl("contains", _Rect(550, 260, 620, 282)),
                _FakeControl("xoxoxo", _Rect(630, 260, 700, 282)),
            ]
        )
        ctrl = self._controller(win)

        self.assertFalse(
            ctrl._click_dropdown_row(
                "Name",
                "contains",
                below_top=200,
                left_min=450,
                value="Pending",
            )
        )
        self.assertEqual(ctrl.clicked, [])

    def test_group_dropdown_picks_exact_match_not_prefix(self):
        # Selecting the configured group must click the EXACT option, never a
        # longer neighbour ('Snapchat2' must not select 'Snapchat20').
        win = _FakeWindow(texts=[
            _FakeControl("Snapchat20", _Rect(200, 430, 400, 452)),
            _FakeControl("Snapchat2", _Rect(200, 460, 400, 482)),
        ])
        ctrl = self._controller(win)
        self.assertTrue(ctrl._click_dropdown_option("snapchat2", below_top=400))
        self.assertEqual(ctrl.clicked, [((300, 471), "xy")])   # centre of Snapchat2

    def test_group_dropdown_matches_case_insensitively(self):
        win = _FakeWindow(texts=[_FakeControl("Snapchat20", _Rect(200, 430, 400, 452))])
        ctrl = self._controller(win)
        self.assertTrue(ctrl._click_dropdown_option("SNAPCHAT20", below_top=400))
        self.assertEqual(ctrl.clicked, [((300, 441), "xy")])

    def test_group_dropdown_no_match_returns_false(self):
        win = _FakeWindow(texts=[_FakeControl("Snapchat19", _Rect(200, 430, 400, 452))])
        ctrl = self._controller(win)
        self.assertFalse(ctrl._click_dropdown_option("Snapchat20", below_top=400))
        self.assertEqual(ctrl.clicked, [])


class RenameDialogFallbackTests(unittest.TestCase):
    def test_open_rename_dialog_accepts_name_modal_without_enter_name_placeholder(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._connect = lambda: None
        ctrl._row_center_y = lambda _pid: 240
        ctrl._name_cell_rect = lambda _row_y: _Rect(520, 230, 610, 250)
        ctrl._click_xy = mock.Mock()
        ctrl._click_row_menu_rename = mock.Mock(return_value=False)

        def find_dialog(title, _control_type, timeout=0, retry=False):
            return object() if title == "OK" else None

        ctrl._find = mock.Mock(side_effect=find_dialog)

        with mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
            self.assertTrue(ctrl._open_rename_dialog("k1target"))

        ctrl._click_xy.assert_called()
        ctrl._click_row_menu_rename.assert_not_called()

    def test_open_rename_dialog_uses_row_menu_when_pencil_click_fails(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._connect = lambda: None
        ctrl._row_center_y = lambda _pid: 240
        ctrl._name_cell_rect = lambda _row_y: _Rect(520, 230, 610, 250)
        ctrl._click_xy = mock.Mock()

        menu_clicked = {"value": False}

        def click_menu(profile_id):
            menu_clicked["value"] = True
            return True

        ctrl._click_row_menu_rename = mock.Mock(side_effect=click_menu)

        def find_dialog(_title, _control_type, timeout=0, retry=False):
            return object() if menu_clicked["value"] else None

        ctrl._find = mock.Mock(side_effect=find_dialog)

        with mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
            self.assertTrue(ctrl._open_rename_dialog("k1target"))

        ctrl._click_xy.assert_called()
        ctrl._click_row_menu_rename.assert_called_once_with("k1target")

    def test_rename_profile_uses_full_edit_form_when_inline_dialog_is_absent(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._connect = mock.Mock()
        ctrl._ensure_row_visible = mock.Mock()
        ctrl._open_rename_dialog = mock.Mock(return_value=True)
        ctrl._rect = mock.Mock(return_value=None)
        ctrl._fill_name = mock.Mock()
        ctrl._click_ok = mock.Mock()
        ctrl._rename_confirmed_or_absent = mock.Mock(return_value=True)

        result = ctrl.rename_profile_by_id("k1target", "Snapchat: opalmily")

        self.assertEqual(result, {
            "profile_id": "k1target",
            "name": "Snapchat: opalmily",
        })
        ctrl._rect.assert_called_once_with("Enter Name", "Edit", timeout=0.2)
        ctrl._fill_name.assert_called_once_with("Snapchat: opalmily")
        ctrl._click_ok.assert_called_once_with()

    def test_rename_profile_retries_full_edit_form_when_temp_prefix_remains_visible(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._connect = mock.Mock()
        ctrl._ensure_row_visible = mock.Mock()
        ctrl._open_rename_dialog = mock.Mock(return_value=True)
        ctrl._rect = mock.Mock(return_value=None)
        ctrl._fill_name = mock.Mock()
        ctrl._click_ok = mock.Mock()
        ctrl._rename_confirmed_or_absent = mock.Mock(side_effect=[False, True])

        result = ctrl.rename_profile_by_id("k1target", "Snapchat: glowyemz")

        self.assertEqual(result, {
            "profile_id": "k1target",
            "name": "Snapchat: glowyemz",
        })
        self.assertEqual(ctrl._open_rename_dialog.call_count, 2)
        self.assertEqual(ctrl._fill_name.call_count, 2)
        ctrl._fill_name.assert_has_calls([
            mock.call("Snapchat: glowyemz"),
            mock.call("Snapchat: glowyemz"),
        ])
        self.assertEqual(ctrl._click_ok.call_count, 2)

    def test_rename_refreshes_dashboard_before_final_failure(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._connect = mock.Mock()
        ctrl._ensure_row_visible = mock.Mock(return_value=True)
        ctrl._recover_presearch_row_for_rename = mock.Mock(return_value=False)
        ctrl._open_rename_dialog = mock.Mock(return_value=True)
        ctrl._rect = mock.Mock(return_value=None)
        ctrl._fill_name = mock.Mock()
        ctrl._click_ok = mock.Mock()
        ctrl._rename_confirmed_or_absent = mock.Mock(return_value=False)
        ctrl._refresh_window = mock.Mock(return_value=True)

        with self.assertRaises(AdsPowerUIError):
            ctrl.rename_profile_by_id("k1target", "Snapchat: fixeduser")

        ctrl._refresh_window.assert_called_once_with()

    def test_rename_profile_pastes_inline_name_after_clearing_existing_text(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        name_rect = _Rect(520, 230, 760, 266)
        ok_rect = _Rect(680, 300, 740, 332)
        ctrl._connect = mock.Mock()
        ctrl._ensure_row_visible = mock.Mock()
        ctrl._open_rename_dialog = mock.Mock(return_value=True)
        ctrl._paste_rect = mock.Mock()
        ctrl._type_rect = mock.Mock()
        ctrl._click_rect = mock.Mock()

        def rect(title, control_type, timeout=3.0):
            if (title, control_type) == ("Enter Name", "Edit"):
                return name_rect
            if (title, control_type) == ("OK", "Button"):
                return ok_rect
            return None

        ctrl._rect = mock.Mock(side_effect=rect)
        ctrl._find = mock.Mock(return_value=None)

        with mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
            result = ctrl.rename_profile_by_id("k1target", "Snapchat: opalmily")

        self.assertEqual(result, {
            "profile_id": "k1target",
            "name": "Snapchat: opalmily",
        })
        ctrl._paste_rect.assert_called_once_with(name_rect, "Snapchat: opalmily")
        ctrl._type_rect.assert_not_called()
        ctrl._click_rect.assert_called_once_with(ok_rect, template_name="rename_ok_btn")

    def test_rename_profile_inline_path_uses_short_ready_probes(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        name_rect = _Rect(520, 230, 760, 266)
        ok_rect = _Rect(680, 300, 740, 332)
        rect_calls = []
        find_calls = []
        ctrl._connect = mock.Mock()
        ctrl._ensure_row_visible = mock.Mock()
        ctrl._open_rename_dialog = mock.Mock(return_value=True)
        ctrl._paste_rect = mock.Mock()
        ctrl._click_rect = mock.Mock()

        def rect(title, control_type, timeout=3.0):
            rect_calls.append((title, control_type, timeout))
            if (title, control_type) == ("Enter Name", "Edit"):
                return name_rect
            if (title, control_type) == ("OK", "Button"):
                return ok_rect
            return None

        def find(title, control_type, timeout=3.0, retry=True):
            find_calls.append((title, control_type, timeout, retry))
            return None

        ctrl._rect = mock.Mock(side_effect=rect)
        ctrl._find = mock.Mock(side_effect=find)

        with mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
            result = ctrl.rename_profile_by_id("k1target", "Snapchat: fastname")

        self.assertEqual(result, {
            "profile_id": "k1target",
            "name": "Snapchat: fastname",
        })
        self.assertIn(("Enter Name", "Edit", 0.2), rect_calls)
        self.assertIn(("OK", "Button", 0.35), rect_calls)
        self.assertNotIn(("Enter Name", "Edit", 1.2), rect_calls)
        self.assertNotIn(("OK", "Button", 4), rect_calls)
        self.assertTrue(all(call[2] <= 0.35 for call in find_calls))
        ctrl._click_rect.assert_called_once_with(ok_rect, template_name="rename_ok_btn")

    def test_rename_profile_reapplies_temp_filter_when_presearch_row_is_missing(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl.config = SimpleNamespace(assume_presearch=True)
        ctrl._temp_search_name = "Snapchat: xyz"
        events = []
        visible = {"value": False}

        def ensure_row(profile_id):
            events.append(("ensure", profile_id))
            if visible["value"]:
                return True
            return False

        def ensure_temp(name):
            events.append(("temp", name))
            visible["value"] = True

        def open_dialog(profile_id):
            events.append(("open", profile_id))
            return True

        ctrl._connect = mock.Mock()
        ctrl._ensure_row_visible = mock.Mock(side_effect=ensure_row)
        ctrl._ensure_temp_filter = mock.Mock(side_effect=ensure_temp)
        ctrl._wait_list_settled = mock.Mock()
        ctrl._open_rename_dialog = mock.Mock(side_effect=open_dialog)
        ctrl._rect = mock.Mock(return_value=None)
        ctrl._fill_name = mock.Mock()
        ctrl._click_ok = mock.Mock()
        ctrl._rename_confirmed_or_absent = mock.Mock(return_value=True)

        result = ctrl.rename_profile_by_id("k1target", "Snapchat: playelia")

        self.assertEqual(result, {
            "profile_id": "k1target",
            "name": "Snapchat: playelia",
        })
        self.assertEqual(events, [
            ("ensure", "k1target"),
            ("temp", "Snapchat: xyz"),
            ("ensure", "k1target"),
            ("open", "k1target"),
        ])
        ctrl._wait_list_settled.assert_called_once_with(timeout=3.0)
        ctrl._fill_name.assert_called_once_with("Snapchat: playelia")


class DeleteProfileLockTests(unittest.TestCase):
    def test_delete_profile_runs_full_gui_flow_under_global_lock(self):
        events = []

        class RecordingLock:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, *_exc):
                events.append("exit")
                return False

        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._ensure_row_visible = mock.Mock(
            side_effect=lambda _pid: events.append("visible") or True
        )
        ctrl._row_has_button = mock.Mock(return_value=False)
        ctrl._click_row_menu_delete = mock.Mock(
            side_effect=lambda _pid: events.append("delete-click") or True
        )
        ctrl._connect = mock.Mock()
        ctrl._find = mock.Mock(return_value=object())
        ctrl._tick_clear_cache = mock.Mock()
        ctrl._maybe_confirm = mock.Mock(return_value=True)
        ctrl._row_center_y = mock.Mock(side_effect=[240, None])
        ctrl._a11y_enter = mock.Mock()
        ctrl._a11y_exit = mock.Mock()

        with mock.patch.object(aui, "_GUI_LOCK", RecordingLock()), \
             mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
            self.assertEqual(
                ctrl.delete_profile_by_id("k1delete"),
                {"code": 0, "deleted": True, "profile_id": "k1delete"},
            )

        self.assertEqual(events, ["enter", "visible", "delete-click", "exit"])


class ProxyFillTests(unittest.TestCase):
    def test_macos_raw_paste_uses_short_condition_friendly_settle(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl.config = SimpleNamespace(macos_paste_settle=0.12)
        rect = _Rect(396, 453, 614, 493)
        events = []

        fake_pg = SimpleNamespace(
            click=lambda *args: events.append(("click", args)),
            hotkey=lambda *args: events.append(("hotkey", args)),
            press=lambda key: events.append(("press", key)),
        )

        with mock.patch.object(aui, "_pg", lambda: fake_pg):
            with mock.patch.object(aui, "_set_clipboard",
                                   lambda text: events.append(("clipboard", text))):
                with mock.patch.object(aui.sys, "platform", "darwin"):
                    with mock.patch.object(aui.time, "sleep",
                                           lambda secs: events.append(("sleep", secs))):
                        ctrl._paste_rect(rect, "191.44.83.115:46539", clear=False)

        self.assertNotIn(("press", "delete"), events)
        sleeps = [secs for kind, secs in events if kind == "sleep"]
        self.assertTrue(sleeps)
        self.assertLessEqual(max(sleeps), 0.12)

    def test_paste_sets_clipboard_before_focusing_field(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        rect = _Rect(396, 453, 614, 493)
        events = []
        fake_pg = SimpleNamespace(
            click=lambda *args: events.append("click"),
            hotkey=lambda *args: events.append(("hotkey", args)),
            press=lambda key: events.append(("press", key)),
        )

        with mock.patch.object(aui, "_pg", lambda: fake_pg):
            with mock.patch.object(aui, "_set_clipboard",
                                   lambda text: events.append(("clipboard", text))):
                with mock.patch.object(aui.time, "sleep", lambda _secs: None):
                    ctrl._paste_rect(rect, "191.44.83.115:46539", clear=False)

        self.assertEqual(events[0], ("clipboard", "191.44.83.115:46539"))
        self.assertEqual(events[1], "click")

    def test_macos_paste_uses_system_events_before_pyautogui_hotkey(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        rect = _Rect(396, 453, 614, 493)
        events = []
        fake_pg = SimpleNamespace(
            click=lambda *args: events.append("click"),
            hotkey=lambda *args: events.append(("hotkey", args)),
            press=lambda key: events.append(("press", key)),
        )

        with mock.patch.object(aui, "_pg", lambda: fake_pg):
            with mock.patch.object(aui, "_set_clipboard", lambda _text: None):
                with mock.patch.object(aui.sys, "platform", "darwin"):
                    with mock.patch.object(aui.time, "sleep", lambda _secs: None):
                        with mock.patch.object(aui.subprocess, "run",
                                               lambda cmd, check=True, **_kw: events.append(tuple(cmd))):
                            ctrl._paste_rect(rect, "191.44.83.115:46539", clear=False)

        self.assertIn(("osascript", "-e",
                       'tell application "System Events" to keystroke "v" using command down'),
                      events)
        self.assertNotIn(("hotkey", ("command", "v")), events)

    def test_macos_clear_uses_system_events_before_paste(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        rect = _Rect(396, 453, 614, 493)
        events = []
        fake_pg = SimpleNamespace(
            click=lambda *args: events.append("click"),
            hotkey=lambda *args: events.append(("hotkey", args)),
            press=lambda key: events.append(("press", key)),
        )

        with mock.patch.object(aui, "_pg", lambda: fake_pg):
            with mock.patch.object(aui, "_set_clipboard", lambda _text: None):
                with mock.patch.object(aui.sys, "platform", "darwin"):
                    with mock.patch.object(aui.time, "sleep", lambda _secs: None):
                        with mock.patch.object(aui.subprocess, "run",
                                               lambda cmd, check=True, **_kw: events.append(tuple(cmd))):
                            ctrl._paste_rect(rect, "Snapchat: glowyemz")

        self.assertIn(("osascript", "-e",
                       'tell application "System Events" to keystroke "a" using command down'),
                      events)
        self.assertIn(("osascript", "-e",
                       'tell application "System Events" to key code 51'),
                      events)
        self.assertIn(("osascript", "-e",
                       'tell application "System Events" to keystroke "v" using command down'),
                      events)
        self.assertNotIn(("hotkey", ("command", "a")), events)

    def test_macos_proxy_fill_pastes_full_proxy_without_preclear(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        host = _Rect(396, 453, 614, 493)
        port = _FakeControl("46539", _Rect(646, 453, 746, 493))
        proxy = "191.44.83.115:46539:eligiblemodels:wGf836ZoWQ"
        pasted = []

        ctrl._rect = lambda title, control_type, timeout=3.0: (
            host if (title, control_type) == ("Please enter host", "Edit") else None
        )
        ctrl._find = lambda title, control_type, timeout=3.0, retry=True: (
            port if (title, control_type) == ("Port", "Edit") else None
        )
        ctrl._paste_rect = lambda rect, text, clear=True: pasted.append((rect, text, clear))
        ctrl._connect = lambda: None

        with mock.patch.object(aui.sys, "platform", "darwin"):
            with mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
                ctrl._fill_proxy(proxy)

        self.assertEqual(pasted, [(host, proxy, False)])

    def test_proxy_fill_reconnects_before_deciding_autoparse_failed(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        host = _Rect(396, 453, 614, 493)
        proxy = "191.44.83.115:46539:eligiblemodels:wGf836ZoWQ"
        connected = [False]
        pasted = []

        ctrl._rect = lambda title, control_type, timeout=3.0: (
            host if (title, control_type) == ("Please enter host", "Edit") else None
        )
        ctrl._paste_rect = lambda rect, text, clear=True: pasted.append((rect, text, clear))
        ctrl._connect = lambda: connected.__setitem__(0, True)
        ctrl._find = lambda title, control_type, timeout=3.0, retry=True: (
            SimpleNamespace(get_value=lambda: "46539" if connected[0] else "Port")
            if (title, control_type) == ("Port", "Edit") else None
        )
        ctrl._proxy_edit_rects = lambda _host: self.fail(
            "stale placeholder must not trigger individual fallback"
        )

        with mock.patch.object(aui.sys, "platform", "darwin"):
            with mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
                ctrl._fill_proxy(proxy)

        self.assertTrue(connected[0])
        self.assertEqual(pasted, [(host, proxy, False)])

    def test_macos_proxy_fill_normalizes_scroll_before_locating_host(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        host = _Rect(396, 453, 614, 493)
        calls = []

        ctrl._stabilize_proxy_form_scroll = lambda: calls.append("stabilize")
        ctrl._rect = lambda title, control_type, timeout=3.0: (
            calls.append("rect") or host
            if (title, control_type) == ("Please enter host", "Edit") else None
        )
        ctrl._paste_rect = lambda *_args, **_kwargs: None
        ctrl._connect = lambda: None
        ctrl._find = lambda *_args, **_kwargs: None

        with mock.patch.object(aui.sys, "platform", "darwin"):
            with mock.patch.object(aui.time, "sleep", lambda *_args, **_kwargs: None):
                ctrl._fill_proxy("191.44.83.115:46539:eligiblemodels:wGf836ZoWQ")

        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[:2], ["stabilize", "rect"])


class ProxyCheckSpeedTests(unittest.TestCase):
    def test_check_proxy_reads_visible_result_before_sleeping(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl.config = SimpleNamespace(proxy_check_timeout=2.0, proxy_poll_interval=0.25)
        btn = _Rect(396, 453, 614, 493)
        sleeps = []

        ctrl._rect = mock.Mock(return_value=btn)
        ctrl._click_rect = mock.Mock()
        ctrl._visible_text_blob = mock.Mock(return_value="Connection test passed success")

        with mock.patch.object(aui.time, "sleep", lambda secs: sleeps.append(secs)):
            self.assertTrue(ctrl._check_proxy())

        self.assertEqual(sleeps, [])
        ctrl._visible_text_blob.assert_called_once_with()


class ProfileNameFillTests(unittest.TestCase):
    def test_fill_name_falls_back_to_edit_right_of_name_label(self):
        name_rect = _Rect(396, 243, 908, 283)
        win = _FakeWindow(
            texts=[_FakeControl("Name", _Rect(333, 250, 372, 276))],
            edits=[
                _FakeControl("Snapchat: xxxxxx", name_rect),
                _FakeControl("Mozilla/5.0", _Rect(550, 437, 908, 477)),
            ],
        )
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._win = win
        ctrl._connect = lambda: None
        ctrl._rect = lambda *_args, **_kwargs: None
        pasted = []
        ctrl._paste_rect = lambda rect, text: pasted.append((rect, text))

        ctrl._fill_name("Snapchat: xxxxxx")

        self.assertEqual(pasted, [(name_rect, "Snapchat: xxxxxx")])


class CreateProfileSettingsTests(unittest.TestCase):
    def test_explicit_blank_group_setting_does_not_fall_back_to_default_group(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl.config = SimpleNamespace(
            group_name="HardcodedDefault",
            skip_group=False,
            assume_presearch=False,
            form_settle=0,
            check_proxy_in_form=False,
            require_proxy_ok=False,
        )
        ctrl._connect = mock.Mock()
        ctrl._goto_profiles = mock.Mock()
        ctrl._max_serial = mock.Mock(return_value=10)
        ctrl._open_new_profile_form = mock.Mock()
        ctrl._switch_tab = mock.Mock()
        ctrl._fill_name = mock.Mock()
        ctrl._select_group = mock.Mock()
        ctrl._fill_proxy = mock.Mock()
        ctrl._click_ok = mock.Mock()
        ctrl._wait_for_new_profile_id = mock.Mock(return_value="k1settings")

        result = ctrl.create_profile(
            name="Snapchat: from-settings",
            proxy="191.44.83.115:46539:eligiblemodels:wGf836ZoWQ",
            group="",
        )

        self.assertEqual(result["group"], "")
        ctrl._select_group.assert_not_called()

    def test_create_profile_refills_rotated_proxy_after_gui_check_failure(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl.config = SimpleNamespace(
            group_name="HardcodedDefault",
            skip_group=True,
            assume_presearch=False,
            form_settle=0,
            check_proxy_in_form=True,
            require_proxy_ok=True,
            proxy_check_rotation_attempts=2,
        )
        ctrl._connect = mock.Mock()
        ctrl._goto_profiles = mock.Mock()
        ctrl._max_serial = mock.Mock(return_value=10)
        ctrl._open_new_profile_form = mock.Mock()
        ctrl._switch_tab = mock.Mock()
        ctrl._fill_name = mock.Mock()
        ctrl._select_group = mock.Mock()
        ctrl._click_ok = mock.Mock()
        ctrl._wait_for_new_profile_id = mock.Mock(return_value="k1rotated")
        fills = []
        rotations = []
        ctrl._fill_proxy = mock.Mock(side_effect=lambda proxy: fills.append(proxy))
        ctrl._check_proxy = mock.Mock(side_effect=[False, True])

        def proxy_rotator(**kwargs):
            rotations.append(kwargs)
            return "2.2.2.2:2222:user:pass"

        result = ctrl.create_profile(
            name="Snapchat: rotate",
            proxy="1.1.1.1:1111:user:pass",
            group="",
            proxy_rotator=proxy_rotator,
        )

        self.assertEqual(fills, [
            "1.1.1.1:1111:user:pass",
            "2.2.2.2:2222:user:pass",
        ])
        self.assertEqual(len(rotations), 1)
        self.assertEqual(rotations[0]["current_proxy"], "1.1.1.1:1111:user:pass")
        self.assertEqual(rotations[0]["attempt"], 1)
        self.assertEqual(rotations[0]["reason"], "gui_proxy_check_failed")
        self.assertEqual(result["proxy"], "2.2.2.2:2222:user:pass")
        self.assertTrue(result["proxy_passed"])


class BulkToolbarActionTests(unittest.TestCase):
    """The popup-proof bulk path: tick every row's checkbox, then click the ONE
    toolbar button — never the per-row Action buttons (which the launching browser
    windows kept stealing)."""

    def _controller(self, win):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._win = win
        ctrl._connect = lambda: None
        ctrl.clicked = []
        ctrl._click_rect = lambda rect, template_name="": ctrl.clicked.append((rect, template_name))
        ctrl._click_xy = lambda x, y: ctrl.clicked.append(((x, y), "xy"))
        return ctrl

    def _headers(self):
        return [
            _FakeControl("No./ID", _Rect(80, 170, 150, 194)),
            _FakeControl("Name", _Rect(260, 170, 320, 194)),
            _FakeControl("Action", _Rect(900, 170, 980, 194)),
        ]

    def _results(self, ids):
        return {pid: _BatchResult() for pid in ids}

    def test_open_ticks_checkboxes_then_clicks_toolbar_open_once(self):
        cb_one = _Rect(40, 232, 60, 252)
        cb_two = _Rect(40, 272, 60, 292)
        row_open_one = _Rect(910, 231, 970, 257)
        row_open_two = _Rect(910, 271, 970, 297)
        toolbar_open = _Rect(420, 110, 480, 136)          # above the headers
        win = _FakeWindow(
            texts=[
                *self._headers(),
                _FakeControl("k1one", _Rect(90, 232, 155, 252)),
                _FakeControl("k1two", _Rect(90, 272, 155, 292)),
            ],
            buttons=[
                _FakeControl("Open", toolbar_open),        # toolbar Open (cy < header)
                _FakeControl("Open", row_open_one),        # per-row Open buttons
                _FakeControl("Open", row_open_two),
            ],
            checkboxes=[_FakeControl("", cb_one), _FakeControl("", cb_two)],
        )
        ctrl = self._controller(win)
        results = self._results(["k1one", "k1two"])

        with mock.patch.object(aui.time, "sleep", lambda *_a, **_k: None):
            ctrl._bulk_row_action(["k1one", "k1two"], results, needs_label="Open",
                                  done_label="Close", toolbar_label="Open", verb="open")

        # Both checkboxes ticked first, then exactly one toolbar Open click last.
        self.assertEqual(ctrl.clicked,
                         [(cb_one, ""), (cb_two, ""), (toolbar_open, "toolbar_open_btn")])
        # The per-row Open buttons were never clicked.
        self.assertNotIn((row_open_one, "open_btn"), ctrl.clicked)
        self.assertNotIn((row_open_two, "open_btn"), ctrl.clicked)
        for pid in ("k1one", "k1two"):
            self.assertTrue(results[pid].ok)
            self.assertTrue(results[pid].event.is_set())

    def test_open_skips_already_open_row_without_ticking_it(self):
        cb_two = _Rect(40, 272, 60, 292)
        toolbar_open = _Rect(420, 110, 480, 136)
        win = _FakeWindow(
            texts=[
                *self._headers(),
                _FakeControl("k1open", _Rect(90, 232, 155, 252)),   # already open
                _FakeControl("k1shut", _Rect(90, 272, 155, 292)),   # needs opening
            ],
            buttons=[
                _FakeControl("Open", toolbar_open),
                _FakeControl("Close", _Rect(910, 231, 970, 257)),   # k1open shows Close
                _FakeControl("Open", _Rect(910, 271, 970, 297)),    # k1shut shows Open
            ],
            checkboxes=[_FakeControl("", _Rect(40, 232, 60, 252)),
                        _FakeControl("", cb_two)],
        )
        ctrl = self._controller(win)
        results = self._results(["k1open", "k1shut"])

        with mock.patch.object(aui.time, "sleep", lambda *_a, **_k: None):
            ctrl._bulk_row_action(["k1open", "k1shut"], results, needs_label="Open",
                                  done_label="Close", toolbar_label="Open", verb="open")

        # Only the closed row's checkbox is ticked, then one toolbar click.
        self.assertEqual(ctrl.clicked, [(cb_two, ""), (toolbar_open, "toolbar_open_btn")])
        self.assertTrue(results["k1open"].ok)     # already open -> success, untouched
        self.assertTrue(results["k1shut"].ok)

    def test_close_clicks_toolbar_close_and_reports_bool(self):
        cb_one = _Rect(40, 232, 60, 252)
        toolbar_close = _Rect(500, 110, 560, 136)
        win = _FakeWindow(
            texts=[*self._headers(), _FakeControl("k1run", _Rect(90, 232, 155, 252))],
            buttons=[
                _FakeControl("Close", toolbar_close),                # toolbar Close
                _FakeControl("Close", _Rect(910, 231, 970, 257)),    # row is running
            ],
            checkboxes=[_FakeControl("", cb_one)],
        )
        ctrl = self._controller(win)
        ctrl._maybe_confirm = lambda *a, **k: False
        ctrl._wait_rows_done = lambda pids, done_label, timeout=12.0: set(pids)
        results = self._results(["k1run"])

        with mock.patch.object(aui.time, "sleep", lambda *_a, **_k: None):
            ctrl._bulk_row_action(["k1run"], results, needs_label="Close",
                                  done_label="Open", toolbar_label="Close",
                                  verb="close", confirm=True, wait_done=True)

        self.assertEqual(ctrl.clicked,
                         [(cb_one, ""), (toolbar_close, "toolbar_close_btn")])
        self.assertTrue(results["k1run"].ok)
        self.assertIs(results["k1run"].value, True)

    def test_missing_row_fails_only_that_id(self):
        cb_one = _Rect(40, 232, 60, 252)
        toolbar_open = _Rect(420, 110, 480, 136)
        win = _FakeWindow(
            texts=[*self._headers(), _FakeControl("k1here", _Rect(90, 232, 155, 252))],
            buttons=[
                _FakeControl("Open", toolbar_open),
                _FakeControl("Open", _Rect(910, 231, 970, 257)),
            ],
            checkboxes=[_FakeControl("", cb_one)],
        )
        ctrl = self._controller(win)
        results = self._results(["k1here", "k1gone"])

        with mock.patch.object(aui.time, "sleep", lambda *_a, **_k: None):
            ctrl._bulk_row_action(["k1here", "k1gone"], results, needs_label="Open",
                                  done_label="Close", toolbar_label="Open", verb="open")

        self.assertTrue(results["k1here"].ok)
        self.assertFalse(results["k1gone"].ok)
        self.assertIsInstance(results["k1gone"].error, AdsPowerUIError)
        # The present row still opened (checkbox + one toolbar click).
        self.assertEqual(ctrl.clicked, [(cb_one, ""), (toolbar_open, "toolbar_open_btn")])

    def test_missing_row_treated_as_done_when_missing_ok(self):
        # Nyxify close: a renamed/done profile that dropped out of the temp-name
        # view resolves as already-closed WITHOUT any id search, never as a
        # failure. Only the still-running visible row is actually closed.
        cb_one = _Rect(40, 232, 60, 252)
        toolbar_close = _Rect(500, 110, 560, 136)
        win = _FakeWindow(
            texts=[*self._headers(), _FakeControl("k1run", _Rect(90, 232, 155, 252))],
            buttons=[
                _FakeControl("Close", toolbar_close),
                _FakeControl("Close", _Rect(910, 231, 970, 257)),   # k1run running
            ],
            checkboxes=[_FakeControl("", cb_one)],
        )
        ctrl = self._controller(win)
        ctrl._maybe_confirm = lambda *a, **k: False
        ctrl._wait_rows_done = lambda pids, done_label, timeout=12.0: set(pids)
        results = self._results(["k1run", "k1renamed"])

        with mock.patch.object(aui.time, "sleep", lambda *_a, **_k: None):
            ctrl._bulk_row_action(["k1run", "k1renamed"], results, needs_label="Close",
                                  done_label="Open", toolbar_label="Close", verb="close",
                                  confirm=True, wait_done=True, missing_ok=True)

        self.assertTrue(results["k1run"].ok)
        self.assertIs(results["k1run"].value, True)
        # The renamed/gone row is resolved as done — no error, no search.
        self.assertTrue(results["k1renamed"].ok)
        self.assertIs(results["k1renamed"].value, True)
        self.assertIsNone(results["k1renamed"].error)
        self.assertEqual(ctrl.clicked, [(cb_one, ""), (toolbar_close, "toolbar_close_btn")])

    def test_open_uses_derived_checkbox_x_when_no_checkbox_control(self):
        # AdsPower exposes no CheckBox control for the row tick, so selection must
        # click the derived checkbox column x (resolution-independent, from the
        # cell height) — never a hard-coded pixel offset.
        toolbar_open = _Rect(420, 110, 480, 136)
        id_one = _Rect(90, 232, 155, 252)            # h=20 -> cb_dx=round(2.3*20)=46
        win = _FakeWindow(
            texts=[*self._headers(), _FakeControl("k1one", id_one)],
            buttons=[_FakeControl("Open", toolbar_open),
                     _FakeControl("Open", _Rect(910, 231, 970, 257))],
            checkboxes=[],                            # none exposed
        )
        ctrl = self._controller(win)
        results = self._results(["k1one"])

        with mock.patch.object(aui.time, "sleep", lambda *_a, **_k: None):
            ctrl._bulk_row_action(["k1one"], results, needs_label="Open",
                                  done_label="Close", toolbar_label="Open", verb="open")

        # checkbox_x = id_left(90) - cb_dx(46) = 44, at the row centre y=242
        self.assertEqual(ctrl.clicked,
                         [((44, 242), "xy"), (toolbar_open, "toolbar_open_btn")])
        self.assertTrue(results["k1one"].ok)

    def test_snapshot_scales_offsets_with_cell_height(self):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl._connect = lambda: None
        # 2x DPI: headers/cells twice as tall, id_left doubled.
        ctrl._win = _FakeWindow(
            texts=[
                _FakeControl("No./ID", _Rect(160, 340, 300, 388)),   # h=48
                _FakeControl("k1big", _Rect(612, 460, 720, 490)),    # h=30, left=612
            ],
            buttons=[_FakeControl("Open", _Rect(1820, 462, 1940, 514))],  # cy~488 row btn
        )
        info = ctrl._batch_action_snapshot(["k1big"], "Open", "Close")
        e = info["k1big"]
        # cb_dx = round(2.3*30)=69 -> checkbox_x = 612-69 = 543; tol=round(1.6*30)=48
        self.assertEqual(e["checkbox_x"], 543)
        self.assertTrue(e["needs"])               # row Open button aligned within tol

    def test_median_helper(self):
        med = AdsPowerUIController._median
        self.assertEqual(med([15, 15, 16]), 15)
        self.assertEqual(med([10, 20]), 15)
        self.assertEqual(med([]), 0)

    def test_toolbar_action_rect_picks_button_above_headers(self):
        toolbar_open = _Rect(420, 110, 480, 136)
        row_open = _Rect(910, 231, 970, 257)
        win = _FakeWindow(
            texts=self._headers(),
            buttons=[_FakeControl("Open", toolbar_open), _FakeControl("Open", row_open)],
        )
        ctrl = self._controller(win)
        self.assertEqual(ctrl._toolbar_action_rect("Open"), toolbar_open)

    def test_toolbar_close_skips_window_titlebar_x(self):
        # The window's titlebar Close (X) is ALSO a Button named "Close" and sits
        # above the toolbar; the toolbar Close must be chosen, never the X.
        titlebar_x = _Rect(1872, 0, 1920, 22)        # cy=11, very top
        toolbar_close = _Rect(343, 110, 425, 150)    # cy=130, just above headers
        row_close = _Rect(1760, 231, 1828, 257)      # below headers
        win = _FakeWindow(
            texts=self._headers(),
            buttons=[
                _FakeControl("Close", titlebar_x),
                _FakeControl("Close", toolbar_close),
                _FakeControl("Close", row_close),
            ],
        )
        ctrl = self._controller(win)
        self.assertEqual(ctrl._toolbar_action_rect("Close"), toolbar_close)


class ChipEditSearchTests(unittest.TestCase):
    """Re-search by editing the existing 'Profile ID is ...' filter chip in place
    instead of Reset + full re-type (the Nyx-running fast path)."""

    def _bare(self):
        return AdsPowerUIController.__new__(AdsPowerUIController)

    def test_profile_id_chip_found(self):
        ctrl = self._bare()
        chip_rect = _Rect(460, 178, 720, 200)
        ctrl._win = _FakeWindow(texts=[_FakeControl("Profile ID is k1old", chip_rect)])
        ctrl._connect = lambda: None
        found = ctrl._profile_id_chip()
        self.assertIsNotNone(found)
        self.assertEqual(found[0], chip_rect)
        self.assertEqual(found[1], "Profile ID is k1old")

    def test_profile_id_chip_absent(self):
        ctrl = self._bare()
        ctrl._win = _FakeWindow(texts=[_FakeControl("Name contains foo", _Rect(460, 178, 720, 200))])
        ctrl._connect = lambda: None
        self.assertIsNone(ctrl._profile_id_chip())

    def test_chip_editor_edit_rect_picks_wide_box_below_chip(self):
        # Popup below the chip has a narrow operator box + a wide ids box; the
        # group filter sits ABOVE the chip and the main search box is excluded.
        ctrl = self._bare()
        ids_box = _Rect(582, 265, 812, 297)        # widest, below chip -> wanted
        ctrl._win = _FakeWindow(edits=[
            _FakeControl("Search or new search criteria", _Rect(684, 169, 968, 201)),
            _FakeControl("Please select a group.", _Rect(252, 165, 428, 205)),  # above chip
            _FakeControl("Select", _Rect(464, 265, 574, 297)),                  # operator box
            _FakeControl("Search multiple", ids_box),                           # ids box
        ])
        ctrl._connect = lambda: None
        self.assertEqual(ctrl._chip_editor_edit_rect(below_top=192), ids_box)

    def test_via_chip_edits_in_place_and_confirms(self):
        ctrl = self._bare()
        ctrl._connect = lambda: None
        chip_before = (_Rect(460, 178, 720, 200), "Profile ID is k1old")
        chip_after = (_Rect(460, 178, 760, 200), "Profile ID is k1new1 k1new2")
        chips = [chip_before, chip_after]
        ctrl._profile_id_chip = lambda: chips.pop(0)
        ctrl._click_xy = mock.Mock()
        ctrl._chip_editor_edit_rect = lambda below_top: _Rect(600, 260, 800, 296)
        pasted = []
        ctrl._paste_rect = lambda rect, text: pasted.append(text)
        ctrl._click_confirm_chip = mock.Mock(return_value=True)
        ctrl._foreground = lambda: None
        ctrl._wait_list_settled = lambda: None

        with mock.patch.object(aui.time, "sleep", lambda *_a, **_k: None):
            ok = ctrl._search_by_ids_via_chip("k1new1 k1new2")

        self.assertTrue(ok)
        self.assertEqual(pasted, ["k1new1 k1new2"])
        ctrl._click_confirm_chip.assert_called_once()
        ctrl._click_xy.assert_called_once()           # clicked the chip to open it

    def test_via_chip_returns_false_when_no_chip(self):
        ctrl = self._bare()
        ctrl._connect = lambda: None
        ctrl._profile_id_chip = lambda: None
        ctrl._click_xy = mock.Mock()
        self.assertFalse(ctrl._search_by_ids_via_chip("k1a"))
        ctrl._click_xy.assert_not_called()


class PreSearchFastPathTests(unittest.TestCase):
    """Nyxify dashboard mode (assume_presearch): the ONLY search is the temp-name
    'Name contains <temp>' filter, applied only when not already active; row
    actions act on the current view and never search by profile id."""

    def _bare(self, assume_presearch=True, create_id_timeout=5.0):
        ctrl = AdsPowerUIController.__new__(AdsPowerUIController)
        ctrl.config = SimpleNamespace(
            assume_presearch=assume_presearch,
            create_id_timeout=create_id_timeout,
        )
        ctrl._connect = lambda: None
        ctrl._wait_list_settled = lambda timeout=8.0: None
        ctrl._goto_profiles = lambda: None
        ctrl._active_search_filters = lambda: []
        ctrl._temp_search_fragment = None
        return ctrl

    def test_prepare_rows_never_searches_in_presearch_mode(self):
        # The whole point: row actions in Nyxify mode never search by id.
        ctrl = self._bare()
        ctrl._search_by_ids = lambda ids: self.fail("must never search by id")
        ctrl._goto_profiles = lambda: self.fail("must not navigate/refresh")
        ctrl._prepare_rows_for_action(["k1a", "k1renamed"])   # must not raise

    def test_prepare_rows_bulk_searches_in_legacy_mode(self):
        ctrl = self._bare(assume_presearch=False)
        calls = []
        ctrl._search_by_ids = lambda ids, append=False: calls.append((list(ids), append))
        ctrl._prepare_rows_for_action(["k1a"])
        self.assertEqual(calls, [(["k1a"], False)])

    def test_ensure_row_visible_replaces_existing_profile_id_filter_in_nyx_mode(self):
        ctrl = self._bare(assume_presearch=False)
        calls = []
        visible = {"value": False}
        ctrl._row_center_y = lambda _pid: 242 if visible["value"] else None
        ctrl._profile_id_chip = lambda: None

        def search(ids, append=False):
            calls.append((list(ids), append))
            visible["value"] = True

        ctrl._search_by_ids = search

        self.assertTrue(ctrl._ensure_row_visible("k1a"))
        self.assertEqual(calls, [(["k1a"], False)])

    def test_ensure_row_visible_searches_id_even_when_row_visible_without_chip(self):
        ctrl = self._bare(assume_presearch=False)
        calls = []
        ctrl._row_center_y = lambda _pid: 242
        ctrl._profile_id_chip = lambda: None
        ctrl._search_by_ids = lambda ids, append=False: calls.append((list(ids), append))

        self.assertTrue(ctrl._ensure_row_visible("k1a"))
        self.assertEqual(calls, [(["k1a"], False)])

    def test_ensure_row_visible_reuses_matching_profile_id_chip(self):
        ctrl = self._bare(assume_presearch=False)
        ctrl._row_center_y = lambda _pid: 242
        ctrl._profile_id_chip = lambda: (_Rect(460, 178, 720, 200), "Profile ID is k1a")
        ctrl._search_by_ids = lambda *_a, **_k: self.fail("matching id chip should be reused")

        self.assertTrue(ctrl._ensure_row_visible("k1a"))

    def test_close_profile_leaves_profile_id_chip_for_next_edit(self):
        ctrl = self._bare(assume_presearch=False)
        ctrl._a11y_enter = lambda: None
        ctrl._a11y_exit = lambda: None
        ctrl._ensure_row_visible = lambda _pid: True
        ctrl._click_row_action = lambda *_a, **_k: None
        ctrl._maybe_confirm = lambda *_a, **_k: False
        ctrl._wait_row_button = lambda *_a, **_k: True
        ctrl._remove_ids_from_chip = lambda *_a, **_k: self.fail("must leave chip in place")

        self.assertTrue(ctrl.close_profile_by_id("k1a"))

    def test_ensure_temp_filter_skips_when_already_active(self):
        # After OK: if the temp-name filter is already the active search, do NOT
        # search again (consecutive creates reuse the one standing filter).
        ctrl = self._bare()
        ctrl._active_search_filters = lambda: [("name_contains", "xoxoxo", "Name contains xoxoxo")]
        ctrl._search_by = lambda *a, **k: self.fail("must not re-search when already active")
        ctrl._ensure_temp_filter("Snapchat: xoxoxo")
        self.assertEqual(ctrl._temp_search_fragment, "xoxoxo")
        self.assertEqual(ctrl._temp_search_name, "Snapchat: xoxoxo")

    def test_ensure_temp_filter_searches_temp_name_when_not_active(self):
        # If the temp-name filter isn't applied, search it (and only it — by
        # Name contains, never by profile id).
        ctrl = self._bare()
        filters = [[], [("name_contains", "xoxoxo", "Name contains xoxoxo")]]
        ctrl._active_search_filters = lambda: filters.pop(0) if filters else [
            ("name_contains", "xoxoxo", "Name contains xoxoxo")]
        calls = []
        ctrl._search_by = lambda value, field, operator, **kwargs: calls.append(
            (value, field, operator, kwargs))
        ctrl._ensure_temp_filter("Snapchat: xoxoxo")
        self.assertEqual(calls, [("xoxoxo", "Name", "contains", {
            "allow_enter_fallback": False,
            "verify": True,
        })])

    def test_ensure_temp_filter_resets_profile_id_plus_hidden_name_filter(self):
        ctrl = self._bare()
        mixed = [
            ("profile_id", "xoxoxo", "Profile ID is xoxoxo"),
            ("name_contains", "xoxoxo", "Name contains xoxoxo"),
        ]
        states = [mixed, mixed, [("name_contains", "xoxoxo", "Name contains xoxoxo")]]
        ctrl._active_search_filters = lambda: states.pop(0) if states else [
            ("name_contains", "xoxoxo", "Name contains xoxoxo")]
        resets = []
        calls = []
        ctrl._reset_search = lambda: resets.append(True)
        ctrl._search_by = lambda value, field, operator, **kwargs: calls.append(
            (value, field, operator, kwargs))

        ctrl._ensure_temp_filter("Snapchat: xoxoxo")

        self.assertEqual(resets, [True])
        self.assertEqual(calls, [("xoxoxo", "Name", "contains", {
            "allow_enter_fallback": False,
            "verify": True,
        })])

    def test_search_applied_verifies_profile_id_chip(self):
        # Nyx fast path: after pressing Enter, the search is trusted only when the
        # active chip is the requested Profile-ID filter.
        ctrl = self._bare()
        ctrl._profile_id_chip = lambda: (object(), "Profile ID is k1a k1b")
        self.assertTrue(ctrl._search_applied("Profile ID", "k1a k1b"))
        ctrl._profile_id_chip = lambda: (object(), "Profile No./ID is k1a")
        self.assertTrue(ctrl._search_applied("Profile ID", "k1a"))
        ctrl._profile_id_chip = lambda: None          # Enter applied the wrong field
        self.assertFalse(ctrl._search_applied("Profile ID", "k1a"))

    def test_search_applied_verifies_name_filter(self):
        ctrl = self._bare()
        ctrl._active_search_filters = lambda: [("name_contains", "xoxoxo", "Name contains xoxoxo")]
        self.assertTrue(ctrl._search_applied("Name", "xoxoxo"))
        ctrl._active_search_filters = lambda: []
        self.assertFalse(ctrl._search_applied("Name", "xoxoxo"))

    def test_new_id_resolved_from_view_by_serial_watermark(self):
        ctrl = self._bare()
        # Two temp-named rows visible; only the one above the watermark is new.
        ctrl._scan_rows = lambda: [(202, "k1new", "Snapchat: xoxoxo"),
                                   (200, "k1old", "Snapchat: xoxoxo")]
        self.assertEqual(
            ctrl._wait_for_new_profile_id_in_view("Snapchat: xoxoxo", before_max=200),
            "k1new")

    def test_new_id_prefers_exact_name_match_among_fresh_rows(self):
        ctrl = self._bare()
        ctrl._scan_rows = lambda: [(205, "k1other", "Snapchat: someoneelse"),
                                   (203, "k1mine", "Snapchat: xoxoxo")]
        self.assertEqual(
            ctrl._wait_for_new_profile_id_in_view("Snapchat: xoxoxo", before_max=200),
            "k1mine")

    def test_new_id_falls_back_to_search_when_view_has_nothing_fresh(self):
        # Standing filter wasn't applied -> nothing above the watermark in view;
        # degrade to ONE name search rather than failing the create.
        ctrl = self._bare(create_id_timeout=0.0)     # skip the in-view poll
        ctrl._scan_rows = lambda: []
        ctrl._rows_for_name = lambda name: [(210, "k1searched", name)]
        with mock.patch.object(aui.time, "sleep", lambda *_a, **_k: None):
            self.assertEqual(
                ctrl._wait_for_new_profile_id_in_view("Snapchat: xoxoxo", before_max=200),
                "k1searched")


class ParseChipIdsTests(unittest.TestCase):
    def test_parses_chip_text_with_standard_prefix(self):
        result = AdsPowerUIController._parse_chip_ids("Profile ID is a1 b2 c3")
        self.assertEqual(result, ["a1", "b2", "c3"])

    def test_parses_chip_text_with_alt_prefix(self):
        result = AdsPowerUIController._parse_chip_ids("Profile No./ID is x1 y2")
        self.assertEqual(result, ["x1", "y2"])

    def test_parses_empty_chip_text(self):
        result = AdsPowerUIController._parse_chip_ids("")
        self.assertEqual(result, [])

    def test_parses_chip_with_no_ids(self):
        result = AdsPowerUIController._parse_chip_ids("Profile ID is")
        self.assertEqual(result, [])


class GuiBatcherTests(unittest.TestCase):
    def setUp(self):
        # Hermetic GUI lock — never touch the real cross-process named mutex.
        self._patch = mock.patch.object(aui, "_GUI_LOCK", threading.RLock())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _run_concurrently(self, batcher, fake, ids):
        results = {}
        barrier = threading.Barrier(len(ids))

        def worker(pid):
            barrier.wait()              # all callers hit submit() at once
            try:
                results[pid] = ("ok", batcher.submit(fake, pid))
            except Exception as exc:    # noqa: BLE001 - record per-caller failure
                results[pid] = ("err", str(exc))

        threads = [threading.Thread(target=worker, args=(pid,)) for pid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "a submit() never returned")
        return results

    def test_concurrent_opens_share_one_bulk_search(self):
        batcher = _GuiBatcher("_bulk_open_locked", "open")
        fake = _FakeController()
        ids = [f"k1id{i}" for i in range(5)]

        results = self._run_concurrently(batcher, fake, ids)

        self.assertEqual({pid: results[pid][0] for pid in ids}, {pid: "ok" for pid in ids})
        # The headline win: five opens, ONE search.
        self.assertEqual(len(fake.batches), 1)
        self.assertCountEqual(fake.batches[0], ids)

    def test_concurrent_closes_share_one_bulk_search_and_return_value(self):
        batcher = _GuiBatcher("_bulk_close_locked", "close")
        fake = _FakeController()
        ids = [f"k1cl{i}" for i in range(4)]

        results = self._run_concurrently(batcher, fake, ids)

        # Each caller gets close's bool back, and four closes cost ONE search.
        self.assertEqual(results, {pid: ("ok", True) for pid in ids})
        self.assertEqual(len(fake.batches), 1)
        self.assertCountEqual(fake.batches[0], ids)

    def test_per_id_failure_only_fails_that_caller(self):
        batcher = _GuiBatcher("_bulk_open_locked", "open")
        fake = _FakeController(fail_ids={"k1bad"})
        ids = ["k1ok1", "k1bad", "k1ok2"]

        results = self._run_concurrently(batcher, fake, ids)

        self.assertEqual(results["k1ok1"][0], "ok")
        self.assertEqual(results["k1ok2"][0], "ok")
        self.assertEqual(results["k1bad"][0], "err")
        self.assertIn("boom k1bad", results["k1bad"][1])
        # Still one batch — the bad id didn't sink the others.
        self.assertEqual(len(fake.batches), 1)
        self.assertCountEqual(fake.batches[0], ids)

    def test_staggered_arrivals_within_quiet_window_coalesce(self):
        # Opens that trickle in closer together than _QUIET join ONE search — this
        # is what makes "5 parallel -> search 5" hold despite the start stagger.
        batcher = _GuiBatcher("_bulk_open_locked", "open")
        batcher._QUIET = 0.3
        fake = _FakeController()
        ids = ["k1s0", "k1s1", "k1s2"]
        threads = []
        for pid in ids:
            t = threading.Thread(target=lambda p=pid: batcher.submit(fake, p))
            t.start()
            threads.append(t)
            time.sleep(0.1)             # < _QUIET, so each resets the window
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive())

        self.assertEqual(len(fake.batches), 1)
        self.assertCountEqual(fake.batches[0], ids)

    def test_arrivals_after_quiet_form_separate_batches(self):
        batcher = _GuiBatcher("_bulk_open_locked", "open")
        batcher._QUIET = 0.2
        fake = _FakeController()
        t0 = threading.Thread(target=lambda: batcher.submit(fake, "k1a"))
        t0.start()
        time.sleep(0.9)                 # >> _QUIET: first batch fires alone
        t1 = threading.Thread(target=lambda: batcher.submit(fake, "k1b"))
        t1.start()
        t0.join(timeout=10)
        t1.join(timeout=10)

        self.assertEqual(len(fake.batches), 2)
        self.assertEqual(sorted(b[0] for b in fake.batches), ["k1a", "k1b"])


class GuiBatcherImportTimeAttrs(unittest.TestCase):
    def test_quiet_and_max_wait_present(self):
        # Guard the coalescing knobs exist (used to tune batch gathering).
        self.assertTrue(hasattr(_GuiBatcher, "_QUIET"))
        self.assertTrue(hasattr(_GuiBatcher, "_MAX_WAIT"))
        self.assertTrue(hasattr(_GuiBatcher, "_MAX_BATCH"))


if __name__ == "__main__":
    unittest.main()
