"""AdsPower GUI automation (no Local API required).

The AdsPower Local API is permission-gated on this account (``/browser/start``
returns ``9110 No local API permission``), so profile *creation* and *starting*
cannot go through the API. This module drives the AdsPower desktop app directly.

Design — robust across resolution / DPI / window position
---------------------------------------------------------
* **Primary locator: Windows UI Automation (pywinauto/uia).** Every control is
  found by name and clicked at the centre of its *real* on-screen rectangle, so
  the flow is resolution- and DPI-independent (no hard-coded coordinates).
* **Foreground guarantee:** Windows blocks ``SetForegroundWindow`` from a
  background process, and Chromium only builds the form's accessibility tree
  once the window is focused. ``core.win_focus`` defeats the foreground lock
  before interactions that need AdsPower's live dashboard tree.
* **Clipboard paste** for text entry - matches AdsPower's proxy auto-parse
  (pasting ``host:port:user:pass`` into the Host field fills all four fields)
  and avoids per-keystroke flakiness.
* **Vision fallback:** ``core.ui_vision`` (opencv multi-scale template match) is
  the cross-platform safety net for the rare case UIA cannot see a control.

Public API
----------
    ctrl = AdsPowerUIController()
    info = ctrl.create_profile(name="Snapchat: Pending",
                               proxy="48.45.190.63:42438:hwwrghLD:j432NPbg",
                               group="Snapchat20")
    endpoint = ctrl.open_profile_by_id(info["profile_id"])   # ws:// for Playwright
"""
from __future__ import annotations

import ctypes
import functools
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.logger import logger


