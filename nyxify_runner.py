import asyncio
import os
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

import requests as _requests
from dotenv import load_dotenv

from core.macos_dock import hide_macos_dock_icon

hide_macos_dock_icon()

from core.adspower import AdsPowerManager
from core.adspower_extension_cleanup import (
    clear_unrelated_tabs_except_adspower,
    disable_profile_extensions,
    open_snapchat_signup,
    warm_ads_profile_cookies,
)
from core.logger import logger
from core.nyxify_cleanup import (
    CLEANUP_DELETE_FAILED_STEP,
    cleanup_delete_failed_error,
    close_and_delete_profile,
)
from core.nyx_handoff import NYX_TASK_DB_PATH, enqueue_profile_for_nyx
from core.nyxify_runtime_config import load_nyxify_config
from core.whox_check import run_whox_trust_check
from core.nyxify_task_store import NyxifyTaskStore
from core.process_utils import APP_DATA_DIR, LOGS_DIR
from core.signup_data import DEFAULT_SIGNUP_NAMES_DIR, resolve_model_name
from core.signup_flow import perform_snapchat_signup

load_dotenv()

POLL_INTERVAL_SECONDS = int(os.getenv("NYXIFY_POLL_INTERVAL_SECONDS", "8"))
NYXIFY_LOCAL_API_URL = os.getenv("NYXIFY_LOCAL_API_URL", "http://127.0.0.1:8866")
NYXIFY_LOCAL_API_TOKEN = os.getenv("NYXIFY_LOCAL_API_TOKEN") or os.getenv("NYXSUITE_TOKEN") or ""
SNAPBOARD_BRIDGE_POLL_SECONDS = float(os.getenv("NYXIFY_SNAPBOARD_BRIDGE_POLL_SECONDS", "0.5"))
SNAPBOARD_BRIDGE_DISPATCH_TIMEOUT_SECONDS = float(
    os.getenv("NYXIFY_SNAPBOARD_BRIDGE_DISPATCH_TIMEOUT_SECONDS", "8")
)
SNAPBOARD_REFRESH_TIMEOUT_SECONDS = float(os.getenv("NYXIFY_SNAPBOARD_REFRESH_TIMEOUT_SECONDS", "45"))
SNAPBOARD_OTP_INITIAL_WAIT_SECONDS = float(os.getenv("NYXIFY_SNAPBOARD_OTP_INITIAL_WAIT_SECONDS", "35"))
SNAPBOARD_OTP_REFRESH_RETRY_SECONDS = float(os.getenv("NYXIFY_SNAPBOARD_OTP_REFRESH_RETRY_SECONDS", "75"))
PLAYWRIGHT_RELEASE_TIMEOUT_SECONDS = float(os.getenv("NYXIFY_PLAYWRIGHT_RELEASE_TIMEOUT_SECONDS", "2"))
PAUSE_FILE = os.getenv("NYXIFY_PAUSE_FILE", str(LOGS_DIR / "nyxify_runner.paused"))
TASK_DB_PATH = os.getenv("NYXIFY_TASK_DB_PATH", str(APP_DATA_DIR / "data" / "nyxify_tasks.db"))
RUNNER_LOCK_HOST = os.getenv("NYXIFY_RUNNER_LOCK_HOST", "127.0.0.1")
RUNNER_LOCK_PORT = int(os.getenv("NYXIFY_RUNNER_LOCK_PORT", "8867"))
NYXIFY_COMPLETION_SOUND_ENABLED = str(os.getenv("NYXIFY_COMPLETION_SOUND", "1")).strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
MAX_PROXY_ROTATION_ATTEMPTS = 30

_LOCAL_API_TOKEN_CACHED = False


def _continuous_nyx_handoff_active(db_path=None):
    try:
        from core.task_store import TaskStore

        store = TaskStore(db_path=str(db_path or NYX_TASK_DB_PATH))
        return store.has_active_continuous_handoff()
    except Exception as exc:
        logger.warning(f"Could not read Nyx continuous handoff state: {exc}")
        return False


def _mark_pending_tasks_waiting_for_continuous_nyx(store):
    marked = 0
    try:
        rows = store.list_tasks(limit=500)
    except Exception as exc:
        logger.warning(f"Could not list Nyxify rows for continuous wait marker: {exc}")
        return 0

    for row in rows:
        status = str(row.get("status") or "").strip().upper()
        username = str(row.get("username") or "").strip()
        if status != "PENDING" or not username or username.lower().startswith("temp"):
            continue
        if str(row.get("last_step") or "").strip() == "waiting_for_continuous_nyx":
            continue
        try:
            marked += int(store.update_task_last_step_by_row_key(
                row.get("row_key"),
                "waiting_for_continuous_nyx",
            ) or 0)
        except Exception as exc:
            logger.warning(
                f"Could not mark Nyxify row {row.get('row_key')!r} waiting for continuous Nyx: {exc}"
            )
    return marked


def _ensure_local_api_token(force_refresh=False):
    global NYXIFY_LOCAL_API_TOKEN, _LOCAL_API_TOKEN_CACHED
    if force_refresh:
        NYXIFY_LOCAL_API_TOKEN = ""
        _LOCAL_API_TOKEN_CACHED = False
    if _LOCAL_API_TOKEN_CACHED:
        return NYXIFY_LOCAL_API_TOKEN
    if NYXIFY_LOCAL_API_TOKEN:
        _LOCAL_API_TOKEN_CACHED = True
        return NYXIFY_LOCAL_API_TOKEN
    try:
        resp = _requests.get(f"{NYXIFY_LOCAL_API_URL}/token", timeout=3)
        data = resp.json()
        token = str(data.get("token") or "").strip()
        if token:
            NYXIFY_LOCAL_API_TOKEN = token
            _LOCAL_API_TOKEN_CACHED = True
    except Exception:
        pass
    return NYXIFY_LOCAL_API_TOKEN


def _local_api_auth(payload=None, *, force_refresh=False):
    token = _ensure_local_api_token(force_refresh=force_refresh)
    headers = {}
    body = dict(payload or {})
    if token:
        headers["X-Nyxify-Token"] = token
        body["token"] = token
    return body, headers


def _post_local_api_response(path, payload=None, *, timeout=5):
    last_response = None
    for force_refresh in (False, True):
        body, headers = _local_api_auth(payload, force_refresh=force_refresh)
        response = _requests.post(
            f"{NYXIFY_LOCAL_API_URL}{path}",
            json=body,
            headers=headers or None,
            timeout=timeout,
        )
        last_response = response
        if response.status_code == 401 and not force_refresh:
            continue
        return response
    return last_response


# Single-instance lock shared with the Nyx runner and the bridge supervisor.
from core.runner_lock import RunnerLock as _RunnerLock


def _play_completion_sound():
    if not NYXIFY_COMPLETION_SOUND_ENABLED:
        return

    try:
        if sys.platform.startswith("win"):
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return

        if sys.platform == "darwin":
            for command in (
                ["afplay", "/System/Library/Sounds/Glass.aiff"],
                ["osascript", "-e", "beep 1"],
            ):
                try:
                    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except Exception:
                    continue

        print("\a", end="", flush=True)
    except Exception as exc:
        logger.warning(f"Could not play Nyxify completion sound: {exc}")


def _is_proxy_banned(proxy_value, banned_proxies):
    normalized_proxy = str(proxy_value or "").strip().lower()
    if not normalized_proxy:
        return False
    proxy_host = normalized_proxy.split(":")[0]
    for banned in banned_proxies or []:
        normalized_banned = str(banned or "").strip().lower()
        if not normalized_banned:
            continue
        if proxy_host.startswith(normalized_banned) or normalized_proxy.startswith(normalized_banned):
            return True
        if normalized_banned in normalized_proxy:
            return True
    return False


_proxy_ranking_store = None


def _get_proxy_ranking_store():
    """Lazily open the per-subnet proxy ranking store. Returns None (and never
    raises) if it can't be opened, so ranking is best-effort telemetry that can
    never break a creation run."""
    global _proxy_ranking_store
    if _proxy_ranking_store is None:
        try:
            from core.proxy_ranking_store import ProxyRankingStore

            _proxy_ranking_store = ProxyRankingStore()
        except Exception as exc:
            logger.debug(f"Proxy ranking store unavailable: {exc}")
            _proxy_ranking_store = False
    return _proxy_ranking_store or None


def _record_proxy_ranking_event(proxy_value, event, reason=""):
    """Record a proxy-ranking event ('use' | 'retry' | 'creation_fail' | 'ban').
    Best-effort: any failure is swallowed so telemetry never affects the run."""
    if not str(proxy_value or "").strip():
        return
    store = _get_proxy_ranking_store()
    if not store:
        return
    try:
        if event == "use":
            store.record_use(proxy_value)
        elif event == "retry":
            store.record_retry(proxy_value, reason=reason)
        elif event == "creation_fail":
            store.record_creation_fail(proxy_value)
        elif event == "ban":
            store.record_ban_hit(proxy_value)
    except Exception as exc:
        logger.debug(f"Could not record proxy ranking event {event!r}: {exc}")


