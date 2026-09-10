import json

from core.process_utils import APP_DATA_DIR

DATA_DIR = APP_DATA_DIR / "data"
CONFIG_PATH = DATA_DIR / "nyxify_config.json"

# Canonical cookie warm-up site pool. This is the list the dashboard textbox
# pre-fills with (so it can be edited/removed) and the list the runner samples
# from when the user hasn't saved a custom one. Kept in this lightweight module
# so the config API and the warm-up code share a single source of truth
# (core/adspower_extension_cleanup.py imports it as COOKIE_WARMUP_GOOD_WEBSITES).
DEFAULT_COOKIE_WARMUP_SITES = [
    "https://wikipedia.org/",
    "https://cnn.com/",
    "https://nytimes.com/",
    "https://washingtonpost.com/",
    "https://nbcnews.com/",
    "https://cbsnews.com/",
    "https://abcnews.go.com/",
    "https://apnews.com/",
    "https://reuters.com/",
    "https://usatoday.com/",
    "https://npr.org/",
    "https://foxnews.com/",
    "https://bloomberg.com/",
    "https://wsj.com/",
    "https://forbes.com/",
    "https://businessinsider.com/",
    "https://theverge.com/",
    "https://wired.com/",
    "https://techcrunch.com/",
    "https://medium.com/",
    "https://quora.com/",
    "https://hulu.com/",
    "https://disneyplus.com/",
    "https://max.com/",
    "https://paramountplus.com/",
    "https://peacocktv.com/",
    "https://spotify.com/",
    "https://soundcloud.com/",
    "https://imdb.com/",
    "https://rottentomatoes.com/",
    "https://homedepot.com/",
    "https://lowes.com/",
    "https://costco.com/",
    "https://macys.com/",
    "https://kohls.com/",
    "https://wayfair.com/",
    "https://gap.com/",
    "https://nordstrom.com/",
    "https://chewy.com/",
    "https://yelp.com/",
    "https://starbucks.com/",
    "https://weather.com/",
    "https://accuweather.com/",
    "https://opentable.com/",
    "https://alltrails.com/",
]

DEFAULTS = {
    "max_parallel_profiles": 1,
    "temporary_profile_name": "Snapchat:",
    "adspower_group": "Snapchat",
    "extension_category": "Snap",
    "tag_one": "",
    "tag_two": "",
    "adspower_tags_enabled": False,
    "blocked_proxies": [],
    "proxy_blocker_enabled": True,
    "proxy_checker_enabled": True,
    "proxy_priority_enabled": False,
    "proxy_priority_patterns": [],
    "push_adspower_id_enabled": True,
    "full_auto_mode_enabled": False,
    "continuous_mode_enabled": False,
    "verification_priority": "auto",
    "keep_profile_open_after_signup": False,
    # Turning off the profile's Chrome extensions during account creation is now
    # opt-in and OFF by default (users asked to stop disabling extensions while
    # the Snapchat account is being created).
    "disable_extensions_enabled": False,
    "launch_on_windows_startup": False,
    "names_dir": "",
    # Cookie warm-up (runs right after the profile opens, before signup). The
    # site list is now editable from the dashboard; an empty list falls back to
    # the built-in curated pool in core/adspower_extension_cleanup.py.
    "cookie_warmup_enabled": True,
    "cookie_warmup_sites": [],
    # whox.com trust-score gate. When enabled, the runner opens whox.com right
    # after the profile opens (before cookie warm-up), runs the deep scan, and
    # only continues when the deep trust score is >= the threshold. Below it, the
    # AdsPower profile is closed + deleted, its SnapBoard id is cleared, and the
    # row is requeued to create from scratch. ON by default; toggle it off in the
    # dashboard to skip the gate entirely.
    "whox_check_enabled": True,
    "whox_min_trust_score": 70,
    "whox_url": "https://whox.com/",
}

VERIFICATION_PRIORITIES = {"email", "phone", "auto"}


def _safe_int(value, default):
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _safe_str(value, default=""):
    value = str(value or "").strip()
    if value:
        return value
    return default


def _stored_str(raw, key, default=""):
    if key not in raw:
        return default
    return str(raw.get(key) or "").strip()


def _updated_str(updates, current, key, *, allow_blank=True, blank_default=None):
    if key not in updates:
        return current[key]
    value = str(updates.get(key) or "").strip()
    if value or allow_blank:
        return value
    return blank_default if blank_default is not None else current[key]


