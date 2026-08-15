import json

from core.process_utils import APP_DATA_DIR

DATA_DIR = APP_DATA_DIR / "data"
CONFIG_PATH = DATA_DIR / "nyx_config.json"
DEFAULTS = {
    "pending_threshold": 10,
    "max_parallel_profiles": 5,
    "ignore_done_profiles": True,
    "outfit_style": "default",
    "outfit_custom_preset_id": "",
    "automation_speed": 1.0,
    "hair_randomizer_enabled": False,
    "launch_on_windows_startup": False,
    "hubstaff_control_enabled": False,
    "hubstaff_stop_mode": "queue_finished",
    "hubstaff_timer_minutes": 60,
    "hubstaff_cli_path": "",
    # AdsPower Local API credentials/overrides. Empty = fall back to
    # ADSPOWER_*/ADSP_* env vars and defaults (host 127.0.0.1, port 50325).
    "adspower_control_mode": "auto",
    "adspower_api_key": "",
    "adspower_host": "",
    "adspower_port": "",
}

VALID_OUTFIT_STYLES = {"default", "mix", "casual", "sexy", "custom", "no_dresses", "cute_preset"}
VALID_HUBSTAFF_STOP_MODES = {"queue_finished", "timer"}
VALID_ADSPOWER_CONTROL_MODES = {"auto", "api", "gui"}


def _safe_int(value, default):
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _safe_outfit_style(value, default):
    normalized = str(value or "").strip().lower()
    if normalized == "mixed":
        normalized = "mix"
    return normalized if normalized in VALID_OUTFIT_STYLES else default


def _safe_automation_speed(value, default):
    try:
        parsed = float(value)
    except Exception:
        return default

    if parsed < 0.1:
        return 0.1
    if parsed > 2.0:
        return 2.0
    return round(parsed, 2)


def _safe_timer_minutes(value, default):
    try:
        parsed = int(float(value))
    except Exception:
        return default

    if parsed < 1:
        return 1
    if parsed > 1440:
        return 1440
    return parsed


def _safe_hubstaff_stop_mode(value, default):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_HUBSTAFF_STOP_MODES else default


def _safe_adspower_control_mode(value, default):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_ADSPOWER_CONTROL_MODES else default


def _safe_text(value, default=""):
    if value is None:
        return default
    return str(value or "").strip()


def _safe_bool(value, default):
    if value is None:
        return default
    return bool(value)


def load_nyx_config():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")
    except Exception:
        raw = {}

    return {
        "pending_threshold": _safe_int(raw.get("pending_threshold"), DEFAULTS["pending_threshold"]),
        "max_parallel_profiles": _safe_int(raw.get("max_parallel_profiles"), DEFAULTS["max_parallel_profiles"]),
        "ignore_done_profiles": _safe_bool(raw.get("ignore_done_profiles"), DEFAULTS["ignore_done_profiles"]),
        "outfit_style": _safe_outfit_style(raw.get("outfit_style"), DEFAULTS["outfit_style"]),
        "outfit_custom_preset_id": _safe_text(
            raw.get("outfit_custom_preset_id"),
            DEFAULTS["outfit_custom_preset_id"],
        ),
        "automation_speed": _safe_automation_speed(raw.get("automation_speed"), DEFAULTS["automation_speed"]),
        "hair_randomizer_enabled": _safe_bool(
            raw.get("hair_randomizer_enabled"), DEFAULTS["hair_randomizer_enabled"]
        ),
        "launch_on_windows_startup": _safe_bool(
            raw.get("launch_on_windows_startup"), DEFAULTS["launch_on_windows_startup"]
        ),
        "hubstaff_control_enabled": _safe_bool(
            raw.get("hubstaff_control_enabled"), DEFAULTS["hubstaff_control_enabled"]
        ),
        "hubstaff_stop_mode": _safe_hubstaff_stop_mode(
            raw.get("hubstaff_stop_mode"), DEFAULTS["hubstaff_stop_mode"]
        ),
        "hubstaff_timer_minutes": _safe_timer_minutes(
            raw.get("hubstaff_timer_minutes"), DEFAULTS["hubstaff_timer_minutes"]
        ),
        "hubstaff_cli_path": _safe_text(raw.get("hubstaff_cli_path"), DEFAULTS["hubstaff_cli_path"]),
        "adspower_control_mode": _safe_adspower_control_mode(
            raw.get("adspower_control_mode"),
            DEFAULTS["adspower_control_mode"],
        ),
        "adspower_api_key": _safe_text(raw.get("adspower_api_key"), DEFAULTS["adspower_api_key"]),
        "adspower_host": _safe_text(raw.get("adspower_host"), DEFAULTS["adspower_host"]),
        "adspower_port": _safe_text(raw.get("adspower_port"), DEFAULTS["adspower_port"]),
    }


def save_nyx_config(updates):
    current = load_nyx_config()
    next_config = {
        "pending_threshold": _safe_int(updates.get("pending_threshold", current["pending_threshold"]), current["pending_threshold"]),
        "max_parallel_profiles": _safe_int(updates.get("max_parallel_profiles", current["max_parallel_profiles"]), current["max_parallel_profiles"]),
        "ignore_done_profiles": _safe_bool(
            updates.get("ignore_done_profiles"), current["ignore_done_profiles"]
        ),
        "outfit_style": _safe_outfit_style(updates.get("outfit_style", current["outfit_style"]), current["outfit_style"]),
        "outfit_custom_preset_id": _safe_text(
            updates.get("outfit_custom_preset_id"),
            current["outfit_custom_preset_id"],
        ),
        "automation_speed": _safe_automation_speed(updates.get("automation_speed", current["automation_speed"]), current["automation_speed"]),
        "hair_randomizer_enabled": _safe_bool(
            updates.get("hair_randomizer_enabled"), current["hair_randomizer_enabled"]
        ),
        "launch_on_windows_startup": _safe_bool(
            updates.get("launch_on_windows_startup"), current["launch_on_windows_startup"]
        ),
        "hubstaff_control_enabled": _safe_bool(
            updates.get("hubstaff_control_enabled"), current["hubstaff_control_enabled"]
        ),
        "hubstaff_stop_mode": _safe_hubstaff_stop_mode(
            updates.get("hubstaff_stop_mode", current["hubstaff_stop_mode"]), current["hubstaff_stop_mode"]
        ),
        "hubstaff_timer_minutes": _safe_timer_minutes(
            updates.get("hubstaff_timer_minutes", current["hubstaff_timer_minutes"]),
            current["hubstaff_timer_minutes"],
        ),
        "hubstaff_cli_path": _safe_text(updates.get("hubstaff_cli_path"), current["hubstaff_cli_path"]),
        "adspower_control_mode": _safe_adspower_control_mode(
            updates.get("adspower_control_mode", current["adspower_control_mode"]),
            current["adspower_control_mode"],
        ),
        # API key: absent (None) keeps the saved key; a non-empty value updates it.
        # The dashboard omits this field when its masked input is left blank, so a
        # routine save never wipes the key.
        "adspower_api_key": _safe_text(updates.get("adspower_api_key"), current["adspower_api_key"]),
        "adspower_host": _safe_text(updates.get("adspower_host"), current["adspower_host"]),
        "adspower_port": _safe_text(updates.get("adspower_port"), current["adspower_port"]),
    }
    CONFIG_PATH.write_text(json.dumps(next_config, indent=2), encoding="utf-8")
    return next_config
