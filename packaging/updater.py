"""Sidecar updater binary.

Lives next to the Nyx or Nyxify desktop app. The desktop app downloads
a new release ZIP, extracts it into a staging folder, then spawns this
updater and exits. We wait for the parent's file locks to release,
copy the staged files in (skipping user data), bump the VERSION file,
and relaunch the app.

Run manually for testing:

    Updater.exe --staging "C:\\path\\to\\extracted\\Nyx v3.1" \
                --install-root "C:\\path\\to\\install\\Nyx v3"

The --install-root arg defaults to the folder this binary lives in.
Without --staging the updater exits with usage info.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_SKIP_PATHS = [
    "data",
    "logs",
    ".env",
]

# Default patterns for files to preserve during data merge (new structure).
DEFAULT_DATA_PRESERVE_PATHS = [
    "data/*.db",
    "data/bridge_config.json",
    "data/nyx_config.json",
    "data/nyxify_config.json",
    "data/bitmoji_models.json",
    "data/bitmoji_outfits.json",
    "data/full_auto_usernames/*",
    "data/signup_names/*",
    "data/logs/*",
]

# Wait up to this long for the parent app to release file locks on the
# binaries we are about to overwrite. We retry the copy at the per-file
# level too, so this is just an initial grace period.
INITIAL_LOCK_WAIT_SECONDS = 6
PER_FILE_RETRY_COUNT = 12
PER_FILE_RETRY_DELAY_SECONDS = 0.6
DEFERRED_UPDATER_BINARY_NAME = "Updater.new.exe" if os.name == "nt" else "Updater.new"


def _resolve_install_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    # When frozen we live inside the install folder; the install root is
    # the folder containing this exe.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _read_config(install_root: Path) -> dict:
    config_path = install_root / "update_config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _should_skip(rel_path: Path, skip_paths: list[str]) -> bool:
    rel_str = str(rel_path).replace("\\", "/")
    for skip in skip_paths:
        skip_clean = str(skip).replace("\\", "/").strip("/")
        if not skip_clean:
            continue
        if rel_str == skip_clean:
            return True
        if rel_str.startswith(skip_clean + "/"):
            return True
    return False


def _copy_with_retry(src: Path, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(PER_FILE_RETRY_COUNT):
        try:
            shutil.copy2(src, dest)
            return True
        except PermissionError as exc:
            last_error = exc
            time.sleep(PER_FILE_RETRY_DELAY_SECONDS)
        except Exception as exc:
            last_error = exc
            time.sleep(PER_FILE_RETRY_DELAY_SECONDS)
    print(f"[updater] failed to copy {src} -> {dest}: {last_error}")
    return False


def _current_updater_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def _is_current_updater_dest(dest: Path) -> bool:
    try:
        return dest.resolve() == _current_updater_path()
    except Exception:
        return False


def _walk_staging(staging_root: Path):
    for dirpath, _dirnames, filenames in os.walk(staging_root):
        for filename in filenames:
            yield Path(dirpath) / filename


def _swap_files(staging_root: Path, install_root: Path, skip_paths: list[str]) -> tuple[int, int]:
    copied = 0
    skipped = 0
    for src in _walk_staging(staging_root):
        try:
            rel = src.relative_to(staging_root)
        except ValueError:
            continue

        if _should_skip(rel, skip_paths):
            skipped += 1
            continue

        dest = install_root / rel
        if _is_current_updater_dest(dest):
            deferred_dest = install_root / DEFERRED_UPDATER_BINARY_NAME
            if _copy_with_retry(src, deferred_dest):
                print("[updater] deferred Updater.exe self-update; app will apply it after relaunch.")
                copied += 1
            else:
                skipped += 1
            continue

        if _copy_with_retry(src, dest):
            copied += 1
        else:
            skipped += 1
    return copied, skipped


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _copy_extension_tree(source: Path, target: Path) -> bool:
    try:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        return True
    except Exception as exc:
        print(f"[updater] failed to sync extension {source} -> {target}: {exc}")
        return False


def _sync_extension_folders(staging_root: Path, install_root: Path, config: dict) -> int:
    app = str(config.get("app") or "").strip().lower()
    if app == "nyxify":
        source_name = "nyxify_extension"
        target_names = ["nyxify_extension", "nyxify_v3_extension"]
    elif app == "nyx":
        source_name = "nyx_extension"
        target_names = ["nyx_extension", "nyx_v3_extension"]
    else:
        return 0

    source = staging_root / source_name
    if not source.exists() or not source.is_dir():
        return 0

    candidates = [install_root / source_name]
    for base in [install_root.parent, install_root.parent.parent]:
        for target_name in target_names:
            candidates.append(base / target_name)

    allowed_roots = [install_root, install_root.parent, install_root.parent.parent]
    synced = 0
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        if resolved.name not in target_names:
            continue
        if not any(_is_relative_to(resolved, root) for root in allowed_roots):
            continue
        if resolved != (install_root / source_name).resolve() and not resolved.exists():
            continue
        if _copy_extension_tree(source, resolved):
            synced += 1
    return synced


def _write_version(install_root: Path, staged_version: str | None) -> None:
    if not staged_version:
        return
    version_path = install_root / "VERSION"
    try:
        version_path.write_text(staged_version.strip(), encoding="ascii")
    except Exception as exc:
        print(f"[updater] could not write VERSION: {exc}")


def _read_staged_version(staging_root: Path) -> str | None:
    version_file = staging_root / "VERSION"
    if not version_file.exists():
        return None
    try:
        return version_file.read_text(encoding="utf-8-sig").strip() or None
    except Exception:
        return None


def _relaunch(install_root: Path, exe_name: str | None) -> None:
    if not exe_name:
        print("[updater] no exe_to_relaunch configured; finished.")
        return
    target = install_root / exe_name
    if not target.exists():
        print(f"[updater] cannot relaunch, exe missing at {target}")
        return
    try:
        subprocess.Popen(
            [str(target)],
            cwd=str(install_root),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        print(f"[updater] relaunched {target}")
    except Exception as exc:
        print(f"[updater] failed to relaunch {target}: {exc}")


def _matches_any(path: str, patterns: list[str]) -> bool:
    import fnmatch

    norm = path.replace("\\", "/")
    for pat in patterns:
        pat_norm = pat.replace("\\", "/").strip("/")
        if fnmatch.fnmatch(norm, pat_norm):
            return True
    return False


def _merge_data(staging_root: Path, install_root: Path, preserve_patterns: list[str]) -> tuple[int, int]:
    """Merge staging_root/data/ into install_root/data/, preserving files
    matching preserve_patterns. Returns (copied, preserved)."""
    data_staging = staging_root / "data"
    data_install = install_root / "data"
    if not data_staging.is_dir():
        return 0, 0
    if not data_install.exists():
        data_install.mkdir(parents=True)
    copied = 0
    preserved = 0
    for dirpath, _dirnames, filenames in os.walk(data_staging):
        for filename in filenames:
            src = Path(dirpath) / filename
            try:
                rel = src.relative_to(data_staging)
            except ValueError:
                continue
            rel_str = str(rel).replace("\\", "/")
            if _matches_any(f"data/{rel_str}", preserve_patterns):
                preserved += 1
                continue
            dest = data_install / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dest)
                copied += 1
            except Exception:
                pass
    return copied, preserved


def main() -> int:
    parser = argparse.ArgumentParser(description="Nyx / Nyxify sidecar updater.")
    parser.add_argument("--staging", required=True, help="Path to the extracted new-version folder.")
    parser.add_argument("--install-root", default=None, help="Path to the install folder (defaults to this exe's folder).")
    args = parser.parse_args()

    staging_root = Path(args.staging).resolve()
    if not staging_root.exists() or not staging_root.is_dir():
        print(f"[updater] staging path is not a directory: {staging_root}")
        return 2

    install_root = _resolve_install_root(args.install_root)
    install_root.mkdir(parents=True, exist_ok=True)

    config = _read_config(install_root)
    skip_paths = list(config.get("skip_paths") or DEFAULT_SKIP_PATHS)
    exe_name = config.get("exe_to_relaunch") or ""

    # Detect new-style release (extensions + data only, no exes).
    staging_has_data = (staging_root / "data").is_dir()
    staging_has_extensions = (staging_root / "nyx_extension").is_dir() or (staging_root / "nyxify_extension").is_dir()
    new_style_release = staging_has_data and not any(
        (staging_root / f"{name}.exe").exists()
        for name in ["Updater", "Updater.new"]
    )

    # Capture the version we're replacing BEFORE the swap overwrites VERSION, so
    # the launch watchdog can roll back to it if the new build fails to start.
    prev_version = ""
    try:
        version_file = install_root / "VERSION"
        if version_file.exists():
            prev_version = version_file.read_text(encoding="utf-8-sig").strip()
    except Exception:
        prev_version = ""

    print(f"[updater] install_root = {install_root}")
    print(f"[updater] staging_root = {staging_root}")
    if new_style_release:
        print(f"[updater] detected new-style release (extensions + data only)")
    print(f"[updater] waiting {INITIAL_LOCK_WAIT_SECONDS}s for parent to exit ...")
    time.sleep(INITIAL_LOCK_WAIT_SECONDS)

    if new_style_release:
        # New-style: sync extensions + merge data (no exe swapping needed).
        copied = 0
        skipped = 0

        # Sync extensions
        ext_config = config.get("app", "")
        if staging_has_extensions:
            # _sync_extension_folders handles both nyx/nyxify app types
            pass
        extension_synced = _sync_extension_folders(staging_root, install_root, config)

        # Merge data
        preserve = config.get("data_preserve_paths") or DEFAULT_DATA_PRESERVE_PATHS
        data_copied, data_preserved = _merge_data(staging_root, install_root, preserve)
        copied = extension_synced + data_copied
        skipped = data_preserved

        print(f"[updater] extensions synced={extension_synced} data_copied={data_copied} data_preserved={data_preserved}")
    else:
        # Legacy full-swap: backup then swap all files (skipping data/logs/.env).
        try:
            from core.update_backup import snapshot_install

            backup_dir = snapshot_install(install_root, skip_paths=skip_paths)
            print(f"[updater] backed up current install ({prev_version or 'unknown'}) to {backup_dir}")
        except Exception as exc:
            print(f"[updater] backup skipped: {exc}")

        copied, skipped = _swap_files(staging_root, install_root, skip_paths)
        print(f"[updater] copied={copied} skipped={skipped}")
        extension_synced = _sync_extension_folders(staging_root, install_root, config)
        if extension_synced:
            print(f"[updater] extension folders synced={extension_synced}")

    staged_version = _read_staged_version(staging_root)
    _write_version(install_root, staged_version)

    # Arm the post-update launch watchdog: if the new build crash-loops on
    # startup, the app rolls back to prev_version (see core.update_backup).
    try:
        from core.update_backup import write_pending_verify

        write_pending_verify(new=staged_version or "", prev=prev_version, install_root=install_root)
    except Exception as exc:
        print(f"[updater] pending-verify write skipped: {exc}")

    # Best-effort cleanup of the staging folder so it doesn't accumulate.
    try:
        shutil.rmtree(staging_root, ignore_errors=True)
    except Exception:
        pass

    _relaunch(install_root, exe_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
