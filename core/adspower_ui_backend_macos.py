"""macOS Accessibility backend for AdsPower GUI automation.

This module exposes a small pywinauto-shaped wrapper over AXUIElement so the
shared AdsPower controller can keep using the same element API on Windows and
macOS: ``child_window()``, ``descendants()``, ``window_text()``, ``rectangle()``,
``is_visible()``, and ``invoke()``.
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Optional


class MacOSAccessibilityPermissionError(RuntimeError):
    def __init__(self, app_name: str = "Terminal, Codex, or Nyx Suite"):
        super().__init__(
            "macOS Accessibility permission is required for AdsPower no-API GUI "
            "automation. Grant Accessibility permission to "
            f"{app_name} in System Settings -> Privacy & Security -> Accessibility. "
            "If Nyx Suite was started from the browser extension, macOS may prompt "
            "for Google Chrome; approve that prompt too. Then restart Nyx Suite "
            "and run the smoke test again."
        )


class MacOSAdsPowerNotFoundError(RuntimeError):
    """AdsPower Global is not running or no accessible window is visible."""


@dataclass(frozen=True)
class Rect:
    left: int = 0
    top: int = 0
    right: int = 1
    bottom: int = 1

    def width(self) -> int:
        return max(0, int(self.right - self.left))

    def height(self) -> int:
        return max(0, int(self.bottom - self.top))


_ROLE_TO_CONTROL_TYPE = {
    "AXButton": "Button",
    "AXCheckBox": "CheckBox",
    "AXComboBox": "ComboBox",
    "AXGroup": "Pane",
    "AXImage": "Image",
    "AXLink": "Hyperlink",
    "AXMenu": "Menu",
    "AXMenuItem": "MenuItem",
    "AXOutline": "Tree",
    "AXPopUpButton": "Button",
    "AXRadioButton": "RadioButton",
    "AXScrollArea": "Pane",
    "AXStaticText": "Text",
    "AXTabGroup": "Tab",
    "AXTextArea": "Edit",
    "AXTextField": "Edit",
    "AXToolbar": "ToolBar",
    "AXWindow": "Window",
}


def ax_role_to_control_type(role: str) -> str:
    return _ROLE_TO_CONTROL_TYPE.get(str(role or ""), "Pane")


class MacOSControlSpec:
    def __init__(self, root: "MacOSControl", title: str = "",
                 control_type: Optional[str] = None):
        self._root = root
        self._title = title
        self._control_type = control_type

    def _resolve(self, timeout: float = 0.0) -> Optional["MacOSControl"]:
        deadline = time.time() + max(0.0, float(timeout or 0.0))
        while True:
            for ctrl in self._root.descendants(self._control_type):
                if self._title and ctrl.window_text() != self._title:
                    continue
                return ctrl
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    def exists(self, timeout: float = 0.0) -> bool:
        return self._resolve(timeout=timeout) is not None

    def _required(self) -> "MacOSControl":
        ctrl = self._resolve(timeout=0.0)
        if ctrl is None:
            raise RuntimeError(
                f"AX control not found: title={self._title!r} "
                f"control_type={self._control_type!r}"
            )
        return ctrl

    def __getattr__(self, name):
        return getattr(self._required(), name)


class MacOSControl:
    _MAX_DESCENDANT_DEPTH = 80

    def __init__(self, backend: "MacOSAdsPowerBackend", element, role: str = "",
                 title: str = "", rect: Optional[Rect] = None,
                 children: Optional[Iterable["MacOSControl"]] = None,
                 visible: bool = True):
        self._backend = backend
        self._element = element
        self._role = role
        self._title = title
        self._rect = rect
        self._static_children = list(children) if children is not None else None
        self._visible = visible

    @property
    def element_info(self):
        return SimpleNamespace(name=self.window_text(), rectangle=self.rectangle())

    def window_text(self) -> str:
        if self._title:
            return str(self._title)
        for attr in ("AXTitle", "AXValue", "AXDescription", "AXHelp", "AXPlaceholderValue"):
            value = self._backend.attr(self._element, attr)
            if value not in (None, ""):
                return str(value)
        return ""

    def get_value(self) -> str:
        value = self._backend.attr(self._element, "AXValue")
        return str(value if value not in (None, "") else self.window_text())

    def rectangle(self) -> Rect:
        if self._rect is not None:
            return self._rect
        return self._backend.element_rect(self._element)

    def is_visible(self) -> bool:
        if not self._visible:
            return False
        hidden = self._backend.attr(self._element, "AXHidden")
        if hidden is True:
            return False
        rect = self.rectangle()
        return rect.width() > 0 and rect.height() > 0

    def control_type(self) -> str:
        role = self._role or str(self._backend.attr(self._element, "AXRole") or "")
        return ax_role_to_control_type(role)

    def descendants(self, control_type: Optional[str] = None, _seen=None, _depth: int = 0):
        if _seen is None:
            _seen = set()
        if _depth >= self._MAX_DESCENDANT_DEPTH:
            return []
        results = []
        for child in self._children():
            key = repr(getattr(child, "_element", child))
            if key in _seen:
                continue
            _seen.add(key)
            if control_type is None or child.control_type() == control_type:
                results.append(child)
            results.extend(child.descendants(control_type=control_type, _seen=_seen, _depth=_depth + 1))
        return results

    def child_window(self, title: str = "", control_type: Optional[str] = None):
        return MacOSControlSpec(self, title=title, control_type=control_type)

    def exists(self, timeout: float = 0.0) -> bool:
        return True

    def invoke(self):
        if not self._backend.perform_action(self._element, "AXPress"):
            raise RuntimeError(f"AXPress failed for {self.window_text()!r}")

    def _children(self):
        if self._static_children is not None:
            return list(self._static_children)
        raw_children = list(self._backend.attr(self._element, "AXChildren") or [])
        if not raw_children:
            raw_children = list(self._backend.attr(self._element, "AXChildrenInNavigationOrder") or [])
        if not raw_children:
            raw_children = list(self._backend.attr(self._element, "AXRows") or [])
        combined = []
        seen = set()
        for child in raw_children:
            key = repr(child)
            if key in seen:
                continue
            seen.add(key)
            combined.append(child)
        wrapped = []
        for child in combined:
            wrapped.append(self._backend.wrap(child))
        return wrapped


class MacOSAdsPowerBackend:
    TITLE_SUBSTRINGS = ("AdsPower Browser |", "AdsPower Global", "AdsPower")
    MIN_MAIN_WINDOW_WIDTH = 320
    MIN_MAIN_WINDOW_HEIGHT = 240

    def __init__(self):
        self._load_frameworks()
        if not self._ax_is_trusted(prompt=True):
            raise MacOSAccessibilityPermissionError(self._accessibility_app_name())
        self._app = None
        self._app_ref = None
        self._window = None
        self.window_id = None
        self._attr_cache = {}
        self._foreground_settle_seconds = self._env_float(
            "ADSPOWER_UI_MACOS_FOREGROUND_SETTLE", 0.08
        )

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        import os

        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except Exception:
            return default
        return value if value >= 0 else default

    def _load_frameworks(self):
        try:
            import ApplicationServices as application_services
            import AppKit
            import Quartz
        except Exception as exc:
            raise ImportError(
                "PyObjC is required for macOS AdsPower GUI automation. Install "
                "requirements on macOS so pyobjc-framework-ApplicationServices, "
                "pyobjc-framework-Cocoa, and pyobjc-framework-Quartz are present."
            ) from exc
        self._as = application_services
        self._appkit = AppKit
        self._quartz = Quartz

    def _ax_is_trusted(self, prompt: bool = False) -> bool:
        if prompt:
            checker = getattr(self._as, "AXIsProcessTrustedWithOptions", None)
            if checker is not None:
                key = getattr(
                    self._as,
                    "kAXTrustedCheckOptionPrompt",
                    "AXTrustedCheckOptionPrompt",
                )
                try:
                    return bool(checker({key: True}))
                except Exception:
                    pass
        try:
            return bool(self._as.AXIsProcessTrusted())
        except Exception:
            return False

    def _accessibility_app_name(self) -> str:
        executable = Path(str(sys.executable or "")).resolve()
        for part in executable.parts:
            if part.endswith(".app"):
                name = part[:-4].strip()
                if name:
                    return name
        name = executable.name.strip()
        if name.lower().startswith("python"):
            return "Python"
        return name or "Python or Nyx Suite"

    def connect(self) -> MacOSControl:
        self._attr_cache = {}
        if self._cached_window_is_usable():
            if not self._frontmost_is_adspower():
                self.foreground(force=True)
            return self.wrap(self._window)

        app = self._find_adspower_app()
        if app is None:
            raise MacOSAdsPowerNotFoundError(
                "AdsPower Global is not running. Launch AdsPower and sign in."
            )
        self._app = app
        self._app_ref = self._as.AXUIElementCreateApplication(app.processIdentifier())
        self.set_attr(self._app_ref, "AXManualAccessibility", True)
        self.foreground()
        time.sleep(0.2)
        windows = self.attr(self._app_ref, "AXWindows") or []
        chosen = self._choose_window(windows)
        if chosen is None:
            raise MacOSAdsPowerNotFoundError(
                "AdsPower Global is running, but no accessible AdsPower window was found."
            )
        self._window = chosen
        self.window_id = id(chosen)
        self.foreground(force=True)
        return self.wrap(chosen)

    def foreground(self, force: bool = False):
        if self._app is None:
            self._app = self._find_adspower_app()
        if not force and self._frontmost_is_adspower():
            return
        if self._app is not None:
            try:
                opts = self._appkit.NSApplicationActivateIgnoringOtherApps
                self._app.activateWithOptions_(opts)
            except Exception:
                pass
        if self._window is not None:
            # A profile browser can be the frontmost AdsPower-owned window while
            # the Global dashboard is minimized or behind it. Restore the main
            # dashboard before any accessibility lookup or coordinate click.
            self.set_attr(self._window, "AXMinimized", False)
            self.perform_action(self._window, "AXRaise")
            self.set_attr(self._window, "AXMain", True)
            self.set_attr(self._window, "AXFocused", True)
        time.sleep(max(0.0, float(getattr(self, "_foreground_settle_seconds", 0.08))))

    def refresh_window(self) -> bool:
        """Trigger AdsPower's Window > Refresh (shown as Shift-Cmd-R) without a
        manual menu click.

        Brings AdsPower forward, then asks System Events to click the actual
        menu item; if the menu wording differs across builds, it falls back to
        the Shift-Cmd-R keystroke, which reaches AdsPower because we just focused
        it. Used to recover an unresponsive dashboard during GUI automation."""
        try:
            self.foreground(force=True)
        except Exception:
            pass
        time.sleep(0.3)

        proc_name = ""
        try:
            if self._app is not None:
                proc_name = str(self._app.localizedName() or "")
        except Exception:
            proc_name = ""
        proc_name = (proc_name or "AdsPower Global").replace('"', '\\"')

        scripts = [
            (
                f'tell application "System Events" to tell process "{proc_name}" '
                'to click menu item "Refresh" of menu "Window" of menu bar 1'
            ),
            'tell application "System Events" to keystroke "r" using {command down, shift down}',
        ]
        for script in scripts:
            try:
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    text=True,
                    timeout=6,
                )
                if result.returncode == 0:
                    return True
            except Exception:
                continue
        return False

    def _cached_window_is_usable(self) -> bool:
        if self._window is None or self._app is None or self._app_ref is None:
            return False
        try:
            if bool(self.attr(self._window, "AXHidden")) or bool(
                self.attr(self._window, "AXMinimized")
            ):
                return False
            rect = self.element_rect(self._window)
        except Exception:
            return False
        return (
            rect.width() >= self.MIN_MAIN_WINDOW_WIDTH
            and rect.height() >= self.MIN_MAIN_WINDOW_HEIGHT
        )

    def _frontmost_is_adspower(self) -> bool:
        name = str(self._frontmost_app_name() or "").strip().lower()
        # AdsPower profile Chromium windows are often named "AdsPower Browser".
        # They must not suppress raising the AdsPower Global dashboard.
        return "adspower" in name and "browser" not in name

    def current_foreground(self):
        try:
            return self._appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        except Exception:
            return None

    def restore_foreground(self, token):
        if token is None:
            return
        try:
            token.activateWithOptions_(self._appkit.NSApplicationActivateIgnoringOtherApps)
        except Exception:
            pass

    def wrap(self, element) -> MacOSControl:
        return MacOSControl(
            self,
            element,
            role=str(self.attr(element, "AXRole") or ""),
            title="",
        )

    def attr(self, element, name: str):
        if element is None or getattr(self, "_as", None) is None:
            return None
        key = (repr(element), name)
        if key in self._attr_cache:
            return self._attr_cache[key]
        try:
            result = self._as.AXUIElementCopyAttributeValue(element, name, None)
        except TypeError:
            try:
                result = self._as.AXUIElementCopyAttributeValue(element, name)
            except Exception:
                self._attr_cache[key] = None
                return None
        except Exception:
            self._attr_cache[key] = None
            return None
        value = self._unwrap_ax_result(result)
        self._attr_cache[key] = value
        return value

    def set_attr(self, element, name: str, value) -> bool:
        try:
            result = self._as.AXUIElementSetAttributeValue(element, name, value)
            return self._ax_ok(result)
        except Exception:
            return False

    def perform_action(self, element, action: str) -> bool:
        try:
            result = self._as.AXUIElementPerformAction(element, action)
            return self._ax_ok(result)
        except Exception:
            return False

    def element_rect(self, element) -> Rect:
        if getattr(self, "_quartz", None) is None:
            return Rect()
        pos = self.attr(element, "AXPosition")
        size = self.attr(element, "AXSize")
        x, y = self._ax_point(pos)
        w, h = self._ax_size(size)
        return Rect(int(round(x)), int(round(y)), int(round(x + w)), int(round(y + h)))

    def _find_adspower_app(self):
        fallback = None
        try:
            apps = self._appkit.NSWorkspace.sharedWorkspace().runningApplications()
        except Exception:
            apps = []
        for app in apps:
            try:
                name = str(app.localizedName() or "")
                bundle = str(app.bundleIdentifier() or "")
                if "AdsPower Global" in name or "adspower.global" in bundle.lower():
                    return app
                if fallback is None and ("AdsPower" in name or "adspower" in bundle.lower()):
                    fallback = app
            except Exception:
                continue
        return fallback

    def _choose_window(self, windows):
        visible = []
        fallback = []
        for window in windows:
            title = str(self._best_text(window) or "")
            rect = self.element_rect(window)
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            if any(fragment in title for fragment in self.TITLE_SUBSTRINGS):
                visible.append((rect.width() * rect.height(), window))
            else:
                fallback.append((rect.width() * rect.height(), window))
        candidates = visible or fallback
        if not candidates:
            return None
        candidates.sort(reverse=True, key=lambda item: item[0])
        return candidates[0][1]

    def _best_text(self, element) -> str:
        for attr in ("AXTitle", "AXValue", "AXDescription", "AXHelp", "AXPlaceholderValue"):
            value = self.attr(element, attr)
            if value not in (None, ""):
                return str(value)
        return ""

    def _frontmost_app_name(self) -> str:
        try:
            app = self._appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
            return str(app.localizedName() or "")
        except Exception:
            return ""

    def _ax_point(self, value):
        converted = self._ax_value(value, getattr(self._as, "kAXValueCGPointType", 1))
        if converted is not None:
            try:
                return float(converted.x), float(converted.y)
            except Exception:
                pass
            try:
                return float(converted[0]), float(converted[1])
            except Exception:
                pass
        try:
            return float(value.x), float(value.y)
        except Exception:
            return 0.0, 0.0

    def _ax_size(self, value):
        converted = self._ax_value(value, getattr(self._as, "kAXValueCGSizeType", 2))
        if converted is not None:
            try:
                return float(converted.width), float(converted.height)
            except Exception:
                pass
            try:
                return float(converted[0]), float(converted[1])
            except Exception:
                pass
        try:
            return float(value.width), float(value.height)
        except Exception:
            return 1.0, 1.0

    def _ax_value(self, value, value_type):
        if value is None:
            return None
        try:
            result = self._as.AXValueGetValue(value, value_type, None)
            return self._unwrap_ax_result(result)
        except TypeError:
            pass
        except Exception:
            return None
        for maker in (
            getattr(self._quartz, "CGPointMake", None),
            getattr(self._quartz, "CGSizeMake", None),
        ):
            if maker is None:
                continue
            try:
                out = maker(0, 0)
                result = self._as.AXValueGetValue(value, value_type, out)
                if self._ax_ok(result):
                    return out
            except Exception:
                continue
        return None

    @staticmethod
    def _unwrap_ax_result(result):
        if isinstance(result, tuple):
            if len(result) == 2:
                first, second = result
                return second if first in (0, True, None) else None
            if result:
                return result[-1]
        return result

    @staticmethod
    def _ax_ok(result) -> bool:
        if isinstance(result, tuple):
            result = result[0] if result else None
        return result in (0, True, None)


def set_clipboard_text(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=str(text).encode("utf-8"), check=True)
        return True
    except Exception:
        return False