def _classify_failure_last_step(created, last_step, error_message):
    normalized_error = str(error_message or "").strip().lower()
    normalized_step = str(last_step or "").strip()

    if _is_macos_accessibility_permission_error(error_message):
        return "adspower_accessibility_permission_missing"

    if _is_unable_to_process_error(error_message):
        return "unable_to_process"

    # Signup-blocker classes raised by core/signup_flow.py. Each one means the
    # current profile/proxy can't finish signup, so they all route to the
    # cleanup/retry path (delete profile, rotate proxy, requeue as PENDING).
    if "account_creation_blocked" in normalized_error:
        return "account_creation_blocked"
    if "signup_non_english_page" in normalized_error:
        return "signup_non_english_page"
    if "signup_stuck_retry_exhausted" in normalized_error:
        return "signup_stuck_retry_exhausted"
    if "email_order_unavailable" in normalized_error:
        return "email_order_unavailable"
    if "phone_verification_rejected" in normalized_error:
        return "phone_verification_rejected"

    if "number of imported accounts exceeds the limit" in normalized_error:
        return "adspower_account_limit_reached"

    if "adspower local api is unreachable" in normalized_error:
        return "adspower_api_unreachable"

    if "adsPower group not found".lower() in normalized_error:
        return "adspower_group_not_found"

    if "adspower tag(s) not found" in normalized_error or "tag(s) not found" in normalized_error:
        return "adspower_tags_not_found"

    if "adspower extension category not found" in normalized_error:
        return "adspower_extension_category_not_found"

    if "proxy value is required" in normalized_error or "proxy host/port is invalid" in normalized_error:
        return "proxy_invalid"

    if created is None and normalized_step == "checking_proxy":
        return "proxy_check_failed"

    if created is None and normalized_step == "fetching_email":
        return "email_fetch_failed"

    if created is None and normalized_step == "creating_adspower_profile":
        return "adspower_profile_creation_failed"

    if (
        "signup handoff timed out" in normalized_error
        or "signup page did not become ready" in normalized_error
        or "waiting_for_signup_page" in normalized_error
        or "timeouterror" in normalized_error
        or last_step == "signup_handoff"
    ):
        return "signup_handoff_failed"

    if "whox_trust_score_below_threshold" in normalized_error or normalized_step == "whox_trust_check":
        return "whox_trust_score_below_threshold"

    if last_step == "cookie_warmup" or "cookie warm" in normalized_error:
        return "cookie_warmup_failed"

    if "accounts.snapchat.com/v2/signup" in normalized_error or "err_socks_connection_failed" in normalized_error:
        return "signup_navigation_failed"

    signup_progress_steps = {
        "retrying_signup_username",
        "clicking_use_email_instead",
        "awaiting_email_verification",
        "fetching_email",
        "fetching_replacement_email",
        "filling_email_verification",
        "awaiting_phone_verification",
        "fetching_phone_verification",
        "filling_phone_verification",
        "awaiting_otp",
        "fetching_otp",
        "fetching_sms_otp",
        "retrying_otp",
        "submitting_otp",
        "refreshing_signup_recaptcha",
        "refreshing_stalled_signup",
        "refreshing_stuck_signup",
        "refreshing_signup_page_issue",
        "refreshing_signup_blank_shell",
        "refilling_signup_form",
        "waiting_for_signup_handoff",
        "signup_form_submitted",
        "awaiting_welcome_username",
    }
    # Preserve the exact signup sub-step the row died on — both the known steps
    # above and any emitted email/otp/username step — so a failure reads e.g.
    # "fetching_otp" instead of a generic "signup_automation_failed". Without
    # this, steps the signup flow emits but that aren't whitelisted (e.g.
    # "fetching_replacement_email") were mislabeled as "create_tags_unconfirmed".
    if normalized_step in signup_progress_steps:
        return normalized_step
    if normalized_step and any(
        token in normalized_step
        for token in ("otp", "email_verification", "signup_username", "verification")
    ):
        return normalized_step

    if "replacement snapchat username" in normalized_error:
        return "signup_username_retry_failed"

    if last_step == "running_signup":
        return "signup_automation_failed"

    if created and last_step in {"opening_profile", "extensions_disabled"}:
        return "extensions_disable_failed"

    if created:
        return "create_tags_unconfirmed"

    return "profile_creation_failed"


def _is_unable_to_process_error(error_message):
    normalized_error = str(error_message or "").strip().lower()
    return (
        "unable_to_process" in normalized_error
        or "unable to process" in normalized_error
        or "we were unable to process your request" in normalized_error
    )


def _is_macos_accessibility_permission_error(error_message):
    normalized_error = str(error_message or "").strip().lower()
    return (
        "macos accessibility permission is required" in normalized_error
        and "adspower no-api gui automation" in normalized_error
    )


def _is_proxy_navigation_error(error_message):
    normalized_error = str(error_message or "").strip().lower()
    return any(
        marker in normalized_error
        for marker in [
            "err_socks_connection_failed",
            "err_proxy_connection_failed",
            "err_tunnel_connection_failed",
            "connection test failed",
            "proxy check failed",
            "net::err",
        ]
    )


def _should_cleanup_failed_created_profile(failure_last_step, error_message):
    normalized_step = str(failure_last_step or "").strip().lower()
    if normalized_step == "unable_to_process":
        return True
    if normalized_step in {
        "whox_trust_score_below_threshold",
        "signup_navigation_failed",
        "signup_handoff_failed",
        "cookie_warmup_failed",
        "signup_automation_failed",
        "signup_username_retry_failed",
        "retrying_signup_username",
        "clicking_use_email_instead",
        "awaiting_email_verification",
        "fetching_email",
        "fetching_replacement_email",
        "filling_email_verification",
        "awaiting_phone_verification",
        "fetching_phone_verification",
        "filling_phone_verification",
        "awaiting_otp",
        "fetching_otp",
        "fetching_sms_otp",
        "retrying_otp",
        "submitting_otp",
        "refreshing_signup_recaptcha",
        "refreshing_stalled_signup",
        "refreshing_stuck_signup",
        "refreshing_signup_page_issue",
        "refreshing_signup_blank_shell",
        "refilling_signup_form",
        "waiting_for_signup_handoff",
        "signup_handoff",
        "signup_opened",
        "signup_form_submitted",
        "awaiting_welcome_username",
        "proxy_check_failed",
        "email_fetch_failed",
        "account_creation_blocked",
        "signup_non_english_page",
        "signup_stuck_retry_exhausted",
        "email_order_unavailable",
        "phone_verification_rejected",
    }:
        return True
    # Keep cleanup consistent for any signup sub-step the classifier now
    # preserves (otp / email-verification / username), so a more accurate
    # last_step never leaves an orphan AdsPower profile behind.
    if any(
        token in normalized_step
        for token in ("otp", "email_verification", "signup_username", "verification")
    ):
        return True
    if normalized_step in {
        "adspower_profile_creation_failed",
        "adspower_account_limit_reached",
        "adspower_api_unreachable",
        "adspower_group_not_found",
        "adspower_tags_not_found",
        "adspower_extension_category_not_found",
    }:
        return True
    if normalized_step == "proxy_invalid":
        return True
    return _is_proxy_navigation_error(error_message)


def _is_placeholder_username(username):
    normalized = str(username or "").strip().lower()
    if not normalized:
        return False
    return normalized.startswith("temp")


def _is_valid_email(email):
    normalized = str(email or "").strip()
    if not normalized or "@" not in normalized:
        return False
    local_part, _, domain = normalized.partition("@")
    return bool(local_part and "." in domain)


def _same_username(lhs, rhs):
    return str(lhs or "").strip().lower() == str(rhs or "").strip().lower()