def _safe_bool(value, default):
    if value is None:
        return default
    return bool(value)


def _safe_verification_priority(value, default="auto"):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VERIFICATION_PRIORITIES else default


def _safe_score(value, default, lo=1, hi=100):
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, parsed))


def _safe_str_list(value):
    if isinstance(value, str):
        items = [part.strip() for part in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        items = [str(part).strip() for part in value]
    else:
        items = []
    return [item for item in items if item]


def _safe_proxy_patterns(value):
    if isinstance(value, str):
        items = [part.strip() for part in value.splitlines()]
    elif isinstance(value, list):
        items = [str(part).strip() for part in value]
    else:
        items = []
    return [item for item in items if item]


def load_nyxify_config():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")
    except Exception:
        raw = {}

    return {
        "max_parallel_profiles": _safe_int(raw.get("max_parallel_profiles"), DEFAULTS["max_parallel_profiles"]),
        "temporary_profile_name": _stored_str(raw, "temporary_profile_name", DEFAULTS["temporary_profile_name"]),
        "adspower_group": _stored_str(raw, "adspower_group", DEFAULTS["adspower_group"]),
        "extension_category": _stored_str(raw, "extension_category", DEFAULTS["extension_category"]),
        "tag_one": _stored_str(raw, "tag_one", DEFAULTS["tag_one"]),
        "tag_two": _stored_str(raw, "tag_two", DEFAULTS["tag_two"]),
        "adspower_tags_enabled": _safe_bool(
            raw.get("adspower_tags_enabled"),
            DEFAULTS["adspower_tags_enabled"],
        ),
        "blocked_proxies": _safe_proxy_patterns(raw.get("blocked_proxies") or raw.get("banned_proxies")),
        "proxy_blocker_enabled": _safe_bool(raw.get("proxy_blocker_enabled"), DEFAULTS["proxy_blocker_enabled"]),
        "proxy_checker_enabled": _safe_bool(raw.get("proxy_checker_enabled"), DEFAULTS["proxy_checker_enabled"]),
        "proxy_priority_enabled": _safe_bool(
            raw.get("proxy_priority_enabled"),
            DEFAULTS["proxy_priority_enabled"],
        ),
        "proxy_priority_patterns": _safe_proxy_patterns(raw.get("proxy_priority_patterns")),
        "push_adspower_id_enabled": _safe_bool(
            raw.get("push_adspower_id_enabled"),
            DEFAULTS["push_adspower_id_enabled"],
        ),
        "full_auto_mode_enabled": _safe_bool(
            raw.get("full_auto_mode_enabled"),
            DEFAULTS["full_auto_mode_enabled"],
        ),
        "continuous_mode_enabled": _safe_bool(
            raw.get("continuous_mode_enabled"),
            DEFAULTS["continuous_mode_enabled"],
        ),
        "verification_priority": _safe_verification_priority(
            raw.get("verification_priority"),
            DEFAULTS["verification_priority"],
        ),
        "keep_profile_open_after_signup": _safe_bool(
            raw.get("keep_profile_open_after_signup"),
            DEFAULTS["keep_profile_open_after_signup"],
        ),
        "disable_extensions_enabled": _safe_bool(
            raw.get("disable_extensions_enabled"),
            DEFAULTS["disable_extensions_enabled"],
        ),
        "launch_on_windows_startup": _safe_bool(
            raw.get("launch_on_windows_startup"),
            DEFAULTS["launch_on_windows_startup"],
        ),
        "names_dir": _safe_str(raw.get("names_dir"), DEFAULTS["names_dir"]),
        "cookie_warmup_enabled": _safe_bool(
            raw.get("cookie_warmup_enabled"),
            DEFAULTS["cookie_warmup_enabled"],
        ),
        "cookie_warmup_sites": _safe_str_list(raw.get("cookie_warmup_sites")),
        # Read-only: the built-in pool, so the dashboard can pre-fill the editor
        # when no custom list is saved. Not persisted by save_nyxify_config.
        "cookie_warmup_sites_default": list(DEFAULT_COOKIE_WARMUP_SITES),
        "whox_check_enabled": _safe_bool(
            raw.get("whox_check_enabled"),
            DEFAULTS["whox_check_enabled"],
        ),
        "whox_min_trust_score": _safe_score(
            raw.get("whox_min_trust_score"),
            DEFAULTS["whox_min_trust_score"],
        ),
        "whox_url": _stored_str(raw, "whox_url", DEFAULTS["whox_url"]),
    }


def save_nyxify_config(updates):
    current = load_nyxify_config()
    next_config = {
        "max_parallel_profiles": _safe_int(
            updates.get("max_parallel_profiles", current["max_parallel_profiles"]),
            current["max_parallel_profiles"],
        ),
        "temporary_profile_name": _updated_str(
            updates,
            current,
            "temporary_profile_name",
            allow_blank=False,
            blank_default=DEFAULTS["temporary_profile_name"],
        ),
        "adspower_group": _updated_str(updates, current, "adspower_group"),
        "extension_category": _updated_str(
            updates,
            current,
            "extension_category",
            allow_blank=False,
            blank_default=DEFAULTS["extension_category"],
        ),
        "tag_one": _updated_str(updates, current, "tag_one"),
        "tag_two": _updated_str(updates, current, "tag_two"),
        "adspower_tags_enabled": _safe_bool(
            updates.get("adspower_tags_enabled"),
            current["adspower_tags_enabled"],
        ),
        "blocked_proxies": _safe_proxy_patterns(
            updates.get("blocked_proxies", updates.get("banned_proxies", current["blocked_proxies"]))
        ),
        "proxy_blocker_enabled": _safe_bool(
            updates.get("proxy_blocker_enabled"),
            current["proxy_blocker_enabled"],
        ),
        "proxy_checker_enabled": _safe_bool(
            updates.get("proxy_checker_enabled"),
            current["proxy_checker_enabled"],
        ),
        "proxy_priority_enabled": _safe_bool(
            updates.get("proxy_priority_enabled"),
            current["proxy_priority_enabled"],
        ),
        "proxy_priority_patterns": (
            _safe_proxy_patterns(updates.get("proxy_priority_patterns"))
            if "proxy_priority_patterns" in updates
            else current["proxy_priority_patterns"]
        ),
        "push_adspower_id_enabled": _safe_bool(
            updates.get("push_adspower_id_enabled"),
            current["push_adspower_id_enabled"],
        ),
        "full_auto_mode_enabled": _safe_bool(
            updates.get("full_auto_mode_enabled"),
            current["full_auto_mode_enabled"],
        ),
        "continuous_mode_enabled": _safe_bool(
            updates.get("continuous_mode_enabled"),
            current["continuous_mode_enabled"],
        ),
        "verification_priority": _safe_verification_priority(
            updates.get("verification_priority", current["verification_priority"]),
            current["verification_priority"],
        ),
        "keep_profile_open_after_signup": _safe_bool(
            updates.get("keep_profile_open_after_signup"),
            current["keep_profile_open_after_signup"],
        ),
        "disable_extensions_enabled": _safe_bool(
            updates.get("disable_extensions_enabled"),
            current["disable_extensions_enabled"],
        ),
        "launch_on_windows_startup": _safe_bool(
            updates.get("launch_on_windows_startup"),
            current["launch_on_windows_startup"],
        ),
        "names_dir": _updated_str(updates, current, "names_dir"),
        "cookie_warmup_enabled": _safe_bool(
            updates.get("cookie_warmup_enabled"),
            current["cookie_warmup_enabled"],
        ),
        "cookie_warmup_sites": (
            _safe_str_list(updates.get("cookie_warmup_sites"))
            if "cookie_warmup_sites" in updates
            else current["cookie_warmup_sites"]
        ),
        "whox_check_enabled": _safe_bool(
            updates.get("whox_check_enabled"),
            current["whox_check_enabled"],
        ),
        "whox_min_trust_score": _safe_score(
            updates.get("whox_min_trust_score", current["whox_min_trust_score"]),
            current["whox_min_trust_score"],
        ),
        "whox_url": _updated_str(
            updates,
            current,
            "whox_url",
            allow_blank=False,
            blank_default=DEFAULTS["whox_url"],
        ),
    }
    CONFIG_PATH.write_text(json.dumps(next_config, indent=2), encoding="utf-8")
    # Return (but never persist) the read-only built-in pool so the dashboard's
    # post-save re-render stays consistent with load_nyxify_config().
    next_config["cookie_warmup_sites_default"] = list(DEFAULT_COOKIE_WARMUP_SITES)
    return next_config