def _env_float(name: str, default: float) -> float:
    """Read a non-negative float tuning knob from the environment, falling back
    to ``default`` when unset or invalid. Lets the GUI pacing be sped up / dialed
    back without a code change (e.g. ADSPOWER_UI_NEW_PROFILE_WAIT=0.5)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}

# Every GUI action drives the one real mouse/keyboard, so all profile operations
# (create / open / rename / delete / close) must run one at a time. This must hold
# not just across threads but across *processes*: Nyx (main.py) and Nyxify
# (nyxify_runner.py) run as separate processes and can be active at the same time,
# so an in-process lock alone would let them fight over the mouse.
_GUI_RLOCK = threading.RLock()


class _GuiLock:
    """Serialise AdsPower GUI automation across threads AND across the separate
    Nyx and Nyxify runner processes. The cross-process part is a Windows named
    mutex (released automatically — WAIT_ABANDONED — if a holder crashes); on
    non-Windows it degrades to the in-process lock, which is fine because the GUI
    automation (pywinauto/win32) is Windows-only anyway. The Playwright work that
    follows an open still runs in parallel — only the GUI touchpoints serialise."""

    # Session-local namespace (no "Global\\"): both runners run as the same user
    # in the same session, so they share it without needing elevation.
    _MUTEX_NAME = "NyxSuite.AdsPowerGui.Lock"

    def __init__(self):
        self._mutex = None
        self._win32event = None
        self._fcntl = None
        self._lock_file = None
        try:
            import win32event
            self._win32event = win32event
            self._mutex = win32event.CreateMutex(None, False, self._MUTEX_NAME)
        except Exception:
            self._mutex = None
        if self._mutex is None and sys.platform == "darwin":
            try:
                import fcntl
                self._fcntl = fcntl
                self._lock_file = open(
                    os.path.join(tempfile.gettempdir(), "nyxsuite_adspower_gui.lock"),
                    "a+",
                    encoding="utf-8",
                )
            except Exception:
                self._fcntl = None
                self._lock_file = None

    def __enter__(self):
        _GUI_RLOCK.acquire()                       # intra-process (reentrant) first
        if self._mutex is not None:
            try:
                self._win32event.WaitForSingleObject(self._mutex, self._win32event.INFINITE)
            except Exception:
                pass
        elif self._fcntl is not None and self._lock_file is not None:
            try:
                self._fcntl.flock(self._lock_file.fileno(), self._fcntl.LOCK_EX)
            except Exception:
                pass
        return self

    def __exit__(self, *exc):
        if self._mutex is not None:
            try:
                self._win32event.ReleaseMutex(self._mutex)
            except Exception:
                pass
        elif self._fcntl is not None and self._lock_file is not None:
            try:
                self._fcntl.flock(self._lock_file.fileno(), self._fcntl.LOCK_UN)
            except Exception:
                pass
        _GUI_RLOCK.release()
        return False


_GUI_LOCK = _GuiLock()


# Process-wide record of every profile id a create has already resolved. Profile
# id discovery excludes these so two parallel Nyxify creates can NEVER be handed
# the same id — the root cause of "two profiles merged onto one" and the same
# AdsPower id written into two SnapBoard rows. A transient a11y mis-read of the
# serial watermark used to make discovery return the newest row in the view,
# which could be another task's just-created profile; excluding assigned ids
# (and preferring ids not present before the create) makes that impossible.
_ASSIGNED_IDS_LOCK = threading.Lock()
_ASSIGNED_PROFILE_IDS: set = set()


def _remember_assigned_id(profile_id: str) -> None:
    pid = str(profile_id or "").strip()
    if not pid:
        return
    with _ASSIGNED_IDS_LOCK:
        _ASSIGNED_PROFILE_IDS.add(pid)


def _is_assigned_id(profile_id: str) -> bool:
    pid = str(profile_id or "").strip()
    if not pid:
        return False
    with _ASSIGNED_IDS_LOCK:
        return pid in _ASSIGNED_PROFILE_IDS


def _serialized(fn):
    """Run a controller method under the global (cross-process) GUI lock."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with _GUI_LOCK:
            return fn(self, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# GUI batcher — coalesce concurrent opens/closes into ONE bulk search
# ---------------------------------------------------------------------------
class _BatchResult:
    """Per-id result slot the batch leader signals once it has done the row
    action (or failed) for that id. ``value`` carries an action return (e.g. the
    bool a close produces); opens leave it ``None``."""
    __slots__ = ("event", "ok", "error", "value")

    def __init__(self):
        self.event = threading.Event()
        self.ok = False
        self.error = None
        self.value = None


class _GuiBatcher:
    """Coalesce concurrent profile row-actions (within one process) into a single
    bulk ``Profile ID is <id1> <id2> ...`` search — used for both **open** and
    **close** so whenever more than one profile needs opening/closing at once it
    is one search instead of one per profile.

    The win: each open/close used to run its own full search under the global GUI
    lock (Reset -> paste -> dropdown-match -> list-settle, several seconds). When
    the runner opens/closes many profiles at once they queued behind that lock
    and each one paid for its own search. Here, whichever caller holds the GUI
    lock drains every other waiting caller and searches them ALL in one query,
    then does each row's action.

    Coalescing window: after taking the GUI lock the leader waits for stragglers
    to register, gathering until either no new id has arrived for ``_QUIET`` (the
    runner opens with a small stagger, so arrivals trickle in close together) or a
    hard ``_MAX_WAIT`` cap, or a full page. So ``max_parallel`` opens become ONE
    search of N rather than N searches; a lone action only pays ``_QUIET``.

    Per-process only: Nyx and Nyxify run as separate processes and serialise via
    the named mutex, so a cross-process pair won't share a search — but the
    parallel work that matters happens inside one runner (``max_parallel``)."""

    _QUIET = 0.3        # fire once no new id has arrived for this long
    _MAX_WAIT = 2.5     # absolute cap on the coalescing window
    _MAX_BATCH = 12     # one page of AdsPower results

    def __init__(self, action: str, verb: str):
        self._action = action            # controller method name, e.g. "_bulk_open_locked"
        self._verb = verb                # human word for logs, e.g. "open" / "close"
        self._lock = threading.Lock()    # guards _pending (short holds only)
        self._pending = {}               # profile_id -> _BatchResult
        self._last_add = 0.0             # monotonic time of the most recent register

    def submit(self, controller, profile_id: str):
        """Register ``profile_id`` and block until its row action has run (or
        failed). Returns the action's value (e.g. close's bool); raises
        ``AdsPowerUIError`` on failure."""
        with self._lock:
            res = self._pending.get(profile_id)
            if res is None or res.event.is_set():
                res = _BatchResult()
                self._pending[profile_id] = res
            self._last_add = time.monotonic()
        self._drive(controller, profile_id)
        res.event.wait()
        if not res.ok:
            raise res.error or AdsPowerUIError(
                f"Could not {self._verb} profile {profile_id}.")
        return res.value

    def _drive(self, controller, my_id: str):
        """Become the batch leader under the GUI lock and process all pending ids
        in one search. If our id was already handled by a previous leader while we
        waited for the lock, return immediately (our event is/becomes set)."""
        with _GUI_LOCK:
            with self._lock:
                if my_id not in self._pending:
                    return                       # already handled by a prior leader
            # Gather stragglers OUTSIDE _lock so concurrent submit()s can register.
            start = time.monotonic()
            while True:
                with self._lock:
                    count = len(self._pending)
                    last_add = self._last_add
                now = time.monotonic()
                if count >= self._MAX_BATCH:
                    break
                if (now - last_add) >= self._QUIET:
                    break
                if (now - start) >= self._MAX_WAIT:
                    break
                time.sleep(0.03)
            with self._lock:
                batch = list(self._pending.items())[: self._MAX_BATCH]
                for pid, _res in batch:
                    self._pending.pop(pid, None)
            ids = [pid for pid, _res in batch]
            results = {pid: res for pid, res in batch}
            batch_error = None
            try:
                getattr(controller, self._action)(ids, results)
            except Exception as exc:             # whole-batch failure (e.g. no search bar)
                batch_error = exc
                logger.warning(f"Bulk {self._verb} batch failed: {exc}")
            finally:
                for _pid, res in batch:          # never leave a caller blocked
                    if not res.event.is_set():
                        res.ok = False
                        res.error = res.error or batch_error or AdsPowerUIError(
                            f"Bulk {self._verb} did not complete for {_pid}.")
                        res.event.set()


_OPEN_BATCHER = _GuiBatcher("_bulk_open_locked", "open")
_CLOSE_BATCHER = _GuiBatcher("_bulk_close_locked", "close")


try:
    from pywinauto import Application
    _PYWINAUTO = True
except Exception:  # pragma: no cover
    _PYWINAUTO = False

from core import win_focus
from core import ui_vision

try:
    import win32gui as _WG
    import win32con as _WC
except Exception:
    _WG = _WC = None

# AdsPower main-window title looks like "AdsPower Browser | 8.4.3 | 2.8.6.9".
_WINDOW_TITLE_SUBSTR = "AdsPower Browser |"

# A profile serial/ID in the No./ID column is an 8-ish char alphanumeric with at
# least one letter (e.g. "k1e0lch1"); the No. above it is digits-only.
_PROFILE_ID_RE = re.compile(r"^(?=.*[a-z])[a-z0-9]{7,9}$")

# Failure words that appear in AdsPower's proxy-check result toast/text.
_PROXY_FAIL_WORDS = ("failed", "timed out", "timeout", "unavailable", "unable",
                     "cannot", "error", "invalid", "not available")


class AdsPowerWindowNotFoundError(RuntimeError):
    """AdsPower desktop app is not running / window not found."""


class AdsPowerUIError(RuntimeError):
    """Generic AdsPower GUI-automation failure."""


class AdsPowerProfileNotFoundError(AdsPowerUIError):
    """Raised when a searched-for profile ID is not visible in the AdsPower UI
    table after the filter is applied.  The ``profile_id`` attribute carries the
    missing ID for upstream callers that want to mark it in a task store."""
    def __init__(self, profile_id: str, message: str = ""):
        self.profile_id = profile_id
        super().__init__(message or f"profile does not exist: {profile_id}")


@dataclass
class AdsPowerUIConfig:
    # GUI pacing. The waits below are the dominant "it doesn't immediately act"
    # latency on a create; every one is env-overridable so the pacing can be
    # sped up (or dialed back if a slower machine starts misclicking) without a
    # rebuild. The defaults were trimmed from their original conservative values
    # (new_profile_wait 2.0->1.0, form_settle 0.6->0.35, proxy_check_timeout
    # 12->6) — set the env vars higher again if the form ever isn't ready in time.
    group_name: str = "Snapchat20"
    # Enter the AdsPower *group* in the New Profile form (default on). Set
    # ADSPOWER_UI_SKIP_GROUP=1 to skip it (e.g. when the create form already
    # defaults to the right group).
    skip_group: bool = field(
        default_factory=lambda: _env_bool("ADSPOWER_UI_SKIP_GROUP", False))
    # Nyxify dashboard mode. The operator keeps a single standing 'Name contains
    # <temp>' filter applied; the no-API flow then NEVER searches by profile id.
    # The ONLY search it ever runs is that temp-name filter, and only when it is
    # not already the active one. Profiles are created/opened/renamed/closed/
    # deleted by acting on the rows visible in that view; a row that has dropped
    # out (renamed = done) is simply left alone — no id search.
    #
    # Default OFF: a plain AdsPowerManager() (Nyx and every other caller) uses
    # Nyx mode — bulk Profile-ID search, editing the chip in place. Nyxify opts
    # in via AdsPowerManager(ui_assume_presearch=True). Nyx and Nyxify run as
    # separate processes so they don't share this. Env override applies only when
    # the manager doesn't set it explicitly.
    assume_presearch: bool = field(
        default_factory=lambda: _env_bool("ADSPOWER_UI_ASSUME_PRESEARCH", False))
    # A failing connection test can take longer than a few seconds to surface
    # its "connection test failed" verdict; too short a window times out into the
    # "no verdict -> assume OK" branch and a bad proxy is never rotated. Good
    # proxies still break early on a success marker, so this only bounds the
    # ambiguous/failing case.
    proxy_check_timeout: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_PROXY_CHECK_TIMEOUT", 10.0))
    require_proxy_ok: bool = False     # if True, abort create when proxy check fails
    # Skip the in-form 'Check Proxy' step entirely. The proxy is already
    # validated upstream (Nyxify's _rotate_proxy_until_usable) before create is
    # called, so the in-form recheck is informational only; set
    # ADSPOWER_UI_CHECK_PROXY_IN_FORM=0 to drop it and shave the whole wait.
    check_proxy_in_form: bool = field(
        default_factory=lambda: _env_bool("ADSPOWER_UI_CHECK_PROXY_IN_FORM", True))
    new_profile_wait: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_NEW_PROFILE_WAIT", 1.0))
    form_settle: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_FORM_SETTLE", 0.35))
    create_id_timeout: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_CREATE_ID_TIMEOUT", 25.0))
    # How long to wait for an opened profile's CDP endpoint to come up. Bumped
    # from 25s: when a whole batch (e.g. max_parallel=5) opens at once, the last
    # browsers in the batch can take 40-55s to finish launching and write their
    # DevToolsActivePort, so 25s falsely failed the stragglers.
    open_cdp_timeout: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_OPEN_CDP_TIMEOUT", 60.0))
    click_settle: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_CLICK_SETTLE", 0.06))
    poll_interval: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_POLL_INTERVAL", 0.08))
    reconnect_settle: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_RECONNECT_SETTLE", 0.08))
    proxy_poll_interval: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_PROXY_POLL_INTERVAL", 0.25))
    proxy_parse_timeout: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_PROXY_PARSE_TIMEOUT", 1.2))
    proxy_check_rotation_attempts: int = field(
        default_factory=lambda: int(_env_float("ADSPOWER_UI_PROXY_CHECK_ROTATIONS", 3)))
    macos_paste_settle: float = field(
        default_factory=lambda: _env_float("ADSPOWER_UI_MACOS_PASTE_SETTLE", 0.12))
    capture_templates: bool = True     # auto-snapshot UIA-found controls for the vision fallback


def _pg():
    import pyautogui
    pyautogui.FAILSAFE = False
    # No implicit per-call pause — every place that needs the UI to react places
    # its own explicit (and now much shorter) wait, so this just added latency.
    pyautogui.PAUSE = 0.0
    return pyautogui


def _set_clipboard(text: str):
    if sys.platform == "darwin":
        try:
            from core.adspower_ui_backend_macos import set_clipboard_text
            if set_clipboard_text(str(text)):
                return
        except Exception:
            try:
                subprocess.run(["pbcopy"], input=str(text).encode("utf-8"), check=True)
                return
            except Exception:
                pass
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(str(text), win32clipboard.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        return
    except Exception:
        pass
    try:
        import pyperclip
        pyperclip.copy(str(text))
    except Exception as exc:
        logger.debug(f"clipboard set failed: {exc}")


def _mod_key() -> str:
    return "command" if sys.platform == "darwin" else "ctrl"


def _send_macos_system_events(action: str) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "System Events" to {action}'],
            check=True,
        )
        return True
    except Exception as exc:
        logger.debug(f"System Events action failed ({action!r}): {exc}")
        return False


def _send_select_all_hotkey(pg):
    if _send_macos_system_events('keystroke "a" using command down'):
        return
    pg.hotkey(_mod_key(), "a")


def _send_delete_key(pg, fallback_key: str = "delete"):
    if _send_macos_system_events("key code 51"):
        return
    pg.press(fallback_key)


def _send_paste_hotkey(pg):
    if _send_macos_system_events('keystroke "v" using command down'):
        return
    pg.hotkey(_mod_key(), "v")


def _send_refresh_hotkey(pg) -> bool:
    if sys.platform == "darwin":
        if _send_macos_system_events('keystroke "r" using {command down, shift down}'):
            return True
        pg.hotkey("command", "shift", "r")
        return True
    pg.hotkey("ctrl", "shift", "r")
    return True


class AdsPowerUIController:
    def __init__(self, config: Optional[AdsPowerUIConfig] = None):
        self._backend = None
        if sys.platform == "darwin":
            from core.adspower_ui_backend_macos import MacOSAdsPowerBackend
            self._backend = MacOSAdsPowerBackend()
        elif not _PYWINAUTO:
            raise ImportError("pywinauto is required for AdsPower UI automation "
                              "(pip install pywinauto).")
        self.config = config or AdsPowerUIConfig()
        self._app = None
        self._win = None
        self._hwnd = None
        # The temp-name search fragment last applied by a create — the one
        # standing 'Name contains <temp>' filter the Nyxify flow reuses.
        self._temp_search_fragment = None
        self._temp_search_name = None
        self._a11y_depth = 0
        self._prev_fg = None
        # Callback invoked when a searched-for profile ID is not visible in the
        # UI table after the filter is applied.  Receives ``(profile_id: str)``.
        self.on_profile_missing = None

    # ------------------------------------------------------------------
    # Window / connection
    # ------------------------------------------------------------------

    def _connect(self):
        """Locate AdsPower, bring it foreground so Chromium exposes the UIA tree,
        and return its top window."""
        if self._backend is not None:
            self._win = self._backend.connect()
            self._hwnd = getattr(self._backend, "window_id", None)
            return self._win
        hwnd = win_focus.find_window(_WINDOW_TITLE_SUBSTR)
        if not hwnd:
            raise AdsPowerWindowNotFoundError(
                "AdsPower desktop app not found. Launch AdsPower and sign in.")
        win_focus.ensure_foreground(_WINDOW_TITLE_SUBSTR)
        self._minimize_overlapping_browsers(hwnd)
        # Defensive: __init__ may not have been called (e.g. bare mock controllers
        # in tests) — ensure all instance attributes exist before using them.
        if not hasattr(self, "_hwnd"):
            self._hwnd = None
            self._app = None
        if hwnd != self._hwnd or self._app is None:
            self._app = Application(backend="uia").connect(handle=hwnd, timeout=10)
            self._hwnd = hwnd
        self._win = self._app.window(handle=hwnd)
        self._a11y_depth = getattr(self, "_a11y_depth", 0)
        self._prev_fg = getattr(self, "_prev_fg", None)
        return self._win

    def _refresh_window(self) -> bool:
        backend = getattr(self, "_backend", None)
        refresh = getattr(backend, "refresh_window", None)
        if callable(refresh):
            try:
                return bool(refresh())
            except Exception as exc:
                logger.debug(f"AdsPower window refresh failed: {exc}")
        return False

    def _refresh_dashboard(self):
        """Recover an unresponsive AdsPower dashboard by triggering Window >
        Refresh / the OS hard-refresh shortcut, then reconnecting. Returns True
        if a refresh was issued."""
        backend = getattr(self, "_backend", None)
        issued = False
        logger.warning(
            "AdsPower dashboard appears unresponsive; auto-refreshing "
            "(Window > Refresh / hard refresh hotkey)."
        )
        try:
            if backend is not None and hasattr(backend, "refresh_window"):
                issued = bool(backend.refresh_window())
        except Exception as exc:
            logger.debug(f"AdsPower auto-refresh failed: {exc}")

        if not issued:
            try:
                if backend is not None and hasattr(backend, "foreground"):
                    backend.foreground(force=True)
                else:
                    hwnd = win_focus.ensure_foreground(_WINDOW_TITLE_SUBSTR)
                    if hwnd:
                        self._hwnd = hwnd
                        self._minimize_overlapping_browsers(hwnd)
                issued = bool(_send_refresh_hotkey(_pg()))
            except Exception as exc:
                logger.debug(f"AdsPower hard-refresh hotkey failed: {exc}")
                issued = False

        if not issued:
            return False

        # Let the dashboard re-render, then rebuild our window handle.
        time.sleep(1.5)
        try:
            self._connect()
        except Exception:
            pass
        return issued

    def _a11y_enter(self):
        """Enter a UIA-operation depth level, saving the previous foreground
        window so it can be restored when the outermost operation finishes."""
        if self._a11y_depth == 0:
            if self._backend is not None:
                self._prev_fg = self._backend.current_foreground()
            else:
                self._prev_fg = ctypes.windll.user32.GetForegroundWindow()
            self._connect()      # ensure UIA is connected
            if not self._a11y_available():
                if self._backend is not None:
                    self._backend.foreground()
                else:
                    win_focus.ensure_foreground(_WINDOW_TITLE_SUBSTR)
                self._connect()
        self._a11y_depth += 1

    def _a11y_exit(self):
        """Leave a UIA-operation depth level and restore the previous foreground
        window when the outermost operation finishes."""
        if self._a11y_depth > 0:
            self._a11y_depth -= 1
            if self._a11y_depth == 0 and self._prev_fg:
                try:
                    if self._backend is not None:
                        self._backend.restore_foreground(self._prev_fg)
                    elif _WG and _WG.IsWindow(self._prev_fg):
                        win_focus.force_foreground(self._prev_fg)
                except Exception:
                    pass
                self._prev_fg = None

    def _a11y_available(self):
        """Quick check: does the Chromium UIA accessibility tree contain web
        content?  Returns False when AdsPower is not foreground and Chromium
        has purged the tree."""
        try:
            search = self._win.child_window(
                title="Search or new search criteria", control_type="Edit")
            if search.exists(timeout=0.3):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _minimize_overlapping_browsers(main_hwnd):
        """Minimize any visible windows that might overlap the AdsPower main
        window — owned windows (profile browser panels) and other top-level
        windows whose title suggests an open Snapchat profile."""
        if _WG is None or not main_hwnd or not _WG.IsWindow(main_hwnd):
            return
        try:
            def _enum_cb(eh, _):
                if eh == main_hwnd or not _WG.IsWindowVisible(eh):
                    return True
                try:
                    if _WG.GetWindow(eh, _WC.GW_OWNER) == main_hwnd:
                        _WG.ShowWindow(eh, win_focus.SW_MINIMIZE)
                        return True
                    title = (_WG.GetWindowText(eh) or "").lower()
                    if "snapchat" in title or "bitmoji" in title:
                        _WG.ShowWindow(eh, win_focus.SW_MINIMIZE)
                except Exception:
                    pass
                return True
            _WG.EnumWindows(_enum_cb, None)
        except Exception:
            pass

    def _foreground(self):
        """Minimize any overlapping browser popups so subsequent mouse clicks
        reach AdsPower."""
        if self._backend is not None:
            self._backend.foreground()
            return
        hwnd = win_focus.find_window(_WINDOW_TITLE_SUBSTR)
        if not hwnd:
            return
        if self._hwnd != hwnd:
            self._hwnd = hwnd
        self._minimize_overlapping_browsers(hwnd)

    def _raise_if_plan_limit_popup(self):
        """Check if AdsPower is showing the plan-limit popup and fail fast.

        Scans visible text descendants for the plan-limit message; if found,
        dismisses the popup via Escape and raises ``AdsPowerUIError`` so the
        runner classifies the step as ``adspower_limit_reached`` and marks the
        task FAILED without retrying.
        """
        try:
            for t in self._win.descendants(control_type="Text"):
                try:
                    if not t.is_visible():
                        continue
                    text = (t.window_text() or "").strip()
                    if "current plan allows" in text.lower():
                        _pg().press("esc")
                        time.sleep(0.5)
                        raise AdsPowerUIError(
                            f"AdsPower plan limit reached: {text}"
                        )
                except AdsPowerUIError:
                    raise
                except Exception:
                    continue
        except AdsPowerUIError:
            raise
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Low-level helpers (all resolution-independent: click real rects)
    # ------------------------------------------------------------------

    def _find(self, title: str, control_type: str, timeout: float = 3.0,
              retry: bool = True):
        """Find a control by name+type.

        ``exists(timeout)`` blocks for the *full* timeout when the element is
        absent, so absence checks must pass a short timeout and ``retry=False``
        (otherwise every "is X gone?" probe costs ~timeout*2 + a reconnect).
        Present elements resolve fast. ``retry`` adds one reconnect attempt for
        elements that should exist (the Chromium a11y tree is rebuilt on
        navigation, transiently invalidating cached refs)."""
        try:
            ctrl = self._win.child_window(title=title, control_type=control_type)
            if ctrl.exists(timeout=timeout):
                return ctrl
        except Exception as exc:
            logger.debug(f"_find({title!r},{control_type}) error: {exc}")
        if retry:
            self._connect()
            time.sleep(max(0.02, float(getattr(
                getattr(self, "config", None), "reconnect_settle", 0.08))))
            try:
                ctrl = self._win.child_window(title=title, control_type=control_type)
                if ctrl.exists(timeout=min(timeout, 1.5)):
                    return ctrl
            except Exception:
                pass
        return None

    @staticmethod
    def _center(rect):
        return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2

    @staticmethod
    def _rect_height(rect) -> int:
        return max(0, int(rect.bottom - rect.top)) if rect is not None else 0

    @staticmethod
    def _rect_width(rect) -> int:
        return max(0, int(rect.right - rect.left)) if rect is not None else 0

    def _settle_after_click(self):
        time.sleep(max(0.02, float(getattr(self.config, "click_settle", 0.06))))

    def _invoke_ctrl(self, ctrl) -> bool:
        """Try UIA InvokePattern before falling back to a physical mouse click."""
        try:
            fn = getattr(ctrl, "invoke", None)
            if callable(fn):
                fn()
                self._settle_after_click()
                return True
        except Exception:
            pass
        try:
            ctrl.iface_invoke.Invoke()
            self._settle_after_click()
            return True
        except Exception:
            return False

    def _rect(self, title: str, control_type: str, timeout: float = 3.0):
        """Find a control and capture its screen rectangle *immediately*,
        retrying across reconnects.  pywinauto specs resolve lazily, so holding a
        spec across a page reload and calling ``.rectangle()`` later throws
        ElementNotFound — capturing the geometry up front avoids that.
        Only returns a rect for elements that are actually **visible** on screen
        (``is_visible()`` checks the UIA ``IsOffscreen`` property), so hidden
        tab content or off-screen elements are safely ignored."""
        for attempt in range(2):
            ctrl = self._find(title, control_type,
                              timeout=timeout if attempt == 0 else 1.2, retry=False)
            if ctrl is not None:
                try:
                    r = ctrl.rectangle()
                    if r.width() > 0 and r.height() > 4 and ctrl.is_visible():
                        return r
                except Exception:
                    pass
            self._connect()
            time.sleep(max(0.02, float(getattr(
                getattr(self, "config", None), "reconnect_settle", 0.08))))
        return None

    def _click_rect(self, rect, template_name: str = ""):
        if self.config.capture_templates and template_name and rect is not None:
            try:
                ui_vision.save_template(template_name, rect.left, rect.top,
                                        rect.width(), rect.height())
            except Exception:
                pass
        _pg().click(*self._center(rect))
        self._settle_after_click()

    @staticmethod
    def _rect_ltrb(left, top, right, bottom):
        """Build a lightweight rect object with .left/.top/.right/.bottom."""
        class _R:
            __slots__ = ("left", "top", "right", "bottom")
            def __init__(s, l, t, r, b):
                s.left, s.top, s.right, s.bottom = l, t, r, b
        return _R(left, top, right, bottom)

    def _click_xy(self, x: int, y: int):
        _pg().click(x, y)
        self._settle_after_click()

    def _click_vision(self, template_name: str) -> bool:
        m = ui_vision.locate(template_name)
        if m:
            logger.info(f"UIA miss; located {template_name!r} via vision (score={m.score:.2f}).")
            self._click_xy(m.x, m.y)
            return True
        return False

    def _paste_rect(self, rect, text: str, clear: bool = True):
        """Focus an edit (by rect), clear it (text + any filter chips), paste."""
        pg = _pg()
        if text:
            _set_clipboard(text)
        pg.click(*self._center(rect))
        raw_macos_paste = sys.platform == "darwin" and not clear
        macos_paste_settle = max(0.02, float(getattr(
            getattr(self, "config", None), "macos_paste_settle", 0.12)))
        time.sleep(macos_paste_settle if raw_macos_paste else 0.05)
        if clear:
            _send_select_all_hotkey(pg)
            _send_delete_key(pg)
            for _ in range(3):          # backspace clears leftover filter chips
                _send_delete_key(pg, fallback_key="backspace")
        if text:
            _send_paste_hotkey(pg)
            time.sleep(macos_paste_settle if raw_macos_paste else 0.06)

    def _type_rect(self, rect, text: str, interval: float = 0.06):
        pg = _pg()
        pg.click(*self._center(rect))
        time.sleep(0.05)
        pg.hotkey(_mod_key(), "a")
        pg.press("delete")
        time.sleep(0.06)
        pg.typewrite(text, interval=interval)
        time.sleep(0.1)

    # ------------------------------------------------------------------
    # CREATE PROFILE
    # ------------------------------------------------------------------

    @_serialized
    def create_profile(
        self,
        name: str,
        proxy: str,
        group: Optional[str] = None,
        proxy_rotator: Optional[Callable[..., str]] = None,
    ) -> dict:
        """Create a profile through the GUI. Returns a dict with the resolved
        ``profile_id`` (discovered from the Profiles list), ``name``, ``group``,
        ``proxy`` and ``proxy_passed``."""
        group = (self.config.group_name if group is None else str(group or "")).strip()
        name = name.strip()
        logger.info(
            f"AdsPower UI: creating profile name={name!r} "
            f"group={'<skipped>' if self.config.skip_group else group!r} "
            f"proxy={proxy.split(':')[0]}:***")

        self._connect()
        self._goto_profiles()

        # Nyxify must stay in the standing temp-name dashboard view. This applies
        # the filter once at startup and skips later creates when it is already
        # the exact active search.
        if self.config.assume_presearch:
            self._ensure_temp_filter(name)

        # Record the highest existing serial. AdsPower serials increase
        # monotonically and the list is newest-first, so the profile we are
        # about to create will be the first row with serial > this watermark —
        # dup-safe even if several profiles share the temp name. In
        # assume_presearch mode we watermark the CURRENT filtered view without
        # touching the operator's standing filter (Reset would wipe it).
        if self.config.assume_presearch:
            before_max = self._max_serial_in_view()
        else:
            before_max = self._max_serial()
        # Snapshot the ids already visible before we create. The just-created
        # profile is the id that appears afterward but is NOT in this set — a
        # signal that does not depend on the serial watermark being read
        # correctly, so a transient a11y glitch can't make us return another
        # task's profile.
        before_ids = self._visible_profile_ids()
        logger.debug(f"Serial watermark before create: {before_max}; {len(before_ids)} ids in view")

        self._open_new_profile_form()
        self._switch_tab("General")
        self._fill_name(name)
        # Group is intentionally NOT entered when skip_group is set: the operator
        # pre-selects the AdsPower group in the dashboard, so the new profile
        # inherits it and the in-form autocomplete is pure latency.
        if group and not self.config.skip_group:
            self._select_group(group)

        self._switch_tab("Proxy")
        time.sleep(self.config.form_settle)
        active_proxy = str(proxy or "").strip()
        self._fill_proxy(active_proxy)
        if self.config.check_proxy_in_form:
            proxy_ok, active_proxy = self._check_proxy_with_rotation(active_proxy, proxy_rotator)
            if not proxy_ok and self.config.require_proxy_ok:
                raise AdsPowerUIError("Proxy check did not pass; aborting profile creation.")
        else:
            # Proxy already validated upstream; skip the redundant in-form recheck.
            proxy_ok = True

        self._click_ok()

        # After OK, make the temp-name filter active so the new row shows — but
        # ONLY search when it isn't already applied (skip the search when it is).
        # Then resolve the new id by scanning that view for the row whose serial
        # exceeds the watermark — no profile-id search. Legacy path re-searches.
        if self.config.assume_presearch:
            self._ensure_temp_filter(name)
            profile_id = self._wait_for_new_profile_id_in_view(name, before_max, before_ids)
        else:
            profile_id = self._wait_for_new_profile_id(name, before_max, before_ids)
        _remember_assigned_id(profile_id)
        logger.info(f"AdsPower UI: created profile {profile_id or '<unknown>'} ({name!r}).")
        return {
            "profile_id": profile_id,
            "name": name,
            "group": group,
            "proxy": active_proxy,
            "proxy_passed": proxy_ok,
        }

    def _open_new_profile_form(self):
        # If a form is already open, close it first (Cancel / Escape) to get a
        # clean slate — a stale validation-blocked form can't be reused.
        if self._find("OK", "Button", timeout=0.8, retry=False):
            cancel = self._rect("Cancel", "Text", timeout=1)
            if cancel is not None:
                self._click_rect(cancel)
                time.sleep(0.8)
            else:
                _pg().press("esc")
                time.sleep(0.5)
            self._foreground()
            self._connect()
        rect = self._rect("New Profile", "Button", timeout=3)
        if rect is None:
            # The New Profile button never resolved: the dashboard is very likely
            # unresponsive (a known macOS AdsPower quirk under heavy GUI
            # automation). Auto-refresh (Window > Refresh) and retry before the
            # vision fallback / raising, so a stuck dashboard self-recovers.
            for _ in range(2):
                if not self._refresh_dashboard():
                    break
                rect = self._rect("New Profile", "Button", timeout=4)
                if rect is not None:
                    break
        if rect is not None:
            self._click_rect(rect, template_name="new_profile_btn")
        elif not self._click_vision("new_profile_btn"):
            raise AdsPowerUIError("Could not find the 'New Profile' button.")
        timeout = max(
            1.0,
            float(getattr(self.config, "new_profile_wait", 1.0))
            + float(getattr(self.config, "form_settle", 0.35))
            + 3.0,
        )
        poll = max(0.03, float(getattr(self.config, "poll_interval", 0.08)))
        deadline = time.time() + timeout
        form_opened = False
        while time.time() < deadline:
            self._foreground()
            self._connect()
            self._raise_if_plan_limit_popup()
            if self._find("OK", "Button", timeout=0.15, retry=False):
                form_opened = True
                break
            time.sleep(poll)
        if not form_opened:
            raise AdsPowerUIError("New Profile form did not open.")

    def _switch_tab(self, tab: str):
        rect = self._rect(tab, "Text", timeout=3)
        if rect is not None:
            self._click_rect(rect)
            time.sleep(min(0.12, max(0.02, float(getattr(self.config, "form_settle", 0.35)))))
        else:
            logger.warning(f"Tab {tab!r} not found via UIA; assuming already active.")

    def _edit_right_of_label(self, label: str, y_tolerance: int = 18):
        self._connect()
        labels = []
        for t in self._win.descendants(control_type="Text"):
            try:
                if not t.is_visible():
                    continue
                if (t.window_text() or "").strip().lower() != label.strip().lower():
                    continue
                r = t.rectangle()
                if r.width() > 0 and r.height() > 0:
                    labels.append(r)
            except Exception:
                continue
        if not labels:
            return None
        edits = []
        for e in self._win.descendants(control_type="Edit"):
            try:
                if not e.is_visible():
                    continue
                r = e.rectangle()
                if r.width() > 0 and r.height() > 4:
                    edits.append(r)
            except Exception:
                continue
        for label_rect in sorted(labels, key=lambda r: (r.top, r.left)):
            label_y = (label_rect.top + label_rect.bottom) // 2
            row_edits = [
                r for r in edits
                if r.left > label_rect.right
                and abs(((r.top + r.bottom) // 2) - label_y) <= y_tolerance
            ]
            if row_edits:
                return max(row_edits, key=lambda r: r.width())
        return None

    def _fill_name(self, name: str):
        rect = self._rect("Optional: profile name", "Edit", timeout=1.5)
        if rect is None:
            rect = self._edit_right_of_label("Name")
        if rect is None:
            raise AdsPowerUIError("Profile name field not found.")
        self._paste_rect(rect, name)
        logger.info(f"Filled profile name: {name!r}")

    def _select_group(self, group: str):
        """Open the AdsPower group dropdown and select the option matching the
        Nyxify ``adspower_group`` setting. The Group field is required, and the
        list is long/scrollable, so this types the configured name to filter the
        list, then clicks the option whose text matches EXACTLY — retrying while
        the dropdown renders and scrolling the list if the match isn't on screen
        — instead of a single shot + blind keyboard pick that could land on a
        neighbouring group (e.g. 'Snapchat2' vs 'Snapchat20')."""
        rect = self._rect("Find a group", "Edit", timeout=3)
        if rect is None:
            logger.warning("Group field not found; profile will use the default group.")
            return
        target = group.strip()
        # Windows needed typing to trigger AdsPower's group autocomplete. On
        # macOS/Electron, pasting is the reliable path; typewrite can leave the
        # field unchanged even though it clicked the right AX rect.
        if sys.platform == "darwin":
            self._paste_rect(rect, target)
        else:
            self._type_rect(rect, target, interval=0.08)
        if self._pick_group_option(target, below_top=rect.bottom):
            self._log_group_selection(target)
            return
        # The group list is scrollable; if the exact match isn't in the rendered
        # window, scroll the dropdown a few times and retry the exact-match click.
        pg = _pg()
        scroll_x = (rect.left + rect.right) // 2
        scroll_y = rect.bottom + 120
        for _ in range(6):
            pg.scroll(-300, x=scroll_x, y=scroll_y)
            time.sleep(0.25)
            if self._pick_group_option(target, below_top=rect.bottom, timeout=0.6):
                self._log_group_selection(target)
                return
        # Last resort only: the top filtered suggestion after typing the exact
        # name is normally the group itself. Warn — this is the one path that
        # could pick a near-match if the configured group name doesn't exist.
        logger.warning(f"Group {target!r} not found by exact match in the dropdown; "
                       f"accepting the top filtered suggestion.")
        pg.press("down")
        time.sleep(0.2)
        pg.press("enter")
        time.sleep(0.3)
        self._log_group_selection(target)

    def _pick_group_option(self, group: str, below_top: int, timeout: float = 2.5) -> bool:
        """Retry the exact-match dropdown click for ``timeout`` seconds (the
        filtered options render after a short, variable delay)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._connect()
            if self._click_dropdown_option(group, below_top=below_top):
                return True
            time.sleep(0.2)
        return False

    def _log_group_selection(self, group: str):
        time.sleep(0.3)
        try:
            val = self._find("Find a group", "Edit", timeout=1)
            current = (val.get_value() or "").strip() if val else ""
        except Exception:
            current = ""
        if current and current.strip().lower() != group.strip().lower():
            logger.warning(f"Group field shows {current!r} after selecting {group!r}.")
        else:
            logger.info(f"Selected group {group!r}"
                        + (f" (field now: {current!r})." if current else "."))

    def _click_dropdown_option(self, text: str, below_top: int) -> bool:
        """Click the dropdown option whose text matches ``text`` EXACTLY
        (case-insensitive, trimmed) and is rendered below ``below_top``. Exact
        match only, so a configured 'Snapchat2' never selects 'Snapchat20'."""
        target = str(text or "").strip().lower()
        if not target:
            return False
        for ct in ("ListItem", "Text"):
            for d in self._win.descendants(control_type=ct):
                try:
                    if (d.window_text() or "").strip().lower() != target:
                        continue
                    if not d.is_visible():
                        continue
                    r = d.rectangle()
                    if r.top > below_top and r.width() > 0:
                        try:
                            self._click_xy(*self._center(r))
                            return True
                        except AdsPowerUIError:
                            return False
                except Exception:
                    continue
        return False

    @staticmethod
    def _split_proxy(proxy: str):
        parts = [p.strip() for p in str(proxy or "").strip().split(":")]
        host = parts[0] if len(parts) >= 1 else ""
        port = parts[1] if len(parts) >= 2 else ""
        user = parts[2] if len(parts) >= 3 else ""
        password = ":".join(parts[3:]).strip() if len(parts) >= 4 else ""
        return host, port, user, password

    def _proxy_edit_rects(self, host_rect):
        """Visible proxy edit boxes in left-to-right/top-to-bottom order.

        The AdsPower Proxy tab has changed placeholder names across builds. The
        Host edit is the stable anchor, so this collects the nearby edits from
        that form area and lets `_fill_proxy` set host/port/user/password by
        position when named fields are not available.
        """
        rects = []
        for e in self._win.descendants(control_type="Edit"):
            try:
                if not e.is_visible():
                    continue
                r = e.rectangle()
                if r.width() <= 0 or r.height() <= 0:
                    continue
                if r.top < host_rect.top - 40 or r.top > host_rect.top + 260:
                    continue
                rects.append(r)
            except Exception:
                continue
        rects.sort(key=lambda r: (r.top, r.left))
        return rects

    def _stabilize_proxy_form_scroll(self):
        """On macOS, normalize the Proxy tab scroll position before pasting.

        AdsPower can reopen the New Profile form with the Proxy tab scrolled so
        Host:Port is pinned near the tab header. In that clipped position,
        Electron sometimes focuses the field but drops the paste event. Scrolling
        upward first brings the Custom/Proxy type area back into view and gives
        the Host input the same stable geometry as a fresh form.
        """
        if sys.platform != "darwin":
            return
        pg = _pg()
        for _ in range(6):
            self._connect()
            host = self._rect("Please enter host", "Edit", timeout=0.5)
            if host is not None and host.top >= 360:
                return
            try:
                wr = self._win.rectangle()
                pg.scroll(6, x=(wr.left + wr.right) // 2, y=(wr.top + wr.bottom) // 2)
            except Exception:
                pg.scroll(6)
            time.sleep(0.15)

    def _fill_proxy(self, proxy: str):
        """Paste the full proxy string into the Host field. AdsPower auto-parses
        ``host:port:user:pass`` into host/port/user/pass; type defaults to Socks5
        and IP checker to IP2Location (verified)."""
        if sys.platform == "darwin":
            self._stabilize_proxy_form_scroll()
        host = self._rect("Please enter host", "Edit", timeout=4)
        if host is None and sys.platform == "darwin":
            # macOS AdsPower exposes clipped below-fold proxy fields in AX before
            # they are actually visible. Scroll the form until Host has a real
            # hit target, then paste.
            pg = _pg()
            for _ in range(8):
                try:
                    wr = self._win.rectangle()
                    pg.scroll(-5, x=(wr.left + wr.right) // 2, y=(wr.top + wr.bottom) // 2)
                except Exception:
                    pg.scroll(-5)
                time.sleep(0.2)
                self._connect()
                host = self._rect("Please enter host", "Edit", timeout=0.8)
                if host is not None:
                    break
        if host is None:
            raise AdsPowerUIError("Proxy Host field not found (is the Proxy tab open?).")
        self._paste_rect(host, proxy.strip(), clear=(sys.platform != "darwin"))
        # Best-effort sanity log of the parsed result.
        parsed_port = ""
        parse_deadline = time.time() + max(
            0.1,
            float(getattr(getattr(self, "config", None), "proxy_parse_timeout", 1.2)),
        )
        poll = max(
            0.03,
            float(getattr(getattr(self, "config", None), "proxy_poll_interval", 0.25)),
        )
        try:
            while time.time() < parse_deadline:
                self._connect()
                port = self._find("Port", "Edit", timeout=0.2, retry=False)
                parsed_port = str(port.get_value() or "").strip() if port else ""
                if parsed_port and parsed_port.lower() != "port":
                    break
                time.sleep(min(poll, max(0.0, parse_deadline - time.time())))
        except Exception:
            pass
        if parsed_port.lower() == "port":
            parts = self._split_proxy(proxy)
            rects = self._proxy_edit_rects(host)
            # Host, Port, Username, Password are the four visible edits after
            # proxy type / checker on the custom proxy form.
            proxy_rects = [r for r in rects if r.top >= host.top - 4][:4]
            if len(proxy_rects) >= 4:
                for rect, value in zip(proxy_rects, parts):
                    self._paste_rect(rect, value)
                    time.sleep(0.1)
                try:
                    port = self._find(parts[1], "Edit", timeout=0.5, retry=False)
                    parsed_port = str(port.get_value() or "").strip() if port else parts[1]
                except Exception:
                    parsed_port = parts[1]
                logger.info("Proxy auto-parse unavailable; filled host/port/user/password individually.")
        logger.info(f"Proxy pasted; parsed port={parsed_port!r}.")

    def _check_proxy(self) -> bool:
        btn = self._rect("Check Proxy", "Button", timeout=3)
        if btn is None:
            logger.warning("Check Proxy button not found; skipping proxy verification.")
            return False
        self._click_rect(btn, template_name="check_proxy_btn")
        logger.info("Clicked 'Check Proxy'; waiting for result...")
        deadline = time.time() + self.config.proxy_check_timeout
        poll = max(0.03, float(getattr(self.config, "proxy_poll_interval", 0.25)))
        result = None
        while time.time() < deadline:
            text = self._visible_text_blob()
            low = text.lower()
            if any(w in low for w in _PROXY_FAIL_WORDS):
                result = False
                break
            # success markers: a country/IP echoed, or explicit success words
            if "success" in low or "connected" in low or "available" in low:
                result = True
                break
            time.sleep(min(poll, max(0.0, deadline - time.time())))
        if result is None:
            # No explicit verdict within the window — assume OK (production
            # pre-validates proxies via SnapBoard before we ever get here). Logged
            # at WARNING so a proxy that silently never rendered a verdict — and so
            # was never rotated — is visible in the logs.
            result = True
            logger.warning(
                "Proxy check: no explicit verdict within "
                f"{self.config.proxy_check_timeout:.0f}s; proceeding (assumed OK)."
            )
        else:
            logger.info(f"Proxy check verdict: {'OK' if result else 'FAILED'}.")
        return result

    def _check_proxy_with_rotation(self, proxy: str, proxy_rotator=None):
        active_proxy = str(proxy or "").strip()
        proxy_ok = self._check_proxy()
        if proxy_ok:
            return True, active_proxy

        max_attempts = max(
            0,
            int(getattr(self.config, "proxy_check_rotation_attempts", 3) or 0),
        )
        for attempt in range(1, max_attempts + 1):
            if not callable(proxy_rotator):
                break
            next_proxy = self._rotate_failed_proxy(
                proxy_rotator,
                current_proxy=active_proxy,
                attempt=attempt,
            )
            if not next_proxy:
                break
            if next_proxy == active_proxy:
                logger.warning(
                    "Proxy rotator returned the same proxy after GUI check failure; "
                    "stopping rotation."
                )
                break
            active_proxy = next_proxy
            logger.info(
                f"Proxy check failed in AdsPower GUI; trying rotated proxy "
                f"{attempt}/{max_attempts}: {active_proxy.split(':')[0]}:***"
            )
            self._fill_proxy(active_proxy)
            proxy_ok = self._check_proxy()
            if proxy_ok:
                return True, active_proxy
        return False, active_proxy

    @staticmethod
    def _rotate_failed_proxy(proxy_rotator, **kwargs) -> str:
        try:
            value = proxy_rotator(**kwargs, reason="gui_proxy_check_failed")
        except TypeError:
            value = proxy_rotator(kwargs.get("current_proxy"), kwargs.get("attempt"))
        return str(value or "").strip()

    def _click_ok(self):
        btn = self._rect("OK", "Button", timeout=1.5)
        if btn is not None:
            self._click_rect(btn, template_name="ok_btn")
        elif not self._click_vision("ok_btn"):
            raise AdsPowerUIError("Could not find the form OK button.")
        self._raise_if_plan_limit_popup()
        # Wait for the form to close (OK gone / New Profile button back).
        deadline = time.time() + 5
        while time.time() < deadline:
            self._connect()
            if not self._find("OK", "Button", timeout=0.3, retry=False):
                logger.info("Profile form submitted (OK closed).")
                return
            time.sleep(0.08)
        logger.warning("OK still present after submit — possible validation error.")

    # ------------------------------------------------------------------
    # PROFILE DISCOVERY (Profiles list)
    # ------------------------------------------------------------------

    _ROW_ID_HEADERS = {"id", "no./id"}
    _ROW_DATA_HEADERS = {"id", "no./id", "group", "name", "ip", "action"}
    _IGNORED_ROW_HEADERS = {
        "#", "no.", "last opened", "last\xa0opened", "date created",
        "platform", "tags", "custom no.",
    }
    _HEADER_LABELS = {
        "id", "no./id", "no.", "group", "name", "ip", "last opened", "last\xa0opened",
        "platform", "tags", "date created", "custom no.", "#", "action",
        "profiles", "proxies", "trash", "cloud phone", "reset", "and",
        "referral bonus", "active", "employee", "overview",
    }

    @staticmethod
    def _norm_text(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").replace("\xa0", " ")).strip().lower()

    @classmethod
    def _is_row_data_header(cls, text: str) -> bool:
        return cls._norm_text(text) in cls._ROW_DATA_HEADERS

    @classmethod
    def _is_ignored_row_header(cls, text: str) -> bool:
        return cls._norm_text(text) in {cls._norm_text(v) for v in cls._IGNORED_ROW_HEADERS}

    @staticmethod
    def _is_profile_id_text(text: str) -> bool:
        return bool(_PROFILE_ID_RE.match(str(text or "").strip().lower()))

    @staticmethod
    def _rect_center_y(rect) -> int:
        return (rect.top + rect.bottom) // 2

    def _visible_text_items(self, fast: bool = False):
        """Visible text controls as ``(text, rect)`` pairs.

        ``fast=True`` avoids an explicit ``is_visible`` call and uses the cached
        element_info shape, matching the old hot-path behavior in ``_scan_rows``.
        Row-action helpers use the stricter default.
        """
        for t in self._win.descendants(control_type="Text"):
            try:
                if fast:
                    try:
                        info = t.element_info
                        text = (info.name or "").strip()
                        rect = info.rectangle
                    except Exception:
                        text = (t.window_text() or "").strip()
                        rect = t.rectangle()
                else:
                    if not t.is_visible():
                        continue
                    text = (t.window_text() or "").strip()
                    rect = t.rectangle()
                if not text:
                    continue
                if rect.width() <= 0 or rect.height() <= 0:
                    continue
                yield text, rect
            except Exception:
                continue

    def _id_column_visible(self) -> bool:
        """True when AdsPower exposes an `ID` or legacy `No./ID` table header."""
        try:
            for text, _rect in self._visible_text_items():
                if self._norm_text(text) in self._ROW_ID_HEADERS:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _id_column_required_message(profile_id: str) -> str:
        return (
            f"Could not locate profile {profile_id} because the AdsPower ID column "
            "is not visible. Open AdsPower List Settings and enable the ID column."
        )

    @staticmethod
    def _search_fragment(name: str) -> str:
        """The part of the temporary name to type into a 'Name contains' search:
        the text after the last ':' so the shared 'Snapchat:' prefix is dropped
        ('Snapchat: Pending' -> 'Pending'). Searching the distinctive suffix keeps
        the result set tight; the EXACT full name is still used for the row match.
        Falls back to the whole (separator-stripped) name when there's no suffix
        ('Snapchat:' -> 'Snapchat')."""
        parts = [p.strip() for p in str(name or "").split(":") if p.strip()]
        return parts[-1] if parts else str(name or "").strip()

    def _rows_for_name(self, name: str):
        """Search 'Name contains <suffix of the temp name>' and return rows whose
        full name matches ``name`` exactly.

        ``name`` is the Nyxify ``temporary_profile_name`` setting (e.g.
        'Snapchat: Pending'). We type only the distinctive suffix ('Pending') into
        the 'Name contains' filter — dropping the shared 'Snapchat:' prefix — then
        keep only rows whose full name equals the configured temp name. AdsPower
        lists newest-first, so the just-created row lands on page 1; the caller
        pairs this with a serial watermark to stay correct under concurrent
        creators."""
        target = name.strip().lower()
        fragment = self._search_fragment(name)
        for attempt in range(2):
            self._search_by(
                fragment,
                field="Name",
                operator="contains",
                allow_enter_fallback=False,
                verify=True,
            )
            rows = [r for r in self._scan_rows() if r[2].strip().lower() == target and r[1]]
            if rows:
                return rows
        return []

    def _visible_profile_ids(self) -> set:
        """Set of profile ids currently visible in the Profiles list."""
        try:
            return {r[1] for r in self._scan_rows() if r[1]}
        except Exception:
            return set()

    def _pick_created_id(self, rows, before_max, before_ids, name=""):
        """Choose the just-created profile's id from scanned ``rows``.

        Priority, each excluding ids already handed to another create
        (``_is_assigned_id``) so two parallel creates can never collide:
          1. an id that was NOT visible before this create (the strongest,
             glitch-proof signal that it is the new row),
          2. else a row whose serial exceeds the pre-create watermark,
          3. else nothing (caller keeps polling / falls back).
        Within a tier an exact temp-name match is preferred (disambiguates
        another differently-named fresh row); highest serial breaks remaining
        ties (newest = just created).
        """
        before_ids = before_ids or set()
        target = str(name or "").strip().lower()
        usable = [r for r in rows if r[1] and not _is_assigned_id(r[1])]

        def _choose(candidates):
            if not candidates:
                return ""
            if target:
                named = [r for r in candidates if r[2].strip().lower() == target]
                if named:
                    candidates = named
            return sorted(candidates, reverse=True)[0][1]

        new_ids = [r for r in usable if r[1] not in before_ids]
        pid = _choose(new_ids)
        if pid:
            return pid

        return _choose([r for r in usable if r[0] > before_max])

    def _wait_for_new_profile_id(self, name: str, before_max: int, before_ids=None) -> str:
        """Poll until the just-created profile (a newly-appeared / above-watermark
        id, not one already assigned to another create) appears; return its id."""
        deadline = time.time() + self.config.create_id_timeout
        while time.time() < deadline:
            pid = self._pick_created_id(self._scan_rows(), before_max, before_ids, name)
            if pid:
                return pid
            pid = self._pick_created_id(self._rows_for_name(name), before_max, before_ids, name)
            if pid:
                return pid
            time.sleep(1.2)
        logger.warning(f"Could not resolve new profile id for name {name!r}.")
        return ""

    def _wait_for_new_profile_id_in_view(self, name: str, before_max: int, before_ids=None) -> str:
        """Resolve the just-created profile id by scanning the CURRENT filtered
        view — no fresh search. The operator keeps a standing 'Name contains
        <temp>' filter applied, so right after OK the new row simply appears in
        that view; it is the id that was not present before the create (and is
        not one already assigned to a concurrent create).

        Degrades gracefully: if no fresh row shows up in the current view within
        the timeout (the standing filter wasn't applied), it falls back to ONE
        name search rather than failing the create."""
        deadline = time.time() + self.config.create_id_timeout
        while time.time() < deadline:
            pid = self._pick_created_id(self._scan_rows(), before_max, before_ids, name)
            if pid:
                return pid
            time.sleep(0.8)
        logger.warning(
            f"No new row appeared in the current view for {name!r}; falling back to a "
            f"one-time name search. Keep the 'Name contains' filter applied in AdsPower "
            f"to avoid this slow path.")
        pid = self._pick_created_id(self._rows_for_name(name), before_max, before_ids, name)
        if pid:
            return pid
        logger.warning(f"Could not resolve new profile id for name {name!r}.")
        return ""

    @_serialized
    def find_profile_id_by_name(self, name: str) -> Optional[str]:
        self._connect()
        rows = self._rows_for_name(name)
        if rows:
            rows.sort(reverse=True)
            return rows[0][1]
        return None

    def _max_serial(self) -> int:
        """Highest serial currently on page 1 (newest-first). Scanned twice to
        tolerate a slow list reload after Reset."""
        self._reset_search()
        self._wait_list_settled()
        best = 0
        for _ in range(2):
            rows = self._scan_rows()
            serials = [r[0] for r in rows]
            if serials:
                best = max(best, max(serials))
            time.sleep(0.6)
        return best

    def _max_serial_in_view(self) -> int:
        """Highest serial currently visible WITHOUT touching the active search.

        assume_presearch mode: the operator keeps a standing 'Name contains
        <temp>' filter applied, so the view already shows only the temp-named
        profiles. We must NOT Reset it (that is what _max_serial does) —
        resetting would wipe the operator's filter and force a slow full
        re-scan. Scanned twice to tolerate a late row render. Returns 0 when the
        filter currently matches nothing (the first create), which is correct:
        any created row then has a serial > 0."""
        self._connect()
        self._wait_list_settled(timeout=5.0)
        best = 0
        for _ in range(2):
            serials = [r[0] for r in self._scan_rows()]
            if serials:
                best = max(best, max(serials))
            time.sleep(0.3)
        return best

    def _active_search_filters(self):
        """Parsed active search/filter labels visible in AdsPower.

        AdsPower can show filters as chips in the search bar and inside the
        filter-count menu. Treat both as active state, and parse only complete
        chip labels such as "Name contains x" or "Profile ID is k1...".
        """
        self._connect()
        filters = []
        seen = set()
        prefixes = (
            ("name_contains", "name contains"),
            ("profile_id", "profile id is"),
            ("profile_id", "profile no./id is"),
        )
        for t in self._win.descendants(control_type="Text"):
            try:
                if not t.is_visible():
                    continue
                s = (t.window_text() or "").strip()
                low = re.sub(r"\s+", " ", s.lower())
                for kind, prefix in prefixes:
                    if not low.startswith(prefix):
                        continue
                    value = s[len(prefix):].strip()
                    if not value:
                        continue
                    key = (kind, value.lower())
                    if key not in seen:
                        seen.add(key)
                        filters.append((kind, value, s))
                    break
            except Exception:
                continue
        return filters

    def _standing_name_filter(self) -> Optional[str]:
        """Value of the active 'Name contains <X>' filter chip, or None when no
        such chip is applied. This is the operator's standing temp-name search —
        the one and only search the Nyxify flow ever uses."""
        for kind, value, _text in self._active_search_filters():
            if kind == "name_contains":
                return value
        return None

    def _exact_temp_filter_active(self, fragment: str) -> bool:
        target = fragment.strip().lower()
        filters = self._active_search_filters()
        if len(filters) != 1:
            return False
        kind, value, _text = filters[0]
        return kind == "name_contains" and value.strip().lower() == target

    def _ensure_temp_filter(self, name: str):
        """Make the temp-name 'Name contains <temp>' filter the active search —
        but ONLY search when it isn't already applied. Called right after a
        create's OK so the new row is visible; when the operator's standing
        filter is already that temp name (the steady state across creates) this
        is a no-op, so consecutive creates never re-search or reset to default.

        This is the ONLY place the Nyxify flow initiates a search, and the search
        is always the temp name — never a profile id."""
        temp_name = str(name or "").strip()
        if temp_name:
            self._temp_search_name = temp_name
        fragment = self._search_fragment(temp_name)
        if not fragment:
            return
        self._temp_search_fragment = fragment
        if self._exact_temp_filter_active(fragment):
            logger.info(
                f"AdsPower UI: temp-name filter 'Name contains {fragment}' already "
                f"active; not searching again.")
            return
        active = self._active_search_filters()
        if active:
            logger.info(
                "AdsPower UI: resetting non-temp or mixed filters before applying "
                f"'Name contains {fragment}': "
                + ", ".join(text for _kind, _value, text in active)
            )
            self._reset_search()
        last_error = None
        for _attempt in range(2):
            logger.info(f"AdsPower UI: applying temp-name filter 'Name contains {fragment}'.")
            try:
                self._search_by(
                    fragment,
                    field="Name",
                    operator="contains",
                    allow_enter_fallback=False,
                    verify=True,
                )
            except AdsPowerUIError as exc:
                last_error = exc
                logger.warning(f"Strict temp-name search attempt failed: {exc}")
                self._reset_search()
                continue
            if self._exact_temp_filter_active(fragment):
                return
            logger.warning(
                f"AdsPower UI: temp-name search did not leave exactly one "
                f"'Name contains {fragment}' filter; resetting and retrying.")
            self._reset_search()
        raise AdsPowerUIError(
            f"Could not apply the exact temp-name filter 'Name contains {fragment}'."
            + (f" Last error: {last_error}" if last_error else ""))

    def _prepare_rows_for_action(self, ids):
        """Make ``ids`` actionable before a row action (open/close/delete/rename).

        Nyxify (assume_presearch): never search by profile id. Just settle the
        CURRENT view and let the caller act on whatever rows are visible under
        the standing temp-name filter — a just-created/renamed row lingers there.
        A target that has dropped out of the view (renamed = done) is left alone
        by the caller; we do NOT search for it.

        Nyx (legacy): bulk Profile-ID search. ``_search_by_ids`` tries the
        in-place chip edit first (~1.5s, no navigation) and only the cold full
        search navigates — and it does that itself when the search bar is
        missing. We deliberately do NOT call ``_goto_profiles`` here: it costs a
        ~16s cold UIA tree walk and was being paid on every single batch."""
        if self.config.assume_presearch:
            self._connect()
            self._wait_list_settled(timeout=3.0)
            return
        self._connect()
        self._search_by_ids(ids, append=False)

    def _scan_rows(self):
        """Parse the visible Profiles list into ``(serial:int, profile_id:str,
        name:str)`` tuples.

        Position-independent: rows are anchored by the visible AdsPower profile
        ID itself. Optional chronology/order columns (``#``, old numeric No.,
        dates, platform, tags, etc.) are ignored so user column order and sorting
        never decide which profile row is acted on.
        """
        header_bottoms = []
        name_headers = []
        ip_headers = []
        ids = []       # (center_y, top, left, height, str)
        names = []     # (center_y, top, left, str)
        for s, r in self._visible_text_items(fast=True):
            low = self._norm_text(s)
            if low in self._HEADER_LABELS:
                if low in self._ROW_DATA_HEADERS:
                    header_bottoms.append(r.bottom)
                if low == "name":
                    name_headers.append(r)
                elif low == "ip":
                    ip_headers.append(r)
                continue
            if low.startswith("profile id is") or low.startswith("profile no./id is") or "filter" in low:
                continue
            if self._is_profile_id_text(s):
                ids.append((self._rect_center_y(r), r.top, r.left, max(1, r.height()), s))
            elif low not in self._HEADER_LABELS and not low.startswith("profile id is") and not low.startswith("profile no./id is") and "filter" not in low:
                names.append((self._rect_center_y(r), r.top, r.left, s))

        header_bottom = max(header_bottoms) if header_bottoms else None
        rows = []
        seen = set()
        for cy, top, left, height, pid in sorted(ids, key=lambda item: (item[1], item[2])):
            if header_bottom is not None and cy <= header_bottom:
                continue
            pid_key = pid.strip().lower()
            if pid_key in seen:
                continue
            seen.add(pid_key)
            tol = max(18, int(round(1.8 * height)))
            name_header = min(name_headers, key=lambda r: abs(r.left - left), default=None)
            ip_header = min(ip_headers, key=lambda r: abs(r.left - left), default=None)
            if name_header is not None:
                name_left = name_header.left - 40
                name_right = (
                    ip_header.left - 12
                    if ip_header is not None and ip_header.left > name_header.left
                    else name_header.right + 280
                )
                row_names = [
                    item for item in names
                    if name_left <= item[2] <= name_right
                ]
            else:
                row_names = names
            same_band_names = [
                (abs(ncy - cy), nleft, name)
                for (ncy, _ntop, nleft, name) in row_names
                if abs(ncy - cy) <= tol
            ]
            rname = ""
            if same_band_names:
                same_band_names.sort(key=lambda item: (item[0], item[1]))
                rname = same_band_names[0][2]
            rows.append((0, pid, rname))
        return rows

    # ------------------------------------------------------------------
    # SEARCH + OPEN BY ID
    # ------------------------------------------------------------------

    def _goto_profiles(self):
        # If a form is open (OK button present), cancel it first.
        if self._find("OK", "Button", timeout=0.8, retry=False):
            cancel = self._rect("Cancel", "Text", timeout=1)
            if cancel is not None:
                self._click_rect(cancel)
                time.sleep(1.0)
                self._foreground()
                self._connect()
            else:
                _pg().press("esc")
                time.sleep(0.5)
                self._connect()
        nav = self._rect("Profiles", "Text", timeout=1)
        if nav is not None:
            self._click_rect(nav)
            # Allow a brief moment for re-render, then probe once.
            time.sleep(0.12)
            self._connect()
            if not self._find("Search or new search criteria", "Edit", timeout=1.5, retry=False):
                logger.warning("Profiles nav click did not land on the Profiles tab; retrying...")
                nav = self._rect("Profiles", "Text", timeout=2)
                if nav is not None:
                    self._click_rect(nav)
                    time.sleep(0.12)
                    self._connect()
            if not self._find("Search or new search criteria", "Edit", timeout=1.5, retry=False):
                logger.warning("Retry of Profiles nav click also did not land on the Profiles tab.")
        self._connect()

    def _reset_search(self):
        """Remove any active search/filter via the 'Reset' link (user req:
        'first remove the current search'). 'Reset' only renders when a filter is
        active, so this is a fast absence-tolerant check."""
        self._connect()
        ctrl = self._find("Reset", "Text", timeout=0.8, retry=False)
        if ctrl is None:
            return
        try:
            r = ctrl.rectangle()
            self._click_rect(r)
            self._connect()
            self._wait_list_settled()   # return as soon as the unfiltered list renders
        except Exception:
            pass

    def _search_by(self, value: str, field: str, operator: str,
                   allow_enter_fallback: bool = True, verify: bool = False):
        """Clear the current search, type ``value``, and apply the
        ``field``+``operator`` filter (e.g. 'Profile ID'/'is' or 'Name'/'contains').

        Tries the dropdown-row match first (no Enter fast path — AdsPower's
        default suggestion is unreliable).  Falls back to Enter only when the
        dropdown row can't be found within the timeout.

        Note: this is the COLD path only — a warm re-search edits the existing
        chip in place (``_search_by_ids_via_chip``) and never gets here."""
        self._connect()
        self._reset_search()
        search = self._rect("Search or new search criteria", "Edit", timeout=4)
        if search is None:
            self._goto_profiles()
            search = self._rect("Search or new search criteria", "Edit", timeout=4)
        if search is None:
            # Profiles page never rendered its search bar — auto-refresh the
            # (likely unresponsive) dashboard and try once more before raising.
            if self._refresh_dashboard():
                self._goto_profiles()
                search = self._rect("Search or new search criteria", "Edit", timeout=4)
        if search is None:
            raise AdsPowerUIError("Search bar not found on the Profiles page.")
        below = search.bottom
        # When chips are already present, AdsPower moves the "Search or new
        # search criteria" edit to the right, but the dropdown suggestions still
        # anchor near the left edge of the whole search control. Use a broader,
        # rect-derived threshold so the exact Name/Profile row is not filtered
        # out just because an old chip is visible.
        left_min = max(0, search.left - 260)
        self._paste_rect(search, value)

        # Try the dropdown-row match first (no Enter).  Poll for the dropdown to
        # render, then click the exact field+operator suggestion.
        clicked = False
        deadline = time.time() + (7.0 if field.strip().lower() == "profile id" else 4.5)
        while time.time() < deadline:
            self._connect()
            if self._click_dropdown_row(
                field, operator, below_top=below, left_min=left_min, value=value
            ):
                clicked = True
                break
            time.sleep(0.15)
        if not clicked:
            if not allow_enter_fallback:
                raise AdsPowerUIError(
                    f"Dropdown row {field!r}/{operator!r} not found; refusing "
                    "to press Enter because AdsPower may apply the wrong filter."
                )
            logger.warning(
                f"Dropdown row {field!r}/{operator!r} not found; pressing Enter "
                f"(may apply the wrong default filter).")
            _pg().press("enter")
        self._foreground()
        self._connect()
        self._wait_list_settled()
        if verify and not self._search_applied(field, value):
            raise AdsPowerUIError(
                f"Search did not apply as '{field} {operator} {value}'.")

    def _search_applied(self, field: str, value: str) -> bool:
        """True when the active filter chip matches the requested ``field`` and
        ``value`` — used to verify the fast Enter-applied search before trusting
        it (so a wrong default like 'Name contains' for a Profile-ID search is
        caught and corrected)."""
        fl = field.strip().lower()
        if fl == "profile id":
            chip = self._profile_id_chip()
            if not chip:
                return False
            first = str(value).split()[0].strip().lower()
            return bool(first) and first in chip[1].lower()
        if fl == "name":
            return self._exact_temp_filter_active(str(value))
        return False

    @staticmethod
    def _parse_chip_ids(chip_text: str):
        """Parse space-separated profile IDs from a chip label like
        ``'Profile ID is id1 id2 id3'`` or ``'Profile No./ID is id1 id2 id3'``.
        Returns a list of normalized profile IDs."""
        text = (chip_text or "").strip()
        for prefix in ("profile id is", "profile no./id is"):
            idx = text.lower().find(prefix)
            if idx >= 0:
                text = text[idx + len(prefix):].strip()
                break
        return [t for t in text.split() if t.strip()]

    def _search_by_ids(self, ids, append=False):
        """Bulk ``Profile ID is <id1> <id2> ...`` search. AdsPower matches every
        space-separated id and shows them on one page, so a whole batch of opens
        shares ONE search instead of one (slow) search per profile. A single id
        is just a one-element bulk search — same path as a plain id search.

        Fast path: while Nyx is running there is already a ``Profile ID is ...``
        filter chip in the search bar from the previous batch. Editing that chip
        in place (click chip -> merge or replace ids -> Confirm) skips the slow
        Reset + re-type + dropdown-row-match dance, so each subsequent batch
        re-searches in a couple of clicks.

        When ``append`` is True, new IDs are merged into the existing chip. The
        production Nyx path uses one id at a time with ``append=False`` so an
        existing Profile-ID filter is edited in place instead of stacking another
        filter or carrying old IDs forward. Falls back to the full search when
        no chip is present or the inline editor fails."""
        new_ids = [str(i).strip() for i in ids if str(i).strip()]
        if not new_ids:
            raise AdsPowerUIError("_search_by_ids requires at least one profile id.")

        # If appending and a chip exists, merge old + new IDs (deduplicated)
        if append:
            chip = self._profile_id_chip()
            if chip is not None:
                existing = self._parse_chip_ids(chip[1])
                merged = list(dict.fromkeys(existing + new_ids))  # ordered dedup
                if merged:
                    query = " ".join(merged)
                    try:
                        if self._search_by_ids_via_chip(query):
                            return
                    except Exception as exc:
                        logger.debug(f"Chip-append re-search failed ({exc}); using full search.")

        query = " ".join(new_ids)
        try:
            if self._search_by_ids_via_chip(query):
                return
        except Exception as exc:
            logger.debug(f"Chip-edit re-search failed ({exc}); using full search.")
        # The chip edit failed — ensure any leftover popup is dismissed, then
        # fully reset the search before creating a fresh filter.  Skipping the
        # reset here is the root cause of the "double filter" bug where a second
        # "Profile ID is ..." chip is stacked on top of the first, producing
        # zero results.
        try:
            _pg().press("esc")
            time.sleep(0.06)
            self._reset_search()
        except Exception:
            pass  # best-effort; _search_by below also resets
        self._search_by(query, field="Profile ID", operator="is", verify=True)

    def _remove_ids_from_chip(self, ids):
        """Remove the given profile IDs from the active 'Profile ID is ...' chip.
        If all IDs are removed, resets the search entirely. No-op when no chip
        is present or none of the given IDs are in the chip."""
        remove_set = set(str(i).strip().lower() for i in ids if str(i).strip())
        if not remove_set:
            return

        self._connect()
        chip = self._profile_id_chip()
        if chip is None:
            return
        chip_rect, chip_text = chip
        existing = self._parse_chip_ids(chip_text)
        if not existing:
            return

        filtered = [pid for pid in existing if pid.lower() not in remove_set]
        if len(filtered) == len(existing):
            return  # none of the given ids were in the chip

        if not filtered:
            # All IDs removed — fully reset the search
            try:
                self._reset_search()
            except Exception as exc:
                logger.debug(f"Chip reset after removing all ids failed ({exc}); ignoring.")
            return

        # Update chip with the remaining IDs
        query = " ".join(filtered)
        try:
            self._search_by_ids_via_chip(query)
        except Exception as exc:
            logger.debug(f"Chip update after removing ids failed ({exc}); ignoring.")

    def _profile_id_chip(self):
        """Return ``(rect, text)`` of the active 'Profile ID is ...' filter chip in
        the search bar, or ``None`` if no such chip is present. AdsPower has used
        both 'Profile ID is' and 'Profile No./ID is' for this chip."""
        self._connect()
        for t in self._win.descendants(control_type="Text"):
            try:
                if not t.is_visible():
                    continue
                s = (t.window_text() or "").strip()
                low = s.lower()
                if not (low.startswith("profile id is")
                        or low.startswith("profile no./id is")):
                    continue
                r = t.rectangle()
                if r.width() > 0 and r.height() > 0:
                    return r, s
            except Exception:
                continue
        return None

    def _chip_editor_edit_rect(self, below_top: int):
        """Rect of the ids Edit inside the opened chip-editor popup. The popup
        ('Profile ID' / 'is' / ids box / Confirm) drops just below the chip and
        holds two Edits: the narrow operator box and the wide ids box. We want the
        wide ids box, so pick the WIDEST visible Edit that opened just below the
        chip (``below_top``) — never the group filter above it or the main search
        box."""
        self._connect()
        best = None
        best_w = 0
        for e in self._win.descendants(control_type="Edit"):
            try:
                if not e.is_visible():
                    continue
                if (e.window_text() or "").strip().lower() == "search or new search criteria":
                    continue
                r = e.rectangle()
                w, h = r.width(), r.height()
                if w <= 0 or h <= 0:
                    continue
                if not (below_top < r.top <= below_top + 220):
                    continue                    # only the popup that just opened
                if w > best_w:
                    best_w, best = w, r
            except Exception:
                continue
        return best

    def _click_confirm_chip(self, below_top: int = 0) -> bool:
        """Click the chip-editor 'Confirm' control (a Button in some AdsPower
        builds, a Text in others) — the one in the popup below the chip."""
        for ct in ("Button", "Text"):
            best = None
            for c in self._win.descendants(control_type=ct):
                try:
                    if not c.is_visible():
                        continue
                    if (c.window_text() or "").strip().lower() != "confirm":
                        continue
                    r = c.rectangle()
                    if r.width() > 0 and r.height() > 0 and r.top >= below_top:
                        if best is None or r.top < best.top:
                            best = r
                except Exception:
                    continue
            if best is not None:
                try:
                    self._click_rect(best)
                    return True
                except AdsPowerUIError:
                    return False
        return False

    def _search_by_ids_via_chip(self, query: str) -> bool:
        """Re-search by editing the existing 'Profile ID is ...' chip in place.
        Returns True when the search was applied via the chip; False when there is
        no chip to edit (or the editor failed) so the caller runs the full search.

        Only the Profile-ID chip is touched, so a leftover 'Name contains' chip
        (Nyxify) is ignored and the caller's full search resets it."""
        self._connect()
        chip = self._profile_id_chip()
        if chip is None:
            return False
        chip_rect, chip_text = chip
        # Skip edit if query already matches the current chip IDs (same set)
        existing_ids = set(self._parse_chip_ids(chip_text))
        new_ids = set(query.split())
        if existing_ids == new_ids:
            return True
        # Click the chip body, left of its trailing remove 'x', to open the editor.
        cx = chip_rect.left + min(40, max(8, chip_rect.width() // 4))
        cy = (chip_rect.top + chip_rect.bottom) // 2
        self._click_xy(cx, cy)
        # Let the editor popup render, then try once. 0.15s is enough for the CEF
        # popup to appear — scanning the full UIA tree for it on every poll cycle
        # is far more expensive than a short fixed wait.
        self._connect()
        time.sleep(0.15)
        edit = self._chip_editor_edit_rect(below_top=chip_rect.bottom)
        if edit is None:
            # One retry with a small delay; the popup can be slow on first open.
            time.sleep(0.2)
            self._connect()
            edit = self._chip_editor_edit_rect(below_top=chip_rect.bottom)
            if edit is None:
                return False
        # Once the editor popup is open, ANY bail-out path must close it (Esc) —
        # a leftover popup pollutes the caller's fallback full search (its Reset /
        # dropdown match then land in the popup and the filter never applies).
        success = False
        try:
            self._paste_rect(edit, query)
            if not self._click_confirm_chip(below_top=chip_rect.bottom):
                _pg().press("enter")
            # Allow the search to start, then probe the chip once. 0.2s covers
            # the typical AdsPower round-trip without scanning the tree every 50ms.
            time.sleep(0.2)
            self._foreground()
            self._connect()
            self._wait_list_settled()
            # Confirm the chip now reflects the new query; otherwise the edit
            # silently failed and we must not act on a stale result set.
            after = self._profile_id_chip()
            first_id = query.split()[0]
            success = after is not None and first_id.lower() in after[1].lower()
            return success
        finally:
            if not success:
                try:
                    _pg().press("esc")
                    time.sleep(0.06)
                except Exception:
                    pass

    def _click_dropdown_row(self, field: str, operator: str, below_top: int,
                            left_min: int = 460, value: str = "") -> bool:
        """Click the suggestion row containing both ``field`` and ``operator``
        labels. AdsPower renders each suggestion as separate Text controls
        (field / operator / value) sharing a row top, so we cluster by top.

        ``left_min`` is the left edge of the suggestion column — it aligns under
        the search box (~L480), NOT far right, so the threshold is derived from
        the search box position. (The old hard-coded ``left > 600`` skipped the
        whole dropdown, so 'Name contains' never matched and the search fell back
        to AdsPower's default 'Profile No./ID is' — searching by id, not name.)"""
        # Collect suggestion labels (below the search bar, in the suggestion
        # column). A suggestion's field/operator/value labels share a row band.
        # The row grouping + click point are computed by the pure, zoom-agnostic
        # ``_dropdown_click_target`` so they can be regression-tested at every
        # resolution/zoom without a live AdsPower (see tests).
        items = []
        for t in self._win.descendants(control_type="Text"):
            try:
                s = (t.window_text() or "").strip()
                r = t.rectangle()
                if (s and r.width() > 0 and r.height() > 0
                        and r.top > below_top and r.left >= left_min):
                    items.append((r.top, r.left, r.right, r.bottom, s.lower()))
            except Exception:
                continue
        target = self._dropdown_click_target(items, field, operator, value=value)
        if target is None:
            return False
        cx, cy = target
        try:
            self._click_xy(cx, cy)
            return True
        except AdsPowerUIError:
            return False

    @staticmethod
    def _dropdown_click_target(items, field: str, operator: str, value: str = ""):
        """Pure geometry: from the dropdown's Text rects ``(top, left, right,
        bottom, lower_label)`` (already filtered to the suggestion region),
        return the ``(x, y)`` to click for the ``field``+``operator`` suggestion
        row, or ``None`` when no row matches.

        Two macOS hardening changes over the old union-box-centre click, which
        could land *beside* the row — the reported "clicking on the side of Name
        contains":

        * Rows are grouped by **vertical-overlap ratio**, not a fixed pixel gap.
          The old ``abs(top - prev_top) <= 12`` merged two adjacent suggestions
          into one "row" whenever AdsPower was zoomed out (rows sit <12px apart),
          and the click then landed in the seam between "Name contains" and
          "Profile ID is". Overlap-ratio grouping is scale-free, so it holds at
          any window zoom and screen resolution.
        * For a Name search we click the matched **field-label element itself**
          (the "Name contains" text), which always sits on the real clickable
          row — never in the gap between the field label and the echoed value.
          The Profile-ID (Nyx) path keeps its original row-centre click so that
          working flow is untouched.
        """
        fl, op = field.lower(), operator.lower()

        def field_matches(label: str) -> bool:
            if fl == "profile id":
                # AdsPower builds have used both "Profile ID" and
                # "Profile No./ID" in this dropdown. Either is the exact ID
                # search we want; requiring a literal match makes the code fall
                # back to Enter, which can pick the wrong suggestion.
                return "profile" in label and "id" in label
            if fl == "name":
                # AdsPower can render "Name contains" as a single combined
                # Text element rather than separate "Name" + "contains"
                # controls.  A substring check handles both layouts.
                return "name" in label
            return label == fl

        # Group into visual rows by vertical-span overlap (zoom-independent):
        # two labels share a row when their [top, bottom] intervals overlap by
        # more than half the shorter label's height. Text on one line overlaps
        # ~fully; distinct rows never overlap, so no fixed threshold is needed.
        rows = []
        bands = []                                   # parallel (band_top, band_bottom)
        for it in sorted(items):                     # by top, then left
            top, bottom = it[0], it[3]
            placed = False
            if rows:
                band_top, band_bottom = bands[-1]
                overlap = min(bottom, band_bottom) - max(top, band_top)
                shorter = max(1, min(bottom - top, band_bottom - band_top))
                if overlap >= 0.5 * shorter:
                    rows[-1].append(it)
                    bands[-1] = (min(band_top, top), max(band_bottom, bottom))
                    placed = True
            if not placed:
                rows.append([it])
                bands.append((top, bottom))

        expected_tokens = [
            tok for tok in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if tok
        ]
        for row in rows:
            labels = {x[4] for x in row}
            row_text = " ".join(label for *_coords, label in row)
            operator_matches = op in labels or re.search(
                rf"(^|[^a-z0-9]){re.escape(op)}([^a-z0-9]|$)", row_text
            )
            field_elems = [it for it in row if field_matches(it[4])]
            if not field_elems:
                continue
            if not operator_matches:
                continue
            if expected_tokens and not all(tok in row_text for tok in expected_tokens):
                continue
            if fl == "name":
                # Click the field-label element itself — dead-centre on the real
                # "Name contains" row, never in the seam beside it.
                target = min(field_elems, key=lambda el: el[1])   # leftmost match
                cx = (target[1] + target[2]) // 2
                cy = (target[0] + target[3]) // 2
            else:
                # Nyx Profile-ID search: original row-centre click, unchanged.
                cx = (min(x[1] for x in row) + max(x[2] for x in row)) // 2
                cy = (min(x[0] for x in row) + max(x[3] for x in row)) // 2
            return (cx, cy)
        return None

    def _wait_list_settled(self, timeout: float = 15.0):
        """Wait until the Profiles list finishes (re)loading: either rows appear
        or an explicit empty-state is shown. Prevents scanning mid-reload.
        The default timeout is generous because AdsPower can take 10+ seconds to
        render Profile ID search results when the dropdown doesn't trigger."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._scan_rows():
                return
            blob = self._visible_text_blob().lower()
            if "no data" in blob or "total: 0" in blob:
                return
            time.sleep(0.12)

    def open_profile_by_id(self, profile_id: str) -> str:
        """Open the profile in the AdsPower GUI via the per-row Action button,
        then return a live Playwright ``ws://`` CDP endpoint.

        Only the GUI interaction (search + click Open) holds the global GUI lock;
        the ``already open?`` probe and the post-click CDP-resolve wait are plain
        HTTP polls, so they run *unlocked* — another task can drive its own open
        while this profile's browser is still launching, instead of every open
        serialising end-to-end (important when the runner opens many in parallel)."""
        from core.adspower_cdp import find_open_profile_cdp_endpoint

        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise AdsPowerUIError("open_profile_by_id requires a profile id.")

        # Already open? Use the cheap direct cache-dir match (deep_scan=False) —
        # the deep fallback HTTP-probes every open browser and is pathologically
        # slow when many profiles are open and this one isn't.
        endpoint = find_open_profile_cdp_endpoint(profile_id, deep_scan=False)
        if endpoint:
            logger.info(f"Profile {profile_id} already open: {endpoint}")
            return endpoint

        # Ensure the profile row is visible (scan current view first, search if
        # not found), then click its per-row "Open" Action button.
        with _GUI_LOCK:
            self._a11y_enter()
            try:
                self._ensure_row_visible(profile_id)
                self._click_row_action(profile_id, "Open", template_name="open_profile_by_id")
            finally:
                self._a11y_exit()

        deadline = time.time() + self.config.open_cdp_timeout
        attempts = 0
        while time.time() < deadline:
            deep_scan = attempts >= 10 and (attempts % 5 == 0)
            endpoint = find_open_profile_cdp_endpoint(profile_id, deep_scan=deep_scan)
            if endpoint:
                logger.info(f"Profile {profile_id} opened via GUI; CDP: {endpoint}")
                return endpoint
            attempts += 1
            time.sleep(0.8)
        raise AdsPowerUIError(
            f"Opened profile {profile_id} in the GUI but could not resolve its CDP "
            f"endpoint. Is the browser still launching?")

    def _bulk_open_locked(self, ids, results):
        """Leader path (the GUI lock is already held by ``_OPEN_BATCHER``): ONE
        bulk ``Profile ID is <id...>`` search for the whole batch, then **tick the
        checkbox of every row and click the single toolbar 'Open' button** so they
        all open at once.

        Why not the per-row Action 'Open' buttons: each one launches a browser
        window that pops over the AdsPower list, so the *next* per-row click landed
        on the wrong place and was lost. Ticking checkboxes opens nothing, so every
        selection click lands; the lone toolbar click then opens the whole
        selection simultaneously. Per-id failures stay isolated."""
        self._connect()
        # Nyxify: act on the rows visible under the standing temp-name filter —
        # the just-created row is right there, so no search. (Legacy mode bulk
        # Profile-ID searches inside _prepare_rows_for_action.)
        self._prepare_rows_for_action(ids)
        self._bulk_row_action(ids, results, needs_label="Open", done_label="Close",
                              toolbar_label="Open", verb="open")

    def _bulk_close_locked(self, ids, results):
        """Leader path (the GUI lock is already held by ``_CLOSE_BATCHER``): tick
        the checkbox of every running row in the current view and click the single
        toolbar 'Close' button. Each result's ``value`` is the close bool.

        Nyxify never searches by id: a finished (renamed) profile that has dropped
        out of the temp-name view is treated as already done — ``missing_ok``
        resolves it without a Profile-ID search instead of failing it."""
        self._connect()
        self._prepare_rows_for_action(ids)
        self._bulk_row_action(ids, results, needs_label="Close", done_label="Open",
                              toolbar_label="Close", verb="close",
                              confirm=True, wait_done=True,
                              missing_ok=self.config.assume_presearch)
        # After closing, remove the closed IDs from the search chip so the next
        # open batch can add fresh ones without the chip growing unboundedly.
        # Leave the Profile-ID chip in place in Nyx mode. The next one-by-one
        # open/close edits that chip instead of rebuilding the search or stacking
        # a second filter.

    def _bulk_row_action(self, ids, results, *, needs_label, done_label,
                         toolbar_label, verb, confirm=False, wait_done=False,
                         missing_ok=False):
        """Select every batch row that still needs the action (tick its checkbox)
        then click the SINGLE toolbar button (``toolbar_label``) to act on the
        whole selection at once — the popup-proof replacement for per-row clicks.

        ``needs_label`` is the Action button a row shows when it still needs the
        action ('Open' to open, 'Close' to close); ``done_label`` is what it shows
        once already in the target state. Rows already in the target state succeed
        without being touched; rows that can't be resolved fail just for that id.
        ``confirm`` clicks a confirmation dialog if one appears; ``wait_done``
        waits for each acted row to reach ``done_label`` and records the bool.

        ``missing_ok`` (Nyxify close): an id whose row isn't in the current
        temp-name view is treated as already done — a renamed/finished profile
        dropped out of the filter — and resolved successfully WITHOUT searching
        for it by id, rather than failed."""
        pending_value = True if wait_done else None

        # Resolve every row's state from as few whole-batch snapshots as possible
        # (AdsPower's CEF tree is huge — a per-id scan loop would be pathological).
        # _search_by_ids already waited for the list to settle, so the first scan
        # usually resolves everything; the short retries only cover a late redraw
        # (buttons render slightly after the row text).
        info = {}
        for attempt in range(5):
            self._connect()
            info = self._batch_action_snapshot(ids, needs_label, done_label)
            unresolved = [pid for pid in ids
                          if results.get(pid) is not None and not results[pid].event.is_set()
                          and not (info[pid]["needs"] or info[pid]["done"])]
            if not unresolved:
                break
            time.sleep(0.2)

        selected = []
        for pid in ids:
            res = results.get(pid)
            if res is None or res.event.is_set():
                continue
            entry = info.get(pid, {})
            if entry.get("done"):
                res.ok = True
                res.value = pending_value
                res.event.set()
                logger.info(f"Profile {pid} already {verb}d (row shows {done_label!r}); "
                            f"no action needed.")
            elif entry.get("needs"):
                if entry.get("checkbox") is not None:
                    self._click_rect(entry["checkbox"])
                elif entry.get("checkbox_x") is not None and entry.get("row_y") is not None:
                    # No CheckBox control (AdsPower renders the tick as a plain
                    # clickable cell); click the derived checkbox column x.
                    self._click_xy(entry["checkbox_x"], entry["row_y"])
                else:
                    res.ok = False
                    res.error = AdsPowerUIError(
                        f"Found row for {pid} but not its checkbox to {verb}.")
                    res.event.set()
                    logger.warning(f"Bulk {verb}: no checkbox for {pid}; skipped.")
                    continue
                selected.append(pid)
            elif missing_ok:
                # Nyxify rule: the row dropped out of the temp-name view, which
                # means the profile was already renamed/finished — leave it alone
                # and resolve it as done; never search for it by id.
                res.ok = True
                res.value = pending_value
                res.event.set()
                logger.info(f"Profile {pid} not in the temp-name view (renamed/done); "
                            f"no id search — treating {verb} as already handled.")
            else:
                res.ok = False
                res.error = AdsPowerUIError(
                    f"Could not find the row for profile {pid} to {verb}.")
                res.event.set()
                logger.warning(f"Bulk {verb}: row for {pid} not resolved; skipped.")

        if not selected:
            return

        # Sanity check the selection — search returns exactly the batch, so the
        # count should match what we ticked (a mismatch only warns; every visible
        # row is still a batch row, so the toolbar action stays safe).
        sel_count = self._selected_count()
        if sel_count not in (-1, 0) and sel_count != len(selected):
            logger.warning(f"Bulk {verb}: ticked {len(selected)} rows but AdsPower "
                           f"reports {sel_count} selected.")

        rect = self._toolbar_action_rect(toolbar_label)
        if rect is None:
            raise AdsPowerUIError(
                f"Toolbar {toolbar_label!r} button not found for the bulk {verb}.")
        self._click_rect(rect, template_name=f"toolbar_{verb}_btn")
        logger.info(f"AdsPower UI: one toolbar {toolbar_label} click {verb}d "
                    f"{len(selected)} profile(s): {' '.join(selected)}")
        if confirm:
            self._maybe_confirm(("OK", "Confirm", "Yes"), timeout=0.8)

        if wait_done:
            # Close: confirm each acted row reverted to its done state (Open).
            done = self._wait_rows_done(selected, done_label, timeout=12.0)
            for pid in selected:
                res = results.get(pid)
                if res is None or res.event.is_set():
                    continue
                ok = pid in done
                res.ok, res.value = ok, ok
                if not ok:
                    res.error = AdsPowerUIError(
                        f"Clicked {toolbar_label} for {pid} but it did not {verb}.")
                    logger.warning(f"Bulk {verb}: {pid} not confirmed {verb}d.")
                res.event.set()
        else:
            # Open: browsers launch asynchronously — the unlocked CDP-resolve wait
            # in open_profile_by_id confirms the real open; the toolbar click done.
            for pid in selected:
                res = results.get(pid)
                if res is None or res.event.is_set():
                    continue
                res.ok = True
                res.event.set()

    def _batch_action_snapshot(self, ids, needs_label, done_label):
        """ONE UIA pass for the whole batch: per id resolve its row centre-y, its
        No./ID cell left edge, whether the row currently shows the ``needs_label``
        / ``done_label`` Action button, its checkbox rect (if any), and a derived
        checkbox-click x for the fallback. Only controls below the column headers
        count, so the titlebar and toolbar buttons (and the header select-all /
        'Opened' checkbox) are ignored.

        Resolution-independent: the row-alignment tolerance and the checkbox x are
        derived from the measured No./ID cell *height* (and the click x is clamped
        to fall left of the cell), so they scale with DPI/zoom instead of relying
        on fixed pixel offsets."""
        id_set = set(ids)
        header_bottoms = []
        row_y, row_x, row_h = {}, {}, {}
        for t in self._win.descendants(control_type="Text"):
            try:
                if not t.is_visible():
                    continue
                s = (t.window_text() or "").strip()
                if not s:
                    continue
                r = t.rectangle()
                if r.width() <= 0:
                    continue
                if self._is_row_data_header(s):
                    header_bottoms.append(r.bottom)
                elif s in id_set:
                    row_y[s] = (r.top + r.bottom) // 2
                    row_x[s] = r.left
                    row_h[s] = max(1, r.bottom - r.top)
            except Exception:
                continue
        header_bottom = max(header_bottoms) if header_bottoms else 460

        # Derive a DPI-relative row tolerance + checkbox offset from the cell
        # height (≈15px at 100%). tol stays under half the row pitch; the checkbox
        # column sits ~2.3 cell-heights left of the No./ID text.
        cell_h = self._median(list(row_h.values())) or 15
        tol = max(12, int(round(1.6 * cell_h)))
        cb_dx = max(16, int(round(2.3 * cell_h)))

        needs_btns, done_btns = [], []
        for b in self._win.descendants(control_type="Button"):
            try:
                if not b.is_visible():
                    continue
                label = (b.window_text() or "").strip()
                if label not in (needs_label, done_label):
                    continue
                r = b.rectangle()
                if r.width() <= 0 or r.height() <= 0:
                    continue
                cy = (r.top + r.bottom) // 2
                if cy <= header_bottom:
                    continue                    # toolbar button, not a row button
                (needs_btns if label == needs_label else done_btns).append(cy)
            except Exception:
                continue

        checkboxes = []
        for cb in self._win.descendants(control_type="CheckBox"):
            try:
                if not cb.is_visible():
                    continue
                r = cb.rectangle()
                if r.width() <= 0 or r.height() <= 0:
                    continue
                cy = (r.top + r.bottom) // 2
                if cy <= header_bottom:
                    continue                    # header select-all / 'Opened' filter
                checkboxes.append((cy, r.left, r))
            except Exception:
                continue

        info = {}
        for pid in ids:
            y = row_y.get(pid)
            id_left = row_x.get(pid)
            entry = {"row_y": y, "id_left": id_left, "needs": False, "done": False,
                     "checkbox": None,
                     "checkbox_x": (id_left - cb_dx) if id_left is not None else None}
            if y is not None:
                entry["needs"] = any(abs(cy - y) <= tol for cy in needs_btns)
                entry["done"] = any(abs(cy - y) <= tol for cy in done_btns)
                aligned = [(left, rect) for (cy, left, rect) in checkboxes
                           if abs(cy - y) <= tol]
                if aligned:
                    aligned.sort(key=lambda a: a[0])   # by left — RECTs aren't orderable
                    entry["checkbox"] = aligned[0][1]
            info[pid] = entry
        return info

    @staticmethod
    def _median(values):
        vals = sorted(v for v in values if v)
        n = len(vals)
        if n == 0:
            return 0
        mid = n // 2
        return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) // 2

    def _toolbar_action_rect(self, label: str):
        """Rect of the batch-toolbar button named ``label`` ('Open' / 'Close') —
        the one in the toolbar row, ABOVE the column headers (per-row Action
        buttons live below them).

        The window's titlebar **Close (X)** is *also* a Button named 'Close' and
        sits even higher (cy ~ 0), so picking the topmost match would hit the X
        and minimise the app instead of closing profiles. The toolbar sits just
        above the headers, so among the above-header matches we take the one with
        the LARGEST cy (closest to the headers) — that's the toolbar button, never
        the titlebar X."""
        self._connect()
        header_bottom = self._list_header_bottom()
        best = None
        best_cy = -1
        for b in self._win.descendants(control_type="Button"):
            try:
                if not b.is_visible() or (b.window_text() or "").strip() != label:
                    continue
                r = b.rectangle()
                cy = (r.top + r.bottom) // 2
                if r.width() > 0 and r.height() > 0 and cy < header_bottom:
                    if cy > best_cy:        # toolbar row sits just above the headers
                        best_cy, best = cy, r
            except Exception:
                continue
        return best

    def _wait_rows_done(self, pids, done_label, timeout: float = 12.0):
        """Poll the whole selection in one snapshot per round until every row shows
        ``done_label`` (or timeout). Returns the set of ids confirmed done."""
        remaining = set(pids)
        done = set()
        deadline = time.time() + timeout
        while remaining and time.time() < deadline:
            self._connect()
            info = self._batch_action_snapshot(list(remaining), "\x00", done_label)
            for pid in list(remaining):
                if info.get(pid, {}).get("done"):
                    done.add(pid)
                    remaining.discard(pid)
            if remaining:
                time.sleep(0.2)
        return done

    def _close_one_in_batch(self, profile_id: str, wait_timeout: float = 12.0) -> bool:
        """Close ``profile_id``'s row within the bulk-search results (aligned to
        its own row — many Close buttons are visible at once during a bulk close).
        Returns True once the row's action reverts to Open (or it wasn't running).
        Relies on ``_click_row_action``'s robust retry rather than a fixed
        deadline (a single scan of AdsPower's huge tree can take 10-40s)."""
        self._connect()
        try:
            self._click_row_action(profile_id, "Close", template_name="close_btn",
                                   require_aligned=True)
        except AdsPowerUIError:
            # No alignable Close button is only harmless when the exact row is
            # visible and already shows Open. Otherwise the search/click failed.
            if self._row_has_button(profile_id, "Open"):
                logger.info(f"Profile {profile_id} is not running (no Close button); "
                            f"nothing to close.")
                return True
            raise
        # closing is immediate in AdsPower (no confirm), but accept one if it appears
        self._maybe_confirm(("OK", "Confirm", "Yes"), timeout=0.8)
        if self._wait_row_button(profile_id, "Open", timeout=wait_timeout):
            logger.info(f"Profile {profile_id} closed via GUI.")
            return True
        logger.warning(f"Clicked Close for {profile_id} but could not confirm it closed.")
        return False

    # ------------------------------------------------------------------
    # row helpers shared by open / close / rename / delete
    # ------------------------------------------------------------------

    def _row_center_y(self, profile_id: str) -> Optional[int]:
        """Vertical centre of the row whose visible ID cell == profile_id."""
        header_bottom = self._list_header_bottom()
        for text, r in self._visible_text_items():
            if text.strip() == profile_id and self._rect_center_y(r) > header_bottom:
                return self._rect_center_y(r)
        return None

    def _row_id_rect(self, profile_id: str):
        header_bottom = self._list_header_bottom()
        for text, r in self._visible_text_items():
            if text.strip() == profile_id and self._rect_center_y(r) > header_bottom:
                return r
        return None

    def _row_alignment_tolerance(self, id_rect=None) -> int:
        cell_h = self._rect_height(id_rect) or 15
        return max(12, int(round(1.6 * cell_h)))

    def _row_primary_click_y(self, profile_id: str) -> Optional[int]:
        """Click y for row-level cells whose hit target aligns with the serial line."""
        id_rect = self._row_id_rect(profile_id)
        if id_rect is not None:
            cell_h = self._rect_height(id_rect) or 15
            return max(0, id_rect.top - max(1, int(round(0.1 * cell_h))))
        return self._row_center_y(profile_id)

    def _row_action_rect_any(self, profile_id: str, labels=("Open", "Close")):
        row_y = self._row_center_y(profile_id)
        if row_y is None:
            return None
        id_rect = self._row_id_rect(profile_id)
        tol = self._row_alignment_tolerance(id_rect)
        header_bottom = self._list_header_bottom()
        best = None
        for b in self._win.descendants(control_type="Button"):
            try:
                if not b.is_visible() or (b.window_text() or "").strip() not in labels:
                    continue
                r = b.rectangle()
                if self._rect_width(r) <= 0 or self._rect_height(r) <= 0:
                    continue
                cy = (r.top + r.bottom) // 2
                if cy > header_bottom and abs(cy - row_y) <= tol:
                    if best is None or r.left > best.left:
                        best = r
            except Exception:
                continue
        return best

    def _list_header_bottom(self) -> int:
        """Bottom Y of the column-header row ('ID', 'Name', 'Action', ...).
        Per-row controls live below it; the window titlebar and the batch
        toolbar live above it. Used to keep row scans off the titlebar Close /
        toolbar buttons (position-independent — derived from the headers)."""
        bottoms = []
        for text, r in self._visible_text_items():
            if self._is_row_data_header(text):
                bottoms.append(r.bottom)
        return max(bottoms) if bottoms else 460

    def _row_action_snapshot(self, profile_id: str, label: str):
        """Return ``(row_y, action_buttons)`` for one row-action scan.

        This combines the old header scan, id-row scan and action-button scan
        into two UIA passes instead of three. UIA reads from AdsPower's CEF tree
        are expensive, so keeping every retry lean matters when many profiles
        are opening or closing.
        """
        header_bottoms = []
        row_y = None
        for t in self._win.descendants(control_type="Text"):
            try:
                if not t.is_visible():
                    continue
                s = (t.window_text() or "").strip()
                if not s:
                    continue
                r = t.rectangle()
                if r.width() <= 0:
                    continue
                if self._is_row_data_header(s):
                    header_bottoms.append(r.bottom)
                if s == profile_id:
                    row_y = (r.top + r.bottom) // 2
            except Exception:
                continue
        header_bottom = max(header_bottoms) if header_bottoms else 460

        # Ignore any row_y match above the header (chip text contains the
        # profile id as word-level elements and would be picked up before the
        # actual row text). Only filter when headers were actually found.
        if header_bottoms and row_y is not None and row_y <= header_bottom:
            row_y = None

        btns = []
        for b in self._win.descendants(control_type="Button"):
            try:
                if not b.is_visible() or (b.window_text() or "").strip() != label:
                    continue
                r = b.rectangle()
                cy = (r.top + r.bottom) // 2
                if r.width() > 0 and r.height() > 0 and cy > header_bottom:
                    btns.append((cy, r.left, r))
            except Exception:
                continue
        if not btns:
            return row_y, []
        max_left = max(b[1] for b in btns)
        return row_y, [b for b in btns if b[1] >= max_left - 60]

    def _click_row_action(self, profile_id: str, label: str, template_name: str = "",
                          require_aligned: bool = True):
        """Click the Action-column button whose text == ``label`` on the row whose
        No./ID == ``profile_id``. Only considers buttons *below the column
        headers*, so the window titlebar 'Close' and the batch toolbar are never
        hit. Aligning to the id's row avoids the toolbar batch button.

        Exact-row alignment is the default: never fall back to the first/topmost
        button, only click the button on the id's own row, and keep retrying
        until that row's id text has rendered. ``require_aligned=False`` is only
        an explicit diagnostics escape hatch."""
        id_top = None
        row_btns = []   # (centre_y, left, rect)
        for _ in range(8):                  # a11y tree can be briefly empty post-filter
            self._connect()
            id_top, row_btns = self._row_action_snapshot(profile_id, label)
            # In bulk mode we MUST resolve the id's row before clicking; keep
            # retrying until both the buttons AND the id row are visible.
            if row_btns and (id_top is not None or not require_aligned):
                break
            time.sleep(0.3)
        if not row_btns:
            if id_top is None:
                if not self._id_column_visible():
                    raise AdsPowerUIError(self._id_column_required_message(profile_id))
                self._fire_profile_missing(profile_id)
                raise AdsPowerProfileNotFoundError(profile_id)
            raise AdsPowerUIError(
                f"No row {label!r} button found for the filtered profile {profile_id}.")

        # Try UIA InvokePattern on the matching button (no mouse, background-safe)
        if id_top is not None:
            try:
                for b in self._win.descendants(control_type="Button"):
                    try:
                        if (b.window_text() or "").strip() != label:
                            continue
                        if not b.is_visible():
                            continue
                        r = b.rectangle()
                        if r.width() > 0 and abs((r.top + r.bottom) // 2 - id_top) <= 22:
                            if self._invoke_ctrl(b):
                                logger.info(f"Clicked {label} for profile {profile_id} (UIA invoke).")
                                return
                    except Exception:
                        continue
            except Exception:
                pass
        target = None
        if id_top is not None:
            aligned = [b for b in row_btns if abs(b[0] - id_top) <= 22]
            if aligned:
                target = min(aligned, key=lambda b: abs(b[0] - id_top))[2]
        if target is None:
            if require_aligned:
                if not self._id_column_visible():
                    raise AdsPowerUIError(self._id_column_required_message(profile_id))
                self._fire_profile_missing(profile_id)
                raise AdsPowerProfileNotFoundError(profile_id)
            row_btns.sort()
            target = row_btns[0][2]
        self._click_rect(target, template_name=template_name)
        logger.info(f"Clicked {label} for profile {profile_id}.")

    def _fire_profile_missing(self, profile_id: str):
        """Invoke the ``on_profile_missing`` callback if one is registered."""
        cb = getattr(self, "on_profile_missing", None)
        if cb is not None:
            try:
                cb(profile_id)
            except Exception as exc:
                logger.warning(f"on_profile_missing callback raised {exc} for {profile_id}")

    def _row_has_button(self, profile_id: str, label: str) -> bool:
        row_y, row_btns = self._row_action_snapshot(profile_id, label)
        if row_y is None:
            return False
        return any(abs(cy - row_y) <= 22 for cy, _left, _rect in row_btns)

    def _wait_row_button(self, profile_id: str, label: str, timeout: float = 12.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._connect()
            if self._row_has_button(profile_id, label):
                return True
            time.sleep(0.3)
        return False

    def _has_text_prefix(self, prefix: str) -> bool:
        for t in self._win.descendants(control_type="Text"):
            try:
                if (t.window_text() or "").strip().startswith(prefix):
                    return True
            except Exception:
                continue
        return False

    def _maybe_confirm(self, labels, timeout: float = 1.5) -> bool:
        """Click the first present dialog button whose text matches ``labels``."""
        for label in labels:
            ctrl = self._find(label, "Button", timeout=timeout, retry=False)
            if ctrl is not None:
                try:
                    r = ctrl.rectangle()
                    if r.width() > 0 and r.height() > 0:
                        self._click_rect(r)
                        return True
                except Exception:
                    pass
        return False

    # ------------------------------------------------------------------
    # CLOSE
    # ------------------------------------------------------------------

    def close_profile_by_id(self, profile_id: str, wait_timeout: float = 12.0) -> bool:
        """Close a running profile via the GUI (search id -> click the row's per-row
        'Close' Action button). Returns True once the row's action reverts to 'Open'."""
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise AdsPowerUIError("close_profile_by_id requires a profile id.")

        # Ensure the profile row is visible (scan current view first, search if
        # not found), then click its per-row "Close" action button.
        with _GUI_LOCK:
            self._a11y_enter()
            try:
                self._ensure_row_visible(profile_id)
                self._click_row_action(profile_id, "Close", template_name="close_profile_by_id")
                self._maybe_confirm(["Yes", "OK", "Confirm", "Close"], timeout=2.0)
            finally:
                self._a11y_exit()
        result = self._wait_row_button(profile_id, "Open", timeout=wait_timeout)
        return result

    # ------------------------------------------------------------------
    # RENAME
    # ------------------------------------------------------------------

    def _name_cell_rect(self, row_y: int):
        """Rect of the Name-column value Text on the row (between the 'Name' and
        'IP' column headers). The edit pencil sits just to its right."""
        name_hdr = ip_hdr = None
        for t in self._win.descendants(control_type="Text"):
            try:
                s = (t.window_text() or "").strip()
                if s == "Name" and name_hdr is None:
                    name_hdr = t.rectangle()
                elif s == "IP" and ip_hdr is None:
                    ip_hdr = t.rectangle()
            except Exception:
                continue
        if name_hdr is None:
            return None
        lo = name_hdr.left - 40
        hi = (ip_hdr.left - 12) if ip_hdr is not None else (name_hdr.right + 280)
        best = None
        for t in self._win.descendants(control_type="Text"):
            try:
                r = t.rectangle()
                if r.width() <= 0:
                    continue
                cy = (r.top + r.bottom) // 2
                cx = (r.left + r.right) // 2
                if abs(cy - row_y) <= 16 and r.left >= lo and cx <= hi:
                    if best is None or r.left < best.left:
                        best = r
            except Exception:
                continue
        return best

    def _open_rename_dialog(self, profile_id: str) -> bool:
        for _ in range(5):
            self._connect()
            row_y = self._row_center_y(profile_id)
            if row_y is not None:
                name_rect = self._name_cell_rect(row_y)
                if name_rect is not None:
                    px = name_rect.right + 12     # the edit pencil, just right of the name
                    py = (name_rect.top + name_rect.bottom) // 2
                    self._click_xy(px, py)
                    deadline = time.time() + 0.8
                    while time.time() < deadline:
                        if self._find("Enter Name", "Edit", timeout=0.3, retry=False) is not None:
                            return True
                        if self._find("OK", "Button", timeout=0.3, retry=False) is not None:
                            return True
                        time.sleep(0.08)
            time.sleep(0.1)
        if self._click_row_menu_rename(profile_id):
            deadline = time.time() + 2.5
            while time.time() < deadline:
                self._connect()
                if self._find("Enter Name", "Edit", timeout=0.3, retry=False) is not None:
                    return True
                # Current AdsPower builds open the full profile Edit form from the
                # row menu. It does not expose the small inline "Enter Name" field,
                # so accept the normal form once its OK button is visible.
                if self._find("OK", "Button", timeout=0.3, retry=False) is not None:
                    return True
                time.sleep(0.08)
        return False

    def _visible_row_name(self, profile_id: str) -> Optional[str]:
        target = str(profile_id or "").strip().lower()
        if not target:
            return None
        for _serial, pid, name in self._scan_rows():
            if str(pid or "").strip().lower() == target:
                return str(name or "").strip()
        return None

    def _rename_confirmed_or_absent(self, profile_id: str, expected_name: str) -> bool:
        """True when the visible row is already renamed or has dropped out of the
        current filtered view. Under Nyxify's temp-name filter, a successful
        rename disappears; a failed clear leaves a visible duplicate-prefix name."""
        self._connect()
        time.sleep(1.0)
        actual = self._visible_row_name(profile_id)
        if actual is None:
            return True
        if actual.strip() == str(expected_name or "").strip():
            return True
        logger.warning(
            f"AdsPower rename for {profile_id} left visible name {actual!r}; "
            f"expected {expected_name!r}. Retrying.")
        return False

    def _recover_presearch_row_for_rename(self, profile_id: str) -> bool:
        """Recover Nyxify's temp-name view before a GUI rename.

        If another operation leaves AdsPower on a Profile-ID or different search
        chip, the just-created temp-named row is not visible and the rename
        pencil/menu cannot be opened. Reapply the remembered Nyxify temp-name
        filter, then re-check the target profile in that view.
        """
        if not getattr(getattr(self, "config", None), "assume_presearch", False):
            return False
        temp_name = str(getattr(self, "_temp_search_name", "") or "").strip()
        if not temp_name:
            logger.warning(
                f"AdsPower UI: profile {profile_id} was not visible for rename, "
                "and no temp profile name is remembered for a temp-name retry."
            )
            return False
        logger.info(
            f"AdsPower UI: profile {profile_id} was not visible for rename; "
            f"reapplying temp-name filter {temp_name!r}."
        )
        self._ensure_temp_filter(temp_name)
        self._wait_list_settled(timeout=3.0)
        return self._ensure_row_visible(profile_id)

    @_serialized
    def rename_profile_by_id(self, profile_id: str, new_name: str) -> dict:
        """Rename a profile via the GUI (find the row -> click the Name edit
        pencil -> paste into the 'Enter Name' field -> OK). Works whether the
        profile is running or not.

        Rename fires mid-signup while the profile is still temp-named, so it is
        already shown under the operator's standing 'Name contains <temp>' filter
        — we act on that view and never search by id."""
        profile_id = str(profile_id or "").strip()
        new_name = str(new_name or "").strip()
        if not profile_id:
            raise AdsPowerUIError("rename_profile_by_id requires a profile id.")
        if not new_name:
            raise AdsPowerUIError("rename_profile_by_id requires a new name.")
        self._connect()
        if not self._ensure_row_visible(profile_id):
            self._recover_presearch_row_for_rename(profile_id)
        if not self._open_rename_dialog(profile_id):
            raise AdsPowerUIError(f"Could not open the rename dialog for {profile_id}.")
        rect = self._rect("Enter Name", "Edit", timeout=0.2)
        if rect is None:
            for attempt in range(2):
                if attempt:
                    self._refresh_window()
                    self._connect()
                    if not self._ensure_row_visible(profile_id):
                        self._recover_presearch_row_for_rename(profile_id)
                    if not self._open_rename_dialog(profile_id):
                        raise AdsPowerUIError(
                            f"Could not reopen the rename dialog for {profile_id} after a bad rename.")
                    rect = self._rect("Enter Name", "Edit", timeout=0.2)
                    if rect is not None:
                        break
                self._fill_name(new_name)
                self._click_ok()
                if self._rename_confirmed_or_absent(profile_id, new_name):
                    logger.info(
                        f"Renamed AdsPower profile {profile_id} -> {new_name!r} via GUI edit form.")
                    return {"profile_id": profile_id, "name": new_name}
            else:
                raise AdsPowerUIError(
                    f"AdsPower rename for {profile_id} did not replace the existing name.")
        self._paste_rect(rect, new_name)
        ok = self._rect("OK", "Button", timeout=0.35)
        if ok is None:
            raise AdsPowerUIError("Rename dialog OK button not found.")
        self._click_rect(ok, template_name="rename_ok_btn")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            self._connect()
            if self._find("Enter Name", "Edit", timeout=0.3, retry=False) is None:
                logger.info(f"Renamed AdsPower profile {profile_id} -> {new_name!r} via GUI.")
                return {"profile_id": profile_id, "name": new_name}
            time.sleep(0.08)
        logger.info(f"Renamed AdsPower profile {profile_id} -> {new_name!r} via GUI (ok didn't close).")
        return {"profile_id": profile_id, "name": new_name}

    # ------------------------------------------------------------------
    # DELETE
    # ------------------------------------------------------------------

    def _select_row_legacy(self, profile_id: str) -> bool:
        """Tick the row's checkbox (just left of the No./ID column) by clicking
        at the known position — AdsPower CheckBoxes are not exposed via UIA."""
        for _ in range(4):
            self._connect()
            cb = self._row_checkbox_rect(profile_id)
            row_y = self._row_center_y(profile_id)
            if cb is not None:
                self._click_rect(cb)
            elif row_y is not None:
                idr = self._row_id_rect(profile_id)
                if idr is not None:
                    # The selectable square is aligned with the serial/top line,
                    # while the profile id text is rendered underneath it.
                    self._click_xy(idr.left - 28, max(idr.top - 1, 0))
                else:
                    info = self._batch_action_snapshot([profile_id], "Open", "Close")
                    entry = info.get(profile_id) or {}
                    cx = entry.get("checkbox_x")
                    cy = entry.get("row_y") or row_y
                    if cx is None:
                        time.sleep(0.2)
                        continue
                    self._click_xy(cx, cy)
                # Poll for the selection count â€” fast poll with early break.
                deadline = time.time() + 1.5
                while time.time() < deadline:
                    selected = self._selected_count()
                    if selected == 1 or selected == -1:
                        return True
                    if selected > 1:
                        raise AdsPowerUIError(
                            f"AdsPower reports {selected} selected rows; "
                            f"refusing to delete multiple profiles.")
                    time.sleep(0.08)
                if idr is None:
                    time.sleep(0.2)
                    continue
                info = self._batch_action_snapshot([profile_id], "Open", "Close")
                entry = info.get(profile_id) or {}
                cx = entry.get("checkbox_x")
                cy = entry.get("row_y") or row_y
                if cx is None:
                    time.sleep(0.2)
                    continue
                self._click_xy(cx, cy)
                # Poll for the selection count — fast poll with early break.
                deadline = time.time() + 1.5
                while time.time() < deadline:
                    selected = self._selected_count()
                    if selected == 1 or selected == -1:
                        return True
                    if selected > 1:
                        raise AdsPowerUIError(
                            f"AdsPower reports {selected} selected rows; "
                            f"refusing to delete multiple profiles.")
                    time.sleep(0.08)
                # Some AdsPower builds do not expose the selected-count marker.
                # We clicked the target row's checkbox in an already-filtered
                # view; let the delete dialog + row-gone check verify the action.
                return True
            time.sleep(0.2)
        return False

    def _select_row(self, profile_id: str) -> bool:
        """Tick the row checkbox using UIA rects first, then DPI-derived points."""
        for _ in range(4):
            self._connect()
            candidates = []

            cb = self._row_checkbox_rect(profile_id)
            if cb is not None:
                candidates.append(("uia checkbox", cb, None))

            idr = self._row_id_rect(profile_id)
            if idr is not None:
                cell_h = self._rect_height(idr) or 15
                y = self._row_primary_click_y(profile_id)
                if y is not None:
                    for scale in (1.9, 2.3):
                        x = idr.left - int(round(scale * cell_h))
                        candidates.append(("id-cell derived checkbox", None, (x, y)))

            row_y = self._row_center_y(profile_id)
            info = self._batch_action_snapshot([profile_id], "Open", "Close")
            entry = info.get(profile_id) or {}
            cx = entry.get("checkbox_x")
            cy = entry.get("row_y") or row_y
            if cx is not None and cy is not None:
                candidates.append(("batch snapshot checkbox", None, (cx, cy)))

            seen = set()
            clicked = False
            for _label, rect, point in candidates:
                if rect is not None:
                    point = self._center(rect)
                if point is None:
                    continue
                key = (int(point[0]) // 3, int(point[1]) // 3)
                if key in seen:
                    continue
                seen.add(key)
                if rect is not None:
                    self._click_rect(rect)
                else:
                    self._click_xy(int(point[0]), int(point[1]))
                clicked = True

                deadline = time.time() + 1.5
                while time.time() < deadline:
                    selected = self._selected_count()
                    if selected == 1 or selected == -1:
                        return True
                    if selected > 1:
                        raise AdsPowerUIError(
                            f"AdsPower reports {selected} selected rows; "
                            f"refusing to delete multiple profiles.")
                    time.sleep(0.08)

            if clicked:
                # Some AdsPower builds do not expose the selected-count marker.
                # We clicked only targets derived from the exact id row; the
                # delete dialog + row-gone check verify the action.
                return True
            time.sleep(0.2)
        return False

    def _row_checkbox_rect(self, profile_id: str):
        row_y = self._row_center_y(profile_id)
        if row_y is None:
            return None
        id_rect = self._row_id_rect(profile_id)
        tol = self._row_alignment_tolerance(id_rect)
        header_bottom = self._list_header_bottom()
        cands = []
        for cb in self._win.descendants(control_type="CheckBox"):
            try:
                if not cb.is_visible():
                    continue
                r = cb.rectangle()
                cy = (r.top + r.bottom) // 2
                if r.width() > 0 and r.height() > 0 and cy > header_bottom and abs(cy - row_y) <= tol:
                    cands.append((r.left, r))
            except Exception:
                continue
        if not cands:
            return None
        cands.sort(key=lambda c: c[0])      # by left only — RECTs aren't orderable
        return cands[0][1]

    def _selected_count(self) -> int:
        """Return selected-row count; 0 means no selected marker, -1 means the
        marker exists but AdsPower did not expose a parseable number."""
        try:
            # Use pywinauto's spec-based lookup (UIA FindFirst) instead of
            # iterating all 97K Text descendants.
            for title in ("Selected:", "Selected :", "selected"):
                t = self._win.child_window(title=title, control_type="Text")
                if t.exists(timeout=0.3):
                    s = (t.window_text() or "").strip()
                    m = re.search(r"\d+", s)
                    return int(m.group(0)) if m else -1
        except Exception:
            pass
        return 0

    def _toolbar_trash_center(self):
        """Centre of the Delete (trash) icon — the 3rd unnamed Button after the
        batch toolbar 'Open': [Open] [Close] [Export] [Trash] [More]. The icons
        carry no accessible name, so we locate by order relative to 'Open'
        (and fall back to an opencv template)."""
        self._connect()
        header_bottom = self._list_header_bottom()
        ob = None
        for b in self._win.descendants(control_type="Button"):
            try:
                if (b.window_text() or "").strip() == "Open":
                    r = b.rectangle()
                    cy = (r.top + r.bottom) // 2
                    if r.width() > 0 and cy < header_bottom:   # the toolbar Open, above headers
                        if ob is None or r.top < ob.top:
                            ob = r
            except Exception:
                continue
        if ob is None:
            m = ui_vision.locate("delete_trash_btn")
            if m:
                return (m.x, m.y)
            raise AdsPowerUIError("Toolbar 'Open' button not found; cannot locate Delete.")
        ob_cy = (ob.top + ob.bottom) // 2
        cands = []
        for b in self._win.descendants(control_type="Button"):
            try:
                r = b.rectangle()
                if r.width() <= 0:
                    continue
                cy = (r.top + r.bottom) // 2
                # same toolbar row, right of Open, within the icon cluster
                if abs(cy - ob_cy) < 10 and r.left > ob.right + 2 and r.left < ob.right + 400:
                    cands.append((r.left, (r.left + r.right) // 2, cy))
            except Exception:
                continue
        cands.sort()
        if len(cands) >= 3:
            _, cx, cy = cands[2]            # [Close, Export, Trash]
            if self.config.capture_templates:
                try:
                    ui_vision.save_template("delete_trash_btn", cx - 20, cy - 20, 40, 40)
                except Exception:
                    pass
            return (cx, cy)
        m = ui_vision.locate("delete_trash_btn")
        if m:
            return (m.x, m.y)
        raise AdsPowerUIError(
            f"Could not locate the Delete toolbar icon (found {len(cands)} toolbar icons).")

    def _row_menu_button_point(self, profile_id: str):
        """Best click point for a row's overflow/menu button.

        Prefer exposed row-aligned Button rectangles to the old window-edge
        fallback so resize, DPI and theme changes do not move the click target.
        """
        row_y = self._row_center_y(profile_id)
        click_y = self._row_primary_click_y(profile_id)
        idr = self._row_id_rect(profile_id)
        if row_y is None and click_y is None:
            return None

        click_y = click_y if click_y is not None else row_y
        tol = self._row_alignment_tolerance(idr)
        header_bottom = self._list_header_bottom()
        action = self._row_action_rect_any(profile_id, ("Open", "Close"))
        wr = self._win.rectangle()

        candidates = []
        for b in self._win.descendants(control_type="Button"):
            try:
                if not b.is_visible():
                    continue
                label = (b.window_text() or "").strip()
                low = label.lower()
                if low in {"open", "close", "delete", "confirm", "ok", "cancel", "yes"}:
                    continue
                r = b.rectangle()
                if self._rect_width(r) <= 0 or self._rect_height(r) <= 0:
                    continue
                cy = (r.top + r.bottom) // 2
                if cy <= header_bottom:
                    continue
                if abs(cy - click_y) > tol and (row_y is None or abs(cy - row_y) > tol):
                    continue
                if action is not None:
                    if r.left < action.right - 8:
                        continue
                    score = abs(cy - click_y) * 3 + max(0, r.left - action.right)
                else:
                    if r.left < wr.left + int(0.55 * wr.width()):
                        continue
                    score = abs(cy - click_y) * 3 + abs((r.left + r.right) // 2 - (wr.right - 44))
                if low in {"more", "..."} or not label:
                    score -= 40
                candidates.append((score, r))
            except Exception:
                continue

        if candidates:
            _score, best = min(candidates, key=lambda item: item[0])
            return self._center(best)
        return (wr.right - 44, click_y)

    def _click_row_menu_delete(self, profile_id: str) -> bool:
        """Open the row's three-dot menu and click its Delete action."""
        return self._click_row_menu_action(profile_id, ("Delete",))

    def _click_row_menu_rename(self, profile_id: str) -> bool:
        """Open the row's three-dot menu and click its Edit/Rename action."""
        return self._click_row_menu_action(profile_id, ("Edit", "Rename"))

    def _click_row_menu_action(self, profile_id: str, labels) -> bool:
        """Open the row's three-dot menu and click the nearest matching action."""
        target_labels = {
            str(label or "").strip().lower()
            for label in labels
            if str(label or "").strip()
        }
        if not target_labels:
            return False
        point = self._row_menu_button_point(profile_id)
        if point is None:
            return False
        try:
            menu_x, menu_y = point
            self._click_xy(menu_x, menu_y)
            time.sleep(0.18)
            self._connect()
            header_bottom = self._list_header_bottom()
            candidates = []
            for b in self._win.descendants(control_type="Button"):
                try:
                    if (b.window_text() or "").strip().lower() not in target_labels:
                        continue
                    r = b.rectangle()
                    if self._rect_width(r) <= 0 or self._rect_height(r) <= 0:
                        continue
                    if r.bottom <= header_bottom:
                        continue
                    cx, cy = self._center(r)
                    dist = abs(cx - menu_x) + int(0.75 * abs(cy - menu_y))
                    candidates.append((dist, r))
                except Exception:
                    continue
            if not candidates:
                return False
            _dist, best = min(candidates, key=lambda item: item[0])
            self._click_rect(best)
            return True
        except Exception as exc:
            logger.debug(f"Row-menu action click failed for {profile_id}: {exc}")
            return False

    @_serialized
    def _tick_clear_cache(self) -> bool:
        """Tick 'Clear cache as well' in the Delete-profile dialog. AdsPower exposes
        no CheckBox control for it, so we click the box that sits just left of its
        label text."""
        for t in self._win.descendants(control_type="Text"):
            try:
                if (t.window_text() or "").strip().lower() == "clear cache as well":
                    r = t.rectangle()
                    if r.width() > 0:
                        self._click_xy(r.left - 14, (r.top + r.bottom) // 2)
                        logger.info("Ticked 'Clear cache as well' in the Delete dialog.")
                        time.sleep(0.08)
                        return True
            except Exception:
                continue
        logger.debug("'Clear cache as well' label not found; proceeding without it.")
        return False

    @_serialized
    def _ensure_row_visible(self, profile_id: str) -> bool:
        """Ensure the profile row is in the current view so operations can act on
        it.  Returns True when the row is visible; False if the profile is not in
        the view at all (renamed / deleted / gone).  Prefers scanning the current
        view first (no search), only searching when the row is not found."""
        self._connect()
        if self.config.assume_presearch:
            if self._row_center_y(profile_id) is not None:
                return True
            if not self._id_column_visible():
                raise AdsPowerUIError(self._id_column_required_message(profile_id))
            return False
        chip = self._profile_id_chip()
        if chip is not None:
            active_ids = {pid.lower() for pid in self._parse_chip_ids(chip[1])}
            if profile_id.lower() in active_ids and self._row_center_y(profile_id) is not None:
                return True
        self._search_by_ids([profile_id], append=False)
        self._connect()
        if self._row_center_y(profile_id) is not None:
            return True
        if not self._id_column_visible():
            raise AdsPowerUIError(self._id_column_required_message(profile_id))
        return False

    @_serialized
    def delete_profile_by_id(self, profile_id: str) -> dict:
        """Delete a profile via the GUI. Selects the row, clicks the toolbar
        trash, ticks 'Clear cache as well', and confirms. Returns
        ``{'code': 0}`` on success."""
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise AdsPowerUIError("delete_profile_by_id requires a profile id.")

        if not self._ensure_row_visible(profile_id):
            logger.info(f"Profile {profile_id} not found; nothing to delete.")
            return {"code": 0, "deleted": True, "profile_id": profile_id, "skipped": True}

        if self._row_has_button(profile_id, "Close"):
            logger.info(f"Profile {profile_id} is running; closing before delete.")
            if not self._close_one_in_batch(profile_id, wait_timeout=12):
                raise AdsPowerUIError(f"Could not confirm profile {profile_id} closed before delete.")
            self._ensure_row_visible(profile_id)

        delete_source = "row menu"
        if not self._click_row_menu_delete(profile_id):
            delete_source = "toolbar click"
            if not self._select_row(profile_id):
                raise AdsPowerUIError(f"Could not select the row for {profile_id} to delete.")
            cx, cy = self._toolbar_trash_center()
            self._click_xy(cx, cy)
        deadline = time.time() + 2.0
        dialog_ready = False
        while time.time() < deadline:
            self._connect()
            if self._find("Confirm deletion", "Button", timeout=0.3, retry=False) is not None:
                dialog_ready = True
                break
            time.sleep(0.05)
        if not dialog_ready:
            logger.warning(f"Delete dialog not found after {delete_source} for {profile_id}.")
        self._tick_clear_cache()
        if not self._maybe_confirm(
                ("Confirm deletion", "Confirm", "Delete", "OK", "Yes"), timeout=3.0):
            logger.warning(f"Delete-dialog confirm button not found for {profile_id}.")
        deadline = time.time() + 6
        while time.time() < deadline:
            self._connect()
            if self._row_center_y(profile_id) is None:
                logger.info(f"Deleted AdsPower profile {profile_id} via GUI.")
                return {"code": 0, "deleted": True, "profile_id": profile_id}
            time.sleep(0.2)
        raise AdsPowerUIError(
            f"Delete requested for {profile_id} but the profile row is still present.")

    # ------------------------------------------------------------------
    # misc
    # ------------------------------------------------------------------

    def _visible_text_blob(self) -> str:
        parts = []
        try:
            for t in self._win.descendants(control_type="Text"):
                s = (t.window_text() or "").strip()
                if s:
                    parts.append(s)
        except Exception:
            pass
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="AdsPower UI automation smoke test")
    ap.add_argument("--name", default="Snapchat: Pending")
    ap.add_argument("--group", default="Snapchat20")
    ap.add_argument("--proxy", default="48.45.190.63:42438:hwwrghLD:j432NPbg")
    ap.add_argument("--open", default="", help="Just open this profile id (skip create)")
    args = ap.parse_args()

    ctrl = AdsPowerUIController()
    if args.open:
        print("Endpoint:", ctrl.open_profile_by_id(args.open))
    else:
        info = ctrl.create_profile(name=args.name, proxy=args.proxy, group=args.group)
        print("Created:", info)
        if info["profile_id"]:
            print("Opening...")
            print("Endpoint:", ctrl.open_profile_by_id(info["profile_id"]))
