"""Shared Nyxmoji outfit configuration.

Outfits are edited once and applied to every model.  A custom preset stores
random pools per garment category, with optional colour pools per garment id.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from core.bitmoji_config import catalog_option, load_catalog_raw, option_colors
from core.process_utils import APP_DATA_DIR

DATA_DIR = APP_DATA_DIR / "data"
OUTFITS_PATH = DATA_DIR / "bitmoji_outfits.json"
OUTFIT_FEATURES = ("tops", "bottoms", "dresses", "footwear")
DEFAULT_CUSTOM_PRESET_ID = "default"
BUILTIN_PRESET_PREFIX = "builtin_"

_BUILTIN_STYLE_NAMES = {
    "default": "Default",
    "mix": "Mix",
    "casual": "Casual",
    "sexy": "Sexy",
}

_lock = threading.Lock()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _safe_id(value: object, fallback: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "_", text).strip("_")
    return text or fallback


def _safe_tuck(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def _safe_name(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    return text[:80] if text else fallback


def _string_collection(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _catalog_has_options(feature: str, catalog: object) -> bool:
    if not isinstance(catalog, dict):
        return False
    features = catalog.get("features") if isinstance(catalog.get("features"), dict) else catalog
    feature_data = features.get(feature) if isinstance(features, dict) else None
    return isinstance(feature_data, dict) and isinstance(feature_data.get("options"), (list, tuple))


def _known_option(feature: str, option_id: str, catalog: object) -> bool:
    if not _catalog_has_options(feature, catalog):
        return True
    return catalog_option(feature, option_id, catalog) is not None


def _valid_colors(feature: str, option_id: str, catalog: object) -> set[str] | None:
    item = catalog_option(feature, option_id, catalog)
    if item is None or item.get("colors_verified") is not True:
        return None
    return {str(color).strip().lower() for color in option_colors(feature, option_id, catalog)}


def _style_entry_ids(entries: object) -> list[str]:
    """Extract the Bitmoji garment ids embedded in style-definition xpaths."""
    ids: list[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        selector = entry.get("selector") if isinstance(entry, dict) else entry
        match = re.search(r"=(\d+)", str(selector or ""))
        if not match:
            continue
        item_id = match.group(1)
        if item_id not in seen:
            seen.add(item_id)
            ids.append(item_id)
    return ids


def builtin_style_presets(catalog: object = None) -> dict:
    """Return the built-in Default / Mix / Casual / Sexy outfit styles as presets.

    The same garment pools the automator draws from for those styles become
    random pools here, filtered to items the live catalog actually knows.
    """
    from core.outfit_generator import BLOCKED_FOOTWEAR_IDS, BLOCKED_TOP_IDS, CASUAL_OUTFITS, SEXY_OUTFITS
    from snap_selectors.selectors import BITMOJI_SELECTORS

    source = load_catalog_raw() if catalog is None else catalog
    default_sets = BITMOJI_SELECTORS.get("outfits") or {}
    style_sets: dict[str, dict] = {
        "default": {
            "tops": default_sets.get("tops") or [],
            "bottoms": default_sets.get("bottoms") or [],
            "dresses": default_sets.get("dresses") or [],
            "footwear": list(default_sets.get("sandals") or []) + list(default_sets.get("sneakers") or []),
        },
        "casual": CASUAL_OUTFITS,
        "sexy": SEXY_OUTFITS,
        "mix": {
            feature: list(CASUAL_OUTFITS.get(feature) or []) + list(SEXY_OUTFITS.get(feature) or [])
            for feature in OUTFIT_FEATURES
        },
    }
    blocked = {"tops": BLOCKED_TOP_IDS, "footwear": BLOCKED_FOOTWEAR_IDS, "bottoms": set(), "dresses": set()}

    presets: dict[str, dict] = {}
    for style, sets in style_sets.items():
        features: dict[str, dict] = {}
        for feature in OUTFIT_FEATURES:
            pool = [
                item for item in _style_entry_ids(sets.get(feature) or [])
                if item not in blocked.get(feature, set()) and _known_option(feature, item, source)
            ]
            if pool:
                features[feature] = {"mode": "random", "pool": pool}
        preset_id = BUILTIN_PRESET_PREFIX + style
        presets[preset_id] = {
            "id": preset_id,
            "name": _BUILTIN_STYLE_NAMES[style],
            "tuck_in": True,
            "features": features,
        }
    return presets


def _sanitize_feature_selection(feature: str, selection: object, catalog: object) -> dict | None:
    if not isinstance(selection, dict):
        return None
    mode = str(selection.get("mode") or "random").strip().lower()
    if mode != "random":
        return None
    pool = [
        option_id for option_id in _string_collection(selection.get("pool"))
        if _known_option(feature, option_id, catalog)
    ]
    if not pool:
        return None

    entry = {"mode": "random", "pool": pool}
    raw_colors = selection.get("colors_by_option")
    colors_by_option = {}
    if isinstance(raw_colors, dict):
        for option_id in pool:
            configured = _string_collection(raw_colors.get(option_id))
            if not configured:
                continue
            allowed = _valid_colors(feature, option_id, catalog)
            if allowed is None:
                valid = configured
            else:
                valid = [color for color in configured if color.strip().lower() in allowed]
            if valid:
                colors_by_option[option_id] = valid
    if colors_by_option:
        entry["colors_by_option"] = colors_by_option
    return entry


def _preset_items(custom_presets: object) -> list[tuple[str, object]]:
    if isinstance(custom_presets, dict):
        return list(custom_presets.items())
    if isinstance(custom_presets, list):
        items = []
        for index, preset in enumerate(custom_presets):
            if not isinstance(preset, dict):
                continue
            items.append((preset.get("id") or f"preset_{index + 1}", preset))
        return items
    return []


def sanitize_outfit_config(config: object, catalog: dict | None = None) -> dict:
    source = load_catalog_raw() if catalog is None else catalog
    raw = config if isinstance(config, dict) else {}
    sanitized_presets = {}

    for index, (preset_key, preset) in enumerate(_preset_items(raw.get("custom_presets"))):
        if not isinstance(preset, dict):
            continue
        preset_id = _safe_id(preset.get("id") or preset_key, f"preset_{index + 1}")
        if preset_id.startswith(BUILTIN_PRESET_PREFIX):
            continue
        features = {}
        raw_features = preset.get("features") if isinstance(preset.get("features"), dict) else {}
        for feature in OUTFIT_FEATURES:
            selection = _sanitize_feature_selection(feature, raw_features.get(feature), source)
            if selection:
                features[feature] = selection
        if not features:
            continue
        sanitized_presets[preset_id] = {
            "id": preset_id,
            "name": _safe_name(preset.get("name"), preset_id.replace("_", " ").title()),
            "tuck_in": _safe_tuck(preset.get("tuck_in", True)),
            "features": features,
        }

    active = _safe_id(
        raw.get("active_custom_preset_id") or raw.get("activePresetId") or raw.get("active"),
        "",
    )
    if active not in sanitized_presets and not active.startswith(BUILTIN_PRESET_PREFIX):
        active = next(iter(sanitized_presets), "")

    return {
        "active_custom_preset_id": active,
        "custom_presets": sanitized_presets,
    }


def load_outfit_config() -> dict:
    config = sanitize_outfit_config(_read_json(OUTFITS_PATH))
    presets = config.setdefault("custom_presets", {})
    if not presets:
        presets[DEFAULT_CUSTOM_PRESET_ID] = {
            "id": DEFAULT_CUSTOM_PRESET_ID,
            "name": "Custom",
            "tuck_in": True,
            "features": {},
        }
    for preset_id, preset in builtin_style_presets().items():
        presets.setdefault(preset_id, preset)
    if not config.get("active_custom_preset_id"):
        config["active_custom_preset_id"] = DEFAULT_CUSTOM_PRESET_ID
    return config


def save_outfit_config(config: object) -> dict:
    sanitized = sanitize_outfit_config(config)
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        OUTFITS_PATH.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")
    return sanitized


def get_custom_preset(config: dict, preset_id: str = "") -> dict | None:
    if not isinstance(config, dict):
        return None
    presets = config.get("custom_presets")
    if not isinstance(presets, dict) or not presets:
        return None
    requested = _safe_id(preset_id, "") or str(config.get("active_custom_preset_id") or "").strip()
    if requested and requested in presets:
        return presets[requested]
    active = str(config.get("active_custom_preset_id") or "").strip()
    if active in presets:
        return presets[active]
    return next(iter(presets.values()))
