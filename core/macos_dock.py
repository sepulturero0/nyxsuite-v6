"""macOS Dock visibility helpers for source-run Python processes."""

import sys
from typing import Callable, List, Optional


def _module_is_test_double(name: str) -> bool:
    module = sys.modules.get(name)
    return module is not None and not hasattr(module, "__file__")


def _pytest_collection_without_mocked_dock_modules() -> bool:
    if "pytest" not in sys.modules:
        return False
    return not (
        _module_is_test_double("ApplicationServices")
        and _module_is_test_double("AppKit")
    )


def hide_macos_dock_icon(log: Optional[Callable[[str], None]] = None) -> List[str]:
    """Best-effort hide of Python.app from the Dock on macOS.

    Source runs use the Homebrew/Python.org ``Python.app`` launcher. Runner
    processes have no real UI, and the bridge only needs a menu-bar icon, so
    both should be accessory/UI-element apps instead of regular Dock apps.
    """
    if sys.platform != "darwin":
        return []
    if _pytest_collection_without_mocked_dock_modules():
        return []

    errors: List[str] = []

    # Apply the older Process Manager transform first. This can take effect
    # before AppKit creates/activates NSApplication for the process.
    try:
        import ApplicationServices

        err, psn = ApplicationServices.GetCurrentProcess(None)
        if err == 0:
            ApplicationServices.TransformProcessType(
                psn,
                ApplicationServices.kProcessTransformToUIElementApplication,
            )
        else:
            errors.append(f"GetCurrentProcess returned {err}")
    except Exception as exc:
        errors.append(str(exc))

    try:
        import AppKit

        policy = getattr(AppKit, "NSApplicationActivationPolicyAccessory", 1)
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(policy)
    except Exception as exc:
        errors.append(str(exc))

    if errors and log:
        try:
            log(f"Could not fully hide macOS dock icon: {'; '.join(errors)}")
        except Exception:
            pass
    return errors