def _resolve_names_dir(config) -> Path:
    override = str(config.get("names_dir") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_SIGNUP_NAMES_DIR


def _missing_task_fields(username):
    missing_fields = []
    if not username:
        missing_fields.append("username")
    elif _is_placeholder_username(username):
        missing_fields.append("real_username")
    return missing_fields


def _build_waiting_step(missing_fields):
    return "waiting_for_" + "_and_".join(missing_fields)


def _request_snapboard_rotation_sync(row_key, timeout_seconds=40, max_clicks=None):
    """Ask the SnapBoard content script (via local API) to click the rotate button and return the new proxy."""
    payload = {"row_key": row_key}
    if max_clicks is not None:
        payload["max_clicks"] = max_clicks
    try:
        _post_local_api_response("/proxy/rotate_request", payload, timeout=5)
    except Exception as exc:
        logger.warning(f"Could not send proxy rotate request to local API: {exc}")
        return None

    for _ in range(timeout_seconds):
        time.sleep(1)
        try:
            resp = _requests.get(
                f"{NYXIFY_LOCAL_API_URL}/proxy/rotate_status",
                params={"row_key": row_key},
                timeout=5,
            )
            data = resp.json()
            if data.get("done"):
                if data.get("error"):
                    logger.warning(f"SnapBoard proxy rotation error for {row_key}: {data['error']}")
                    return None
                new_proxy = str(data.get("proxy") or "").strip()
                return new_proxy or None
        except Exception:
            pass

    logger.warning(f"SnapBoard proxy rotation timed out for {row_key}")
    return None


async def _request_snapboard_rotation(row_key, timeout_seconds=40, max_clicks=None):
    return await asyncio.to_thread(
        _request_snapboard_rotation_sync,
        row_key,
        timeout_seconds,
        max_clicks,
    )


def _post_with_retries(path, payload, *, attempts=4, label=""):
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = _post_local_api_response(path, payload, timeout=5)
            if response.ok:
                return True
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:
            last_error = str(exc)
        if attempt < attempts:
            time.sleep(0.4 * attempt)
    logger.warning(
        f"Could not deliver {label or path} request to local API after {attempts} attempts: {last_error}"
    )
    return False


def _request_snapboard_username_update(row_key, username):
    return _post_with_retries(
        "/username_update/request",
        {"row_key": row_key, "username": username},
        label="username update",
    )


def _request_snapboard_adspower_id_update(row_key, adspower_id):
    return _post_with_retries(
        "/adspower_update/request",
        {"row_key": row_key, "adspower_id": adspower_id},
        label="AdsPower id update",
    )


def _request_snapboard_adspower_name_update(row_key, adspower_name):
    return _post_with_retries(
        "/adspower_name_update/request",
        {"row_key": row_key, "adspower_name": adspower_name},
        label="AdsPower name update",
    )


def _post_local_json(path, payload, timeout=5):
    response = _post_local_api_response(path, payload, timeout=timeout)
    data = response.json()
    if not response.ok or data.get("ok") is False:
        raise RuntimeError(data.get("error") or f"HTTP {response.status_code}")
    return data


async def _request_next_full_auto_username(row_key, model, current_username="", reason=""):
    normalized_row_key = str(row_key or "").strip()
    normalized_model = str(model or "").strip()
    current_username = str(current_username or "").strip()
    if not normalized_row_key or not normalized_model:
        return ""

    try:
        reservation = await asyncio.to_thread(
            _post_local_json,
            "/full_auto/reserve",
            {
                "row_key": normalized_row_key,
                "model": normalized_model,
                "current_username": current_username,
                "reason": str(reason or "").strip(),
            },
        )
    except Exception as exc:
        logger.warning(
            f"Could not reserve a Full Auto username for {normalized_row_key} ({normalized_model}): {exc}"
        )
        return ""

    reserved_username = str(reservation.get("username") or "").strip()
    if not reserved_username:
        logger.warning(
            f"Full Auto Mode has no username available for {normalized_row_key} ({normalized_model})."
        )
        return ""

    update_requested = _request_snapboard_username_update(normalized_row_key, reserved_username)
    update_success = False
    update_error = "Could not dispatch SnapBoard username update."
    if update_requested:
        update_success = await _wait_for_snapboard_update(
            "/username_update/status",
            normalized_row_key,
            "username",
        )
        update_error = "" if update_success else "SnapBoard username update failed."

    try:
        await asyncio.to_thread(
            _post_local_json,
            "/full_auto/commit",
            {
                "row_key": normalized_row_key,
                "reservation_id": str(reservation.get("reservation_id") or "").strip(),
                "username": reserved_username,
                "model": normalized_model,
                "success": update_success,
                "error": update_error,
            },
        )
    except Exception as exc:
        logger.warning(
            f"Could not finalize Full Auto username {reserved_username!r} for {normalized_row_key}: {exc}"
        )

    if update_success:
        logger.info(
            f"Full Auto Mode assigned a new username for {normalized_row_key}: {reserved_username!r}"
        )
        return reserved_username

    return ""


def _get_snapboard_update_status(path, row_key):
    response = _requests.get(
        f"{NYXIFY_LOCAL_API_URL}{path}",
        params={"row_key": row_key},
        timeout=5,
    )
    if not response.ok:
        return {"done": False, "error": f"HTTP {response.status_code}"}
    return response.json()


async def _wait_for_snapboard_update(path, row_key, label, timeout_seconds=30):
    normalized_row_key = str(row_key or "").strip()
    if not normalized_row_key:
        return False

    deadline = time.monotonic() + max(1, int(timeout_seconds or 30))
    last_error = ""
    while time.monotonic() < deadline:
        try:
            data = await asyncio.to_thread(_get_snapboard_update_status, path, normalized_row_key)
            if data.get("done"):
                if data.get("success"):
                    return True
                last_error = str(data.get("error") or f"{label} update failed.").strip()
                logger.warning(f"SnapBoard {label} update failed for {normalized_row_key}: {last_error}")
                return False
            last_error = str(data.get("error") or "").strip()
        except Exception as exc:
            last_error = str(exc)
        await asyncio.sleep(0.5)

    logger.warning(
        f"Timed out waiting for SnapBoard {label} update confirmation for {normalized_row_key}"
        + (f": {last_error}" if last_error else ".")
    )
    return False


async def _cleanup_failed_created_profile(task_id, task, store, adspower, created, failure_last_step, cleanup_reason):
    profile_id = str((created or {}).get("profile_id") or "").strip()
    row_key = str((task or {}).get("row_key") or "").strip()
    normalized_reason = str(cleanup_reason or failure_last_step or "failed signup").strip()
    normalized_failure_step = str(failure_last_step or "profile_creation_failed").strip()
    delete_confirmed = not profile_id
    cleanup_result = None

    if profile_id:
        cleanup_result = await asyncio.to_thread(
            close_and_delete_profile,
            adspower,
            profile_id,
            logger,
            task_id,
            row_key,
            normalized_reason,
        )
        delete_confirmed = bool(cleanup_result.get("deleted"))

    if not delete_confirmed:
        delete_error = cleanup_result.get("delete_error") if cleanup_result else "AdsPower did not confirm profile deletion."
        store.update_task_state(
            task_id,
            status="FAILED",
            last_step=CLEANUP_DELETE_FAILED_STEP,
            error=cleanup_delete_failed_error(profile_id, delete_error, normalized_reason),
        )
        logger.warning(
            f"Task {task_id}: leaving Nyxify row failed at {CLEANUP_DELETE_FAILED_STEP}; "
            f"row_key={row_key or '-'} profile_id={profile_id} delete_error={delete_error}"
        )
        return

    if row_key:
        if _request_snapboard_adspower_id_update(row_key, ""):
            cleared = await _wait_for_snapboard_update(
                "/adspower_update/status",
                row_key,
                "AdsPower id clear",
                timeout_seconds=30,
            )
            if cleared:
                logger.info(f"Task {task_id}: cleared SnapBoard AdsPower id for {row_key}.")
            else:
                logger.warning(f"Task {task_id}: SnapBoard AdsPower id clear was not confirmed for {row_key}.")
        else:
            logger.warning(f"Task {task_id}: could not request SnapBoard AdsPower id clear for {row_key}.")

    refreshed_proxy = ""
    if row_key:
        store.update_task_state(task_id, last_step=f"refreshing_proxy_after_{normalized_failure_step}")
        refreshed_proxy = await _request_snapboard_rotation(row_key, timeout_seconds=55, max_clicks=1) or ""
        if refreshed_proxy:
            store.update_task_proxy(task_id, refreshed_proxy)
            logger.info(
                f"Task {task_id}: refreshed SnapBoard proxy once after {normalized_reason} for {row_key}."
            )
        else:
            logger.warning(
                f"Task {task_id}: SnapBoard proxy did not refresh after {normalized_reason} for {row_key}; "
                f"requeuing anyway — the next attempt re-checks and rotates the proxy before creating."
            )

    # The failed profile is deleted, so always requeue the row to PENDING and
    # create another. We do NOT gate the requeue on the one-shot rotation above
    # (that is only a head start): the next cycle's _rotate_proxy_until_usable
    # re-validates and rotates the proxy itself. Gating here would strand the row
    # in RUNNING forever whenever a single SnapBoard rotation click didn't land
    # (there is no stale-RUNNING reaper), silently losing the account.
    store.update_task_state(
        task_id,
        status="PENDING",
        last_step="proxy_refreshed_retry_pending" if refreshed_proxy
        else f"retry_pending_after_{normalized_failure_step}",
        error="",
        adspower_id="",
        adspower_profile_id="",
        adspower_name="",
        adspower_group="",
        tags=[],
    )


async def _cleanup_stale_pending_profile(task_id, task, store, adspower):
    profile_id = str(
        (task or {}).get("adspower_profile_id")
        or (task or {}).get("adspower_id")
        or ""
    ).strip()
    row_key = str((task or {}).get("row_key") or "").strip()
    if not profile_id:
        return False

    logger.warning(
        f"Task {task_id}: removing stale AdsPower profile {profile_id} before retrying pending Nyxify row."
    )
    cleanup_result = await asyncio.to_thread(
        close_and_delete_profile,
        adspower,
        profile_id,
        logger,
        task_id,
        row_key,
        "stale_pending_profile",
    )

    if not cleanup_result.get("deleted"):
        delete_error = cleanup_result.get("delete_error") or "AdsPower did not confirm profile deletion."
        store.update_task_state(
            task_id,
            status="FAILED",
            last_step=CLEANUP_DELETE_FAILED_STEP,
            error=cleanup_delete_failed_error(profile_id, delete_error, "stale_pending_profile"),
        )
        logger.warning(
            f"Task {task_id}: stale pending cleanup failed; row_key={row_key or '-'} "
            f"profile_id={profile_id} delete_error={delete_error}"
        )
        return "failed"

    if row_key:
        if _request_snapboard_adspower_id_update(row_key, ""):
            cleared = await _wait_for_snapboard_update(
                "/adspower_update/status",
                row_key,
                "stale AdsPower id clear",
                timeout_seconds=30,
            )
            if not cleared:
                logger.warning(f"Task {task_id}: stale SnapBoard AdsPower id clear was not confirmed for {row_key}.")
        else:
            logger.warning(f"Task {task_id}: could not request stale SnapBoard AdsPower id clear for {row_key}.")

    store.update_task_state(
        task_id,
        adspower_id="",
        adspower_profile_id="",
        adspower_name="",
        adspower_group="",
        tags=[],
    )
    task["adspower_id"] = ""
    task["adspower_profile_id"] = ""
    return "cleaned"


def _elapsed_label(started_at):
    return f"{max(0.0, time.monotonic() - float(started_at or time.monotonic())):.1f}s"


async def _request_snapboard_refresh(reason, timeout_seconds=None):
    normalized_reason = str(reason or "snapboard_recovery").strip() or "snapboard_recovery"
    started_at = time.monotonic()
    try:
        response = _post_local_api_response(
            "/snapboard_refresh/request",
            {"reason": normalized_reason},
            timeout=5,
        )
        if not response.ok:
            logger.warning(
                f"Could not request SnapBoard refresh for {normalized_reason}: HTTP {response.status_code}"
            )
            return False
        try:
            request_id = str(response.json().get("request_id") or "").strip()
        except Exception:
            request_id = ""
    except Exception as exc:
        logger.warning(f"Could not request SnapBoard refresh for {normalized_reason}: {exc}")
        return False

    deadline = time.monotonic() + max(1.0, float(timeout_seconds or SNAPBOARD_REFRESH_TIMEOUT_SECONDS or 45))
    poll_delay = max(0.25, float(SNAPBOARD_BRIDGE_POLL_SECONDS or 0.5))
    last_error = ""
    poll_count = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_delay)
        poll_count += 1
        try:
            params = {"request_id": request_id} if request_id else None
            response = _requests.get(
                f"{NYXIFY_LOCAL_API_URL}/snapboard_refresh/status",
                params=params,
                timeout=5,
            )
            if not response.ok:
                last_error = f"HTTP {response.status_code}"
                continue
            data = response.json()
            if data.get("done"):
                if data.get("success"):
                    logger.info(
                        f"[SNAPBOARD_TIMING] SnapBoard refresh for {normalized_reason} "
                        f"completed in {_elapsed_label(started_at)} (polls={poll_count})."
                    )
                    await asyncio.sleep(1.5)
                    return True
                last_error = str(data.get("error") or "SnapBoard refresh failed.").strip()
                logger.warning(
                    f"[SNAPBOARD_TIMING] SnapBoard refresh for {normalized_reason} failed "
                    f"after {_elapsed_label(started_at)}: {last_error}"
                )
                return False
            last_error = str(data.get("error") or "").strip()
        except Exception as exc:
            last_error = str(exc)

    logger.warning(
        f"[SNAPBOARD_TIMING] Timed out waiting for SnapBoard refresh for {normalized_reason}: "
        f"{_elapsed_label(started_at)}, {poll_count} polls"
        + (f", last_error={last_error}" if last_error else ".")
    )
    return False


