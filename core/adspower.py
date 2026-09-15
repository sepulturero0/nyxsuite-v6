from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import random
import re
import socket
from pathlib import Path
import threading
import time
import requests
from requests import exceptions as requests_exceptions
from urllib.parse import urlparse

from core.logger import logger


# Realistic Windows GPU vendor/renderer pairs as Chrome reports them through
# ANGLE/Direct3D11. We pick one at random per profile so every AdsPower profile
# carries a different (but plausible) WebGL metadata fingerprint, instead of the
# whole fleet sharing one hardcoded GPU. All entries are Windows D3D11 GPUs to
# stay consistent with the Windows 10/11 + Chrome user-agent AdsPower assigns —
# this only varies the WebGL *metadata* (which is already set to "Custom" in the
# preferences); it does not touch any of the hardware-noise toggles.
WEBGL_GPU_POOL = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 5700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]


SHARED_BROWSER_TAG_IDS = {
    "snapchat": "1314993",
    "olivia": "1314992",
    "tessa": "1314991",
    "debbie": "1314990",
    "willow": "1314989",
    "alicia": "1314988",
    "lizzie": "1314987",
    "chloe": "1314986",
    "emily": "1314985",
    "clea": "1314983",
    "jade": "1314982",
    "nina": "1314981",
}


ADSPOWER_PERMISSION_HELP = (
    "AdsPower denied the Local API (code 9110: no permission). Open the AdsPower desktop "
    "app, make sure you are logged in, and turn on Settings -> API (Local API). NyxSuite "
    "connects automatically once it is on — no API key is needed for localhost. (Only if "
    "your AdsPower is configured to require an API key, paste it in NyxSuite Settings -> "
    "Advanced Config.)"
)

ADSPOWER_UNREACHABLE_HELP = (
    "Can't reach AdsPower. Open the AdsPower desktop app and make sure it's running and "
    "logged in (its Local API starts automatically). NyxSuite will connect on its own and "
    "resume as soon as AdsPower is up."
)


class AdsPowerError(Exception):
    """Base for AdsPower-side failures the runner can reason about."""


class AdsPowerPermissionError(AdsPowerError):
    """AdsPower rejected the call for lack of Local API permission / API key
    (code 9110, or any code whose message mentions 'no local api permission').
    This is an environment/config problem, never a per-profile failure, so it is
    NOT retried and must never burn the queue."""


class AdsPowerUnreachableError(AdsPowerError, ConnectionError):
    """The AdsPower Local API could not be reached at all (app closed / API off).
    Subclasses ConnectionError so any existing ``except ConnectionError`` paths
    keep working."""


class AdsPowerProfileNotOpenError(AdsPowerError):
    """No-API CDP fallback could not attach because the profile is not open in
    the AdsPower app. Distinct from a permission error: the Local API is gated
    (9110) but the *server* is fine, so only THIS profile is blocked — other
    already-open profiles can still run. Callers should hold just this row
    PENDING (open the profile in AdsPower to proceed), NOT trip a global health
    flag that pauses the whole queue. See [[core/adspower_cdp.py]]."""


def _is_permission_error(payload):
    """True when an AdsPower JSON error means 'no Local API permission'."""
    if isinstance(payload, dict):
        code = payload.get("code")
        msg = str(payload.get("msg") or "").strip().lower()
    else:
        code = None
        msg = str(payload or "").strip().lower()
    if code == 9110:
        return True
    return "no local api permission" in msg or "local api permission" in msg


def _is_proxy_checker_unavailable_error(error_message):
    normalized = str(error_message or "").strip().lower()
    return (
        "404" in normalized
        or "not found for url" in normalized
        or "method not allowed" in normalized
    )


def _is_transient_adspower_api_error(payload):
    normalized = str(payload or "").strip().lower()
    return (
        "too many request per second" in normalized
        or "too many requests per second" in normalized
        or "rate limit" in normalized
    )


def _coerce_bool(*values, default=False):
    """First value that resolves to a real bool wins (config first, then env).
    Accepts actual bools or common string forms; ``None``/empty/unrecognized
    values are skipped so the next source — and finally ``default`` — applies."""
    for value in values:
        if isinstance(value, bool):
            return value
        if value is None:
            continue
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _coerce_control_mode(*values, default="auto"):
    for value in values:
        normalized = str(value or "").strip().lower()
        if normalized in {"auto", "api", "gui"}:
            return normalized
    return default


