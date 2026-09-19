"""No-API fallback: attach to AdsPower profiles over the Chrome DevTools
Protocol (CDP) without using the Local API.

Why this exists
---------------
On AdsPower "Employee"/sub-accounts the Local API is permission-gated: the
server answers ``/status`` keyless, but ``/browser/start`` returns
``{"code":9110,"msg":"No local API permission"}`` because the main/admin
account never granted Local-API permission to the sub-account (there is no
self-serve toggle in the employee app). The permission can only be flipped by
the org admin.

When a profile is opened from the AdsPower **GUI** ("Open" button), AdsPower
still launches its SunBrowser with ``--remote-debugging-port=0``. Chromium then
binds a random port and writes it (plus the browser websocket path) to
``<user-data-dir>/DevToolsActivePort``. The per-profile user-data-dir lives under
the AdsPower cache directory, named ``<profile-serial>_<hash>`` where the serial
is the profile's ``user_id`` (the same value passed to ``/browser/start``).

So with zero Local-API calls we can: find the open profile's DevToolsActivePort,
confirm the port is live, and hand the ``ws://`` endpoint to Playwright's
``connect_over_cdp`` — full control with the real fingerprint + proxy intact.

Hard limit: this can only ATTACH to a profile that is already open in the
AdsPower app. It cannot *start* one — starting is what ``/browser/start`` (and
the gated permission) is for. So the fallback covers "run the Bitmoji flow on
profiles a human opened", not hands-off fleet startup.
"""
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

from core.logger import logger


# DevToolsActivePort persists on disk after a browser closes, so a matching file
# is NOT proof the browser is up — every endpoint is liveness-checked before use.
# A closed localhost port refuses instantly (RST), so a short timeout is plenty.
_TCP_PROBE_TIMEOUT = float(os.getenv("ADSPOWER_CDP_TCP_TIMEOUT", "0.3"))
_HTTP_PROBE_TIMEOUT = float(os.getenv("ADSPOWER_CDP_HTTP_TIMEOUT", "2.5"))
# The cache dir accumulates one stale folder per profile ever opened (hundreds+).
# The brute-force "match by start-page serial" scan only probes the N folders with
# the most recently written DevToolsActivePort — the open browsers — so it stays
# cheap no matter how many stale folders pile up. Tunable for huge fleets.
_SCAN_LIMIT = max(1, int(os.getenv("ADSPOWER_CDP_SCAN_LIMIT", "64") or "64"))


def _cache_base_dirs():
    """Candidate AdsPower cache roots (each holds one dir per open profile).

    The default Windows root is ``C:\\.ADSPOWER_GLOBAL\\cache``; the cache root is
    user-configurable in AdsPower (Settings -> Local settings -> "Custom data
    cache directory"), so an explicit override wins via ``ADSPOWER_CACHE_DIR``.
    """
    candidates = []

    override = str(os.getenv("ADSPOWER_CACHE_DIR") or "").strip()
    if override:
        base = Path(override)
        # Accept either the cache dir itself or the parent that contains it.
        candidates.append(base)
        candidates.append(base / "cache")

    if os.name == "nt":
        candidates.append(Path(r"C:\.ADSPOWER_GLOBAL\cache"))
        appdata = os.getenv("APPDATA", "")
        if appdata:
            candidates.append(Path(appdata) / "adspower_global" / "cache")
    else:
        home = Path.home()
        candidates.append(home / ".ADSPOWER_GLOBAL" / "cache")
        candidates.append(home / "Library" / "Application Support" / "adspower_global" / "cache")
        candidates.append(
            home / "Library" / "Application Support" / "adspower_global"
            / "cwd_global" / "source" / "cache"
        )
        candidates.append(home / ".config" / "adspower_global" / "cache")
        candidates.append(Path("/Users/Shared/.ADSPOWER_GLOBAL/cache"))

    seen = set()
    resolved = []
    for path in candidates:
        try:
            key = str(path)
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        if path.is_dir():
            resolved.append(path)
    return resolved


def _read_devtools_active_port(cache_dir):
    """Return ``(port, ws_path)`` from ``<cache_dir>/DevToolsActivePort`` or None.

    Line 1 is the port; line 2 (when present) is ``/devtools/browser/<guid>``.
    """
    dtap = Path(cache_dir) / "DevToolsActivePort"
    try:
        lines = dtap.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    if not lines:
        return None
    try:
        port = int(str(lines[0]).strip())
    except (ValueError, IndexError):
        return None
    if port <= 0:
        return None
    ws_path = str(lines[1]).strip() if len(lines) > 1 else ""
    return port, ws_path


