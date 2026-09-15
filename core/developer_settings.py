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
}


def _slots(value, default=2):
    try:
        return min(5, max(2, int(value)))
    except (TypeError, ValueError):
        return default


def _enabled(value, default=False):
    return default if value is None else bool(value)


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
    }


def save_developer_settings(updates):
    current = load_developer_settings()
    next_config = {
        "parallel_continuous_pipeline_enabled": _enabled(
            updates.get("parallel_continuous_pipeline_enabled"),
            current["parallel_continuous_pipeline_enabled"],
        ),
        "parallel_continuous_slots": _slots(
            updates.get("parallel_continuous_slots"), current["parallel_continuous_slots"]
        ),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(next_config, indent=2), encoding="utf-8")
    return next_config