async def _request_snapboard_value(
    row_key,
    *,
    request_path,
    status_path,
    value_key,
    label,
    timeout_seconds,
    force_new=None,
    extra_payload=None,
):
    normalized_row_key = str(row_key or "").strip()
    if not normalized_row_key:
        return ""

    payload = {"row_key": normalized_row_key}
    if force_new is not None:
        payload["force_new"] = bool(force_new)
    if extra_payload:
        payload.update({k: v for k, v in dict(extra_payload).items() if v is not None})

    def queue_request():
        request_started_at = time.monotonic()
        try:
            response = _post_local_api_response(request_path, payload, timeout=5)
            if not response.ok:
                logger.warning(
                    f"Could not request SnapBoard {label} fetch for {normalized_row_key}: "
                    f"HTTP {response.status_code}"
                )
                return False, request_started_at, 0.0
        except Exception as exc:
            logger.warning(f"Could not request SnapBoard {label} fetch for {normalized_row_key}: {exc}")
            return False, request_started_at, 0.0
        return True, request_started_at, time.monotonic() - request_started_at

    queued, current_request_started_at, request_elapsed = queue_request()
    if not queued:
        return ""

    overall_started_at = current_request_started_at
    deadline = time.monotonic() + max(1.0, float(timeout_seconds or 75))
    last_error = ""
    poll_count = 0
    poll_delay = max(0.1, float(SNAPBOARD_BRIDGE_POLL_SECONDS or 0.5))
    dispatch_timeout = max(1.0, float(SNAPBOARD_BRIDGE_DISPATCH_TIMEOUT_SECONDS or 8))
    refresh_attempted = False
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_delay)
        poll_count += 1
        try:
            response = _requests.get(
                f"{NYXIFY_LOCAL_API_URL}{status_path}",
                params={"row_key": normalized_row_key},
                timeout=5,
            )
            if not response.ok:
                last_error = f"HTTP {response.status_code}"
                continue
            data = response.json()
            if data.get("done"):
                value = str(data.get(value_key) or "").strip()
                elapsed = time.monotonic() - overall_started_at
                if value:
                    logger.info(
                        f"[SNAPBOARD_TIMING] Got {label} for {normalized_row_key} "
                        f"in {elapsed:.1f}s total (req={request_elapsed:.1f}s, polls={poll_count})"
                    )
                    return value
                last_error = str(data.get("error") or f"SnapBoard {label} fetch returned no value.").strip()
                logger.warning(
                    f"[SNAPBOARD_TIMING] SnapBoard {label} fetch returned empty for "
                    f"{normalized_row_key} after {_elapsed_label(overall_started_at)} "
                    f"(polls={poll_count}): {last_error}"
                )
                return ""
            last_error = str(data.get("error") or "").strip()
            if data.get("requested") and not data.get("dispatched"):
                try:
                    request_age = float(data.get("age_seconds") or 0.0)
                except Exception:
                    request_age = 0.0
                request_age = max(request_age, time.monotonic() - current_request_started_at)
                if request_age >= dispatch_timeout:
                    logger.warning(
                        f"[SNAPBOARD_TIMING] SnapBoard {label} fetch for {normalized_row_key} "
                        f"was not picked up by the SnapBoard bridge after {request_age:.1f}s; "
                        "refreshing SnapBoard before retrying."
                    )
                    if refresh_attempted:
                        return ""
                    refresh_attempted = True
                    refreshed = await _request_snapboard_refresh(
                        f"{label}_fetch_not_dispatched",
                        timeout_seconds=SNAPBOARD_REFRESH_TIMEOUT_SECONDS,
                    )
                    if not refreshed:
                        return ""
                    queued, current_request_started_at, request_elapsed = queue_request()
                    if not queued:
                        return ""
                    last_error = ""
                    continue
        except Exception as exc:
            last_error = str(exc)

    logger.warning(
        f"[SNAPBOARD_TIMING] Timed out waiting for SnapBoard {label} fetch for {normalized_row_key}"
        + f": {_elapsed_label(overall_started_at)}, {poll_count} polls"
        + (f", last_error={last_error}" if last_error else ".")
    )
    return ""


async def _request_snapboard_email(row_key, timeout_seconds=75, force_new=False):
    return await _request_snapboard_value(
        row_key,
        request_path="/email/request",
        status_path="/email/status",
        value_key="email",
        label="email",
        timeout_seconds=timeout_seconds,
        force_new=force_new,
    )


async def _request_snapboard_phone(row_key, timeout_seconds=120, force_new=False):
    return await _request_snapboard_value(
        row_key,
        request_path="/phone/request",
        status_path="/phone/status",
        value_key="phone",
        label="phone",
        timeout_seconds=timeout_seconds,
        force_new=force_new,
    )


async def _request_snapboard_sms(row_key, timeout_seconds=150, expected_phone=""):
    return await _request_snapboard_value(
        row_key,
        request_path="/sms/request",
        status_path="/sms/status",
        value_key="code",
        label="SMS",
        timeout_seconds=timeout_seconds,
        force_new=None,
        extra_payload={"phone": str(expected_phone or "").strip()},
    )


def _request_store_otp_for_row(store, row_key, expected_email=""):
    try:
        return store.request_otp_for_row(row_key, email=str(expected_email or "").strip())
    except TypeError:
        return store.request_otp_for_row(row_key)


async def _consume_snapboard_otp_from_store(
    store,
    row_key,
    task_id,
    *,
    timeout_seconds=90,
    poll_seconds=1,
    deadline=None,
    return_error=False,
):
    timeout_seconds = max(1.0, float(timeout_seconds or 90))
    poll_seconds = max(0.1, float(poll_seconds or 1))
    deadline = float(deadline) if deadline is not None else time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        error = ""
        if hasattr(store, "consume_otp_result"):
            result = store.consume_otp_result(row_key) or {}
            code = str(result.get("code") or "").strip()
            error = str(result.get("error") or "").strip()
        else:
            code = str(store.consume_otp_code(row_key) or "").strip()
        if code:
            logger.info(f"Received OTP for task {task_id} from main Chrome SnapBoard bridge.")
            return (code, "") if return_error else code
        if error:
            logger.warning(f"SnapBoard OTP bridge returned no code for task {task_id}: {error}")
            return ("", error) if return_error else ""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_seconds, remaining))

    return ("", "") if return_error else ""


def _otp_error_allows_refresh(error):
    normalized = str(error or "").strip().lower()
    if not normalized:
        return True
    terminal_markers = (
        "missing expected email",
        "row email does not match",
        "row phone does not match",
        "no pending email order",
        "no pending phone order",
        "no pending order",
    )
    if any(marker in normalized for marker in terminal_markers):
        return False
    return True


