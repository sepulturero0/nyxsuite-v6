"""Live test: rename an AdsPower profile at every zoom level.

Zooms the AdsPower Global window via the Window menu shortcuts
(Cmd+0 / Cmd+- / Cmd+=) and drives the real AdsPower desktop GUI
to rename a profile at each zoom level. This validates that the
UI-automation rename path is robust regardless of zoom scaling.

Usage:
    python tools/live_test_rename_zoom.py --profile k1new --name "Nyx: test"
    python tools/live_test_rename_zoom.py --profile k1new --name "Nyx: test" --dry-run
    python tools/live_test_rename_zoom.py --profile k1new --name "Nyx: test" --keep

Requirements
------------
* AdsPower Global running and signed in.
* macOS Accessibility permission for the Python interpreter.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.adspower_ui import AdsPowerUIController


def _zoom(action: str, ctrl: AdsPowerUIController = None):
    """Nudge AdsPower's zoom via the Window-menu shortcuts.
    AdsPower must be foreground first for the keystrokes to land."""
    if ctrl is not None:
        ctrl._foreground()
        time.sleep(0.3)
    keymap = {
        "reset": 'keystroke "0" using command down',
        "out":  'keystroke "-" using command down',
        "in":   'keystroke "=" using command down',
    }
    script = keymap[action]
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to {script}'],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        time.sleep(0.4)
        return True
    except Exception as exc:
        print(f"  (could not zoom {action!r}: {exc})")
        return False


def _run_rename(ctrl: AdsPowerUIController, profile_id: str, new_name: str) -> bool:
    """Attempt to rename; return True on success."""
    try:
        result = ctrl.rename_profile_by_id(profile_id, new_name)
        print(f"  rename result: {result}")
        return True
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live test: rename AdsPower profile at every zoom level"
    )
    parser.add_argument("--profile", default="", help="Profile ID to rename")
    parser.add_argument("--name", default="Nyx: live-test", help="New name to set")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds to wait at each zoom level before renaming (default: 1.0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the zoom ladder without running")
    parser.add_argument("--keep", action="store_true",
                        help="Leave the final filter applied instead of resetting")
    args = parser.parse_args()

    preflight = None
    try:
        from core.adspower_ui_backend_macos import MacOSAccessibilityPermissionError
        from core.adspower_ui import AdsPowerUIController as _Ctrl
        ctrl = _Ctrl()
    except MacOSAccessibilityPermissionError as exc:
        preflight = str(exc)
    except Exception as exc:
        preflight = f"Could not start AdsPower controller: {exc}"

    if preflight:
        print("FAIL:", preflight, flush=True)
        return 1

    if not args.profile:
        print("FAIL: --profile is required", flush=True)
        return 1

    profile_id = args.profile.strip()
    new_name = args.name.strip()

    # Zoom ladder: reset -> zoomed out -> reset -> zoomed in -> reset
    steps = [
        ("zoom 100% (reset)", "reset"),
        ("zoomed out (-)", "out"),
        ("zoomed out (-) again", "out"),
        ("zoomed in (+)", "in"),
        ("zoomed in (+) again", "in"),
        ("zoomed in (+) again", "in"),
    ]

    print(f"Profile: {profile_id!r} -> {new_name!r}", flush=True)
    print(f"Zoom ladder: {len(steps)} steps", flush=True)
    print(f"Delay: {args.delay}s", flush=True)

    if args.dry_run:
        print("\nDry run — zoom steps to test:")
        for label, _action in steps:
            print(f"  {label}")
        return 0

    ctrl = AdsPowerUIController()
    ctrl._connect()
    ctrl._foreground()
    time.sleep(0.5)

    results = []
    try:
        for label, action in steps:
            print(f"\n[{label}] zooming ...", flush=True, end="")

            ok = _zoom(action, ctrl)
            if not ok:
                results.append((label, False, "zoom failed"))
                continue

            time.sleep(args.delay)

            ctrl._connect()
            ctrl._foreground()
            time.sleep(0.3)

            success = _run_rename(ctrl, profile_id, new_name)
            results.append((label, success, None))
            time.sleep(0.3)

    finally:
        _zoom("reset")
        print("\nReset zoom to 100%", flush=True)

    print("\n" + "=" * 60, flush=True)
    print("SUMMARY", flush=True)
    print("=" * 60, flush=True)
    for label, ok, err in results:
        status = "PASS" if ok else "FAIL"
        detail = f"  ({err})" if err else ""
        print(f"  {label:<26} {status}{detail}", flush=True)

    all_ok = all(ok for _l, ok, _e in results)
    print(f"\n{'ALL PASS' if all_ok else 'SOME FAILED'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
