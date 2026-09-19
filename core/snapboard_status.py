"""Mark a Snapchat account's SnapBoard status from the Bitmoji (Nyx) bot.

When the Bitmoji flow hits a Snapchat *authorization error* ("Failed to load
authorization data") the account is banned/locked, so it can never produce a
Bitmoji. This module:

  1. maps the AdsPower profile id to its SnapBoard row (via NyxifyTaskStore),
  2. marks that local Nyxify row BANNED so it is never re-signed-up, and
  3. asks the SnapBoard content script — through the Nyxify local API relay —
     to flip the row's status cell to "Banned" on snapboard-production.up.railway.app.

Everything here is best-effort: the SnapBoard tab may not be open, or the row
may not exist locally (e.g. an account created outside Nyxify). Failures are
logged and swallowed so the Bitmoji runner always moves on to the next profile.
Uses only the standard library so it is safe inside the frozen runner exe.
"""

import json
import os
import time
import urllib.parse
import urllib.request

NYXIFY_LOCAL_API_URL = os.getenv("NYXIFY_LOCAL_API_URL", "http://127.0.0.1:8866").rstrip("/")
BANNED_STATUS_LABEL = os.getenv("SNAPBOARD_BANNED_LABEL", "Banned")

_HTTP_TIMEOUT = 5
_CONFIRM_TIMEOUT_SECONDS = 25
_CONFIRM_POLL_SECONDS = 1.0


def _http_request(method, path, payload=None, token=""):
    url = f"{NYXIFY_LOCAL_API_URL}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Nyxify-Token"] = token
    if payload is not None:
        body = dict(payload)
        if token:
            body["token"] = token
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        raw = response.read().decode("utf-8") or "{}"
    return json.loads(raw)


def _get_token():
    try:
        data = _http_request("GET", "/token")
        return str(data.get("token") or "").strip()
    except Exception:
        return ""


def _request_snapboard_status(row_key, status, logger=None):
    token = _get_token()
    try:
        _http_request(
            "POST",
            "/status_update/request",
            {"row_key": row_key, "status": status},
            token=token,
        )
    except Exception as exc:
        if logger:
            logger.warning(f"Could not dispatch SnapBoard status update for {row_key}: {exc}")
        return False

    deadline = time.monotonic() + _CONFIRM_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            data = _http_request(
                "GET",
                f"/status_update/status?row_key={urllib.parse.quote(row_key)}",
                token=token,
            )
        except Exception:
            data = {}
        if data.get("done"):
            if data.get("success"):
                return True
            if logger:
                logger.warning(
                    f"SnapBoard status update for {row_key} failed: {data.get('error') or 'unknown error'}"
                )
            return False
        time.sleep(_CONFIRM_POLL_SECONDS)

    if logger:
        logger.warning(f"Timed out waiting for SnapBoard to confirm '{status}' for {row_key}")
    return False


def mark_account_banned(profile_id, logger=None, status_label=None):
    """Best-effort: mark the account banned locally and on SnapBoard.

    Returns True only when the external SnapBoard status cell was confirmed
    updated; the local Nyxify row is always updated when a match is found.
    """
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return False

    status_label = status_label or BANNED_STATUS_LABEL

    try:
        from core.nyxify_task_store import NyxifyTaskStore

        store = NyxifyTaskStore()
        task = store.get_task_by_adspower_profile_id(profile_id)
    except Exception as exc:
        if logger:
            logger.warning(f"Could not look up SnapBoard row for {profile_id}: {exc}")
        task = None

    if not task:
        if logger:
            logger.info(
                f"No SnapBoard/Nyxify row found for AdsPower profile {profile_id}; "
                "skipping banned status update."
            )
        return False

    row_key = str(task.get("row_key") or "").strip()

    try:
        store.update_task_state(
            task["id"],
            status="BANNED",
            last_step="banned_bitmoji",
            error="Snapchat authorization error during Bitmoji creation",
        )
    except Exception as exc:
        if logger:
            logger.warning(f"Could not mark local Nyxify row BANNED for {profile_id}: {exc}")

    if not row_key:
        return False

    return _request_snapboard_status(row_key, status_label, logger=logger)