async def _request_snapboard_otp_from_store(
    store,
    row_key,
    *,
    task_id,
    timeout_seconds=90,
    poll_seconds=1,
    retry_timeout_seconds=None,
    expected_email="",
):
    normalized_row_key = str(row_key or "").strip()
    if not normalized_row_key:
        logger.warning(f"Task {task_id} is missing row_key for OTP retrieval.")
        return ""

    timeout_seconds = max(1.0, float(timeout_seconds or 90))
    deadline = time.monotonic() + timeout_seconds
    if retry_timeout_seconds is not None:
        try:
            retry_budget = max(0.1, float(retry_timeout_seconds))
        except Exception:
            retry_budget = 0.1
        first_deadline = min(deadline, time.monotonic() + max(0.1, timeout_seconds - retry_budget))
    else:
        first_deadline = min(
            deadline,
            time.monotonic() + max(1.0, float(SNAPBOARD_OTP_INITIAL_WAIT_SECONDS or 35)),
        )
    logger.info(f"Task {task_id} requesting OTP from SnapBoard bridge.")
    _request_store_otp_for_row(store, normalized_row_key, expected_email)
    code, error = await _consume_snapboard_otp_from_store(
        store,
        normalized_row_key,
        task_id,
        poll_seconds=poll_seconds,
        deadline=first_deadline,
        return_error=True,
    )
    if code:
        return code

    store.clear_otp_request(normalized_row_key)
    if error and not _otp_error_allows_refresh(error):
        return ""
    remaining_after_first = deadline - time.monotonic()
    if remaining_after_first <= max(0.1, float(poll_seconds or 1)):
        logger.warning(f"Timed out waiting for OTP for task {task_id}.")
        return ""

    logger.warning(f"Timed out waiting for OTP for task {task_id}; refreshing SnapBoard before retrying.")
    refreshed = await _request_snapboard_refresh(
        "otp_timeout",
        timeout_seconds=min(float(SNAPBOARD_REFRESH_TIMEOUT_SECONDS or 45), max(1.0, remaining_after_first)),
    )
    if not refreshed:
        return ""
    remaining_after_refresh = deadline - time.monotonic()
    if remaining_after_refresh <= 0:
        return ""

    logger.info(f"Task {task_id} retrying OTP after SnapBoard refresh.")
    _request_store_otp_for_row(store, normalized_row_key, expected_email)
    retry_timeout = retry_timeout_seconds
    if retry_timeout is None:
        retry_timeout = min(
            max(0.1, remaining_after_refresh),
            float(SNAPBOARD_OTP_REFRESH_RETRY_SECONDS or 75),
        )
    retry_deadline = min(deadline, time.monotonic() + max(0.1, float(retry_timeout or 0.1)))
    code = await _consume_snapboard_otp_from_store(
        store,
        normalized_row_key,
        task_id,
        poll_seconds=poll_seconds,
        deadline=retry_deadline,
    )
    if code:
        return code

    store.clear_otp_request(normalized_row_key)
    logger.warning(f"Timed out waiting for OTP for task {task_id} after SnapBoard refresh.")
    return ""


def _build_final_adspower_name(final_username):
    normalized_username = str(final_username or "").strip()
    if not normalized_username:
        return ""
    return f"Snapchat: {normalized_username}"


async def _run_proxy_check(adspower, proxy_value, proxy_checker_enabled, proxy_check_id=""):
    if proxy_checker_enabled:
        return await asyncio.to_thread(
            adspower.check_proxy_via_adspower,
            proxy_check_id or proxy_value,
            20,
            True,
        )
    return await asyncio.to_thread(adspower.test_proxy_connection, proxy_value)


async def _rotate_proxy_until_usable(
    task_id,
    task_row_key,
    store,
    adspower,
    proxy_value,
    blocked_proxies,
    proxy_checker_enabled,
    proxy_check_id="",
    max_rotation_attempts=MAX_PROXY_ROTATION_ATTEMPTS,
):
    last_message = ""
    attempt = 0

    while True:
        runtime_config = load_nyxify_config()
        active_blocked_proxies = (
            runtime_config.get("blocked_proxies", blocked_proxies)
            if runtime_config.get("proxy_blocker_enabled", True)
            else []
        )
        active_proxy_checker_enabled = runtime_config.get("proxy_checker_enabled", proxy_checker_enabled)

        is_blocked = bool(active_blocked_proxies and _is_proxy_banned(proxy_value, active_blocked_proxies))
        if not is_blocked:
            proxy_check = await _run_proxy_check(
                adspower,
                proxy_value,
                active_proxy_checker_enabled,
                proxy_check_id or proxy_value,
            )
            if proxy_check.get("ok"):
                return proxy_value, proxy_check
            reason = "check_failed"
            last_message = str(proxy_check.get("message") or "Proxy check failed.").strip()
            logger.info(f"Task {task_id}: proxy check failed ({last_message}), rotating proxy")
        else:
            reason = "blocked"
            last_message = "Proxy matches a blocked pattern."
            logger.info(f"Task {task_id}: proxy {proxy_value[:60]!r} is blocked, rotating proxy")

        _record_proxy_ranking_event(proxy_value, "retry", reason=reason)

        attempt += 1
        if attempt > max_rotation_attempts:
            raise ValueError(
                f"{last_message} (proxy could not be rotated to a usable value after "
                f"{max_rotation_attempts} attempts; reason={reason})"
            )

        old_proxy = str(proxy_value or "").strip()
        # Ask SnapBoard to click rotate a few times: escaping a blocked subnet or
        # a dead proxy often needs more than one swap, and a single click that
        # lands on another bad proxy would otherwise burn an attempt.
        new_proxy = await _request_snapboard_rotation(task_row_key, timeout_seconds=55, max_clicks=3)
        if new_proxy:
            store.update_task_proxy(task_id, new_proxy)
            proxy_value = new_proxy
            proxy_check_id = new_proxy
            logger.info(
                f"Task {task_id}: SnapBoard rotated proxy on attempt {attempt}/{max_rotation_attempts} "
                f"(reason={reason}): {old_proxy[:40]!r} -> {proxy_value[:40]!r}"
            )
            continue

        # No new proxy came back (rotate button missing on SnapBoard, or the
        # proxy cell never changed). Back off briefly and log why, instead of
        # hammering SnapBoard and looking like a generic stall.
        logger.warning(
            f"Task {task_id}: SnapBoard proxy rotation attempt {attempt}/{max_rotation_attempts} "
            f"(reason={reason}) did not return a new proxy; retrying."
        )
        await asyncio.sleep(min(5.0, 1.0 + attempt * 0.5))


