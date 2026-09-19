"""Lightweight device heartbeat client for the Developer Settings fleet view."""

import json
import platform
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from core.process_utils import APP_DATA_DIR

DATA_DIR = APP_DATA_DIR / "data"
DEVICE_ID_PATH = DATA_DIR / "device_fleet_id.txt"
# Keep the fleet signal intentionally infrequent so the feature stays quiet
# while still providing a useful online/offline view in Developer Settings.
HEARTBEAT_INTERVAL_SECONDS = 5 * 60.0
ACTIVE_WINDOW_SECONDS = 11 * 60.0
HTTP_TIMEOUT_SECONDS = 3.0


def normalize_os(value=None):
    raw = str(value if value is not None else sys.platform).lower()
    if raw.startswith("win"):
        return "windows"
    if raw == "darwin" or raw.startswith("mac"):
        return "macos"
    if raw.startswith("linux"):
        return "linux"
    return "unknown"


def default_device_name():
    for candidate in (socket.gethostname(), platform.node()):
        value = str(candidate or "").strip()
        if value:
            return value[:80]
    return "NyxSuite Device"


def get_or_create_device_id():
    try:
        if DEVICE_ID_PATH.exists():
            existing = DEVICE_ID_PATH.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except Exception:
        pass
    device_id = uuid.uuid4().hex
    try:
        DEVICE_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEVICE_ID_PATH.write_text(device_id, encoding="utf-8")
    except Exception:
        pass
    return device_id


def build_heartbeat_payload(settings, version=""):
    device_name = str((settings or {}).get("device_name") or "").strip() or default_device_name()
    return {
        "device_id": get_or_create_device_id(),
        "device_name": device_name[:80],
        "os": normalize_os(),
        "app_version": str(version or "").strip()[:40],
    }


def _join_url(base_url, path):
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return base + "/" + path.lstrip("/")


def _request_json(method, base_url, path, token, payload=None):
    url = _join_url(base_url, path)
    if not url:
        return {"ok": False, "error": "Fleet API URL is not configured."}
    if not str(token or "").strip():
        return {"ok": False, "error": "Fleet token is not configured."}
    data = None
    headers = {
        "Content-Type": "application/json",
        "X-NyxSuite-Fleet-Token": str(token or "").strip(),
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8") or "{}"
            body = json.loads(raw)
        except Exception:
            body = {}
        return {
            "ok": False,
            "status": exc.code,
            "error": body.get("error") or f"Fleet API returned HTTP {exc.code}.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc) or "Fleet API request failed."}
    try:
        body = json.loads(raw)
    except Exception:
        return {"ok": False, "error": "Fleet API returned invalid JSON."}
    if not isinstance(body, dict):
        return {"ok": False, "error": "Fleet API returned an unexpected response."}
    return body


def send_heartbeat(settings, version=""):
    if not bool((settings or {}).get("device_heartbeat_enabled")):
        return {"ok": False, "skipped": True, "error": "Device heartbeat is disabled."}
    payload = build_heartbeat_payload(settings, version=version)
    return _request_json(
        "POST",
        (settings or {}).get("fleet_api_url"),
        "heartbeat",
        (settings or {}).get("fleet_token"),
        payload=payload,
    )


def fetch_devices(settings):
    result = _request_json(
        "GET",
        (settings or {}).get("fleet_api_url"),
        "devices",
        (settings or {}).get("fleet_token"),
    )
    if not result.get("ok"):
        return result
    rows = result.get("devices") or []
    if not isinstance(rows, list):
        rows = []
    return {"ok": True, "devices": [sanitize_device(row) for row in rows]}


def sanitize_device(row):
    data = row if isinstance(row, dict) else {}
    last_seen = str(data.get("last_seen") or "")
    active = _is_active(last_seen)
    return {
        "device_id": str(data.get("device_id") or "")[:80],
        "device_name": str(data.get("device_name") or "")[:80],
        "os": normalize_os(data.get("os")),
        "app_version": str(data.get("app_version") or "")[:40],
        "last_seen": last_seen,
        "active": active,
    }


def _is_active(last_seen):
    try:
        normalized = str(last_seen or "").replace("Z", "+00:00")
        seen = datetime.fromisoformat(normalized)
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc).timestamp() - seen.timestamp()
        return age <= ACTIVE_WINDOW_SECONDS
    except Exception:
        return False


def should_send(last_attempt_at):
    try:
        return (time.monotonic() - float(last_attempt_at or 0.0)) >= HEARTBEAT_INTERVAL_SECONDS
    except Exception:
        return True
