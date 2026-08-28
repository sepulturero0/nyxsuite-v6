"""Release-based updater helper for packaged Nyx / Nyxify builds.

This file holds the non-UI logic — version detection, GitHub Releases
lookup, ZIP download, sidecar invocation. The Tk button in phase 4
imports from here. It is intentionally Tk-free so it can also be
driven from a CLI or smoke test.

Behaviour
---------

1. `get_current_version()` reads the `VERSION` file that the build
   script stamps into the release folder, falling back to
   `core.version.NYX_VERSION` / `NYXIFY_VERSION` in source mode.

2. `get_latest_release(repo, asset_pattern)` calls the GitHub Releases
   API for the latest tag and returns the matching asset's download
   URL plus its tag name.

3. `download_to_staging(url, dest_zip, on_progress=None)` streams the
   release ZIP to disk and `extract_staging_zip` unpacks it.

4. `apply_update_via_sidecar(install_root, staging_root)` spawns
   `Updater.exe --staging <path>` and returns the spawned PID. The
   caller is expected to exit the app immediately after.

Nothing here imports tkinter, so you can run smoke tests directly:

    python -c "from core.release_updater import get_current_version; \
               print(get_current_version())"
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core.process_utils import APP_DATA_DIR, ROOT_DIR

try:
    from core.logger import logger as _update_logger
except Exception:
    import logging
    _update_logger = logging.getLogger("nyx_update")
    _update_logger.setLevel(logging.INFO)


def _log(msg: str, level: str = "info") -> None:
    """Log to the file logger and print to stdout."""
    print(f"[release_updater] {msg}", flush=True)
    fn = getattr(_update_logger, level, _update_logger.info)
    try:
        fn(msg)
    except Exception:
        pass


GITHUB_API_BASE = "https://api.github.com"
DEFAULT_USER_AGENT = "snap-bitmoji-bot-updater"
_CERTIFICATE_VERIFY_HINT = (
    "Could not verify GitHub's HTTPS certificate. This usually means the app was "
    "running without a trusted CA bundle, or the Windows account/network is using "
    "a local security certificate that is not trusted by Python. Reinstall from a "
    "new build that includes the bundled CA bundle; if it still fails, install the "
    "network/security root certificate for this Windows user."
)


@dataclass
class ReleaseInfo:
    tag_name: str
    asset_name: str
    asset_url: str
    asset_size: int
    release_name: str = ""
    body: str = ""
    html_url: str = ""


UPDATER_BINARY_NAME = "Updater.exe" if os.name == "nt" else "Updater"
DEFERRED_UPDATER_BINARY_NAME = "Updater.new.exe" if os.name == "nt" else "Updater.new"
UPDATE_NOTICE_STATE_PATH = APP_DATA_DIR / "update_notices.json"
MAX_RELEASE_NOTES_CHARS = 1800


def _https_context() -> ssl.SSLContext:
    """Return a TLS context with an explicit CA bundle when available.

    PyInstaller builds that use urllib can fail on some Windows machines because
    OpenSSL does not find a usable certificate store. Requests already depends on
    certifi, so we prefer its bundled CA file here without weakening verification.
    """
    cafile = os.getenv("NYX_UPDATE_CA_BUNDLE") or os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")
    if cafile:
        return ssl.create_default_context(cafile=cafile)

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _urlopen(req: urllib.request.Request, timeout: float):
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_https_context())
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
            raise RuntimeError(_CERTIFICATE_VERIFY_HINT) from exc
        raise


def _install_root() -> Path:
    """Folder that contains the running .exe (or the source root in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return ROOT_DIR


def _staging_root() -> Path:
    """Where downloaded releases are unpacked before the swap."""
    staging = APP_DATA_DIR / "updates"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def get_current_version(fallback: str = "0.0.0") -> str:
    """Read the VERSION file shipped in the release folder."""
    version_file = _install_root() / "VERSION"
    if version_file.exists():
        try:
            text = version_file.read_text(encoding="utf-8-sig").strip()
            if text:
                return text
        except Exception:
            pass
    return fallback


def _parse_version_tuple(value: str) -> tuple[int, ...]:
    cleaned = value.strip().lstrip("vV")
    parts: list[int] = []
    for raw in re.split(r"[.\-+]", cleaned):
        match = re.match(r"^(\d+)", raw or "")
        if not match:
            continue
        parts.append(int(match.group(1)))
    return tuple(parts) if parts else (0,)


def compare_versions(left: str, right: str) -> int:
    """Return -1 / 0 / 1 if left is older / equal / newer than right."""
    left_tuple = _parse_version_tuple(left)
    right_tuple = _parse_version_tuple(right)
    length = max(len(left_tuple), len(right_tuple))
    left_padded = left_tuple + (0,) * (length - len(left_tuple))
    right_padded = right_tuple + (0,) * (length - len(right_tuple))
    if left_padded < right_padded:
        return -1
    if left_padded > right_padded:
        return 1
    return 0


def _github_request(url: str, token: Optional[str] = None, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", DEFAULT_USER_AGENT)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with _urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                f"GitHub release was not found at {url} (HTTP 404). "
                "If the repo is private, publish updates from a public release-only repo "
                "and reinstall once from the new release ZIP so the bundled update_config.json "
                "points at the public repo."
            ) from exc
        raise


def _github_request_list(url: str, token: Optional[str] = None, timeout: float = 15.0) -> list:
    """Like _github_request but returns a list (for paginated endpoints)."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", DEFAULT_USER_AGENT)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with _urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_latest_release(repo: str, asset_pattern: str, token: Optional[str] = None) -> ReleaseInfo:
    """Look up the latest release for `repo` (including prereleases) and
    find the matching asset.

    Uses the releases list endpoint instead of ``/releases/latest`` so
    that pre‑releases are also found. Drafts are skipped.

    `asset_pattern` is a glob, for example ``NyxSuite-v*.zip``.
    """
    if not repo:
        raise ValueError("repo is required (e.g. 'owner/name').")
    url = f"{GITHUB_API_BASE}/repos/{repo}/releases?per_page=10"
    releases = _github_request_list(url, token=token)
    for payload in releases:
        if payload.get("draft"):
            continue
        tag_name = str(payload.get("tag_name") or "").strip()
        if not tag_name:
            continue
        release_name = str(payload.get("name") or tag_name).strip()
        release_body = str(payload.get("body") or "").strip()
        html_url = str(payload.get("html_url") or "").strip()
        assets = payload.get("assets") or []
        for asset in assets:
            name = str(asset.get("name") or "")
            if not name:
                continue
            if fnmatch.fnmatch(name, asset_pattern):
                download_url = str(asset.get("browser_download_url") or "")
                if not download_url:
                    continue
                size = int(asset.get("size") or 0)
                return ReleaseInfo(
                    tag_name=tag_name,
                    asset_name=name,
                    asset_url=download_url,
                    asset_size=size,
                    release_name=release_name,
                    body=release_body,
                    html_url=html_url,
                )
    raise RuntimeError(
        f"No non-draft release with asset matching {asset_pattern!r} found in {repo}. "
        "Make sure the release is published (not a draft) and has an asset matching the pattern."
    )


def list_all_releases(repo: str, asset_pattern: str, token: Optional[str] = None, limit: int = 30) -> list["ReleaseInfo"]:
    """Return every published (non-draft) release with a matching asset, newest
    first.

    This backs "roll back to any old version": rather than only the one or two
    local snapshots this install happened to make, the user can pick any build
    ever shipped to the release repo. Drafts are skipped; releases without a
    matching asset are skipped.
    """
    if not repo:
        raise ValueError("repo is required (e.g. 'owner/name').")
    url = f"{GITHUB_API_BASE}/repos/{repo}/releases?per_page={max(1, int(limit))}"
    releases = _github_request_list(url, token=token)
    infos: list[ReleaseInfo] = []
    for payload in releases:
        if payload.get("draft"):
            continue
        tag_name = str(payload.get("tag_name") or "").strip()
        if not tag_name:
            continue
        for asset in (payload.get("assets") or []):
            name = str(asset.get("name") or "")
            if not name or not fnmatch.fnmatch(name, asset_pattern):
                continue
            download_url = str(asset.get("browser_download_url") or "")
            if not download_url:
                continue
            infos.append(
                ReleaseInfo(
                    tag_name=tag_name,
                    asset_name=name,
                    asset_url=download_url,
                    asset_size=int(asset.get("size") or 0),
                    release_name=str(payload.get("name") or tag_name).strip(),
                    body=str(payload.get("body") or "").strip(),
                    html_url=str(payload.get("html_url") or "").strip(),
                )
            )
            break
    return infos


def get_release_by_version(repo: str, asset_pattern: str, version: str, token: Optional[str] = None) -> "ReleaseInfo":
    """Find the published release whose tag matches ``version`` (``v`` optional)."""
    target = _normalize_notice_tag(version)
    for info in list_all_releases(repo, asset_pattern, token=token):
        if _normalize_notice_tag(info.tag_name) == target:
            return info
    raise RuntimeError(
        f"No published release matching version {version!r} with an asset like "
        f"{asset_pattern!r} was found in {repo}."
    )


def _normalize_notice_tag(tag_name: str) -> str:
    return str(tag_name or "").strip().lstrip("vV") or "0.0.0"


def _read_update_notice_state() -> dict:
    try:
        if UPDATE_NOTICE_STATE_PATH.exists():
            return json.loads(UPDATE_NOTICE_STATE_PATH.read_text(encoding="utf-8-sig") or "{}")
    except Exception:
        pass
    return {}


def _write_update_notice_state(state: dict) -> None:
    try:
        UPDATE_NOTICE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_NOTICE_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def was_update_notice_shown(app_key: str, phase: str, tag_name: str) -> bool:
    state = _read_update_notice_state()
    key = _normalize_notice_tag(tag_name)
    app_state = state.get(str(app_key or "").strip().lower()) or {}
    shown = app_state.get(str(phase or "").strip().lower()) or []
    return key in set(str(item) for item in shown)


def mark_update_notice_shown(app_key: str, phase: str, tag_name: str) -> None:
    state = _read_update_notice_state()
    app_key = str(app_key or "").strip().lower()
    phase = str(phase or "").strip().lower()
    key = _normalize_notice_tag(tag_name)
    app_state = state.setdefault(app_key, {})
    shown = app_state.setdefault(phase, [])
    if key not in set(str(item) for item in shown):
        shown.append(key)
    _write_update_notice_state(state)


def _trim_release_notes(body: str) -> str:
    notes = str(body or "").strip()
    if not notes:
        return "No release notes were published for this update."
    if len(notes) <= MAX_RELEASE_NOTES_CHARS:
        return notes
    return notes[:MAX_RELEASE_NOTES_CHARS].rstrip() + "\n..."


def format_release_notice(app_name: str, release: ReleaseInfo, current_version: str, phase: str) -> tuple[str, str]:
    tag = str(release.tag_name or "").strip() or "latest"
    title = f"{app_name} Update {tag}"
    if str(phase or "").strip().lower() == "installed":
        heading = f"{app_name} has been updated to {tag}."
    else:
        heading = f"{app_name} {tag} is available. Current version: {current_version or 'unknown'}."

    lines = [
        heading,
        "",
        "What's new:",
        _trim_release_notes(release.body),
    ]
    return title, "\n".join(lines)


def download_to_staging(
    asset_url: str,
    dest_zip: Path,
    on_progress: Optional[Callable[[int, int], None]] = None,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> Path:
    """Stream `asset_url` to `dest_zip`. `on_progress(read, total)` if provided."""
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(asset_url)
    req.add_header("User-Agent", DEFAULT_USER_AGENT)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with _urlopen(req, timeout=timeout) as response, dest_zip.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        read = 0
        chunk_size = 64 * 1024
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            out.write(chunk)
            read += len(chunk)
            if on_progress is not None:
                try:
                    on_progress(read, total)
                except Exception:
                    pass
    return dest_zip


def extract_staging_zip(zip_path: Path, dest_dir: Path) -> Path:
    """Extract `zip_path` into `dest_dir` and return the inner folder.

    Release ZIPs are expected to contain a single top-level folder like
    ``NyxSuite-v6.3.0/``. We return that folder so the updater can be pointed
    at it.

    Robust against ZIPs whose entries use backslash (``\\``) path separators.
    Older release ZIPs were built with PowerShell's ``Compress-Archive``, which
    stored Windows-style separators; non-Windows Python treats ``\\`` as a
    literal filename character, so ``extractall()`` created flat files like
    ``NyxSuite-v6.3.0\\core\\foo.py`` instead of nested folders. The updater
    then found no ``core/``/``webui/``/``data/`` and reported "0 files synced"
    (the macOS update-does-nothing bug). We normalize separators ourselves.
    """
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            raw_name = (info.filename or "").replace("\\", "/")
            # Skip macOS resource-fork noise and absolute/escape paths.
            if raw_name.startswith("__MACOSX/") or raw_name.startswith("/"):
                continue
            parts = [p for p in raw_name.split("/") if p and p != "."]
            if not parts or ".." in parts:
                continue
            target = dest_dir.joinpath(*parts)
            if raw_name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

    children = [
        entry for entry in dest_dir.iterdir()
        if entry.is_dir() and entry.name != "__MACOSX"
    ]
    if len(children) == 1:
        return children[0]
    return dest_dir


def stage_release(
    asset_url: str,
    tag_name: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
    token: Optional[str] = None,
) -> Path:
    """Download + extract the asset; return the folder ready for the sidecar."""
    staging = _staging_root()
    safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", tag_name.strip("/\\")) or "release"
    zip_path = staging / f"{safe_tag}.zip"
    download_to_staging(asset_url, zip_path, on_progress=on_progress, token=token)
    extract_dir = staging / safe_tag
    inner = extract_staging_zip(zip_path, extract_dir)
    try:
        zip_path.unlink(missing_ok=True)
    except Exception:
        pass
    return inner


def apply_update_via_sidecar(staging_root: Path, install_root: Optional[Path] = None) -> int:
    """Spawn Updater.exe --staging <path>. The caller should exit immediately."""
    resolved_install = (install_root or _install_root()).resolve()
    updater = resolved_install / UPDATER_BINARY_NAME
    if not updater.exists():
        raise FileNotFoundError(f"Updater binary not found at {updater}")
    args = [
        str(updater),
        "--staging",
        str(staging_root.resolve()),
        "--install-root",
        str(resolved_install),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    process = subprocess.Popen(
        args,
        cwd=str(resolved_install),
        close_fds=True,
        creationflags=creationflags,
    )
    return process.pid


def repair_sidecar_updater(
    install_root: Optional[Path] = None,
    attempts: int = 24,
    delay_seconds: float = 0.5,
) -> bool:
    """Install a deferred updater copy after the sidecar process exits.

    Windows cannot overwrite the running Updater.exe during the update swap.
    Release packages also carry Updater.new.exe, which older updaters can copy
    safely. The newly launched app calls this helper once on startup to replace
    the sidecar without showing an alarming copy error to the user.
    """
    resolved_install = (install_root or _install_root()).resolve()
    deferred = resolved_install / DEFERRED_UPDATER_BINARY_NAME
    target = resolved_install / UPDATER_BINARY_NAME
    if not deferred.exists():
        return False

    for _attempt in range(max(1, int(attempts))):
        try:
            shutil.copy2(deferred, target)
            try:
                deferred.unlink(missing_ok=True)
            except Exception:
                pass
            return True
        except PermissionError:
            time.sleep(max(0.05, float(delay_seconds)))
        except Exception:
            return False
    return False


def load_update_config() -> dict:
    """Read update_config.json next to the running app."""
    config_path = _install_root() / "update_config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Data-preservation merge helpers
# ---------------------------------------------------------------------------

def _matches_any(path: str, patterns: list[str]) -> bool:
    """Return True if ``path`` matches any glob in ``patterns``."""
    norm = path.replace("\\", "/")
    for pat in patterns:
        pat_norm = pat.replace("\\", "/").strip("/")
        if fnmatch.fnmatch(norm, pat_norm):
            return True
    return False


def _ensure_data_dirs(install_root: Path) -> None:
    """Create all expected data subdirectories if they don't exist."""
    data_dir = install_root / "data"
    subdirs = [
        "full_auto_usernames",
        "signup_names",
        "logs",
        "cache",
        "updates",
        "templates",
    ]
    for sub in subdirs:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)