async def process_task(task, store, adspower):
    task_id = task["id"]
    proxy_value = str(task.get("proxy_address") or task.get("ip_address") or "").strip()
    username = str(task.get("username") or "").strip()
    email = str(task.get("email") or "").strip()
    signup_password = str(task.get("password") or "").strip()
    task_adspower_id = str(task.get("adspower_id") or "").strip()
    config = load_nyxify_config()
    blocked_proxies = config.get("blocked_proxies", [])
    proxy_blocker_enabled = config.get("proxy_blocker_enabled", True)
    proxy_checker_enabled = config.get("proxy_checker_enabled", True)
    model_tag = resolve_model_name(str(task.get("model") or "").strip())
    # Display tag should always be Title-cased (e.g. "willow" -> "Willow")
    # so AdsPower groups stay consistent regardless of how SnapBoard
    # capitalises the model name.
    display_model_tag = (model_tag[:1].upper() + model_tag[1:]) if model_tag else ""
    # AdsPower tags are optional: when the off-switch is set, profiles are
    # created with no tags at all (tag sync is skipped downstream because the
    # list is empty).
    adspower_tags_enabled = bool(config.get("adspower_tags_enabled", True))
    if adspower_tags_enabled:
        tags = [config.get("tag_one"), display_model_tag or config.get("tag_two")]
        tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    else:
        tags = []
    temporary_name = str(config.get("temporary_profile_name") or "Snapchat:").strip()
    adspower_group = str(config.get("adspower_group") or "").strip()
    extension_category = str(config.get("extension_category") or "Snap").strip()
    push_adspower_id_enabled = bool(config.get("push_adspower_id_enabled", True))
    full_auto_mode_enabled = bool(config.get("full_auto_mode_enabled", False))
    continuous_mode_enabled = bool(config.get("continuous_mode_enabled", False))
    keep_profile_open_after_signup = bool(config.get("keep_profile_open_after_signup", False))
    names_dir = _resolve_names_dir(config)
    created = None
    final_adspower_name = ""
    final_username_applied = False
    final_profile_renamed = False
    final_profile_rename_pending = False
    snapboard_username_synced = False
    snapboard_adspower_id_synced = not push_adspower_id_enabled
    snapboard_adspower_name_synced = False
    close_profile_after_completion = False
    close_profile_id = ""
    last_step = "checking_proxy"
    completion_error = ""
    playwright_instance = None
    signup_completed = False

    try:
        store.update_task_state(task_id, status="RUNNING", last_step=last_step, error="")

        missing_fields = _missing_task_fields(username)
        if missing_fields:
            waiting_step = _build_waiting_step(missing_fields)
            store.update_task_state(
                task_id,
                status="PENDING",
                last_step=waiting_step,
                error="",
            )
            logger.info(
                f"Task {task_id} is waiting for row data before AdsPower creation: missing={missing_fields}"
            )
            return

        task_row_key = str(task.get("row_key") or "").strip()
        stale_cleanup_state = await _cleanup_stale_pending_profile(task_id, task, store, adspower)
        if stale_cleanup_state == "failed":
            return
        if stale_cleanup_state == "cleaned":
            task_adspower_id = ""

        async def email_fetcher(force_new=False):
            nonlocal email
            if _is_valid_email(email) and not force_new:
                return email
            if not task_row_key:
                logger.warning(f"Task {task_id} is missing row_key for email retrieval.")
                return ""
            store.update_task_state(task_id, last_step="fetching_replacement_email" if force_new else "fetching_email")
            # Replacement orders wait out SnapBoard's ~60s redo cooldown in the
            # content script, so give the fetch room past that (cooldown + the
            # ~45s appear window + refresh/relogin retries) before timing out.
            fetched_email = await _request_snapboard_email(
                task_row_key,
                timeout_seconds=75,
                force_new=force_new,
            )
            if fetched_email:
                email = fetched_email
                store.update_task_email(task_row_key, fetched_email)
                if force_new:
                    logger.info(f"Task {task_id}: fetched replacement verification email from SnapBoard.")
                else:
                    logger.info(f"Task {task_id}: fetched verification email from SnapBoard.")
            return fetched_email

        async def otp_fetcher(email=""):
            row_key = str(task.get("row_key") or "").strip()
            return await _request_snapboard_otp_from_store(
                store,
                row_key,
                task_id=task_id,
                expected_email=str(email or "").strip(),
            )

        last_verification_phone = ""

        async def phone_fetcher(force_new=False):
            nonlocal last_verification_phone
            if not task_row_key:
                logger.warning(f"Task {task_id} is missing row_key for phone retrieval.")
                return ""
            store.update_task_state(
                task_id,
                last_step="fetching_phone_verification",
            )
            # Replacement numbers wait out SnapBoard's ~60s redo cooldown in the
            # content script, so allow past that (cooldown + appear window +
            # refresh/relogin retries) before timing out.
            phone = await _request_snapboard_phone(
                task_row_key,
                timeout_seconds=75,
                force_new=force_new,
            )
            if phone:
                last_verification_phone = str(phone or "").strip()
                logger.info(f"Task {task_id}: fetched verification phone from SnapBoard.")
            return phone

        async def sms_fetcher(phone=""):
            nonlocal last_verification_phone
            if not task_row_key:
                logger.warning(f"Task {task_id} is missing row_key for SMS retrieval.")
                return ""
            expected_phone = str(phone or "").strip() or last_verification_phone
            store.update_task_state(task_id, last_step="fetching_sms_otp")
            logger.info(f"Task {task_id} requesting SMS OTP from SnapBoard bridge.")
            code = await _request_snapboard_sms(
                task_row_key,
                timeout_seconds=90,
                expected_phone=expected_phone,
            )
            if code:
                logger.info(f"Received SMS OTP for task {task_id} from SnapBoard bridge.")
            return code

        proxy_value, proxy_check = await _rotate_proxy_until_usable(
            task_id=task_id,
            task_row_key=task_row_key,
            store=store,
            adspower=adspower,
            proxy_value=proxy_value,
            blocked_proxies=blocked_proxies if proxy_blocker_enabled else [],
            proxy_checker_enabled=proxy_checker_enabled,
            proxy_check_id=task_adspower_id or proxy_value,
        )

        # The proxy is now validated and about to be used for a real creation —
        # count the use so its subnet's ranking reflects every time it ran.
        _record_proxy_ranking_event(proxy_value, "use")

        last_step = "creating_adspower_profile"
        store.update_task_state(task_id, last_step=last_step)

        def gui_proxy_rotator(**_kwargs):
            nonlocal proxy_value
            if not task_row_key:
                logger.warning(
                    f"Task {task_id}: AdsPower GUI proxy check failed, but row_key is missing; "
                    "cannot request SnapBoard proxy rotation."
                )
                return ""
            store.update_task_state(
                task_id,
                last_step="refreshing_proxy_after_gui_proxy_check_failed",
            )
            old_proxy = str(proxy_value or _kwargs.get("current_proxy") or "").strip()
            new_proxy = _request_snapboard_rotation_sync(
                task_row_key,
                timeout_seconds=55,
                max_clicks=1,
            )
            new_proxy = str(new_proxy or "").strip()
            if not new_proxy:
                logger.warning(
                    f"Task {task_id}: SnapBoard did not return a rotated proxy after "
                    "AdsPower GUI proxy check failure."
                )
                return ""
            proxy_value = new_proxy
            store.update_task_proxy(task_id, new_proxy)
            logger.info(
                f"Task {task_id}: rotated proxy after AdsPower GUI check failure: "
                f"{old_proxy[:40]!r} -> {new_proxy[:40]!r}"
            )
            return new_proxy

        created = await asyncio.to_thread(
            adspower.create_profile,
            name=temporary_name,
            proxy_value=proxy_value,
            group_reference=adspower_group,
            tags=tags,
            user_proxy_config=proxy_check.get("proxy"),
            extension_category_reference=extension_category,
            proxy_rotator=gui_proxy_rotator,
        )

        tag_result = created.get("tag_confirmation") or {
            "confirmed": True,
            "message": "No tags requested.",
        }
        if tags and not tag_result.get("confirmed"):
            logger.warning(
                "Continuing Nyxify task after AdsPower tag confirmation warning: "
                f"{tag_result.get('message') or 'Create-time AdsPower tags were not confirmed.'}"
            )

        store.update_task_state(
            task_id,
            adspower_profile_id=created.get("profile_id"),
            adspower_name=created.get("name"),
            adspower_group=adspower_group,
            tags=tags,
        )
        final_adspower_name = str(created.get("name") or "").strip()
        created_profile_id = str(created.get("profile_id") or "").strip()
        if (
            push_adspower_id_enabled
            and task_row_key
            and created_profile_id
        ):
            if _request_snapboard_adspower_id_update(task_row_key, created_profile_id):
                logger.info(
                    f"Task {task_id}: requested early SnapBoard AdsPower id sync for {created_profile_id}."
                )

        last_step = "opening_profile"
        store.update_task_state(task_id, last_step=last_step)

        # Extension turn-off during account creation is now opt-in and OFF by
        # default (users asked to stop disabling extensions while creating the
        # account). The browser open + context attach still happen either way.
        disable_extensions_enabled = bool(config.get("disable_extensions_enabled", False))

        # keep_playwright=True — we own the playwright instance and use it for signup too
        cleanup_result = await disable_profile_extensions(
            adspower, created.get("profile_id"), logger,
            keep_open=True, keep_playwright=True, open_signup=False,
            disable_extensions=disable_extensions_enabled,
        )

        playwright_instance = cleanup_result.get("playwright_instance")
        context = cleanup_result.get("context")

        last_step = "extensions_disabled" if disable_extensions_enabled else "extension_disable_skipped"
        store.update_task_state(task_id, last_step=last_step)

        if context is None:
            raise RuntimeError("AdsPower browser context was missing after profile open.")

        # whox.com trust-score gate — runs before cookie warm-up. A deep score
        # below the configured threshold means this proxy/profile is not clean
        # enough to proceed, so we raise a marker error that routes into the
        # standard cleanup+retry path (delete profile, clear SnapBoard id,
        # requeue the row to create from scratch).
        whox_check_enabled = bool(config.get("whox_check_enabled", True))
        if whox_check_enabled:
            last_step = "whox_trust_check"
            store.update_task_state(task_id, last_step=last_step)
            whox_min_score = int(config.get("whox_min_trust_score", 70) or 70)
            whox_url = str(config.get("whox_url") or "").strip() or None
            whox_result = await run_whox_trust_check(
                context,
                logger,
                created.get("profile_id"),
                whox_min_score,
                whox_url=whox_url,
            )
            if not whox_result.get("passed"):
                score_value = whox_result.get("score")
                raise RuntimeError(
                    "whox_trust_score_below_threshold: deep trust score "
                    f"{score_value if score_value is not None else 'unknown'} "
                    f"is below the configured threshold {whox_result.get('threshold')}."
                )

        last_step = "cookie_warmup"
        store.update_task_state(task_id, last_step=last_step)
        await warm_ads_profile_cookies(context, logger, created.get("profile_id"))
        await clear_unrelated_tabs_except_adspower(context, logger, created.get("profile_id"))

        last_step = "signup_handoff"
        store.update_task_state(task_id, last_step=last_step)

        signup_result = await open_snapchat_signup(context, logger, created.get("profile_id"))
        signup_page = signup_result.get("page")
        signup_url = signup_result.get("url", "")

        logger.info(
            f"Nyxify signup handoff for task {task_id}: "
            f"profile_id={created.get('profile_id')}, signup_url={signup_url!r}, "
            f"has_page={bool(signup_page)}, has_context={bool(context)}"
        )

        if signup_page is not None:
            def _on_signup_page_close():
                logger.warning(
                    f"Nyxify signup page closed for task {task_id} "
                    f"(profile_id={created.get('profile_id')})."
                )

            signup_page.on("close", _on_signup_page_close)

        if signup_url and signup_page and context:
            last_step = "signup_opened"
            store.update_task_state(task_id, last_step=last_step)

            last_step = "running_signup"
            store.update_task_state(task_id, last_step=last_step)

            logger.info(
                f"Starting signup automation for task {task_id}: "
                f"profile_id={created.get('profile_id')}, current_url={signup_page.url}"
            )

            # Captured by the callback so rename + push fire the moment the
            # username becomes visible — including the case where the OTP
            # auto-flow fails and the operator finishes verification by hand.
            applied_usernames: set[str] = set()

            async def _apply_final_username(detected_username: str):
                nonlocal username
                nonlocal final_adspower_name
                nonlocal final_username_applied, final_profile_renamed
                nonlocal final_profile_rename_pending
                nonlocal snapboard_username_synced, snapboard_adspower_id_synced
                nonlocal snapboard_adspower_name_synced
                normalized = str(detected_username or "").strip()
                if not normalized or normalized in applied_usernames:
                    return
                applied_usernames.add(normalized)
                previous_username = username
                final_username_applied = True
                final_profile_renamed = False
                snapboard_username_synced = False
                snapboard_adspower_name_synced = False
                username = normalized
                task["username"] = normalized

                profile_id_value = str(created.get("profile_id") or "").strip()
                if profile_id_value:
                    renamed_profile_name = _build_final_adspower_name(normalized)
                    if renamed_profile_name:
                        final_adspower_name = renamed_profile_name
                        final_profile_rename_pending = True
                        final_profile_renamed = False
                        store.update_task_state(
                            task_id,
                            adspower_name=final_adspower_name,
                        )
                        if continuous_mode_enabled:
                            rename_timing = "before Nyx handoff"
                        elif keep_profile_open_after_signup:
                            rename_timing = "without closing profile"
                        else:
                            rename_timing = "after close"
                        logger.info(
                            f"Task {task_id}: scheduled AdsPower profile {profile_id_value} "
                            f"rename to {final_adspower_name!r} {rename_timing}."
                        )

                row_key_value = str(task.get("row_key") or "").strip()
                if row_key_value:
                    store.update_task_username(row_key_value, normalized)
                    if _request_snapboard_username_update(row_key_value, normalized):
                        snapboard_username_synced = await _wait_for_snapboard_update(
                            "/username_update/status",
                            row_key_value,
                            "username",
                        )
                    if normalized:
                        snapboard_adspower_name = (
                            final_adspower_name
                            or _build_final_adspower_name(normalized)
                            or normalized
                        )
                        if _request_snapboard_adspower_name_update(row_key_value, snapboard_adspower_name):
                            snapboard_adspower_name_synced = await _wait_for_snapboard_update(
                                "/adspower_name_update/status",
                                row_key_value,
                                "AdsPower name",
                            )
                else:
                    snapboard_adspower_name_synced = bool(normalized)
                if not _same_username(normalized, previous_username):
                    logger.info(
                        f"Task {task_id}: updating SnapBoard username from {previous_username!r} to {normalized!r}."
                    )
                else:
                    logger.info(
                        f"Task {task_id}: syncing final Snapchat username {normalized!r} back to SnapBoard."
                    )

            async def _request_signup_retry_username(current_username_value: str, reason: str = ""):
                nonlocal username
                if not full_auto_mode_enabled:
                    return ""

                retry_username = await _request_next_full_auto_username(
                    task_row_key,
                    model_tag,
                    current_username=current_username_value,
                    reason=reason or "signup_username_already_taken",
                )
                if retry_username:
                    username = retry_username
                return retry_username

            async def _rename_final_profile_if_pending(reason: str = ""):
                nonlocal final_adspower_name
                nonlocal completion_error
                nonlocal final_profile_renamed, final_profile_rename_pending
                profile_id_value = str(created.get("profile_id") or "").strip()
                if final_profile_renamed:
                    return True
                if not final_profile_rename_pending:
                    return False
                if not profile_id_value or not final_adspower_name:
                    return False

                try:
                    renamed = await asyncio.to_thread(
                        adspower.rename_profile,
                        profile_id_value,
                        final_adspower_name,
                    )
                except Exception as exc:
                    completion_error = str(exc) or f"{type(exc).__name__}: {exc!r}"
                    logger.warning(
                        f"Task {task_id}: could not rename AdsPower profile {profile_id_value} "
                        f"to {final_adspower_name!r} before Nyx handoff: {completion_error}"
                    )
                    return False
                final_adspower_name = str(renamed.get("name") or final_adspower_name).strip()
                created["name"] = final_adspower_name
                final_profile_renamed = True
                final_profile_rename_pending = False
                store.update_task_state(
                    task_id,
                    adspower_name=final_adspower_name,
                )
                suffix = f" {reason}" if reason else ""
                logger.info(
                    f"Task {task_id}: renamed AdsPower profile {profile_id_value} "
                    f"to {final_adspower_name!r}{suffix}."
                )
                return True

            async def _release_playwright_before_nyx_handoff():
                nonlocal playwright_instance
                profile_id_value = str(created.get("profile_id") or "").strip()
                if playwright_instance is None:
                    return
                instance = playwright_instance
                playwright_instance = None

                async def _stop_playwright_connection():
                    try:
                        await instance.stop()
                        logger.info(
                            f"Task {task_id}: released Nyxify browser connection before Nyx handoff "
                            f"for AdsPower profile {profile_id_value}."
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Task {task_id}: could not release Nyxify browser connection before Nyx handoff "
                            f"for AdsPower profile {profile_id_value}: {exc}"
                        )

                timeout_seconds = max(0.1, float(PLAYWRIGHT_RELEASE_TIMEOUT_SECONDS or 2))
                stop_task = asyncio.create_task(_stop_playwright_connection())
                try:
                    await asyncio.wait_for(asyncio.shield(stop_task), timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Task {task_id}: Playwright release for AdsPower profile {profile_id_value} "
                        f"is still running after {timeout_seconds:g}s; continuing Nyx handoff."
                    )

            async def _signup_progress(step: str):
                nonlocal last_step
                normalized_step = str(step or "").strip()
                if not normalized_step or normalized_step == last_step:
                    return
                last_step = normalized_step
                store.update_task_state(task_id, last_step=last_step)

            creds = await perform_snapchat_signup(
                signup_page=signup_page,
                model=model_tag,
                username=username,
                email=email,
                names_dir=names_dir,
                logger=logger,
                profile_id=str(task_id),
                otp_fetcher=otp_fetcher,
                password=signup_password,
                username_detected_callback=_apply_final_username,
                email_fetcher=email_fetcher,
                username_retry_provider=_request_signup_retry_username if full_auto_mode_enabled else None,
                progress_callback=_signup_progress,
                phone_fetcher=phone_fetcher,
                sms_fetcher=sms_fetcher,
            )

            signup_error = str(creds.get("error") or "").strip()
            if signup_error:
                logger.warning(f"Signup flow warning for task {task_id}: {signup_error}")

            final_username = str(creds.get("final_username") or "").strip()
            # Safety net: if the callback never fired (e.g. signup_flow set
            # final_username through a non-callback code path), still apply.
            if final_username:
                await _apply_final_username(final_username)
                signup_completed = True
            elif signup_error:
                raise RuntimeError(signup_error)

            if final_username:
                last_step = "signup_complete"
            elif creds.get("otp_entered") or creds.get("reached_verification"):
                last_step = "awaiting_welcome_username"
            else:
                last_step = "signup_form_submitted"

            if (
                continuous_mode_enabled
                and last_step == "signup_complete"
                and final_username_applied
                and final_profile_rename_pending
            ):
                await _release_playwright_before_nyx_handoff()
                store.update_task_state(task_id, last_step="renaming_profile_for_nyx")
                if await _rename_final_profile_if_pending("before Nyx handoff"):
                    store.update_task_state(task_id, last_step=last_step)
                else:
                    # The rename is AdsPower bookkeeping only — the Snapchat
                    # account already exists. Record the problem (visible on the
                    # row) but do NOT block the id push or the Nyx handoff below:
                    # Bitmoji creation must continue once the account is real.
                    store.update_task_state(
                        task_id, last_step="profile_rename_failed", error=completion_error
                    )

            if (
                keep_profile_open_after_signup
                and not continuous_mode_enabled
                and last_step == "signup_complete"
                and final_username_applied
                and final_profile_rename_pending
            ):
                store.update_task_state(task_id, last_step="renaming_profile_keep_open")
                if await _rename_final_profile_if_pending("without closing profile"):
                    store.update_task_state(task_id, last_step=last_step)
                else:
                    # The signup is already complete; keep the row done and
                    # surface the AdsPower bookkeeping issue without closing.
                    store.update_task_state(
                        task_id, last_step="profile_rename_failed", error=completion_error
                    )
                    store.update_task_state(task_id, last_step=last_step)

            if (
                push_adspower_id_enabled
                and last_step == "signup_complete"
                and final_username_applied
                and (final_profile_renamed or continuous_mode_enabled)
            ):
                profile_id_value = str(created.get("profile_id") or "").strip()
                row_key_value = str(task.get("row_key") or "").strip()
                if row_key_value and profile_id_value:
                    if _request_snapboard_adspower_id_update(row_key_value, profile_id_value):
                        snapboard_adspower_id_synced = await _wait_for_snapboard_update(
                            "/adspower_update/status",
                            row_key_value,
                            "AdsPower id",
                        )

            if (
                continuous_mode_enabled
                and last_step == "signup_complete"
                and final_username_applied
            ):
                profile_id_value = str(created.get("profile_id") or "").strip()
                handoff_model = model_tag or str(task.get("model") or "").strip()
                await _release_playwright_before_nyx_handoff()
                store.update_task_state(task_id, last_step="queueing_nyx")
                handoff_result = await asyncio.to_thread(
                    enqueue_profile_for_nyx,
                    profile_id_value,
                    handoff_model,
                    logger,
                    username=final_username,
                    password=signup_password,
                )
                if handoff_result.get("ok"):
                    last_step = "queued_for_nyx"
                else:
                    last_step = "nyx_handoff_failed"
                    completion_error = str(
                        handoff_result.get("error")
                        or handoff_result.get("api_error")
                        or "Could not queue profile for Nyx."
                    )
                    logger.warning(
                        f"Task {task_id}: Nyx handoff failed for AdsPower profile {profile_id_value}: "
                        f"{completion_error}"
                    )
        else:
            logger.warning(f"Signup page or context missing after extension cleanup for task {task_id}.")

        completion_status = "DONE" if last_step in {"signup_complete", "queued_for_nyx"} else "FAILED"
        if completion_status != "DONE" and not completion_error:
            completion_error = "Signup did not reach the Snapchat welcome page with a final username."
        if (
            completion_status != "DONE"
            and created
            and _should_cleanup_failed_created_profile(last_step, completion_error)
        ):
            raise RuntimeError(completion_error)

        close_profile_id = str(created.get("profile_id") or "").strip()
        close_profile_after_completion = bool(
            close_profile_id
            and last_step == "signup_complete"
            and not continuous_mode_enabled
            and not keep_profile_open_after_signup
            and final_username_applied
            and (final_profile_renamed or final_profile_rename_pending)
        )
        # While the close+rename bookkeeping (in the finally block) is still to
        # run, publish the row as DONE/"closing_profile" instead of the ready
        # step: the Nyx guard holds on it, so Nyx never opens/attaches to this
        # profile in the middle of our close — that race is what killed fresh
        # Bitmoji runs with manual_terminate. The step becomes "profile_closed"
        # (or "profile_close_failed") once the bookkeeping finishes.
        stored_last_step = "closing_profile" if close_profile_after_completion else last_step

        store.update_task_state(
            task_id,
            status=completion_status,
            last_step=stored_last_step,
            error=completion_error,
            adspower_profile_id=created.get("profile_id"),
            adspower_name=final_adspower_name or str(created.get("name") or "").strip(),
            adspower_group=adspower_group,
            tags=tags,
        )
        if signup_completed:
            _play_completion_sound()
        if (
            close_profile_id
            and keep_profile_open_after_signup
            and last_step == "signup_complete"
            and final_username_applied
        ):
            logger.info(
                f"Task {task_id}: leaving AdsPower profile {close_profile_id} open because "
                "Keep Profile Open is enabled."
            )
        elif close_profile_id and not close_profile_after_completion:
            logger.info(
                f"Task {task_id}: leaving AdsPower profile {close_profile_id} open because final setup "
                f"is not fully confirmed. last_step={last_step!r}, username_applied={final_username_applied}, "
                f"profile_renamed={final_profile_renamed}, username_synced={snapboard_username_synced}, "
                f"adspower_id_synced={snapboard_adspower_id_synced}, "
                f"adspower_name_synced={snapboard_adspower_name_synced}"
            )
        elif close_profile_id and (
            not snapboard_username_synced
            or not snapboard_adspower_id_synced
            or not snapboard_adspower_name_synced
        ):
            logger.warning(
                f"Task {task_id}: closing AdsPower profile {close_profile_id} after completed signup and "
                "local rename even though one or more SnapBoard sync confirmations were missing. "
                f"username_synced={snapboard_username_synced}, "
                f"adspower_id_synced={snapboard_adspower_id_synced}, "
                f"adspower_name_synced={snapboard_adspower_name_synced}"
            )
    except Exception as exc:
        error_message = str(exc) or f"{type(exc).__name__}: {exc!r}"
        failure_last_step = _classify_failure_last_step(created, last_step, error_message)
        # Proxy-ranking telemetry (best-effort): attribute account bans and
        # creation failures to the proxy's subnet, skipping host-side infra
        # errors that have nothing to do with the proxy.
        if failure_last_step != "adspower_accessibility_permission_missing":
            low_error = error_message.lower()
            if (
                "banned" in low_error
                or "authorization error" in low_error
                or failure_last_step == "account_creation_blocked"
            ):
                _record_proxy_ranking_event(proxy_value, "ban")
            else:
                _record_proxy_ranking_event(proxy_value, "creation_fail")
        should_retry = _should_cleanup_failed_created_profile(failure_last_step, error_message)
        if created and should_retry:
            await _cleanup_failed_created_profile(
                task_id,
                task,
                store,
                adspower,
                created,
                failure_last_step,
                error_message,
            )
            logger.info(
                f"Task {task_id}: cleanup+retry after post-creation failure ({failure_last_step}): {error_message[:120]}"
            )
        elif not created and should_retry:
            store.update_task_state(
                task_id,
                status="PENDING",
                last_step=failure_last_step,
                error="",
                adspower_id="",
                adspower_profile_id="",
                adspower_name="",
                adspower_group="",
                tags=[],
            )
            logger.info(
                f"Task {task_id}: reset to PENDING after pre-creation failure ({failure_last_step}): {error_message[:120]}"
            )
        else:
            store.update_task_state(
                task_id,
                status="FAILED",
                last_step=failure_last_step,
                error=error_message,
                adspower_profile_id=created.get("profile_id") if created else "",
                adspower_name=created.get("name") if created else "",
                adspower_group=adspower_group if created else "",
                tags=tags if created else [],
            )
            logger.error(f"Nyxify task failed for {task.get('row_key')}: {error_message}")
    finally:
        store.clear_otp_request(str(task.get("row_key") or "").strip())
        if playwright_instance is not None:
            try:
                await playwright_instance.stop()
            except Exception:
                pass
        if close_profile_after_completion and close_profile_id:
            try:
                await asyncio.to_thread(adspower.close_profile, close_profile_id)
                logger.info(f"Task {task_id}: closed completed AdsPower profile {close_profile_id}.")
                if final_profile_rename_pending and final_adspower_name:
                    try:
                        renamed = await asyncio.to_thread(
                            adspower.rename_profile,
                            close_profile_id,
                            final_adspower_name,
                        )
                        final_adspower_name = str(renamed.get("name") or final_adspower_name).strip()
                        created["name"] = final_adspower_name
                        final_profile_renamed = True
                        final_profile_rename_pending = False
                        store.update_task_state(
                            task_id,
                            adspower_name=final_adspower_name,
                        )
                        logger.info(
                            f"Task {task_id}: renamed closed AdsPower profile {close_profile_id} "
                            f"to {final_adspower_name!r}."
                        )
                        if (
                            push_adspower_id_enabled
                            and not snapboard_adspower_id_synced
                            and task_row_key
                        ):
                            if _request_snapboard_adspower_id_update(task_row_key, close_profile_id):
                                snapboard_adspower_id_synced = await _wait_for_snapboard_update(
                                    "/adspower_update/status",
                                    task_row_key,
                                    "AdsPower id",
                                )
                    except Exception as rename_exc:
                        logger.warning(
                            f"Task {task_id}: could not rename closed AdsPower profile "
                            f"{close_profile_id} to {final_adspower_name!r}: {rename_exc}"
                        )
                store.update_task_state(task_id, last_step="profile_closed")
            except Exception as exc:
                logger.warning(f"Task {task_id}: could not close AdsPower profile {close_profile_id}: {exc}")
                # The profile is still open — record it so the Nyx guard stops
                # holding (Nyx attaches to the open browser directly) instead of
                # waiting for a "profile_closed" that will never come.
                try:
                    store.update_task_state(task_id, last_step="profile_close_failed")
                except Exception:
                    pass