class AdsPowerManager:

    def __init__(self, ui_assume_presearch=None):
        # GUI-automation mode for the no-API path:
        #   None  -> use AdsPowerUIConfig's default (env ADSPOWER_UI_ASSUME_PRESEARCH,
        #            default False = Nyx's Profile-ID search mode).
        #   True  -> Nyxify dashboard mode (act on the standing 'Name contains <temp>'
        #            view, only ever search the temp name).
        #   False -> Nyx mode (bulk Profile-ID search, editing the chip in place).
        # Nyx and Nyxify run as separate processes, so each sets its own mode
        # rather than sharing one process-wide env flag.
        self._ui_assume_presearch = ui_assume_presearch

        self._resolve_credentials()
        self._api_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._last_request_started_at = 0.0
        self.request_interval_seconds = float(
            os.getenv("ADSPOWER_REQUEST_INTERVAL_SECONDS")
            or os.getenv("ADSP_REQUEST_INTERVAL_SECONDS")
            or "1.2"
        )
        self.transient_retry_count = max(
            0,
            int(
                os.getenv("ADSPOWER_TRANSIENT_RETRY_COUNT")
                or os.getenv("ADSP_TRANSIENT_RETRY_COUNT")
                or "3"
            ),
        )
        self.session = requests.Session()
        self.session.trust_env = False
        self.host_candidates = self._build_host_candidates(self.host)
        self.base_url = f"http://{self.host_candidates[0]}:{self.port}/api/v1"
        self.active_host = self.host_candidates[0]
        # Profiles we attached to via the no-API CDP fallback (the human opened
        # them in the AdsPower GUI). We must NOT /browser/stop these on cleanup —
        # we don't own them and the stop call is 9110-gated anyway.
        self._cdp_fallback_profiles = set()

    def _resolve_credentials(self):
        # Saved Settings (data/nyx_config.json) win over environment so users can
        # paste an API key / host / port in the dashboard without touching .env.
        # Best-effort: a missing/broken config must never stop construction.
        cfg = {}
        try:
            from core.nyx_runtime_config import load_nyx_config
            cfg = load_nyx_config() or {}
        except Exception:
            cfg = {}

        self.host = (
            str(cfg.get("adspower_host") or "").strip()
            or os.getenv("ADSPOWER_HOST")
            or os.getenv("ADSP_HOST")
            or "127.0.0.1"
        )
        self.port = (
            str(cfg.get("adspower_port") or "").strip()
            or os.getenv("ADSPOWER_PORT")
            or os.getenv("ADSP_PORT")
            or "50325"
        )
        self.api_key = (
            str(cfg.get("adspower_api_key") or "").strip()
            or os.getenv("ADSPOWER_API_KEY")
            or os.getenv("ADSP_API_KEY")
            or ""
        )
        self.control_mode = _coerce_control_mode(
            cfg.get("adspower_control_mode"),
            os.getenv("ADSPOWER_CONTROL_MODE"),
            default="auto",
        )
        # No-API CDP fallback: when /browser/start is permission-gated (9110),
        # attach to a profile the user opened in the AdsPower GUI over CDP. On by
        # default; disable with adspower_cdp_fallback=false or ADSPOWER_CDP_FALLBACK=0.
        cdp_fallback_enabled = _coerce_bool(
            cfg.get("adspower_cdp_fallback"),
            os.getenv("ADSPOWER_CDP_FALLBACK"),
            default=True,
        )
        # No-API GUI fallback: when the Local API is permission-gated (9110),
        # drive the AdsPower desktop app to CREATE and OPEN profiles (see
        # core/adspower_ui.py). On by default; disable with adspower_ui_fallback=
        # false or ADSPOWER_UI_FALLBACK=0. Uses Windows UIA or macOS AXUI.
        ui_fallback_enabled = _coerce_bool(
            cfg.get("adspower_ui_fallback"),
            os.getenv("ADSPOWER_UI_FALLBACK"),
            default=True,
        )
        if self.control_mode == "api":
            self.cdp_fallback_enabled = False
            self.ui_fallback_enabled = False
        elif self.control_mode == "gui":
            self.cdp_fallback_enabled = True
            self.ui_fallback_enabled = True
        else:
            self.cdp_fallback_enabled = cdp_fallback_enabled
            self.ui_fallback_enabled = ui_fallback_enabled
        self._ui_controller_obj = None
        self._ui_on_profile_missing = None

    def _is_gui_control_mode(self):
        return str(getattr(self, "control_mode", "auto") or "auto").strip().lower() == "gui"

    def _is_api_control_mode(self):
        return str(getattr(self, "control_mode", "auto") or "auto").strip().lower() == "api"

    def set_ui_on_profile_missing(self, callback):
        """Register a ``callback(profile_id: str)`` invoked when the UI controller
        searches for a profile ID in the AdsPower table and cannot find it.
        The callback is forwarded to the UI controller (created lazily on first
        use), so it may be called before the controller is built."""
        self._ui_on_profile_missing = callback
        if self._ui_controller_obj is not None:
            self._ui_controller_obj.on_profile_missing = callback

    def _ui_controller(self):
        """Lazily build the GUI-automation controller (import is deferred so the
        manager still imports on platforms without pywinauto)."""
        if self._ui_controller_obj is None:
            from core.adspower_ui import AdsPowerUIController, AdsPowerUIConfig
            config = AdsPowerUIConfig()
            if self._ui_assume_presearch is not None:
                config.assume_presearch = bool(self._ui_assume_presearch)
            self._ui_controller_obj = AdsPowerUIController(config=config)
            if self._ui_on_profile_missing is not None:
                self._ui_controller_obj.on_profile_missing = self._ui_on_profile_missing
        return self._ui_controller_obj

    @staticmethod
    def _proxy_to_ui_string(proxy_value, user_proxy_config=None):
        """Build a 'host:port:user:pass' string for the GUI Host field from a
        parsed proxy config (preferred) or the raw proxy value."""
        cfg = dict(user_proxy_config or {})
        host = str(cfg.get("proxy_host") or "").strip()
        port = str(cfg.get("proxy_port") or "").strip()
        if host and port:
            parts = [host, port]
            user = str(cfg.get("proxy_user") or "").strip()
            pwd = str(cfg.get("proxy_password") or "").strip()
            if user:
                parts.append(user)
            if pwd:
                parts.append(pwd)
            return ":".join(parts)
        return str(proxy_value or "").strip()

    def reload_credentials(self):
        """Re-read host/port/API key from Settings then env so a key pasted in
        the dashboard takes effect without restarting the runner."""
        self._resolve_credentials()
        self.host_candidates = self._build_host_candidates(self.host)
        self.base_url = f"http://{self.host_candidates[0]}:{self.port}/api/v1"
        self.active_host = self.host_candidates[0]

    def _read_host_from_local_api_file(self):
        candidates = []
        if os.name == "nt":
            candidates.append(Path(os.getenv("APPDATA", "")) / "adspower_global" / "cwd_global" / "source" / "local_api")
        else:
            candidates.append(Path.home() / "Library" / "Application Support" / "adspower_global" / "cwd_global" / "source" / "local_api")
            candidates.append(Path.home() / ".adspower_global" / "cwd_global" / "source" / "local_api")
        for local_api_path in candidates:
            if local_api_path.exists():
                try:
                    raw_value = local_api_path.read_text(encoding="utf-8").strip()
                    if raw_value:
                        return str(urlparse(raw_value).hostname or "").strip()
                except Exception:
                    pass
        return ""

    def _read_host_from_intranet_file(self):
        candidates = []
        if os.name == "nt":
            candidates.append(Path("C:/.ADSPOWER_GLOBAL/intranet"))
        else:
            candidates.append(Path.home() / ".ADSPOWER_GLOBAL" / "intranet")
            candidates.append(Path("/Users/Shared/.ADSPOWER_GLOBAL/intranet"))
        for intranet_path in candidates:
            if intranet_path.exists():
                try:
                    return intranet_path.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
        return ""

        try:
            return intranet_path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def _build_host_candidates(self, preferred_host):
        candidates = []

        def add(value):
            normalized = str(value or "").strip()
            if not normalized or normalized in candidates:
                return
            candidates.append(normalized)

        add(preferred_host)
        add(self._read_host_from_local_api_file())
        add(self._read_host_from_intranet_file())
        add("127.0.0.1")
        add("localhost")
        add("local.adspower.net")
        add("local.adspower.com")

        return candidates or ["127.0.0.1"]

    def _iter_hosts(self):
        yielded = set()

        for host in [self.active_host, *self.host_candidates]:
            normalized = str(host or "").strip()
            if not normalized or normalized in yielded:
                continue
            yielded.add(normalized)
            yield normalized

    def _build_base_url(self, host):
        return f"http://{host}:{self.port}/api/v1"

    def _build_root_url(self, host):
        return f"http://{host}:{self.port}"

    def _respect_rate_limit(self):
        with self._request_lock:
            now = time.monotonic()
            sleep_seconds = self.request_interval_seconds - (now - self._last_request_started_at)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            self._last_request_started_at = time.monotonic()

    def _request_json(self, method, path, payload=None, timeout=30, **params):
        with self._api_lock:
            self._respect_rate_limit()

            last_connection_error = None

            for host in self._iter_hosts():
                is_absolute_api_path = str(path or "").startswith("/api/")
                base_url = self._build_root_url(host) if is_absolute_api_path else self._build_base_url(host)
                url = f"{base_url}{path}"

                for attempt in range(self.transient_retry_count + 1):
                    if attempt > 0:
                        sleep_seconds = min(4.0, 0.8 * attempt)
                        logger.warning(
                            f"AdsPower API transient error for {path}; retrying in {sleep_seconds:.1f}s "
                            f"({attempt}/{self.transient_retry_count})."
                        )
                        time.sleep(sleep_seconds)
                        self._respect_rate_limit()

                    try:
                        if method == "GET":
                            response = self.session.get(
                                url,
                                params=self._build_params(**params),
                                timeout=timeout,
                            )
                        else:
                            response = self.session.post(
                                url,
                                params=self._build_params(**params),
                                headers=self._build_headers(),
                                json=payload or {},
                                timeout=timeout,
                            )
                    except (requests_exceptions.ConnectionError, requests_exceptions.Timeout) as exc:
                        last_connection_error = exc
                        break

                    try:
                        response.raise_for_status()
                    except requests_exceptions.HTTPError as exc:
                        if (
                            _is_transient_adspower_api_error(getattr(response, "text", "") or exc)
                            and attempt < self.transient_retry_count
                        ):
                            continue
                        raise
                    data = response.json()

                    if host != self.active_host:
                        logger.info(f"AdsPower API host switched to {host}:{self.port}")
                        self.active_host = host
                        self.base_url = self._build_base_url(host)

                    if isinstance(data, dict) and data.get("code", 0) not in (0, None):
                        if _is_transient_adspower_api_error(data) and attempt < self.transient_retry_count:
                            continue
                        # Permission/auth failure is NOT transient — don't retry,
                        # raise a typed error with the actionable fix so callers
                        # can gate the queue instead of marking rows FAILED.
                        if _is_permission_error(data):
                            raise AdsPowerPermissionError(f"{ADSPOWER_PERMISSION_HELP} (raw: {data})")
                        raise Exception(f"AdsPower API error: {data}")

                    return data

            tried_hosts = ", ".join(self._iter_hosts())
            raise AdsPowerUnreachableError(
                f"{ADSPOWER_UNREACHABLE_HELP} "
                f"Tried hosts: {tried_hosts}. Last error: {last_connection_error}"
            )

    def _build_params(self, **extra):

        params = dict(extra)

        if self.api_key:
            params["apikey"] = self.api_key

        return params

    def _build_headers(self):

        headers = {}

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        return headers

    def sanitize_proxy_value(self, proxy_value):
        raw_proxy = str(proxy_value or "")
        if not raw_proxy.strip():
            return ""

        lines = []
        for line in raw_proxy.replace("\r", "\n").split("\n"):
            cleaned = str(line or "").strip()
            if not cleaned:
                continue
            if cleaned in {"↻", "⟳", "⟲"}:
                continue
            if len(cleaned) <= 3 and not re.search(r"[A-Za-z0-9]", cleaned):
                continue
            lines.append(cleaned)

        normalized = "".join(lines).strip()
        normalized = normalized.rstrip("↻⟳⟲").strip()

        if normalized.count(":") >= 3:
            parts = normalized.split(":")
            host = parts[0].strip()
            port = parts[1].strip()
            username = parts[2].strip()
            password = ":".join(parts[3:]).strip()
            normalized = ":".join([host, port, username, password])

        return normalized

    def _get_json(self, path, timeout=30, **params):
        return self._request_json("GET", path, timeout=timeout, **params)

    def _post_json(self, path, payload=None, timeout=30, **params):
        return self._request_json("POST", path, payload=payload, timeout=timeout, **params)

    def _desktop_app_http_running(self, timeout=2):
        """Best-effort check for AdsPower's desktop HTTP server.

        The no-API GUI path does not require the Local API port to be available,
        but the desktop app itself must be running. AdsPower exposes a lightweight
        local server on 20725 while the app is open.
        """
        for host in self._iter_hosts():
            try:
                response = self.session.get(f"http://{host}:20725/", timeout=timeout)
            except Exception:
                continue
            if getattr(response, "status_code", None) == 200:
                return True
        return False

    def preflight_check(self):
        """Lightweight auto-connect probe for the AdsPower Local API.

        Hits the keyless root ``/status`` endpoint — the canonical "is AdsPower
        up" check — across the host candidates (127.0.0.1, local.adspower.net,
        the host AdsPower wrote to its local_api file, etc.), locking onto the
        first that answers so every later call auto-targets the right endpoint.
        No API key is required on localhost. ``/status`` is not rate-limited and
        not permission-gated, unlike ``/user/list``, so it never falsely blocks a
        working setup.

        If the Local API port (50325) is completely unreachable but the AdsPower
        desktop app HTTP server (port 20725) is answering (the user is running
        AdsPower with the Local API disabled — no permission toggled on), the
        check passes anyway: the runner will open/create/close profiles via the
        GUI fallback (``core/adspower_ui.py``). Returns:
            {"ok": bool, "code": "ok"|"adspower_permission"|"adspower_unreachable",
             "message": <actionable text>}
        """
        with self._api_lock:
            self._respect_rate_limit()
            last_error = None

            if self._is_gui_control_mode() and self._desktop_app_http_running(timeout=2):
                return {
                    "ok": True,
                    "code": "ok",
                    "message": "AdsPower desktop app OK (GUI control mode selected).",
                }

            for host in self._iter_hosts():
                url = f"{self._build_root_url(host)}/status"
                try:
                    response = self.session.get(url, params=self._build_params(), timeout=8)
                except (requests_exceptions.ConnectionError, requests_exceptions.Timeout) as exc:
                    last_error = exc
                    continue
                except Exception as exc:
                    last_error = exc
                    continue

                # AdsPower answered: the app is up. Lock onto this host so the
                # real /browser/start calls auto-use the working endpoint.
                if host != self.active_host:
                    logger.info(f"AdsPower API host switched to {host}:{self.port}")
                    self.active_host = host
                    self.base_url = self._build_base_url(host)

                try:
                    data = response.json() if (response.content or b"") else {}
                except Exception:
                    data = {}

                if _is_permission_error(data):
                    if self._is_gui_control_mode():
                        return {
                            "ok": True,
                            "code": "ok",
                            "message": f"AdsPower Local API is permission-gated, continuing in GUI control mode. (raw: {data})",
                        }
                    return {
                        "ok": False,
                        "code": "adspower_permission",
                        "message": f"{ADSPOWER_PERMISSION_HELP} (raw: {data})",
                    }
                # code 0 (or any non-permission response) means we're connected.
                if self._is_gui_control_mode():
                    return {
                        "ok": True,
                        "code": "ok",
                        "message": "Connected to AdsPower; GUI control mode selected.",
                    }
                return {"ok": True, "code": "ok", "message": "Connected to AdsPower Local API."}

            # Local API port (50325) is unreachable. Fall back to probing the
            # AdsPower desktop app HTTP server on port 20725 — if it answers,
            # AdsPower is running; the runner will drive it via the GUI fallback.
            _FALLBACK_PORT = 20725
            if not self._is_api_control_mode():
                for host in self._iter_hosts():
                    url = f"http://{host}:{_FALLBACK_PORT}/"
                    try:
                        response = self.session.get(url, timeout=5)
                    except Exception:
                        continue
                    if response.status_code == 200:
                        logger.info(
                            f"AdsPower desktop app is running (port {_FALLBACK_PORT}); "
                            f"Local API on port {self.port} is not available — will use GUI fallback."
                        )
                        mode = "GUI control mode" if self._is_gui_control_mode() else "GUI-fallback mode"
                        return {
                            "ok": True,
                            "code": "ok",
                            "message": f"AdsPower app OK ({mode}; Local API port {self.port} unreachable).",
                        }

            return {
                "ok": False,
                "code": "adspower_unreachable",
                "message": f"{ADSPOWER_UNREACHABLE_HELP} (tried: {', '.join(self._iter_hosts())}; last error: {last_error})",
            }

    # -------------------------------------------------
    # OPEN PROFILE
    # -------------------------------------------------

    def _extract_ws_endpoint(self, payload):

        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}

        ws_block = data.get("ws", {})
        if not isinstance(ws_block, dict):
            ws_block = {}

        candidates = [
            ws_block.get("puppeteer"),
            ws_block.get("playwright"),
            data.get("ws_endpoint"),
            data.get("wsEndpoint"),
            data.get("websocket"),
            data.get("webSocket"),
        ]

        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value

        raise KeyError("AdsPower start response did not include a usable websocket endpoint.")

    def open_profile(self, profile_id, retries=3):
        """Start the profile via the Local API and return a Playwright CDP
        endpoint. If the API is permission-gated (9110) or unreachable, fall back
        to attaching over CDP to a profile the user already opened in the AdsPower
        GUI (no API needed) — see ``core/adspower_cdp.py``."""
        if self._is_gui_control_mode():
            return self._open_profile_via_gui_first(profile_id)
        try:
            return self._open_profile_via_api(profile_id, retries)
        except AdsPowerError as api_error:
            return self._open_profile_via_cdp_fallback(profile_id, api_error)

    def _open_profile_via_api(self, profile_id, retries=3):

        for attempt in range(retries):

            try:

                data = self._get_json(
                    "/browser/start",
                    user_id=profile_id,
                    open_tabs=1
                )

                ws_endpoint = self._extract_ws_endpoint(data)

                logger.info(f"AdsPower profile started: {profile_id}")
                logger.info(f"Playwright endpoint: {ws_endpoint}")

                self._cdp_fallback_profiles.discard(str(profile_id))
                return ws_endpoint

            except AdsPowerError as e:
                # Permission/unreachable are environment problems, not transient
                # per-profile hiccups — retrying 3x just delays the inevitable and
                # spams the API. Surface immediately so the caller can gate.
                logger.error(f"Failed to start profile {profile_id}: {e}")
                raise

            except Exception as e:

                logger.error(f"Failed to start profile {profile_id}: {e}")

                if attempt < retries - 1:
                    time.sleep(3)
                else:
                    raise

    def _open_profile_via_gui_first(self, profile_id):
        """GUI-selected mode: skip /browser/start and open through AdsPower UI."""
        endpoint = ""
        try:
            from core.adspower_cdp import find_open_profile_cdp_endpoint

            endpoint = find_open_profile_cdp_endpoint(profile_id, session=self.session)
        except Exception as scan_error:
            logger.debug(f"GUI control mode CDP scan failed for {profile_id}: {scan_error}")

        if endpoint:
            self._cdp_fallback_profiles.add(str(profile_id))
            logger.info(f"Attached to already-open AdsPower profile {profile_id} in GUI control mode: {endpoint}")
            return endpoint

        ui_error_text = ""
        try:
            endpoint = self._ui_controller().open_profile_by_id(profile_id)
        except Exception as ui_error:
            logger.error(f"AdsPower GUI open failed for {profile_id} in GUI control mode: {ui_error}")
            ui_error_text = str(ui_error)
            endpoint = ""

        if endpoint:
            self._cdp_fallback_profiles.add(str(profile_id))
            logger.info(f"Opened AdsPower profile {profile_id} via GUI control mode: {endpoint}")
            return endpoint

        raise AdsPowerProfileNotOpenError(
            f"GUI control mode is selected, but profile {profile_id} could not be opened "
            f"through the AdsPower desktop app. Make sure AdsPower is open and visible. "
            f"(UI error: {ui_error_text or 'n/a'})"
        )

    def _open_profile_via_cdp_fallback(self, profile_id, api_error):
        """The Local API could not start the profile. If the fallback is enabled
        and the profile is already open in the AdsPower app, attach to it over
        CDP. Otherwise re-raise an actionable error."""
        if not self.cdp_fallback_enabled:
            raise api_error

        endpoint = ""
        try:
            from core.adspower_cdp import find_open_profile_cdp_endpoint

            endpoint = find_open_profile_cdp_endpoint(profile_id, session=self.session)
        except Exception as scan_error:
            logger.debug(f"No-API CDP fallback scan failed for {profile_id}: {scan_error}")

        if endpoint:
            self._cdp_fallback_profiles.add(str(profile_id))
            logger.warning(
                f"AdsPower Local API could not start {profile_id} ({api_error}); attached to the "
                f"already-open profile over CDP (no-API mode): {endpoint}"
            )
            return endpoint

        # Not already open. Drive the AdsPower GUI to open the profile (search
        # by id -> click Open), then attach over CDP — fully hands-off, no API
        # needed.  This runs whether the Local API is permission-gated (9110) or
        # completely unreachable (port 50325 not listening), as long as the
        # AdsPower desktop app window is still available — the UI automation
        # talks to the app window, not the API.
        ui_error_text = ""
        if self.ui_fallback_enabled:
            try:
                endpoint = self._ui_controller().open_profile_by_id(profile_id)
            except Exception as ui_error:
                logger.error(f"AdsPower GUI open fallback failed for {profile_id}: {ui_error}")
                ui_error_text = str(ui_error)
                endpoint = ""
            if endpoint:
                self._cdp_fallback_profiles.add(str(profile_id))
                logger.warning(
                    f"AdsPower Local API could not start {profile_id} ({api_error}); opened it via "
                    f"the AdsPower GUI and attached over CDP (no-API mode): {endpoint}"
                )
                return endpoint

        # Nothing to attach to. If the whole API is unreachable (app closed),
        # that's a global block — re-raise so the queue pauses. If it's only
        # permission-gated (server up), this is a per-profile hold: the user just
        # hasn't opened THIS profile in AdsPower yet.
        if isinstance(api_error, AdsPowerUnreachableError):
            raise api_error

        raise AdsPowerProfileNotOpenError(
            f"AdsPower's Local API can't start profiles on this account (permission denied / 9110), "
            f"and profile {profile_id} is not open in the AdsPower app. Open it in AdsPower and "
            f"NyxSuite will attach automatically over CDP (no-API mode), or have your AdsPower admin "
            f"grant Local API permission for this account. "
            f"(API error: {api_error}; UI error: {ui_error_text or 'n/a'})"
        )

    # -------------------------------------------------
    # CLOSE PROFILE
    # -------------------------------------------------

    def _close_profile_tabs_via_cdp(self, profile_id):
        pid = str(profile_id or "").strip()
        if not pid:
            return False

        cdp_enabled = bool(getattr(self, "cdp_fallback_enabled", False))
        fallback_profiles = getattr(self, "_cdp_fallback_profiles", set()) or set()
        if not (cdp_enabled or self._is_gui_control_mode() or pid in fallback_profiles):
            return False

        try:
            from core.adspower_cdp import close_open_profile_tabs

            return bool(
                close_open_profile_tabs(
                    pid,
                    session=getattr(self, "session", None),
                    deep_scan=False,
                )
            )
        except Exception as cdp_error:
            logger.warning(f"Could not close AdsPower profile {pid} tabs via CDP: {cdp_error}")
            return False

    def close_profile(self, profile_id):
        pid = str(profile_id or "").strip()

        if self._close_profile_tabs_via_cdp(pid):
            logger.info(f"Closed AdsPower profile {pid} by closing all CDP tabs.")
            return {"code": 0, "msg": "closed_via_cdp_tabs"}

        if self._is_gui_control_mode() and self.ui_fallback_enabled:
            try:
                closed = self._ui_controller().close_profile_by_id(pid)
                if closed is False:
                    raise RuntimeError("AdsPower GUI did not confirm the profile closed.")
                logger.info(f"Closed AdsPower profile {pid} via GUI control mode.")
                return {"code": 0, "msg": "closed_via_gui"}
            except Exception as ui_error:
                logger.warning(f"Could not GUI-close AdsPower profile {pid}: {ui_error}")
                return {"code": 0, "msg": "gui_close_failed_left_open"}

        # No-API mode: this profile was opened by driving the AdsPower GUI (the
        # /browser/stop API is the same 9110-gated endpoint), so close it the same
        # way — click the row's Close button. The browser window should actually
        # close when a run finishes instead of piling up.
        if pid in self._cdp_fallback_profiles:
            # Keep the profile marked no-API after closing: a no-API profile is
            # permanently no-API (the Local API is permission-gated), and the
            # Nyxify flow renames it right AFTER closing — keeping the mark sends
            # that rename straight to the GUI instead of wasting two 9110 API
            # calls first. The mark is only cleared on delete (profile gone).
            if self.ui_fallback_enabled:
                try:
                    closed = self._ui_controller().close_profile_by_id(pid)
                    if closed is False:
                        raise RuntimeError("AdsPower GUI did not confirm the profile closed.")
                    logger.info(f"Closed AdsPower profile {pid} via the GUI (no-API mode).")
                    return {"code": 0, "msg": "closed_via_gui"}
                except Exception as ui_error:
                    logger.warning(f"Could not GUI-close AdsPower profile {pid}: {ui_error}")
                    return {"code": 0, "msg": "gui_close_failed_left_open"}
            logger.info(f"Left AdsPower profile {pid} open (no-API mode, GUI fallback disabled).")
            return {"code": 0, "msg": "left_open_cdp_fallback"}

        try:
            data = self._get_json("/browser/stop", user_id=profile_id)
            logger.info(f"Profile closed: {profile_id}")
            return data

        except (AdsPowerPermissionError, AdsPowerUnreachableError) as api_error:
            # In no-API mode the stop API may be permission-gated or fully
            # unavailable. If the desktop app is open, close via the GUI instead.
            if self.ui_fallback_enabled:
                try:
                    closed = self._ui_controller().close_profile_by_id(pid)
                    if closed is False:
                        raise RuntimeError("AdsPower GUI did not confirm the profile closed.")
                    logger.info(f"Closed AdsPower profile {pid} via the GUI (API unavailable: {api_error}).")
                    return {"code": 0, "msg": "closed_via_gui"}
                except Exception as ui_error:
                    logger.warning(f"Could not GUI-close AdsPower profile {pid}: {ui_error}")
            logger.error(f"Failed to close profile {profile_id}: {api_error}")
            raise

        except Exception as e:
            logger.error(f"Failed to close profile {profile_id}: {e}")
            raise

    def delete_profile(self, profile_id):
        normalized_profile_id = str(profile_id or "").strip()
        if not normalized_profile_id:
            raise ValueError("AdsPower profile id is required.")

        # Close the profile first — AdsPower cannot delete an open profile.
        try:
            self.close_profile(normalized_profile_id)
        except Exception as exc:
            logger.debug(f"Close-before-delete for {normalized_profile_id} (already closed?): {exc}")

        if self._is_gui_control_mode() and self.ui_fallback_enabled:
            logger.warning(
                f"Deleting AdsPower profile {normalized_profile_id} via GUI control mode.")
            data = self._ui_controller().delete_profile_by_id(normalized_profile_id)
            self._cdp_fallback_profiles.discard(normalized_profile_id)
            return data

        # Fast path: a profile we opened in no-API mode -> the delete API is the
        # same 9110-gated endpoint, so skip it and delete through the GUI.
        if normalized_profile_id in self._cdp_fallback_profiles and self.ui_fallback_enabled:
            logger.warning(
                f"Deleting no-API AdsPower profile {normalized_profile_id} via the GUI (no-API mode).")
            data = self._ui_controller().delete_profile_by_id(normalized_profile_id)
            self._cdp_fallback_profiles.discard(normalized_profile_id)
            return data

        attempts = [
            ("/api/v2/browser-profile/delete", {"Profile_id": [normalized_profile_id]}),
            ("/user/delete", {"user_ids": [normalized_profile_id]}),
        ]
        errors = []
        gui_fallback_reason = None

        for path, payload in attempts:
            try:
                data = self._post_json(path, payload=payload, timeout=20)
                if not isinstance(data, dict) or data.get("code") != 0:
                    raise RuntimeError(f"AdsPower delete did not confirm success: {data}")
                logger.info(f"AdsPower profile deleted: {normalized_profile_id} via {path} payload={payload}")
                self._cdp_fallback_profiles.discard(normalized_profile_id)
                return data
            except (AdsPowerPermissionError, AdsPowerUnreachableError) as exc:
                gui_fallback_reason = exc
                errors.append(f"{path}: {exc}")
                break  # both endpoints share the same no-API condition
            except Exception as exc:
                if _is_permission_error(str(exc)):
                    gui_fallback_reason = exc
                errors.append(f"{path} payload={payload}: {exc}")
                logger.debug(f"AdsPower delete profile attempt failed via {path} payload={payload}: {exc}")

        # No-API fallback: drive the AdsPower GUI (select row -> trash -> confirm).
        if gui_fallback_reason is not None and self.ui_fallback_enabled:
            logger.warning(
                f"AdsPower Local API unavailable for delete ({normalized_profile_id}: "
                f"{gui_fallback_reason}); deleting via the GUI (no-API mode).")
            data = self._ui_controller().delete_profile_by_id(normalized_profile_id)
            self._cdp_fallback_profiles.discard(normalized_profile_id)
            return data

        raise RuntimeError(
            f"Could not delete AdsPower profile {normalized_profile_id}; attempts failed: "
            + " | ".join(errors)
        )

    # -------------------------------------------------
    # CHECK PROFILE STATUS
    # -------------------------------------------------

    def get_profile_status(self, profile_id):
        return self._get_json("/browser/active", timeout=8, user_id=profile_id)

    def _extract_profile_ids(self, payload):
        def extract_ids(payload):
            ids = []

            if isinstance(payload, dict):
                for key in ["data", "list", "items"]:
                    nested = payload.get(key)
                    ids.extend(extract_ids(nested))

                for key in ["user_id", "userId", "id", "profile_id", "profileId", "serial_number"]:
                    value = payload.get(key)
                    if value:
                        ids.append(str(value).strip())

            elif isinstance(payload, list):
                for item in payload:
                    ids.extend(extract_ids(item))

            return ids

        unique_ids = []
        seen = set()
        for profile_id in extract_ids(payload):
            if profile_id and profile_id not in seen:
                seen.add(profile_id)
                unique_ids.append(profile_id)

        return unique_ids

    def list_all_profile_ids(self, page_size=1000):

        data = self._get_json("/user/list", page=1, page_size=page_size)
        profile_ids = self._extract_profile_ids(data)
        logger.info(f"AdsPower profile ids detected: {profile_ids}")
        return profile_ids

    def list_recent_profile_entries(self, page_size=200):
        data = self._get_json("/user/list", timeout=12, page=1, page_size=page_size)
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        entries = payload.get("list", []) if isinstance(payload, dict) else []
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def get_profile_count(self):
        data = self._get_json("/user/list", timeout=15, page=1, page_size=1)
        payload = data.get("data", {}) if isinstance(data, dict) else {}

        if isinstance(payload, dict):
            try:
                total = int(payload.get("total") or 0)
                if total >= 0:
                    return total
            except Exception:
                pass

            entries = payload.get("list", [])
            if isinstance(entries, list):
                return len([entry for entry in entries if isinstance(entry, dict)])

        if isinstance(payload, list):
            return len([entry for entry in payload if isinstance(entry, dict)])

        return len(self.list_all_profile_ids(page_size=1000))

    def get_profile_name(self, profile_id, page_size=1000):
        normalized_profile_id = str(profile_id or "").strip()
        if not normalized_profile_id:
            return ""

        for entry in self.list_recent_profile_entries(page_size=min(page_size, 200)):
            entry_profile_id = str(entry.get("user_id") or entry.get("id") or "").strip()
            if entry_profile_id != normalized_profile_id:
                continue
            name = str(entry.get("name") or entry.get("profile_name") or entry.get("user_name") or "").strip()
            if name:
                return name

        data = self._get_json("/user/list", timeout=20, page=1, page_size=page_size)
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        entries = payload.get("list", []) if isinstance(payload, dict) else []

        if not isinstance(entries, list):
            return ""

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_profile_id = str(entry.get("user_id") or entry.get("id") or "").strip()
            if entry_profile_id != normalized_profile_id:
                continue
            return str(entry.get("name") or entry.get("profile_name") or entry.get("user_name") or "").strip()

        return ""

    def get_profile_name_map(self, page_size=1000):
        """Return a ``{profile_id: name}`` map for all AdsPower profiles.

        Used by the dashboard to label rows with the profile's username (the
        AdsPower profile name is what the Nyx extension treats as the username).
        One ``/user/list`` call; callers are expected to cache the result.
        """
        name_map = {}
        try:
            data = self._get_json("/user/list", timeout=20, page=1, page_size=page_size)
        except Exception:
            return name_map
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        entries = payload.get("list", []) if isinstance(payload, dict) else []
        if not isinstance(entries, list):
            return name_map
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            profile_id = str(entry.get("user_id") or entry.get("id") or "").strip()
            if not profile_id:
                continue
            name = str(entry.get("name") or entry.get("profile_name") or entry.get("user_name") or "").strip()
            if name:
                name_map[profile_id] = name
        return name_map

    def rename_profile(self, profile_id, new_name):
        normalized_profile_id = str(profile_id or "").strip()
        normalized_name = str(new_name or "").strip()
        if not normalized_profile_id:
            raise ValueError("AdsPower profile id is required.")
        if not normalized_name:
            raise ValueError("AdsPower profile name is required.")

        if self._is_gui_control_mode() and self.ui_fallback_enabled:
            logger.warning(
                f"Renaming AdsPower profile {normalized_profile_id} -> {normalized_name!r} "
                f"via GUI control mode.")
            info = self._ui_controller().rename_profile_by_id(normalized_profile_id, normalized_name)
            return {
                "profile_id": normalized_profile_id,
                "previous_name": "",
                "name": str(info.get("name") or normalized_name).strip(),
                "raw": info,
            }

        # Fast path: a profile we created in no-API mode -> the update API is the
        # same 9110-gated endpoint, so rename straight through the GUI.
        if normalized_profile_id in self._cdp_fallback_profiles and self.ui_fallback_enabled:
            logger.warning(
                f"Renaming no-API AdsPower profile {normalized_profile_id} -> {normalized_name!r} "
                f"via the GUI (no-API mode).")
            info = self._ui_controller().rename_profile_by_id(normalized_profile_id, normalized_name)
            return {
                "profile_id": normalized_profile_id,
                "previous_name": "",
                "name": str(info.get("name") or normalized_name).strip(),
                "raw": info,
            }

        previous_name = ""
        try:
            previous_name = self.get_profile_name(normalized_profile_id, page_size=1000)
        except Exception as exc:
            logger.warning(f"Could not read current AdsPower name for {normalized_profile_id}: {exc}")

        payload_candidates = [
            {"user_id": normalized_profile_id, "name": normalized_name},
            {"id": normalized_profile_id, "name": normalized_name},
            {"user_id": normalized_profile_id, "profile_name": normalized_name},
            {"id": normalized_profile_id, "profile_name": normalized_name},
        ]

        last_error = None
        gui_fallback_reason = None
        for payload in payload_candidates:
            try:
                response = self._post_json("/user/update", payload=payload, timeout=20)
                logger.info(f"AdsPower profile renamed: {normalized_profile_id} -> {normalized_name}")
                return {
                    "profile_id": normalized_profile_id,
                    "previous_name": previous_name,
                    "name": normalized_name,
                    "raw": response,
                }
            except (AdsPowerPermissionError, AdsPowerUnreachableError) as exc:
                last_error = exc
                gui_fallback_reason = exc
                break  # every payload hits the same no-API condition
            except Exception as exc:
                last_error = exc
                if _is_permission_error(str(exc)):
                    gui_fallback_reason = exc
                    break

        # No-API fallback: rename through the AdsPower GUI (Name edit pencil).
        if gui_fallback_reason is not None and self.ui_fallback_enabled:
            logger.warning(
                f"AdsPower Local API unavailable for rename ({normalized_profile_id}: "
                f"{gui_fallback_reason}); "
                f"renaming via the GUI (no-API mode).")
            info = self._ui_controller().rename_profile_by_id(normalized_profile_id, normalized_name)
            return {
                "profile_id": normalized_profile_id,
                "previous_name": previous_name,
                "name": str(info.get("name") or normalized_name).strip(),
                "raw": info,
            }

        raise Exception(f"Could not rename AdsPower profile {normalized_profile_id}: {last_error}")

    def list_active_profiles(self):

        data = self._get_json("/browser/active")
        unique_ids = self._extract_profile_ids(data)

        logger.info(f"AdsPower active profiles detected: {unique_ids}")
        return unique_ids

    def _is_profile_active_status(self, payload):
        if not isinstance(payload, dict):
            return False

        data = payload.get("data", payload)
        if not isinstance(data, dict):
            return False

        status_value = str(
            data.get("status")
            or data.get("state")
            or data.get("browser_status")
            or data.get("browserStatus")
            or ""
        ).strip().lower()

        if not status_value:
            return False

        return status_value in {"active", "open", "opened", "running", "connected"} or "active" in status_value

    def find_active_profiles_via_status_probe(self, page_size=200, max_candidates=40):
        recent_entries = self.list_recent_profile_entries(page_size=page_size)
        candidates = []

        for entry in recent_entries:
            profile_id = str(entry.get("user_id") or entry.get("id") or "").strip()
            if not profile_id:
                continue
            try:
                last_open_time = int(float(str(entry.get("last_open_time") or "0").strip()))
            except Exception:
                last_open_time = 0
            if last_open_time <= 0:
                continue
            candidates.append((last_open_time, profile_id))

        candidates.sort(reverse=True)
        candidate_ids = []
        seen = set()
        for _last_open_time, profile_id in candidates:
            if profile_id in seen:
                continue
            seen.add(profile_id)
            candidate_ids.append(profile_id)
            if len(candidate_ids) >= max_candidates:
                break

        active_ids = []
        for profile_id in candidate_ids:
            try:
                status_payload = self.get_profile_status(profile_id)
                if self._is_profile_active_status(status_payload):
                    active_ids.append(profile_id)
            except Exception as exc:
                logger.warning(f"Could not probe AdsPower active status for {profile_id}: {exc}")

        logger.info(f"AdsPower active profiles detected via status probe: {active_ids}")
        return active_ids

    def force_close_all_opened_profiles(self):
        fallback_used = False

        try:
            candidate_ids = self.list_active_profiles()
        except Exception as active_error:
            fallback_used = True
            logger.warning(f"Falling back to status-probed AdsPower profiles for force-close: {active_error}")
            candidate_ids = self.find_active_profiles_via_status_probe()

        unique_candidate_ids = []
        seen = set()
        for profile_id in candidate_ids:
            normalized = str(profile_id or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_candidate_ids.append(normalized)

        closed = 0
        failed = 0

        if unique_candidate_ids:
            max_workers = min(
                len(unique_candidate_ids),
                max(1, int(os.getenv("ADSPOWER_CLOSE_ALL_WORKERS", "8"))),
            )

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(self.close_profile, profile_id): profile_id
                    for profile_id in unique_candidate_ids
                }

                for future in as_completed(future_map):
                    profile_id = future_map[future]
                    try:
                        future.result()
                        closed += 1
                    except Exception as exc:
                        failed += 1
                        logger.warning(f"Force-close failed for AdsPower profile {profile_id}: {exc}")

        return {
            "candidate_ids": unique_candidate_ids,
            "closed": closed,
            "failed": failed,
            "fallback_used": fallback_used,
        }

    def _extract_tags_from_payload(self, payload):
        def normalize_tag_list(values):
            normalized = []
            seen = set()

            for item in values or []:
                if isinstance(item, dict):
                    value = str(
                        item.get("name")
                        or item.get("tag_name")
                        or item.get("tagName")
                        or ""
                    ).strip()
                else:
                    value = str(item or "").strip()

                if not value:
                    continue

                key = value.lower()
                if key in seen:
                    continue

                seen.add(key)
                normalized.append(value)

            return normalized

        if isinstance(payload, dict):
            candidate_keys = [
                "tags",
                "tag",
                "tag_list",
                "tagList",
                "user_tags",
                "userTags",
                "fbcc_user_tag",
                "fbccUserTag",
            ]

            for key in candidate_keys:
                value = payload.get(key)
                if isinstance(value, list):
                    normalized = normalize_tag_list(value)
                    if normalized:
                        return normalized
                if isinstance(value, str) and value.strip():
                    return normalize_tag_list(value.split(","))

            for key in ["data", "list", "items"]:
                nested = payload.get(key)
                tags = self._extract_tags_from_payload(nested)
                if tags:
                    return tags

        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue

                matches_profile = str(item.get("user_id", item.get("id", ""))).strip()
                tags = self._extract_tags_from_payload(item)
                if matches_profile or tags:
                    return tags

        return []

    def _extract_profile_entry_id(self, entry):
        if not isinstance(entry, dict):
            return ""

        for key in ["user_id", "id", "profile_id", "profileId", "serial_number"]:
            value = str(entry.get(key) or "").strip()
            if value:
                return value

        return ""

    def _extract_exact_profile_entry(self, payload, profile_id):
        normalized_profile_id = str(profile_id or "").strip()
        if not normalized_profile_id:
            return None

        def visit(node):
            if isinstance(node, dict):
                entry_id = self._extract_profile_entry_id(node)
                if entry_id == normalized_profile_id:
                    return node

                for key in ["data", "list", "items"]:
                    nested = node.get(key)
                    found = visit(nested)
                    if found:
                        return found

            elif isinstance(node, list):
                for item in node:
                    found = visit(item)
                    if found:
                        return found

            return None

        return visit(payload)

    def _find_profile_entry(self, profile_id, page_size=200, max_scan_pages=5):
        normalized_profile_id = str(profile_id or "").strip()
        if not normalized_profile_id:
            return None

        for params in [
            {"user_id": normalized_profile_id, "page": 1, "page_size": 1},
            {"id": normalized_profile_id, "page": 1, "page_size": 1},
        ]:
            try:
                payload = self._get_json("/user/list", timeout=15, **params)
            except Exception:
                continue

            entry = self._extract_exact_profile_entry(payload, normalized_profile_id)
            if entry:
                return entry

        total_pages = max(1, max_scan_pages)
        for page in range(1, total_pages + 1):
            try:
                payload = self._get_json("/user/list", timeout=20, page=page, page_size=page_size)
            except Exception:
                continue

            entry = self._extract_exact_profile_entry(payload, normalized_profile_id)
            if entry:
                return entry

            payload_data = payload.get("data", {}) if isinstance(payload, dict) else {}
            total_value = payload_data.get("total") if isinstance(payload_data, dict) else 0
            try:
                total_items = int(total_value or 0)
            except Exception:
                total_items = 0

            if total_items:
                total_pages = min(max_scan_pages, max(1, (total_items + page_size - 1) // page_size))

        return None

    def get_profile_tags(self, profile_id):

        # /user/info does not exist on most AdsPower versions — only use list/detail
        entry = self._find_profile_entry(profile_id)
        if entry:
            tags = self._extract_tags_from_payload(entry)
            if tags:
                logger.info(f"AdsPower tags for {profile_id}: {tags}")
            return tags

        last_error = None

        for path, params in [
            ("/user/detail", {"user_id": profile_id}),
            ("/user/detail", {"id": profile_id}),
        ]:
            try:
                payload = self._get_json(path, **params)
                entry = self._extract_exact_profile_entry(payload, profile_id) or payload
                tags = self._extract_tags_from_payload(entry)
                if tags:
                    logger.info(f"AdsPower tags for {profile_id}: {tags}")
                    return tags
            except Exception as exc:
                last_error = exc

        if last_error:
            logger.warning(f"Could not fetch AdsPower tags for {profile_id}: {last_error}")

        return []

    def confirm_profile_tags(self, profile_id, expected_tags, attempts=2, delay_seconds=1.5):
        normalized_profile_id = str(profile_id or "").strip()
        normalized_expected = [str(tag).strip() for tag in (expected_tags or []) if str(tag).strip()]

        if not normalized_expected:
            return {
                "profile_id": normalized_profile_id,
                "tags": [],
                "current_tags": [],
                "confirmed": True,
                "message": "No tags requested.",
            }

        last_tags = []
        last_error = None

        for attempt in range(max(1, attempts)):
            if attempt:
                time.sleep(delay_seconds)

            try:
                last_tags = self.get_profile_tags(normalized_profile_id)
            except Exception as exc:
                last_error = exc
                continue

            if self._tags_match(last_tags, normalized_expected):
                return {
                    "profile_id": normalized_profile_id,
                    "tags": normalized_expected,
                    "current_tags": last_tags,
                    "confirmed": True,
                    "message": "Tags were confirmed from the AdsPower create response/readback.",
                }

        detail = f" Last readback tags: {last_tags}." if last_tags else ""
        if last_error and not last_tags:
            detail = f" Last readback error: {last_error}."

        return {
            "profile_id": normalized_profile_id,
            "tags": normalized_expected,
            "current_tags": last_tags,
            "confirmed": False,
            "message": (
                "AdsPower created the profile but did not confirm the requested tags during creation."
                " No post-create tag sync was attempted."
            ) + detail,
        }

    def _tags_match(self, current_tags, expected_tags):
        normalized_current = {str(tag or "").strip().lower() for tag in current_tags or [] if str(tag or "").strip()}
        normalized_expected = {str(tag or "").strip().lower() for tag in expected_tags or [] if str(tag or "").strip()}

        if not normalized_expected:
            return True

        return normalized_expected.issubset(normalized_current)

    def _build_tag_objects(self, tags):
        return [{"name": str(tag).strip()} for tag in tags if str(tag).strip()]

    def _build_tag_payload_variants(self, tags, include_empty=False):
        normalized_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
        variants = []
        if include_empty:
            variants.append({})
        if not normalized_tags:
            return variants

        tag_objects = self._build_tag_objects(normalized_tags)
        tag_name_objects = [{"tag_name": tag} for tag in normalized_tags]
        comma_tags = ",".join(normalized_tags)

        for payload in [
            {"user_tags": normalized_tags},
            {"tags": normalized_tags},
            {"profile_tags": normalized_tags},
            {"tag": normalized_tags},
            {"user_tags": comma_tags},
            {"tags": comma_tags},
            {"profile_tags": comma_tags},
            {"tag": comma_tags},
            {"fbcc_user_tag": tag_objects},
            {"fbcc_user_tag": tag_name_objects},
        ]:
            key = tuple(sorted((name, str(value)) for name, value in payload.items()))
            if key not in {tuple(sorted((name, str(value)) for name, value in existing.items())) for existing in variants}:
                variants.append(payload)

        return variants

    def set_profile_tags(self, profile_id, tags, current_name=""):

        normalized_profile_id = str(profile_id or "").strip()
        if not normalized_profile_id:
            raise ValueError("AdsPower profile id is required.")

        normalized_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
        if not normalized_tags:
            return {
                "profile_id": normalized_profile_id,
                "tags": [],
                "supported": True,
                "message": "No tags to set.",
                "raw": None,
            }

        current_tags = []
        try:
            current_tags = self.get_profile_tags(normalized_profile_id)
        except Exception as exc:
            logger.debug(f"Could not read current AdsPower tags for {normalized_profile_id}: {exc}")

        if self._tags_match(current_tags, normalized_tags):
            return {
                "profile_id": normalized_profile_id,
                "tags": normalized_tags,
                "current_tags": current_tags,
                "supported": True,
                "confirmed": True,
                "message": "Tags already present.",
                "raw": None,
            }

        resolved_tag_ids = []
        try:
            resolved_tag_ids = self.resolve_tag_ids(normalized_tags)
        except Exception as exc:
            logger.debug(f"Could not resolve AdsPower tag IDs for update ({normalized_profile_id}): {exc}")

        resolved_name = str(current_name or "").strip()
        if not resolved_name:
            try:
                resolved_name = self.get_profile_name(normalized_profile_id) or ""
            except Exception as exc:
                logger.debug(f"Could not pre-fetch profile name for tag update ({normalized_profile_id}): {exc}")

        update_attempts = []
        if resolved_tag_ids:
            update_attempts.append(
                {
                    "path": "/api/v2/browser-profile/update",
                    "payload": {"profile_id": normalized_profile_id, "profile_tag_ids": resolved_tag_ids},
                    "tag_key": "profile_tag_ids",
                    "retries": 4,
                    "delay_seconds": 1.5,
                }
            )
            update_attempts.append(
                {
                    "path": "/user/update",
                    "payload": {
                        "user_id": normalized_profile_id,
                        "name": resolved_name,
                        "profile_tag_ids": resolved_tag_ids,
                    },
                    "tag_key": "profile_tag_ids",
                    "retries": 1,
                    "delay_seconds": 1.0,
                }
            )

        for tag_payload in self._build_tag_payload_variants(normalized_tags):
            base = {"user_id": normalized_profile_id, "name": resolved_name}
            base.update(tag_payload)
            tag_key = next(iter(tag_payload.keys()), "tag_names")
            update_attempts.append(
                {
                    "path": "/user/update",
                    "payload": base,
                    "tag_key": tag_key,
                    "retries": 1,
                    "delay_seconds": 1.0,
                }
            )

        last_error = None
        last_raw = None
        last_readback = current_tags
        attempted_mode = "unknown"

        for attempt in update_attempts:
            path = attempt["path"]
            payload = attempt["payload"]
            tag_key = attempt["tag_key"]
            retries = max(1, int(attempt.get("retries") or 1))
            delay_seconds = max(0.2, float(attempt.get("delay_seconds") or 1.0))

            for retry_index in range(1, retries + 1):
                try:
                    attempted_mode = f"{path}:{tag_key}"
                    response = self._post_json(path, payload=payload, timeout=20)
                    last_raw = response
                    time.sleep(delay_seconds)

                    readback_tags = self.get_profile_tags(normalized_profile_id)
                    last_readback = readback_tags
                    if self._tags_match(readback_tags, normalized_tags):
                        logger.info(
                            f"AdsPower profile tags set: {normalized_profile_id} -> "
                            f"{normalized_tags} (field={tag_key!r}, "
                            f"path={path!r}, attempt={retry_index}/{retries})"
                        )
                        return {
                            "profile_id": normalized_profile_id,
                            "tags": normalized_tags,
                            "current_tags": readback_tags,
                            "supported": True,
                            "confirmed": True,
                            "message": "Tags set successfully.",
                            "raw": response,
                        }

                    logger.debug(
                        f"Tag update returned success without confirmed readback "
                        f"(path={path}, field={tag_key}, attempt={retry_index}/{retries}, "
                        f"raw={response}, readback={readback_tags})"
                    )
                except Exception as exc:
                    last_error = exc
                    logger.debug(
                        f"Tag update attempt failed "
                        f"(path={path}, field={tag_key}, attempt={retry_index}/{retries}): {exc}"
                    )

        if last_raw is not None:
            logger.warning(
                f"AdsPower accepted a tag update request for {normalized_profile_id}, "
                f"but readback did not confirm the tags. "
                f"last_mode={attempted_mode!r}, tags={normalized_tags}, readback={last_readback}"
            )
            return {
                "profile_id": normalized_profile_id,
                "tags": normalized_tags,
                "current_tags": last_readback,
                "supported": True,
                "confirmed": False,
                "message": "Tags update request was accepted, but readback did not confirm the tags.",
                "raw": last_raw,
            }

        logger.warning(
            f"set_profile_tags: no format applied tags for {normalized_profile_id}. "
            f"last_raw={last_raw} | last_readback={last_readback} | last_error={last_error}"
        )
        return {
            "profile_id": normalized_profile_id,
            "tags": normalized_tags,
            "current_tags": last_readback,
            "supported": False,
            "confirmed": False,
            "message": (
                "AdsPower local API accepted the update request but did not persist the requested profile tags."
            ),
            "raw": last_raw,
        }

    def list_groups(self, page_size=200, max_pages=20):

        endpoints = [
            "/group/list",
            "/user-group/list",
        ]

        last_error = None
        for path in endpoints:
            try:
                groups = []
                seen = set()
                saw_any_page = False

                for page in range(1, max_pages + 1):
                    payload = self._get_json(path, timeout=15, page=page, page_size=page_size)
                    page_groups = self._extract_groups_from_payload(payload)

                    if page_groups:
                        saw_any_page = True

                    for group in page_groups:
                        group_id = str(group.get("group_id") or "").strip()
                        group_name = str(group.get("group_name") or "").strip().lower()
                        if not group_id or not group_name:
                            continue
                        key = (group_id, group_name)
                        if key in seen:
                            continue
                        seen.add(key)
                        groups.append(group)

                    if saw_any_page and not page_groups:
                        break

                    data = payload.get("data", {}) if isinstance(payload, dict) else {}
                    total = 0
                    if isinstance(data, dict):
                        try:
                            total = int(data.get("total") or data.get("count") or 0)
                        except Exception:
                            total = 0

                    if total and len(groups) >= total:
                        break

                    if page_groups and len(page_groups) < page_size:
                        break

                if groups:
                    return groups
            except Exception as e:
                last_error = e

        if last_error is not None:
            # A permission-gated (9110) group fetch must NOT be flattened to "no
            # groups" — otherwise resolve_group_id raises a misleading "group not
            # found" and create_profile never falls back to the GUI. Propagate it
            # so the no-API GUI create path takes over.
            if isinstance(last_error, AdsPowerPermissionError):
                raise last_error
            logger.warning(f"Could not fetch AdsPower groups: {last_error}")

        return []

    def list_extension_categories(self):

        endpoints = [
            "/application/list",
            "/api/v1/application/list",
        ]

        last_error = None
        for path in endpoints:
            try:
                payload = self._get_json(path.replace("/api/v1", ""), timeout=15, page=1, page_size=200)
                categories = self._extract_extension_categories_from_payload(payload)
                if categories:
                    return categories
            except Exception as e:
                last_error = e

        if last_error is not None:
            # Same as list_groups: a 9110 must propagate so create_profile falls
            # back to the GUI instead of failing with a misleading lookup error.
            if isinstance(last_error, AdsPowerPermissionError):
                raise last_error
            logger.warning(f"Could not fetch AdsPower extension categories: {last_error}")

        return []

    def list_browser_tags(self, page_size=200):
        tags = []
        seen = set()
        page = 1
        total_items = 0
        last_error = None

        while True:
            payload = {
                "page": page,
                "page_size": page_size,
            }
            try:
                data = self._post_json("/api/v2/browser-tags/list", payload=payload, timeout=20)
            except requests_exceptions.HTTPError as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code == 404:
                    logger.warning(
                        "AdsPower browser tags API is unavailable on this AdsPower version; "
                        "shared tag IDs will be used for standard tags when available."
                    )
                    return []
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                break

            payload_data = data.get("data", {}) if isinstance(data, dict) else {}
            entries = payload_data.get("list", []) if isinstance(payload_data, dict) else []

            if not isinstance(entries, list) or not entries:
                break

            try:
                total_items = int(payload_data.get("total") or total_items or 0)
            except Exception:
                total_items = total_items or 0

            for entry in entries:
                if not isinstance(entry, dict):
                    continue

                tag_id = str(entry.get("id") or entry.get("tag_id") or "").strip()
                tag_name = str(entry.get("name") or entry.get("tag_name") or "").strip()
                if not tag_id or not tag_name or tag_id in seen:
                    continue

                seen.add(tag_id)
                tags.append({
                    "tag_id": tag_id,
                    "tag_name": tag_name,
                    "color": str(entry.get("color") or "").strip(),
                })

            if total_items and len(tags) >= total_items:
                break

            if len(entries) < page_size:
                break

            page += 1

        if last_error:
            logger.warning(f"Could not fetch AdsPower browser tags: {last_error}")

        return tags

    def resolve_tag_ids(self, tag_references):

        normalized_references = []
        seen = set()
        for reference in tag_references or []:
            value = str(reference or "").strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized_references.append(value)

        if not normalized_references:
            return []

        shared_resolved_ids = []
        unresolved_references = []
        for reference in normalized_references:
            if reference.isdigit():
                shared_resolved_ids.append(reference)
                continue

            tag_id = SHARED_BROWSER_TAG_IDS.get(reference.strip().lower())
            if tag_id:
                shared_resolved_ids.append(tag_id)
                continue

            unresolved_references.append(reference)

        if not unresolved_references:
            logger.info(
                f"Using shared AdsPower tag IDs for tags {normalized_references}: {shared_resolved_ids}"
            )
            return shared_resolved_ids

        try:
            tag_catalog = self.list_browser_tags(page_size=200)
        except Exception as exc:
            tag_catalog = []
            logger.warning(f"AdsPower browser tags API is unavailable, trying shared tag IDs: {exc}")

        if not tag_catalog:
            resolved_ids = list(shared_resolved_ids)
            missing = []
            for reference in unresolved_references:
                if reference.isdigit():
                    resolved_ids.append(reference)
                    continue

                tag_id = SHARED_BROWSER_TAG_IDS.get(reference.strip().lower())
                if tag_id:
                    resolved_ids.append(tag_id)
                    continue

                missing.append(reference)

            if missing:
                raise ValueError(
                    "AdsPower browser tags list is empty or unavailable, and no shared tag ID is configured for: "
                    + ", ".join(missing)
                )

            logger.info(
                f"Using shared AdsPower tag IDs for tags {normalized_references}: {resolved_ids}"
            )
            return resolved_ids

        grouped_by_name = {}
        for tag in tag_catalog:
            tag_name = str(tag.get("tag_name") or "").strip().lower()
            tag_id = str(tag.get("tag_id") or "").strip()
            if not tag_name or not tag_id:
                continue
            grouped_by_name.setdefault(tag_name, []).append(tag_id)

        resolved_ids = list(shared_resolved_ids)
        missing = []
        for reference in unresolved_references:
            if reference.isdigit():
                resolved_ids.append(reference)
                continue

            candidate_ids = grouped_by_name.get(reference.lower(), [])
            if not candidate_ids:
                missing.append(reference)
                continue

            resolved_ids.append(
                max(candidate_ids, key=lambda value: int(value) if value.isdigit() else -1)
            )

        if missing:
            raise ValueError(f"AdsPower tag(s) not found: {', '.join(missing)}")

        return resolved_ids

    def _extract_extension_categories_from_payload(self, payload):

        categories = []

        def visit(node):
            if isinstance(node, dict):
                category_id = str(node.get("id") or node.get("category_id") or "").strip()
                category_name = str(node.get("name") or node.get("category_name") or "").strip()

                if category_id and category_name:
                    categories.append({
                        "category_id": category_id,
                        "category_name": category_name,
                    })

                for value in node.values():
                    visit(value)

            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(payload)

        unique = []
        seen = set()
        for category in categories:
            key = (category["category_id"], category["category_name"].lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(category)

        return unique

    def resolve_extension_category_id(self, category_reference):

        reference = str(category_reference or "").strip()
        if not reference:
            return ""

        if reference.isdigit():
            return reference

        normalized_reference = reference.lower()
        for category in self.list_extension_categories():
            if str(category.get("category_name", "")).strip().lower() == normalized_reference:
                return str(category.get("category_id", "")).strip()

        raise ValueError(f"AdsPower extension category not found: {reference}")

    def _extract_groups_from_payload(self, payload):

        groups = []

        def visit(node):
            if isinstance(node, dict):
                group_id = str(
                    node.get("group_id")
                    or node.get("groupId")
                    or node.get("id")
                    or ""
                ).strip()
                group_name = str(
                    node.get("group_name")
                    or node.get("groupName")
                    or node.get("name")
                    or ""
                ).strip()

                if group_id and group_name:
                    groups.append({
                        "group_id": group_id,
                        "group_name": group_name,
                    })

                for value in node.values():
                    visit(value)

            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(payload)

        unique = []
        seen = set()
        for group in groups:
            key = (group["group_id"], group["group_name"].lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(group)

        return unique

    def resolve_group_id(self, group_reference):

        reference = str(group_reference or "").strip()
        if not reference:
            return ""

        if reference.isdigit():
            return reference

        normalized_reference = reference.lower()
        groups = self.list_groups()
        for group in groups:
            if str(group.get("group_name", "")).strip().lower() == normalized_reference:
                return str(group.get("group_id", "")).strip()

        available_groups = ", ".join(
            sorted(
                {
                    str(group.get("group_name") or "").strip()
                    for group in groups
                    if str(group.get("group_name") or "").strip()
                }
            )
        )
        if available_groups:
            raise ValueError(f"AdsPower group not found: {reference}. Available groups: {available_groups}")

        raise ValueError(f"AdsPower group not found: {reference}")

    def _extract_created_profile_id(self, payload):

        def visit(node):
            if isinstance(node, dict):
                for key in [
                    "user_id",
                    "userId",
                    "id",
                    "profile_id",
                    "profileId",
                    "serial_number",
                ]:
                    value = str(node.get(key) or "").strip()
                    if value:
                        return value

                for value in node.values():
                    nested = visit(value)
                    if nested:
                        return nested

            elif isinstance(node, list):
                for item in node:
                    nested = visit(item)
                    if nested:
                        return nested

            return ""

        profile_id = visit(payload)
        if not profile_id:
            raise KeyError("AdsPower create response did not include a profile id.")
        return profile_id

    def parse_proxy(self, proxy_value, default_type="socks5"):

        raw_proxy = self.sanitize_proxy_value(proxy_value)
        if not raw_proxy:
            raise ValueError("Proxy value is required.")

        proxy_type = default_type
        working = raw_proxy

        if "://" in working:
            proxy_type, working = working.split("://", 1)
            proxy_type = proxy_type.strip().lower() or default_type

        proxy_user = ""
        proxy_password = ""
        host_port = working

        if "@" in host_port:
            credentials, host_port = host_port.rsplit("@", 1)
            if ":" in credentials:
                proxy_user, proxy_password = credentials.split(":", 1)
            else:
                proxy_user = credentials

        parts = [segment.strip() for segment in host_port.split(":")]
        if len(parts) == 4 and not proxy_user and not proxy_password:
            host, port, proxy_user, proxy_password = parts
        elif len(parts) >= 2:
            host = parts[0]
            port = parts[1]
        else:
            raise ValueError("Proxy must include host and port.")

        host = str(host or "").strip()
        port = str(port or "").strip()
        if not host or not port.isdigit():
            raise ValueError("Proxy host/port is invalid.")

        normalized_type = proxy_type.lower()
        if normalized_type not in {"http", "https", "socks5", "socks4"}:
            normalized_type = default_type

        return {
            "proxy_soft": "other",
            "proxy_type": normalized_type,
            "proxy_host": host,
            "proxy_port": port,
            "proxy_user": str(proxy_user or "").strip(),
            "proxy_password": str(proxy_password or "").strip(),
            "raw_proxy": raw_proxy,
        }

    def test_proxy_connection(self, proxy_value, timeout=8):

        proxy = self.parse_proxy(proxy_value)
        host = proxy["proxy_host"]
        port = int(proxy["proxy_port"])

        try:
            with socket.create_connection((host, port), timeout=timeout):
                return {
                    "ok": True,
                    "message": "Proxy host is reachable.",
                    "proxy": proxy,
                }
        except Exception as exc:
            return {
                "ok": False,
                "message": f"Proxy check failed: {exc}",
                "proxy": proxy,
            }

    def check_proxy_via_adspower(self, proxy_value_or_profile_id, timeout=20, allow_socket_fallback=False):
        """
        Uses AdsPower's own proxy checker (like the 'Check Proxy' button in Edit Proxy dialog).
        Returns {"ok": True/False, "message": "...", "ip": "...", "country": "...", "city": "...", "proxy": {...}}
        Accepts either a raw proxy string (host:port:user:pass) or an existing AdsPower profile ID.
        """
        value = str(proxy_value_or_profile_id or "").strip()
        if not value:
            return {"ok": False, "message": "No proxy value provided."}

        is_profile_id = not any(c in value for c in [":", ".", "@"])
        profile_id = value if is_profile_id else None

        check_errors = []

        if profile_id:
            for path in [
                "/api/v1/proxy/check",
                "/api/v1/browser/proxy/check",
            ]:
                try:
                    data = self._get_json(path, timeout=timeout, user_id=profile_id)
                    return self._parse_proxy_check_response(data)
                except Exception as exc:
                    check_errors.append(str(exc))
                    continue
            # A permission-gated (9110) checker is, for our purposes, "unavailable"
            # (the raw-proxy branch below then falls through to the socket test).
            checker_unavailable = bool(check_errors) and all(
                _is_proxy_checker_unavailable_error(error) or _is_permission_error(error)
                for error in check_errors
            )
            return {
                "ok": False,
                "message": f"AdsPower proxy check failed for profile {profile_id}.",
                "checker_unavailable": checker_unavailable,
            }

        try:
            proxy = self.parse_proxy(value)
        except Exception as exc:
            return {"ok": False, "message": f"Invalid proxy: {exc}"}

        for path in ["/api/v1/proxy/check", "/api/v1/browser/proxy/check"]:
            try:
                data = self._post_json(path, payload={
                    "proxy_type": proxy["proxy_type"],
                    "proxy_host": proxy["proxy_host"],
                    "proxy_port": str(proxy["proxy_port"]),
                    "proxy_user": proxy.get("proxy_user", ""),
                    "proxy_password": proxy.get("proxy_password", ""),
                }, timeout=timeout)
                return self._parse_proxy_check_response(data, proxy=proxy)
            except Exception as exc:
                check_errors.append(str(exc))
                continue

        # Treat a permission-gated (9110) checker as unavailable so no-API mode
        # validates the proxy with the socket test instead of hard-failing the
        # whole rotation loop. (AdsPower's own 'Check Proxy' button still runs
        # during GUI create, so the proxy is validated for real there too.)
        checker_unavailable = bool(check_errors) and all(
            _is_proxy_checker_unavailable_error(error) or _is_permission_error(error)
            for error in check_errors
        )
        if allow_socket_fallback and checker_unavailable:
            fallback = self.test_proxy_connection(value)
            fallback["checker_unavailable"] = True
            fallback["fallback"] = "socket"
            fallback["message"] = (
                "AdsPower proxy checker is unavailable; "
                f"socket fallback result: {fallback.get('message') or ''}"
            ).strip()
            return fallback

        detail = "; ".join(error for error in check_errors if error)
        if checker_unavailable:
            message = "AdsPower proxy checker is unavailable."
        else:
            message = "AdsPower proxy check did not return a passing connection result."
        return {
            "ok": False,
            "message": message + (f" {detail}" if detail else ""),
            "checker_unavailable": checker_unavailable,
            "proxy": proxy,
        }

    def _parse_proxy_check_response(self, data, proxy=None):
        if not isinstance(data, dict):
            return {"ok": False, "message": "Unexpected AdsPower proxy check response.", "proxy": proxy}

        code = data.get("code", 0)
        msg = str(data.get("msg") or data.get("message") or "").strip()
        inner = data.get("data") or {}
        if not isinstance(inner, dict):
            inner = {}

        ip = str(inner.get("ip") or inner.get("proxy_ip") or "").strip()
        country = str(inner.get("country") or inner.get("region") or "").strip()
        city = str(inner.get("city") or "").strip()

        status_values = [
            inner.get("connect"),
            inner.get("status"),
            inner.get("success"),
            data.get("status"),
            data.get("success"),
        ]
        status_text = " ".join(str(value or "").strip().lower() for value in status_values)
        message_text = " ".join(
            str(value or "").strip().lower()
            for value in [
                msg,
                inner.get("msg"),
                inner.get("message"),
                inner.get("error"),
            ]
        )
        failure_markers = [
            "connection test failed",
            "connect test failed",
            "test failed",
            "proxy check failed",
            "connect failed",
            "failed",
            "failure",
            "error",
            "timeout",
            "refused",
            "unreachable",
            "invalid",
            "not connected",
        ]
        if any(marker in message_text for marker in failure_markers):
            failure_message = (
                inner.get("message")
                or inner.get("msg")
                or inner.get("error")
                or msg
                or "Proxy check failed."
            )
            return {
                "ok": False,
                "message": failure_message,
                "proxy": proxy,
            }

        success_markers = ["ok", "success", "true", "pass", "passed"]
        success_by_status = any(marker == status_text or marker in status_text.split() for marker in success_markers)
        success_by_message = any(
            marker in message_text
            for marker in [
                "connection test passed",
                "connect test passed",
                "proxy check passed",
                "test passed",
            ]
        )
        if success_by_status or success_by_message or ip:
            return {
                "ok": True,
                "message": f"Connection test passed! IP:{ip} Country:{country} City:{city}".strip(),
                "ip": ip,
                "country": country,
                "city": city,
                "proxy": proxy,
            }

        return {
            "ok": False,
            "message": msg or "Proxy check did not include a positive connection result.",
            "proxy": proxy,
        }

    def find_profile_id_by_proxy_host(self, proxy_host, page_size=100, max_pages=5):
        normalized_host = str(proxy_host or "").strip().lower()
        if not normalized_host:
            return None

        for page in range(1, max_pages + 1):
            try:
                payload = self._get_json(
                    "/user/list",
                    timeout=20,
                    page=page,
                    page_size=page_size,
                    ipAddress=normalized_host,
                )
            except Exception:
                try:
                    payload = self._get_json(
                        "/user/list",
                        timeout=20,
                        page=page,
                        page_size=page_size,
                        ip=normalized_host,
                    )
                except Exception:
                    break

            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            items = []
            if isinstance(data, dict):
                items = data.get("list", []) or []
            elif isinstance(data, list):
                items = data

            for item in items:
                if not isinstance(item, dict):
                    continue
                proxy_cfg = item.get("user_proxy_config") or {}
                if isinstance(proxy_cfg, dict):
                    item_host = str(proxy_cfg.get("proxy_host") or "").strip().lower()
                    if item_host == normalized_host:
                        profile_id = self._extract_profile_entry_id(item)
                        if profile_id:
                            return profile_id

            total = 0
            if isinstance(data, dict):
                try:
                    total = int(data.get("total") or 0)
                except Exception:
                    pass
            if total and page * page_size >= total:
                break

        return None

    def rotate_proxy_for_profile(self, profile_id):
        normalized_id = str(profile_id or "").strip()
        if not normalized_id:
            return None

        for path in [
            f"/api/v1/proxy/rotate?user_id={normalized_id}",
            f"/api/v1/user/proxy/change?user_id={normalized_id}",
        ]:
            try:
                self._get_json(path, timeout=15)
                break
            except Exception:
                continue

        time.sleep(1.5)

        entry = self._find_profile_entry(normalized_id)
        if not entry:
            return None

        proxy_cfg = entry.get("user_proxy_config") or {}
        if not isinstance(proxy_cfg, dict):
            return None

        host = str(proxy_cfg.get("proxy_host") or "").strip()
        port = str(proxy_cfg.get("proxy_port") or "").strip()
        user = str(proxy_cfg.get("proxy_user") or "").strip()
        password = str(proxy_cfg.get("proxy_password") or "").strip()

        if not host or not port:
            return None

        if user and password:
            return f"{host}:{port}:{user}:{password}"
        return f"{host}:{port}"

    def create_profile(
        self,
        name,
        proxy_value,
        group_reference="",
        tags=None,
        user_proxy_config=None,
        extension_category_reference="",
        proxy_rotator=None,
    ):
        """Create a profile via the Local API. If the API is permission-gated
        (9110) or unreachable (Local API not running) and the GUI fallback is
        enabled, drive the AdsPower desktop app instead (no-API mode) — same
        return shape so callers are unaffected."""
        if self._is_gui_control_mode():
            return self._create_profile_via_ui(
                name,
                proxy_value,
                group_reference,
                user_proxy_config,
                AdsPowerPermissionError("GUI control mode selected; Local API create skipped."),
                tags=tags,
                extension_category_reference=extension_category_reference,
                proxy_rotator=proxy_rotator,
            )
        try:
            return self._create_profile_via_api(
                name, proxy_value, group_reference, tags,
                user_proxy_config, extension_category_reference,
            )
        except AdsPowerError as api_error:
            if not self.ui_fallback_enabled:
                raise
            return self._create_profile_via_ui(
                name, proxy_value, group_reference, user_proxy_config, api_error,
                tags=tags,
                extension_category_reference=extension_category_reference,
                proxy_rotator=proxy_rotator,
            )

    def create_and_open_profile_transaction(
        self,
        name,
        proxy_value,
        group_reference="",
        tags=None,
        user_proxy_config=None,
        extension_category_reference="",
        proxy_rotator=None,
    ):
        """Create and launch one profile without yielding AdsPower GUI control.

        The global GUI lock stays held through the CDP endpoint confirmation.
        This lets signup automation run in parallel after launch, while a second
        task cannot alter the AdsPower dashboard during this transaction.
        """
        if not self._is_gui_control_mode():
            return self.create_profile(
                name, proxy_value, group_reference, tags, user_proxy_config,
                extension_category_reference, proxy_rotator,
            ), ""

        from core.adspower_ui import _GUI_LOCK

        controller = self._ui_controller()
        with _GUI_LOCK:
            # Keep the saved foreground window until the browser/CDP endpoint is
            # confirmed, then restore it once. Nested GUI calls are reentrant.
            controller._a11y_enter()
            try:
                created = self.create_profile(
                    name, proxy_value, group_reference, tags, user_proxy_config,
                    extension_category_reference, proxy_rotator,
                )
                profile_id = str(created.get("profile_id") or "").strip()
                if not profile_id:
                    raise AdsPowerError("AdsPower create did not return a profile id.")
                endpoint = self.open_profile(profile_id)
                logger.info(
                    "AdsPower GUI launch transaction confirmed browser for "
                    f"{profile_id}; releasing dashboard control."
                )
                return created, endpoint
            finally:
                controller._a11y_exit()

    def _create_profile_via_ui(self, name, proxy_value, group_reference,
                               user_proxy_config, api_error, tags=None,
                               extension_category_reference="",
                               proxy_rotator=None):
        """No-API profile creation through the AdsPower GUI.

        Sets the functional fields the runners depend on — name, group and proxy.
        AdsPower *tags* and *extension category* are intentionally skipped: they
        are organisational metadata only (the Bitmoji runner drives the page via
        Playwright and never loads the profile's extensions; Nyx picks tasks from
        its own queue by profile id, not by AdsPower category/tag), so omitting
        them keeps the critical create path simple without affecting either flow.
        """
        resolved_name = str(name or "").strip()
        proxy_str = self._proxy_to_ui_string(proxy_value, user_proxy_config)
        logger.warning(
            f"AdsPower Local API gated for create ({api_error}); creating "
            f"{resolved_name!r} via the AdsPower GUI (no-API mode)."
        )
        skipped = [label for label, value in
                   (("tags", tags), ("extension category", str(extension_category_reference or "").strip()))
                   if value]
        if skipped:
            logger.info(
                f"No-API GUI create sets name/group/proxy only; "
                f"{' and '.join(skipped)} are organisational metadata and are skipped "
                f"(not needed by the Nyx or Nyxify automation)."
            )
        info = self._ui_controller().create_profile(
            name=resolved_name,
            proxy=proxy_str,
            group=str(group_reference or "").strip(),
            proxy_rotator=proxy_rotator,
        )
        profile_id = str(info.get("profile_id") or "").strip()
        if not profile_id:
            raise AdsPowerError(
                f"Created profile {resolved_name!r} via the GUI but could not "
                f"resolve its profile id from the Profiles list."
            )
        # The GUI-created profile must be opened via the GUI too (the start API
        # is the same 9110-gated endpoint), so route open through the CDP/GUI
        # fallback path.
        self._cdp_fallback_profiles.add(profile_id)
        logger.info(f"AdsPower profile created via GUI: {profile_id} ({resolved_name}).")
        return {
            "profile_id": profile_id,
            "name": resolved_name,
            "group_id": "",
            "extension_category_id": "",
            "tags": [],
            "tag_ids": [],
            "proxy": user_proxy_config or {},
            "tag_confirmation": {"confirmed": True, "message": "Created via AdsPower GUI (no-API mode)."},
            "raw": info,
        }

    def _create_profile_via_api(
        self,
        name,
        proxy_value,
        group_reference="",
        tags=None,
        user_proxy_config=None,
        extension_category_reference="",
    ):

        resolved_name = str(name or "").strip()
        if not resolved_name:
            raise ValueError("AdsPower profile name is required.")

        proxy_config = dict(user_proxy_config or {})
        if not proxy_config:
            proxy_config = self.parse_proxy(proxy_value)

        # New WebGL GPU fingerprint per profile (vendor/renderer pair).
        _webgl_vendor, _webgl_renderer = random.choice(WEBGL_GPU_POOL)

        payload = {
            "name": resolved_name,
            "user_proxy_config": {
                "proxy_soft": str(proxy_config.get("proxy_soft") or "other"),
                "proxy_type": proxy_config["proxy_type"],
                "proxy_host": proxy_config["proxy_host"],
                "proxy_port": str(proxy_config["proxy_port"]),
                "proxy_user": str(proxy_config.get("proxy_user") or ""),
                "proxy_password": str(proxy_config.get("proxy_password") or ""),
            },
            "fingerprint_config": {
                # --- Identity / Network ---
                "webrtc": "disabled",           # Disabled (screenshot default)
                "automatic_timezone": "1",       # Based on IP
                "language": ["en-US", "en"],     # Custom: English (United States), English
                "language_switch": "0",          # 0 = use custom language array (not IP)
                "page_language": "en-US",        # Display language: Custom = English
                "page_language_switch": "0",     # 0 = use custom display language
                "location": "ask",               # Based on IP, Ask each time
                # --- Hardware noise (defaults: Canvas/WebGL Image OFF, rest ON) ---
                "canvas": "0",                   # Canvas noise: OFF
                "webgl_image": "0",             # WebGL image noise: OFF
                "audio": "1",                    # AudioContext noise: ON
                "media_device": "1",             # Media device: ON (Auto)
                "client_rects": "1",             # ClientRects noise: ON
                "speech_voices": "1",            # SpeechVoices noise: ON
                # --- Graphics ---
                "webgl": "1",                    # WebGL metadata: Custom
                "webgl_config": {
                    # Randomized per profile from WEBGL_GPU_POOL so each profile
                    # presents a different (but plausible Windows) GPU fingerprint.
                    "unmasked_vendor": _webgl_vendor,
                    "unmasked_renderer": _webgl_renderer,
                },
                "webgpu": "0",                   # WebGPU: Based on WebGL
                # --- Hardware specs ---
                "cpu_number": "random",          # CPU: Random
                "device_memory": "random",       # RAM: Random
                # --- Device identity ---
                "device_name": "ua",             # Device name: Based on User-Agent
                "mac_address_type": "ua",        # MAC Address: Based on User-Agent
                # --- Browser settings ---
                "do_not_track": "default",       # Do Not Track: Default
                "port_scan_protection": "enable", # Port scan protection: Enable
                "hardware_acceleration": "default", # Hardware acceleration: Default
                "disable_tls_features": "0",     # Disable TLS features: Close (off)
                "args": "",                      # Launch Args (blank)
                # --- UA / Kernel ---
                "random_ua": {
                    "ua_browser": ["chrome"],
                    "ua_system_version": ["Windows 10", "Windows 11"],
                },
                "browser_kernel_config": {
                    "type": "chrome",
                    "version": "ua_auto",
                },
            },
        }

        group_id = self.resolve_group_id(group_reference) if str(group_reference or "").strip() else ""
        if group_id:
            payload["group_id"] = group_id

        extension_category_id = (
            self.resolve_extension_category_id(extension_category_reference)
            if str(extension_category_reference or "").strip()
            else ""
        )
        if extension_category_id:
            payload["category_id"] = extension_category_id

        normalized_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
        resolved_tag_ids = []
        tag_resolution_error = ""
        if normalized_tags:
            try:
                resolved_tag_ids = self.resolve_tag_ids(normalized_tags)
            except Exception as exc:
                tag_resolution_error = str(exc)
                logger.warning(
                    "AdsPower profile tags could not be resolved; creating profile without "
                    f"create-time tags. tags={normalized_tags}, error={tag_resolution_error}"
                )

        create_attempts = []
        if resolved_tag_ids:
            tagged_payload = dict(payload)
            tagged_payload["profile_tag_ids"] = resolved_tag_ids
            create_attempts.append(("profile_tag_ids", tagged_payload))
        elif normalized_tags:
            for tag_payload in self._build_tag_payload_variants(normalized_tags):
                tagged_payload = dict(payload)
                tagged_payload.update(tag_payload)
                tag_key = next(iter(tag_payload.keys()), "tag_names")
                create_attempts.append((tag_key, tagged_payload))
        create_attempts.append(("without_tags", payload))

        data = None
        create_tag_mode = "without_tags"
        create_last_error = None
        for create_tag_mode, create_payload in create_attempts:
            try:
                data = self._post_json("/api/v2/browser-profile/create", payload=create_payload, timeout=30)
                if normalized_tags and create_tag_mode != "profile_tag_ids":
                    logger.info(
                        f"AdsPower profile create accepted tag payload mode={create_tag_mode!r} "
                        f"for tags={normalized_tags}"
                    )
                break
            except Exception as exc:
                create_last_error = exc
                if create_tag_mode == "without_tags":
                    raise
                logger.warning(
                    f"AdsPower profile create rejected tag payload mode={create_tag_mode!r}; "
                    f"retrying with another tag format. error={exc}"
                )

        if data is None:
            raise RuntimeError(f"AdsPower profile create failed: {create_last_error}")

        profile_id = self._extract_created_profile_id(data)

        tag_confirmation = self.confirm_profile_tags(profile_id, normalized_tags)
        if normalized_tags and not tag_confirmation.get("confirmed"):
            update_result = self.set_profile_tags(profile_id, normalized_tags, current_name=resolved_name)
            reason = f"Tag ID lookup failed ({tag_resolution_error}); " if tag_resolution_error else ""
            tag_confirmation = {
                "profile_id": profile_id,
                "tags": normalized_tags,
                "current_tags": update_result.get("current_tags", []),
                "confirmed": bool(update_result.get("confirmed")),
                "supported": update_result.get("supported", False),
                "message": (
                    f"{reason}create mode={create_tag_mode}; "
                    f"post-create update: {update_result.get('message', '')}"
                ).strip(),
            }

        if normalized_tags and not tag_confirmation.get("confirmed"):
            logger.warning(
                f"AdsPower profile created without confirmed create-time tags for {profile_id}: "
                f"{tag_confirmation.get('message', '')}"
            )

        logger.info(
            f"AdsPower profile created: {profile_id} ({resolved_name}) "
            f"[webgl={_webgl_renderer}]"
        )

        return {
            "profile_id": profile_id,
            "name": resolved_name,
            "group_id": group_id,
            "extension_category_id": extension_category_id,
            "tags": normalized_tags,
            "tag_ids": resolved_tag_ids,
            "proxy": payload["user_proxy_config"],
            "tag_confirmation": tag_confirmation,
            "raw": data,
        }
