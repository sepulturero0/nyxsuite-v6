"""Private runtime settings controlled from the Developer Settings panel.

This file is intentionally separate from the normal Nyxify configuration, so
extension clients and standard config endpoints never receive its values.
"""

import json

from core.process_utils import APP_DATA_DIR

DATA_DIR = APP_DATA_DIR / "data"
CONFIG_PATH = DATA_DIR / "developer_settings.json"

DEFAULTS = {
    "parallel_continuous_pipeline_enabled": False,
    "parallel_continuous_slots": 2,
    "device_heartbeat_enabled": True,
    "fleet_api_url": "https://bhdovyegahohiwsmqdgr.supabase.co/functions/v1/nyxsuite-devices",
    "fleet_token": "",
    "device_name": "",
}


def _slots(value, default=2):
    try:
        return min(5, max(2, int(value)))
    except (TypeError, ValueError):
        return default


def _enabled(value, default=False):
    return default if value is None else bool(value)


def _text(value, default="", max_length=500):
    if value is None:
        return default
    return str(value).strip()[:max_length]


def load_developer_settings():
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}") if CONFIG_PATH.exists() else {}
    except Exception:
        raw = {}
    return {
        "parallel_continuous_pipeline_enabled": _enabled(
            raw.get("parallel_continuous_pipeline_enabled"),
            DEFAULTS["parallel_continuous_pipeline_enabled"],
        ),
        "parallel_continuous_slots": _slots(raw.get("parallel_continuous_slots")),
        "device_heartbeat_enabled": _enabled(
            raw.get("device_heartbeat_enabled"),
            DEFAULTS["device_heartbeat_enabled"],
        ),
        "fleet_api_url": _text(raw.get("fleet_api_url"), DEFAULTS["fleet_api_url"], 500),
        "fleet_token": _text(raw.get("fleet_token"), DEFAULTS["fleet_token"], 500),
        "device_name": _text(raw.get("device_name"), DEFAULTS["device_name"], 80),
    }


def save_developer_settings(updates):
    current = load_developer_settings()
    fleet_token = current["fleet_token"]
    if bool(updates.get("fleet_token_clear")):
        fleet_token = ""
    elif "fleet_token" in updates:
        next_token = _text(updates.get("fleet_token"), "", 500)
        if next_token:
            fleet_token = next_token
    next_config = {
        "parallel_continuous_pipeline_enabled": _enabled(
            updates.get("parallel_continuous_pipeline_enabled"),
            current["parallel_continuous_pipeline_enabled"],
        ),
        "parallel_continuous_slots": _slots(
            updates.get("parallel_continuous_slots"), current["parallel_continuous_slots"]
        ),
        "device_heartbeat_enabled": _enabled(
            updates.get("device_heartbeat_enabled"),
            current["device_heartbeat_enabled"],
        ),
        "fleet_api_url": _text(updates.get("fleet_api_url"), current["fleet_api_url"], 500),
        "fleet_token": fleet_token,
        "device_name": _text(updates.get("device_name"), current["device_name"], 80),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(next_config, indent=2), encoding="utf-8")
    return next_config


def public_developer_settings(settings=None):
    """Return Developer Settings safe to send to the dashboard."""
    data = dict(settings or load_developer_settings())
    token = str(data.pop("fleet_token", "") or "")
    data["fleet_token_configured"] = bool(token)
    return data