async def main():
    logger.info("====================================")
    logger.info(" NYXIFY RUNNER STARTED ")
    logger.info("====================================")

    runner_lock = _RunnerLock(RUNNER_LOCK_HOST, RUNNER_LOCK_PORT)
    if not runner_lock.acquire():
        logger.warning(
            f"Another Nyxify runner already holds the lock on {RUNNER_LOCK_HOST}:{RUNNER_LOCK_PORT}. "
            "Exiting this duplicate process."
        )
        return

    try:
        store = NyxifyTaskStore(db_path=TASK_DB_PATH)
        adspower = AdsPowerManager(ui_assume_presearch=True)

        # Ctrl+F8 is owned by the bridge so it can reuse dashboard Start/Stop
        # actions and can start this runner even when no runner process exists.

        # Orphan recovery: any row still RUNNING at startup belongs to a previous
        # run that crashed/stopped (the RunnerLock guarantees no live runner owns
        # it). Without this it would sit RUNNING forever — claim_pending_tasks only
        # claims PENDING — which is exactly how a stuck "creating_adspower_profile"
        # row never gets created after a restart.
        try:
            requeued = store.reset_orphaned_running_tasks()
            if requeued:
                logger.info(f"Requeued {requeued} orphaned RUNNING Nyxify task(s) to PENDING after restart.")
        except Exception as exc:
            logger.warning(f"Could not requeue orphaned RUNNING Nyxify tasks at startup: {exc}")

        active_tasks: set[asyncio.Task] = set()

        while True:
            try:
                if os.path.exists(PAUSE_FILE):
                    await asyncio.sleep(2)
                    continue

                # Reap any tasks that finished since last tick
                finished = {t for t in active_tasks if t.done()}
                for t in finished:
                    active_tasks.discard(t)
                    try:
                        t.result()
                    except Exception as task_exc:
                        logger.error(f"Nyxify task raised an unhandled exception: {task_exc}")
                        traceback.print_exc()

                config = load_nyxify_config()
                continuous_mode_enabled = bool(config.get("continuous_mode_enabled", False))
                configured_max_parallel = max(1, int(config.get("max_parallel_profiles") or 1))
                max_parallel = 1 if continuous_mode_enabled else configured_max_parallel
                open_slots = max_parallel - len(active_tasks)

                if open_slots > 0:
                    if continuous_mode_enabled and _continuous_nyx_handoff_active():
                        marked = _mark_pending_tasks_waiting_for_continuous_nyx(store)
                        suffix = f" Marked {marked} ready row(s) waiting." if marked else ""
                        logger.info(
                            "Continuous Mode is waiting for Nyx to finish the active Bitmoji handoff "
                            f"before launching the next Nyxify signup.{suffix}"
                        )
                        await asyncio.sleep(2)
                        continue

                    new_tasks = store.claim_pending_tasks(limit=open_slots)
                    for task in new_tasks:
                        t = asyncio.create_task(process_task(task, store, adspower))
                        active_tasks.add(t)
                        logger.info(
                            f"Task {task['id']} started "
                            f"({len(active_tasks)}/{max_parallel} active)"
                        )

                await asyncio.sleep(2)
            except Exception as loop_error:
                logger.error(f"Nyxify polling loop error: {loop_error}")
                traceback.print_exc()
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.warning("Nyxify runner stopped manually.")
        for t in active_tasks:
            t.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
    except Exception as exc:
        logger.error(f"Nyxify fatal error: {exc}")
        traceback.print_exc()
    finally:
        runner_lock.release()


if __name__ == "__main__":
    asyncio.run(main())