def _port_is_listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_TCP_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _http_get_json(session, port, path):
    url = f"http://127.0.0.1:{port}{path}"
    try:
        response = session.get(url, timeout=_HTTP_PROBE_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def _browser_ws_endpoint(session, port, ws_path, on_browser_started=None):
    """Confirm the port hosts a live CDP browser and return its ``ws://`` URL.

    Prefers the path from DevToolsActivePort; falls back to the
    ``webSocketDebuggerUrl`` advertised by ``/json/version``.
    """
    if not _port_is_listening(port):
        return ""
    if callable(on_browser_started):
        on_browser_started(port)
    version = _http_get_json(session, port, "/json/version")
    if not isinstance(version, dict):
        return ""
    if ws_path:
        return f"ws://127.0.0.1:{port}{ws_path}"
    advertised = str(version.get("webSocketDebuggerUrl") or "").strip()
    return advertised


def _profile_serial_from_target_url(url):
    """Extract the AdsPower profile serial from a SunBrowser start-page URL.

    The first tab is ``https://start.adspower.net/?id=<serial>&host=...``; that
    ``id`` is the profile's ``user_id``.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    if "start.adspower" not in (parsed.netloc or "").lower():
        return ""
    values = parse_qs(parsed.query or "").get("id") or []
    return str(values[0]).strip() if values else ""


def _open_serial_for_port(session, port):
    """Best-effort: which profile serial is this open browser? ('' if unknown)."""
    targets = _http_get_json(session, port, "/json")
    if not isinstance(targets, list):
        return ""
    for target in targets:
        if not isinstance(target, dict):
            continue
        serial = _profile_serial_from_target_url(target.get("url") or "")
        if serial:
            return serial
    return ""


def _new_session():
    session = requests.Session()
    session.trust_env = False
    return session


def _all_profile_dirs():
    """Every per-profile cache dir across all known roots (cheap: names only)."""
    for base in _cache_base_dirs():
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for cache_dir in entries:
            if cache_dir.is_dir():
                yield cache_dir


def _recent_live_candidates(session, limit=_SCAN_LIMIT):
    """Yield ``(cache_dir, port, ws_path)`` for the most-recently-opened browsers
    that are actually listening, newest first, capped at ``limit``.

    Ordering by DevToolsActivePort mtime puts genuinely-open browsers first and
    keeps the (potentially huge) pile of stale folders from ever being probed.
    """
    scored = []
    for cache_dir in _all_profile_dirs():
        dtap = cache_dir / "DevToolsActivePort"
        try:
            mtime = dtap.stat().st_mtime
        except OSError:
            continue
        scored.append((mtime, cache_dir))

    scored.sort(reverse=True)
    for _mtime, cache_dir in scored[:limit]:
        parsed = _read_devtools_active_port(cache_dir)
        if not parsed:
            continue
        port, ws_path = parsed
        if _port_is_listening(port):
            yield cache_dir, port, ws_path


def find_open_profile_cdp_endpoint(
    profile_id,
    session=None,
    deep_scan=True,
    on_browser_started=None,
):
    """Return a live ``ws://`` CDP endpoint for an already-open AdsPower profile,
    or ``""`` if the profile is not currently open.

    Matching strategy:
      1. Cache dir named ``<profile_id>`` or ``<profile_id>_<hash>`` (strong: the
         dir name is the profile's ``user_id``).
      2. Fallback scan of every open browser, matching the profile serial parsed
         from its ``start.adspower.net`` start page (covers any id-naming quirk).
    Every candidate is liveness-checked, so stale DevToolsActivePort files left
    behind by a closed browser are ignored.

    ``deep_scan=False`` skips step 2. The fallback HTTP-probes every open browser
    (~2.5s each), which is very slow when many profiles are open and the target
    is *not* among them. Step 1 alone is reliable for a known id (AdsPower always
    names the dir after the id), so callers that already have the id — e.g. right
    after opening it via the GUI — should pass ``deep_scan=False`` to stay fast.
    """
    wanted = str(profile_id or "").strip()
    if not wanted:
        return ""

    owns_session = session is None
    session = session or _new_session()
    try:
        # 1) Direct match by cache-dir name (== serial or serial_<hash>). Cheap:
        #    only the matching folder(s) get probed, so the stale pile is ignored.
        for cache_dir in _all_profile_dirs():
            name = cache_dir.name
            if name != wanted and not name.startswith(f"{wanted}_"):
                continue
            parsed = _read_devtools_active_port(cache_dir)
            if not parsed:
                continue
            endpoint = _browser_ws_endpoint(
                session,
                parsed[0],
                parsed[1],
                on_browser_started=on_browser_started,
            )
            if endpoint:
                return endpoint

        # 2) Fallback for any id-naming quirk: among the most-recently-opened live
        #    browsers, match the serial on its start.adspower.net start page.
        if deep_scan:
            for _cache_dir, port, ws_path in _recent_live_candidates(session):
                if _open_serial_for_port(session, port) != wanted:
                    continue
                endpoint = _browser_ws_endpoint(
                    session,
                    port,
                    ws_path,
                    on_browser_started=on_browser_started,
                )
                if endpoint:
                    return endpoint

        return ""
    finally:
        if owns_session:
            try:
                session.close()
            except Exception:
                pass


def list_open_profile_endpoints(session=None):
    """Return ``{profile_serial: ws_endpoint}`` for every live, open AdsPower
    profile we can resolve a serial for. Used by diagnostics / the Test button.
    """
    owns_session = session is None
    session = session or _new_session()
    endpoints = {}
    try:
        for cache_dir, port, ws_path in _recent_live_candidates(session):
            serial = _open_serial_for_port(session, port) or cache_dir.name.split("_", 1)[0]
            if not serial:
                continue
            endpoint = _browser_ws_endpoint(session, port, ws_path)
            if endpoint and serial not in endpoints:
                endpoints[serial] = endpoint
        return endpoints
    finally:
        if owns_session:
            try:
                session.close()
            except Exception:
                pass


_CDP_CLOSED_ERROR_PARTS = (
    "target page, context or browser has been closed",
    "page, context or browser has been closed",
    "target closed",
    "browser has been closed",
    "browser closed",
)


def _is_cdp_closed_error(exc):
    text = str(exc or "").strip().lower()
    return any(part in text for part in _CDP_CLOSED_ERROR_PARTS)


def _snapshot_browser_pages(browser):
    try:
        contexts = list(getattr(browser, "contexts", []) or [])
    except Exception as exc:
        if _is_cdp_closed_error(exc):
            return []
        raise

    pages = []
    for context in contexts:
        try:
            pages.extend(list(getattr(context, "pages", []) or []))
        except Exception as exc:
            if _is_cdp_closed_error(exc):
                continue
            raise
    return pages


def _close_page_without_beforeunload(page):
    try:
        page.close(run_before_unload=False)
        return True
    except TypeError:
        page.close()
        return True
    except Exception as exc:
        if _is_cdp_closed_error(exc):
            return False
        raise


def _close_all_browser_pages(browser, max_rounds=4):
    """Close every visible tab/page in every Playwright browser context.

    Closing the last AdsPower/SunBrowser tab usually tears down the browser
    process, so browser/context access can disappear mid-loop. Once at least one
    close has been requested, those "target closed" errors mean the intended
    shutdown has started.
    """
    closed_count = 0
    for _round in range(max(1, int(max_rounds or 1))):
        pages = _snapshot_browser_pages(browser)
        if not pages:
            return closed_count
        for page in pages:
            if _close_page_without_beforeunload(page):
                closed_count += 1
        time.sleep(0.1)
    return closed_count


def close_open_profile_tabs(
    profile_id,
    session=None,
    deep_scan=False,
    playwright_factory=None,
    max_rounds=4,
):
    """Close all tabs for one open AdsPower profile via its own CDP endpoint.

    Returns ``True`` after at least one tab close was requested. Returns ``False``
    when the profile has no live CDP endpoint, letting callers fall back to the
    AdsPower API or GUI row controls.
    """
    wanted = str(profile_id or "").strip()
    if not wanted:
        return False

    endpoint = find_open_profile_cdp_endpoint(wanted, session=session, deep_scan=deep_scan)
    if not endpoint:
        return False

    if playwright_factory is None:
        from playwright.sync_api import sync_playwright

        playwright_factory = sync_playwright

    browser = None
    with playwright_factory() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        try:
            closed_count = _close_all_browser_pages(browser, max_rounds=max_rounds)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception as exc:
                    if not _is_cdp_closed_error(exc):
                        logger.debug(
                            f"Could not detach from AdsPower profile {wanted} CDP browser: {exc}"
                        )

    if closed_count <= 0:
        logger.warning(f"AdsPower profile {wanted} had a live CDP endpoint but no tabs to close.")
        return False

    logger.info(f"Closed {closed_count} tab(s) for AdsPower profile {wanted} via CDP.")
    return True