DATA_PRESERVE_DEFAULTS = [
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


def merge_data(
    staging_root: Path,
    install_root: Path,
    preserve_patterns: list[str] | None = None,
) -> tuple[int, int]:
    """Merge ``staging_root/data/`` into ``install_root/data/``.

    New files from staging are copied in. Files matching ``preserve_patterns``
    are never overwritten. Returns ``(copied, preserved)``.
    """
    _ensure_data_dirs(install_root)
    preserve = list(preserve_patterns or DATA_PRESERVE_DEFAULTS)
    data_staging = staging_root / "data"
    data_install = install_root / "data"
    copied = 0
    preserved = 0

    if not data_staging.is_dir():
        return copied, preserved

    for dirpath, _dirnames, filenames in os.walk(data_staging):
        for filename in filenames:
            src = Path(dirpath) / filename
            try:
                rel = src.relative_to(data_staging)
            except ValueError:
                continue
            rel_str = str(rel).replace("\\", "/")

            if _matches_any(f"data/{rel_str}", preserve):
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


SYNC_DIRS = [
    "core",
    "webui",
    "agent_host",
    "utils",
    "snap_selectors",
    "scripts",
]


def _sync_dir(name: str, staging_root: Path, install_root: Path) -> bool:
    """Replace a single directory from staging to install."""
    src = staging_root / name
    if not src.is_dir():
        return False
    dest = install_root / name
    try:
        if not any(child.is_file() for child in src.rglob("*")):
            _log(f"skipping empty staged directory {name}", "warning")
            return False
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        return True
    except Exception as exc:
        _log(f"failed to sync {name}: {exc}", "error")
        return False


def sync_extensions(staging_root: Path, install_root: Path) -> int:
    """Replace extension folders in install_root with those from staging.

    Returns number of extensions synced.
    """
    synced = 0
    for ext_name in ("nyx_extension", "nyxify_extension"):
        if _sync_dir(ext_name, staging_root, install_root):
            synced += 1
    return synced


def sync_source_dirs(staging_root: Path, install_root: Path) -> int:
    """Replace Python source directories (core/, webui/, etc.) with
    those from the staging release.

    Returns number of directories synced.
    """
    synced = 0
    for name in SYNC_DIRS:
        if _sync_dir(name, staging_root, install_root):
            synced += 1
    return synced


def apply_update_direct(
    asset_url: str,
    tag_name: str,
    on_progress: Callable[[int, int], None] | None = None,
    token: str | None = None,
    install_root: Path | None = None,
) -> dict:
    """Download a new release ZIP, extract it, and apply the update
    directly (source dirs + extensions + data merge). No sidecar needed
    since the release is source-based (cross-platform).

    Returns a dict with keys:
        ok, message, version, copied, preserved,
        synced_extensions, synced_source_dirs
    """
    resolved_install = (install_root or _install_root()).resolve()
    result = {
        "ok": False,
        "message": "",
        "version": "",
        "copied": 0,
        "preserved": 0,
        "synced_extensions": 0,
        "synced_source_dirs": 0,
    }

    try:
        staging = stage_release(asset_url, tag_name, on_progress=on_progress, token=token)
    except Exception as exc:
        _log(f"download/extract failed: {exc}", "error")
        result["message"] = f"Download/extract failed: {exc}"
        return result

    _log(f"staging extracted to {staging}")

    # Snapshot the current install BEFORE overwriting so Roll Back has a restore
    # point. The source-based updater (this function) is what the bridge calls,
    # so without this no backup is ever created and rollback has nothing to do.
    try:
        from core.update_backup import snapshot_install

        backup_dir = snapshot_install(install_root=resolved_install)
        _log(f"backed up current install to {backup_dir}")
    except Exception as exc:
        _log(f"pre-update snapshot failed (rollback may be unavailable): {exc}", "error")

    # Sync Python source directories (core/, webui/, agent_host/, ...)
    synced_dirs = sync_source_dirs(staging, resolved_install)
    result["synced_source_dirs"] = synced_dirs
    _log(f"synced {synced_dirs} source directories")

    # Sync extensions
    synced_ext = sync_extensions(staging, resolved_install)
    result["synced_extensions"] = synced_ext
    _log(f"synced {synced_ext} extensions")

    # Merge data (preserving user configs, DBs, logs)
    config = load_update_config()
    preserve = config.get("data_preserve_paths") or list(DATA_PRESERVE_DEFAULTS)
    copied, preserved = merge_data(staging, resolved_install, preserve)
    result["copied"] = copied
    result["preserved"] = preserved
    _log(f"data merged: {copied} files copied, {preserved} preserved")

    # Copy root-level files (bridge_app.py, launchers, etc.)
    root_copied = _sync_root_files(staging, resolved_install)
    _log(f"synced {root_copied} root-level files")

    # Read staged version
    staged_version = ""
    version_file = staging / "VERSION"
    if version_file.exists():
        try:
            staged_version = version_file.read_text(encoding="utf-8-sig").strip()
        except Exception:
            pass

    # Write VERSION
    if staged_version:
        try:
            (resolved_install / "VERSION").write_text(staged_version, encoding="ascii")
            _log(f"VERSION updated to {staged_version}")
        except Exception as exc:
            _log(f"could not write VERSION: {exc}", "error")

    # Cleanup staging
    try:
        shutil.rmtree(staging.parent, ignore_errors=True)
    except Exception:
        pass

    result["ok"] = True
    result["version"] = staged_version or tag_name
    result["message"] = (
        f"Updated to {staged_version or tag_name} "
        f"(source dirs: {synced_dirs}, extensions: {synced_ext}, "
        f"data files: {copied}, preserved: {preserved})."
    )
    return result


ROOT_FILES_TO_SYNC = [
    "bridge_app.py",
    "main.py",
    "nyxify_runner.py",
    "requirements.txt",
    "run_nyx_suite.bat",
    "run_nyx_suite.sh",
    "run_nyx_suite.command",
    "portable_launch_nyx.ps1",
    "portable_launch_nyx.sh",
    ".env.example",
]


def _sync_root_files(staging_root: Path, install_root: Path) -> int:
    """Replace root-level launcher and config files from staging."""
    copied = 0
    for name in ROOT_FILES_TO_SYNC:
        src = staging_root / name
        if not src.exists():
            continue
        dest = install_root / name
        try:
            shutil.copy2(src, dest)
            copied += 1
        except Exception:
            pass
    return copied
