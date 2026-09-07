"""Live test: rename an AdsPower profile at every window size.

Resizes the AdsPower Global window via System Events to a ladder of
widths, then drives the real AdsPower desktop GUI to rename a profile
at each size. This validates that the UI-automation rename path is
robust regardless of how the user has sized their AdsPower window.

Usage:
    python tools/live_test_rename_window_sizes.py --profile k1new --name "Nyx: test"
    python tools/live_test_rename_window_sizes.py --profile k1new --name "Nyx: test" --min-width 400
    python tools/live_test_rename_window_sizes.py --profile k1new --name "Nyx: test" --dry-run   # show sizes only

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


def _set_window_frame(width: int, height: int, x: int = None, y: int = None):
    """Resize AdsPower's main window via System Events.
    Uses `set position` and `set size` separately since `set frame`
    does not work with AdsPower's window."""
    pos_x = x if x is not None else 0
    pos_y = y if y is not None else 0
    script = (
        f'tell application "System Events" to tell process "AdsPower Global" '
        f'to set position of window 1 to {{{pos_x}, {pos_y}}}'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        script2 = (
            f'tell application "System Events" to tell process "AdsPower Global" '
            f'to set size of window 1 to {{{width}, {height}}}'
        )
        subprocess.run(
            ["osascript", "-e", script2],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        time.sleep(0.3)
        return True
    except Exception as exc:
        print(f"  (could not resize to {width}x{height}: {exc})")
        return False


def _get_window_frame() -> tuple:
    """Read the current AdsPower window frame returns (x, y, width, height)."""
    script_pos = (
        'tell application "System Events" to tell process "AdsPower Global" '
        'to get position of window 1'
    )
    script_size = (
        'tell application "System Events" to tell process "AdsPower Global" '
        'to get size of window 1'
    )
    try:
        pos_result = subprocess.run(
            ["osascript", "-e", script_pos],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        size_result = subprocess.run(
            ["osascript", "-e", script_size],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        px, py = (int(x) for x in pos_result.stdout.strip().split(", "))
        w, h = (int(x) for x in size_result.stdout.strip().split(", "))
        return (px, py, w, h)
    except Exception:
        pass
    return None


def _is_adspower_running() -> bool:
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
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
        description="Live test: rename AdsPower profile at every window size"
    )
    parser.add_argument("--profile", default="", help="Profile ID to rename")
    parser.add_argument("--name", default="Nyx: live-test", help="New name to set")
    parser.add_argument("--min-width", type=int, default=320,
                        help="Smallest window width to test (default: 320)")
    parser.add_argument("--max-width", type=int, default=1400,
                        help="Largest window width to test (default: 1400)")
    parser.add_argument("--step", type=int, default=200,
                        help="Width increment (default: 200)")
    parser.add_argument("--height", type=int, default=700,
                        help="Window height for all sizes (default: 700)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds to wait at each size before renaming (default: 1.0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the size ladder without running")
    parser.add_argument("--no-reset", action="store_true",
                        help="Do not restore window size after test")
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
    widths = list(range(args.min_width, args.max_width + 1, args.step))

    print(f"Profile: {profile_id!r} -> {new_name!r}", flush=True)
    print(f"Size ladder: {args.min_width}px to {args.max_width}px "
          f"(step {args.step}px, height {args.height}px)", flush=True)
    print(f"Delay: {args.delay}s", flush=True)

    if args.dry_run:
        print("\nDry run — sizes to test:")
        for w in widths:
            print(f"  {w}x{args.height}")
        return 0

    # Save original window frame
    orig_x = orig_y = None
    orig_w = orig_h = None
    frame = _get_window_frame()
    if frame:
        orig_x, orig_y, orig_w, orig_h = frame
        print(f"\nInitial AdsPower window: {orig_w}x{orig_h} at ({orig_x},{orig_y})", flush=True)

    ctrl = AdsPowerUIController()
    ctrl._connect()
    ctrl._foreground()
    time.sleep(0.5)

    results = []
    try:
        for width in widths:
            label = f"{width}x{args.height}"
            print(f"\n[{label}] resizing ...", flush=True, end="")

            if orig_x is not None and orig_y is not None:
                ok = _set_window_frame(width, args.height, x=orig_x, y=orig_y)
            else:
                ok = _set_window_frame(width, args.height)
            if not ok:
                results.append((label, False, "resize failed"))
                continue

            actual = _get_window_frame()
            if actual:
                aw, ah = actual[2], actual[3]
                print(f" actual: {aw}x{ah}", flush=True)
            else:
                print(" actual: (could not read)", flush=True)

            time.sleep(args.delay)

            ctrl._connect()
            ctrl._foreground()
            time.sleep(0.3)

            success = _run_rename(ctrl, profile_id, new_name)
            results.append((label, success, None))
            time.sleep(0.3)

    finally:
        if not args.no_reset and orig_x is not None and orig_y is not None:
            _set_window_frame(orig_w, orig_h, x=orig_x, y=orig_y)
            print(f"\nRestored window to {orig_w}x{orig_h} at ({orig_x},{orig_y})", flush=True)
        elif not args.no_reset:
            _set_window_frame(1200, 700)
            print("\nReset window to 1200x700", flush=True)

    print("\n" + "=" * 60, flush=True)
    print("SUMMARY", flush=True)
    print("=" * 60, flush=True)
    for label, ok, err in results:
        status = "PASS" if ok else "FAIL"
        detail = f"  ({err})" if err else ""
        print(f"  {label:<14} {status}{detail}", flush=True)

    all_ok = all(ok for _l, ok, _e in results)
    print(f"\n{'ALL PASS' if all_ok else 'SOME FAILED'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
