import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from core.full_auto_usernames import FullAutoUsernameStore, DEFAULT_FULL_AUTO_USERNAMES_DIR
from core.nyxify_runtime_config import load_nyxify_config, save_nyxify_config
from core.signup_data import ensure_signup_names_dir, resolve_model_name
from core.local_http import apply_cors


class _ProxyRotateStore:
    """Thread-safe store for proxy rotation requests/results."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}   # row_key -> {"dispatched": bool, "created_at": float, "max_clicks": int|None, "force": bool}
        self._results = {}   # row_key -> {"proxy": str|None, "error": str|None, "done_at": float}

    def _normalize_max_clicks(self, max_clicks):
        if max_clicks is None:
            return None
        try:
            parsed = int(max_clicks)
        except Exception:
            return None
        return max(1, min(10, parsed))

    def request(self, row_key, max_clicks=None, force=False):
        with self._lock:
            self._pending[row_key] = {
                "dispatched": False,
                "created_at": time.monotonic(),
                "max_clicks": self._normalize_max_clicks(max_clicks),
                "force": bool(force),
            }
            self._results.pop(row_key, None)

    def pop_pending(self):
        with self._lock:
            for key, val in list(self._pending.items()):
                if not val["dispatched"]:
                    val["dispatched"] = True
                    return {
                        "row_key": key,
                        "max_clicks": val.get("max_clicks"),
                        "force": bool(val.get("force")),
                    }
            return None

    def store_result(self, row_key, proxy=None, error=None):
        with self._lock:
            self._results[row_key] = {"proxy": proxy, "error": error, "done_at": time.monotonic()}
            self._pending.pop(row_key, None)

    def get_result(self, row_key):
        with self._lock:
            return dict(self._results[row_key]) if row_key in self._results else None

    def clear(self, row_key):
        with self._lock:
            self._pending.pop(row_key, None)
            self._results.pop(row_key, None)


class _UsernameUpdateStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}
        self._results = {}

    def request(self, row_key, username):
        with self._lock:
            self._pending[row_key] = {
                "username": username,
                "created_at": time.monotonic(),
                "dispatched": False,
                "dispatched_at": 0.0,
            }
            self._results.pop(row_key, None)

    def pop_pending(self):
        with self._lock:
            now = time.monotonic()
            for row_key, payload in list(self._pending.items()):
                if payload.get("dispatched") and (now - float(payload.get("dispatched_at") or 0.0)) < 5.0:
                    continue
                payload["dispatched"] = True
                payload["dispatched_at"] = now
                return {"row_key": row_key, "username": payload.get("username", "")}
            return None

    def store_result(self, row_key, success, error=None):
        with self._lock:
            self._results[row_key] = {
                "success": bool(success),
                "error": str(error or "").strip(),
                "done_at": time.monotonic(),
            }
            if success:
                self._pending.pop(row_key, None)
            elif row_key in self._pending:
                self._pending[row_key]["dispatched"] = False
                self._pending[row_key]["dispatched_at"] = 0.0

    def get_result(self, row_key):
        with self._lock:
            return dict(self._results[row_key]) if row_key in self._results else None

class _AdspowerUpdateStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}
        self._results = {}

    def request(self, row_key, adspower_id):
        with self._lock:
            self._pending[row_key] = {
                "adspower_id": adspower_id,
                "created_at": time.monotonic(),
                "dispatched": False,
                "dispatched_at": 0.0,
            }
            self._results.pop(row_key, None)

    def pop_pending(self):
        with self._lock:
            now = time.monotonic()
            for row_key, payload in list(self._pending.items()):
                if payload.get("dispatched") and (now - float(payload.get("dispatched_at") or 0.0)) < 5.0:
                    continue
                payload["dispatched"] = True
                payload["dispatched_at"] = now
                return {"row_key": row_key, "adspower_id": payload.get("adspower_id", "")}
            return None

    def store_result(self, row_key, success, error=None):
        with self._lock:
            self._results[row_key] = {
                "success": bool(success),
                "error": str(error or "").strip(),
                "done_at": time.monotonic(),
            }
            if success:
                self._pending.pop(row_key, None)
            elif row_key in self._pending:
                self._pending[row_key]["dispatched"] = False
                self._pending[row_key]["dispatched_at"] = 0.0

    def get_result(self, row_key):
        with self._lock:
            return dict(self._results[row_key]) if row_key in self._results else None

class _AdspowerNameUpdateStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}
        self._results = {}

    def request(self, row_key, adspower_name):
        with self._lock:
            self._pending[row_key] = {
                "adspower_name": adspower_name,
                "created_at": time.monotonic(),
                "dispatched": False,
                "dispatched_at": 0.0,
            }
            self._results.pop(row_key, None)

    def pop_pending(self):
        with self._lock:
            now = time.monotonic()
            for row_key, payload in list(self._pending.items()):
                if payload.get("dispatched") and (now - float(payload.get("dispatched_at") or 0.0)) < 5.0:
                    continue
                payload["dispatched"] = True
                payload["dispatched_at"] = now
                return {"row_key": row_key, "adspower_name": payload.get("adspower_name", "")}
            return None

    def store_result(self, row_key, success, error=None):
        with self._lock:
            self._results[row_key] = {
                "success": bool(success),
                "error": str(error or "").strip(),
                "done_at": time.monotonic(),
            }
            if success:
                self._pending.pop(row_key, None)
            elif row_key in self._pending:
                self._pending[row_key]["dispatched"] = False
                self._pending[row_key]["dispatched_at"] = 0.0

    def get_result(self, row_key):
        with self._lock:
            return dict(self._results[row_key]) if row_key in self._results else None


class _StatusUpdateStore:
    """Thread-safe store for SnapBoard status-cell updates (e.g. Banned).

    Mirrors the username/adspower update stores: a producer queues a row's
    desired status, the SnapBoard content script polls ``pop_pending``, applies
    it to the ``select.status-select`` cell, then reports back via
    ``store_result``.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}
        self._results = {}

    def request(self, row_key, status):
        with self._lock:
            self._pending[row_key] = {
                "status": status,
                "created_at": time.monotonic(),
                "dispatched": False,
                "dispatched_at": 0.0,
            }
            self._results.pop(row_key, None)

    def pop_pending(self):
        with self._lock:
            now = time.monotonic()
            for row_key, payload in list(self._pending.items()):
                if payload.get("dispatched") and (now - float(payload.get("dispatched_at") or 0.0)) < 5.0:
                    continue
                payload["dispatched"] = True
                payload["dispatched_at"] = now
                return {"row_key": row_key, "status": payload.get("status", "")}
            return None

    def store_result(self, row_key, success, error=None):
        with self._lock:
            self._results[row_key] = {
                "success": bool(success),
                "error": str(error or "").strip(),
                "done_at": time.monotonic(),
            }
            if success:
                self._pending.pop(row_key, None)
            elif row_key in self._pending:
                self._pending[row_key]["dispatched"] = False
                self._pending[row_key]["dispatched_at"] = 0.0

    def get_result(self, row_key):
        with self._lock:
            return dict(self._results[row_key]) if row_key in self._results else None


class _EmailFetchStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}
        self._results = {}

    def request(self, row_key, force_new=False):
        with self._lock:
            self._pending[row_key] = {
                "created_at": time.monotonic(),
                "dispatched": False,
                "dispatched_at": 0.0,
                "force_new": bool(force_new),
            }
            self._results.pop(row_key, None)

    def pop_pending(self):
        with self._lock:
            now = time.monotonic()
            for row_key, payload in list(self._pending.items()):
                if payload.get("dispatched") and (now - float(payload.get("dispatched_at") or 0.0)) < 5.0:
                    continue
                payload["dispatched"] = True
                payload["dispatched_at"] = now
                return {"row_key": row_key, "force_new": bool(payload.get("force_new"))}
            return None

    def store_result(self, row_key, email="", error=None):
        with self._lock:
            normalized_email = str(email or "").strip()
            self._results[row_key] = {
                "email": normalized_email,
                "error": str(error or "").strip(),
                "done_at": time.monotonic(),
            }
            if normalized_email:
                self._pending.pop(row_key, None)
            elif row_key in self._pending:
                self._pending[row_key]["dispatched"] = False
                self._pending[row_key]["dispatched_at"] = 0.0

    def get_result(self, row_key):
        with self._lock:
            return dict(self._results[row_key]) if row_key in self._results else None

    def get_pending_info(self, row_key):
        with self._lock:
            payload = self._pending.get(row_key)
            if not payload:
                return None
            now = time.monotonic()
            created_at = float(payload.get("created_at") or now)
            dispatched_at = float(payload.get("dispatched_at") or 0.0)
            dispatched = bool(payload.get("dispatched"))
            info = {
                "requested": True,
                "dispatched": dispatched,
                "age_seconds": max(0.0, now - created_at),
                "force_new": bool(payload.get("force_new")),
            }
            if dispatched and dispatched_at:
                info["dispatched_age_seconds"] = max(0.0, now - dispatched_at)
            return info


class _PhoneFetchStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}
        self._results = {}

    def request(self, row_key, force_new=False):
        with self._lock:
            self._pending[row_key] = {
                "created_at": time.monotonic(),
                "dispatched": False,
                "dispatched_at": 0.0,
                "force_new": bool(force_new),
            }
            self._results.pop(row_key, None)

    def pop_pending(self):
        with self._lock:
            now = time.monotonic()
            for row_key, payload in list(self._pending.items()):
                if payload.get("dispatched") and (now - float(payload.get("dispatched_at") or 0.0)) < 5.0:
                    continue
                payload["dispatched"] = True
                payload["dispatched_at"] = now
                return {"row_key": row_key, "force_new": bool(payload.get("force_new"))}
            return None

    def store_result(self, row_key, phone="", error=None):
        with self._lock:
            normalized_phone = str(phone or "").strip()
            self._results[row_key] = {
                "phone": normalized_phone,
                "error": str(error or "").strip(),
                "done_at": time.monotonic(),
            }
            if normalized_phone:
                self._pending.pop(row_key, None)
            elif row_key in self._pending:
                self._pending[row_key]["dispatched"] = False
                self._pending[row_key]["dispatched_at"] = 0.0

    def get_result(self, row_key):
        with self._lock:
            return dict(self._results[row_key]) if row_key in self._results else None

    def get_pending_info(self, row_key):
        with self._lock:
            payload = self._pending.get(row_key)
            if not payload:
                return None
            now = time.monotonic()
            created_at = float(payload.get("created_at") or now)
            dispatched_at = float(payload.get("dispatched_at") or 0.0)
            dispatched = bool(payload.get("dispatched"))
            info = {
                "requested": True,
                "dispatched": dispatched,
                "age_seconds": max(0.0, now - created_at),
                "force_new": bool(payload.get("force_new")),
            }
            if dispatched and dispatched_at:
                info["dispatched_age_seconds"] = max(0.0, now - dispatched_at)
            return info


class _SmsFetchStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}
        self._results = {}

    def request(self, row_key, phone=""):
        with self._lock:
            self._pending[row_key] = {
                "created_at": time.monotonic(),
                "dispatched": False,
                "dispatched_at": 0.0,
                "phone": str(phone or "").strip(),
            }
            self._results.pop(row_key, None)

    def pop_pending(self):
        with self._lock:
            now = time.monotonic()
            for row_key, payload in list(self._pending.items()):
                if payload.get("dispatched") and (now - float(payload.get("dispatched_at") or 0.0)) < 5.0:
                    continue
                payload["dispatched"] = True
                payload["dispatched_at"] = now
                return {"row_key": row_key, "phone": payload.get("phone", "")}
            return None

    def store_result(self, row_key, code="", error=None):
        with self._lock:
            normalized_code = str(code or "").strip()
            self._results[row_key] = {
                "code": normalized_code,
                "error": str(error or "").strip(),
                "done_at": time.monotonic(),
            }
            if normalized_code:
                self._pending.pop(row_key, None)
            elif row_key in self._pending:
                self._pending[row_key]["dispatched"] = False
                self._pending[row_key]["dispatched_at"] = 0.0

    def get_result(self, row_key):
        with self._lock:
            return dict(self._results[row_key]) if row_key in self._results else None

    def get_pending_info(self, row_key):
        with self._lock:
            payload = self._pending.get(row_key)
            if not payload:
                return None
            now = time.monotonic()
            created_at = float(payload.get("created_at") or now)
            dispatched_at = float(payload.get("dispatched_at") or 0.0)
            dispatched = bool(payload.get("dispatched"))
            info = {
                "requested": True,
                "dispatched": dispatched,
                "age_seconds": max(0.0, now - created_at),
                "phone": payload.get("phone", ""),
            }
            if dispatched and dispatched_at:
                info["dispatched_age_seconds"] = max(0.0, now - dispatched_at)
            return info


class _SnapboardRefreshStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = None
        self._result = None
        self._next_id = 0

    def request(self, reason=""):
        with self._lock:
            self._next_id += 1
            request_id = f"refresh:{self._next_id}"
            self._pending = {
                "request_id": request_id,
                "reason": str(reason or "").strip(),
                "created_at": time.monotonic(),
                "dispatched": False,
                "dispatched_at": 0.0,
            }
            self._result = None
            return request_id

    def pop_pending(self):
        with self._lock:
            if not self._pending:
                return None
            now = time.monotonic()
            if (
                self._pending.get("dispatched")
                and (now - float(self._pending.get("dispatched_at") or 0.0)) < 5.0
            ):
                return None
            self._pending["dispatched"] = True
            self._pending["dispatched_at"] = now
            return {
                "request_id": self._pending.get("request_id", ""),
                "reason": self._pending.get("reason", ""),
            }

    def store_result(self, success, error=None, request_id=""):
        with self._lock:
            normalized_request_id = str(request_id or "").strip()
            current_request_id = str((self._pending or {}).get("request_id") or "").strip()
            if normalized_request_id and current_request_id and normalized_request_id != current_request_id:
                return False
            self._result = {
                "request_id": normalized_request_id or current_request_id,
                "success": bool(success),
                "error": str(error or "").strip(),
                "done_at": time.monotonic(),
            }
            self._pending = None
            return True

    def get_result(self, request_id=""):
        with self._lock:
            if not self._result:
                return None
            normalized_request_id = str(request_id or "").strip()
            result_request_id = str(self._result.get("request_id") or "").strip()
            if normalized_request_id and result_request_id and normalized_request_id != result_request_id:
                return None
            return dict(self._result)

    def get_pending_info(self, request_id=""):
        with self._lock:
            if not self._pending:
                return None
            normalized_request_id = str(request_id or "").strip()
            pending_request_id = str(self._pending.get("request_id") or "").strip()
            if normalized_request_id and pending_request_id and normalized_request_id != pending_request_id:
                return None
            now = time.monotonic()
            created_at = float(self._pending.get("created_at") or now)
            dispatched_at = float(self._pending.get("dispatched_at") or 0.0)
            dispatched = bool(self._pending.get("dispatched"))
            info = {
                "requested": True,
                "request_id": pending_request_id,
                "reason": self._pending.get("reason", ""),
                "dispatched": dispatched,
                "age_seconds": max(0.0, now - created_at),
            }
            if dispatched and dispatched_at:
                info["dispatched_age_seconds"] = max(0.0, now - dispatched_at)
            return info


class _ReplaceBannedScanStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []
        self._updated_at = 0.0

    @staticmethod
    def _normalize_row(row, index=0):
        safe = row or {}
        row_key = str(safe.get("row_key") or "").strip().lower()
        if not row_key:
            row_id = str(safe.get("row_id") or safe.get("id") or "").strip().lower()
            if row_id:
                row_key = f"snapboard:{row_id}"
        if not row_key:
            return None
        model = str(safe.get("model") or "").strip()
        ip_address = str(safe.get("ip_address") or safe.get("ip") or "").strip()
        proxy_address = str(safe.get("proxy_address") or safe.get("proxy") or ip_address).strip()
        status = str(safe.get("status") or "").strip()
        try:
            source_rank = int(safe.get("source_rank") or index or 0)
        except (TypeError, ValueError):
            source_rank = int(index or 0)
        return {
            "row_key": row_key,
            "row_id": str(safe.get("row_id") or "").strip(),
            "model": model,
            "ip_address": ip_address,
            "proxy_address": proxy_address,
            "username": str(safe.get("username") or "").strip(),
            "email": str(safe.get("email") or "").strip(),
            "password": str(safe.get("password") or "").strip(),
            "adspower_id": str(
                safe.get("adspower_id")
                or safe.get("adspower_profile_id")
                or safe.get("profile_id")
                or ""
            ).strip(),
            "status": status,
            "source_rank": source_rank,
        }

    def update(self, rows):
        normalized = []
        seen = set()
        for index, row in enumerate(rows or []):
            item = self._normalize_row(row, index=index)
            if not item or item["row_key"] in seen:
                continue
            seen.add(item["row_key"])
            normalized.append(item)
        normalized.sort(key=lambda item: int(item.get("source_rank") or 0))
        with self._lock:
            self._rows = normalized
            self._updated_at = time.time()
        return normalized

    def rows(self):
        with self._lock:
            return [dict(row) for row in self._rows], self._updated_at

    def banned_rows(self):
        rows, updated_at = self.rows()
        banned = [
            row for row in rows
            if str(row.get("status") or "").strip().lower() == "banned"
        ]
        return banned, updated_at


class NyxifyLocalApiServer:

    def __init__(self, store, host="127.0.0.1", port=8866, token="", status_provider=None, action_handlers=None):
        self.store = store
        self.host = host
        self.port = int(port)
        self.token = str(token or "")
        self.status_provider = status_provider
        self.action_handlers = action_handlers or {}
        self.proxy_rotate_store = _ProxyRotateStore()
        self.username_update_store = _UsernameUpdateStore()
        self.adspower_update_store = _AdspowerUpdateStore()
        self.adspower_name_update_store = _AdspowerNameUpdateStore()
        self.status_update_store = _StatusUpdateStore()
        self.email_fetch_store = _EmailFetchStore()
        self.phone_fetch_store = _PhoneFetchStore()
        self.sms_fetch_store = _SmsFetchStore()
        self.snapboard_refresh_store = _SnapboardRefreshStore()
        self.replace_banned_scan_store = _ReplaceBannedScanStore()
        self.full_auto_username_store = FullAutoUsernameStore()
        try:
            from core.proxy_ranking_store import ProxyRankingStore

            self.proxy_ranking_store = ProxyRankingStore()
        except Exception:
            self.proxy_ranking_store = None
        self._server = None
        self._thread = None

    def _wait_for_update_success(self, store, row_key, timeout_seconds=30):
        deadline = time.monotonic() + max(1, float(timeout_seconds or 30))
        while time.monotonic() < deadline:
            result = store.get_result(row_key)
            if result is not None:
                return bool(result.get("success")), str(result.get("error") or "").strip()
            time.sleep(0.35)
        return False, "Timed out waiting for SnapBoard update."

    def _wait_for_value_result(self, store, row_key, value_key, timeout_seconds=75):
        deadline = time.monotonic() + max(1, float(timeout_seconds or 75))
        last_error = ""
        while time.monotonic() < deadline:
            result = store.get_result(row_key)
            if result is not None:
                value = str(result.get(value_key) or "").strip()
                if value:
                    return value, ""
                last_error = str(result.get("error") or "").strip()
            time.sleep(0.35)
        return "", last_error or f"Timed out waiting for SnapBoard {value_key}."

    def _replace_one_banned_row(self, row):
        item = _ReplaceBannedScanStore._normalize_row(row)
        if not item:
            return {"ok": False, "status": "failed", "error": "Row key is required.", "row": row or {}}

        row_key = item["row_key"]
        model = item["model"]
        ip_address = item["ip_address"]
        if not model or not ip_address:
            return {
                "ok": False,
                "status": "failed",
                "row_key": row_key,
                "error": "Model and IP/proxy are required.",
                "row": item,
            }

        warnings = []
        reservation = None
        username = ""
        try:
            reservation = self.full_auto_username_store.reserve(
                row_key=row_key,
                model=model,
                current_username=item.get("username", ""),
                reason="replace_banned",
            )
            username = str(reservation.get("username") or "").strip()
        except Exception as exc:
            return {"ok": False, "status": "failed", "row_key": row_key, "error": str(exc), "row": item}
        if not username:
            return {
                "ok": False,
                "status": "failed",
                "row_key": row_key,
                "error": f"No Full Auto username available for {model}.",
                "row": item,
            }

        self.username_update_store.request(row_key, username)
        username_ok, username_error = self._wait_for_update_success(
            self.username_update_store,
            row_key,
            timeout_seconds=30,
        )
        if not username_ok:
            try:
                self.full_auto_username_store.commit(
                    row_key=row_key,
                    reservation_id=reservation.get("reservation_id", ""),
                    username=username,
                    model=model,
                    success=False,
                    error=username_error,
                )
            except Exception:
                pass
            return {
                "ok": False,
                "status": "failed",
                "row_key": row_key,
                "error": username_error or "SnapBoard username update failed.",
                "row": item,
            }
        try:
            self.full_auto_username_store.commit(
                row_key=row_key,
                reservation_id=reservation.get("reservation_id", ""),
                username=username,
                model=model,
                success=True,
            )
        except Exception as exc:
            warnings.append(f"Username committed on SnapBoard but pool cleanup failed: {exc}")

        self.email_fetch_store.request(row_key, force_new=True)
        email, email_error = self._wait_for_value_result(
            self.email_fetch_store,
            row_key,
            "email",
            timeout_seconds=100,
        )
        if not email:
            return {
                "ok": False,
                "status": "failed",
                "row_key": row_key,
                "error": email_error or "Fresh email did not appear.",
                "row": item,
            }

        self.proxy_rotate_store.request(row_key, max_clicks=3, force=True)
        proxy, proxy_error = self._wait_for_value_result(
            self.proxy_rotate_store,
            row_key,
            "proxy",
            timeout_seconds=80,
        )
        if not proxy:
            return {
                "ok": False,
                "status": "failed",
                "row_key": row_key,
                "error": proxy_error or "Proxy did not change.",
                "row": item,
            }

        adspower_id = str(item.get("adspower_id") or "").strip()
        if adspower_id:
            delete_action = self.action_handlers.get("delete_adspower_profile")
            if delete_action is None:
                return {
                    "ok": False,
                    "status": "failed",
                    "row_key": row_key,
                    "error": "AdsPower delete handler is unavailable.",
                    "row": item,
                }
            delete_result = delete_action({"profile_id": adspower_id, "row_key": row_key})
            if not isinstance(delete_result, dict):
                delete_result = {"ok": True}
            if delete_result.get("ok") is False:
                return {
                    "ok": False,
                    "status": "failed",
                    "row_key": row_key,
                    "error": delete_result.get("error") or "AdsPower delete failed.",
                    "row": item,
                }

        self.adspower_update_store.request(row_key, "")
        adspower_clear_ok, adspower_clear_error = self._wait_for_update_success(
            self.adspower_update_store,
            row_key,
            timeout_seconds=30,
        )
        if not adspower_clear_ok:
            warnings.append(adspower_clear_error or "SnapBoard AdsPower ID clear was not confirmed.")

        self.status_update_store.request(row_key, "Warm Up")
        status_ok, status_error = self._wait_for_update_success(
            self.status_update_store,
            row_key,
            timeout_seconds=30,
        )
        if not status_ok:
            warnings.append(status_error or "SnapBoard Warm Up status was not confirmed.")

        updated = self.store.replace_for_banned_account(
            row_key=row_key,
            model=model,
            ip_address=ip_address,
            proxy_address=proxy,
            username=username,
            email=email,
            password=item.get("password", ""),
        )
        if not updated:
            return {
                "ok": False,
                "status": "failed",
                "row_key": row_key,
                "error": "Could not reset local Nyxify row.",
                "row": item,
            }

        return {
            "ok": not warnings,
            "status": "partial" if warnings else "replaced",
            "row_key": row_key,
            "username": username,
            "email": email,
            "proxy": proxy,
            "warnings": warnings,
            "row": item,
        }

    def _remove_one_banned_row(self, row):
        item = _ReplaceBannedScanStore._normalize_row(row)
        if not item:
            return {"ok": False, "status": "failed", "error": "Row key is required.", "row": row or {}}

        row_key = item["row_key"]
        warnings = []

        self.adspower_update_store.request(row_key, "")
        adspower_clear_ok, adspower_clear_error = self._wait_for_update_success(
            self.adspower_update_store,
            row_key,
            timeout_seconds=30,
        )
        if not adspower_clear_ok:
            warnings.append(adspower_clear_error or "SnapBoard AdsPower ID clear was not confirmed.")

        self.proxy_rotate_store.request(row_key, max_clicks=3)
        proxy, proxy_error = self._wait_for_value_result(
            self.proxy_rotate_store,
            row_key,
            "proxy",
            timeout_seconds=80,
        )
        if not proxy:
            return {
                "ok": False,
                "status": "failed",
                "row_key": row_key,
                "adspower_id": item.get("adspower_id", ""),
                "error": proxy_error or "Proxy did not change.",
                "row": item,
                "warnings": warnings,
            }

        updated = self.store.remove_for_banned_account(row_key=row_key, proxy_address=proxy)

        return {
            "ok": not warnings,
            "status": "partial" if warnings else "removed",
            "row_key": row_key,
            "adspower_id": item.get("adspower_id", ""),
            "proxy": proxy,
            "local_updated": bool(updated),
            "warnings": warnings,
            "row": item,
        }

    def _warmup_one_banned_row(self, row):
        item = _ReplaceBannedScanStore._normalize_row(row)
        if not item:
            return {"ok": False, "status": "failed", "error": "Row key is required.", "row": row or {}}

        row_key = item["row_key"]
        self.status_update_store.request(row_key, "Warm Up")
        status_ok, status_error = self._wait_for_update_success(
            self.status_update_store,
            row_key,
            timeout_seconds=30,
        )
        if not status_ok:
            return {
                "ok": False,
                "status": "failed",
                "row_key": row_key,
                "adspower_id": item.get("adspower_id", ""),
                "error": status_error or "SnapBoard Warm Up status was not confirmed.",
                "row": item,
            }
        return {
            "ok": True,
            "status": "warmup",
            "row_key": row_key,
            "adspower_id": item.get("adspower_id", ""),
            "row": item,
        }

    def _banned_candidates(self, rows=None):
        if rows:
            candidates = [
                _ReplaceBannedScanStore._normalize_row(row, index=index)
                for index, row in enumerate(rows or [])
            ]
            return [row for row in candidates if row]
        candidates, _updated_at = self.replace_banned_scan_store.banned_rows()
        return candidates

    def replace_banned_rows(self, rows=None):
        candidates = self._banned_candidates(rows)

        results = [self._replace_one_banned_row(row) for row in candidates]
        replaced = sum(1 for item in results if item.get("status") == "replaced")
        partial = sum(1 for item in results if item.get("status") == "partial")
        failed = sum(1 for item in results if item.get("status") == "failed")
        return {
            "ok": failed == 0,
            "count": len(results),
            "replaced": replaced,
            "partial": partial,
            "failed": failed,
            "results": results,
            "rows": self.store.list_tasks(limit=500),
            "message": f"Replace banned finished: {replaced} replaced, {partial} partial, {failed} failed.",
        }

    def remove_banned_rows(self, rows=None):
        candidates = self._banned_candidates(rows)
        results = []
        for row in candidates:
            item = _ReplaceBannedScanStore._normalize_row(row)
            if not item:
                results.append({"ok": False, "status": "failed", "error": "Row key is required.", "row": row or {}})
                continue
            results.append({
                "ok": True,
                "status": "pending",
                "row_key": item["row_key"],
                "adspower_id": item.get("adspower_id", ""),
                "proxy": "",
                "warnings": [],
                "row": item,
            })

        for result in results:
            if not result.get("row_key"):
                continue
            row_key = result["row_key"]
            self.adspower_update_store.request(row_key, "")
            adspower_clear_ok, adspower_clear_error = self._wait_for_update_success(
                self.adspower_update_store,
                row_key,
                timeout_seconds=30,
            )
            if not adspower_clear_ok:
                result["ok"] = False
                result["warnings"].append(
                    adspower_clear_error or "SnapBoard AdsPower ID clear was not confirmed."
                )

        for result in results:
            if not result.get("row_key"):
                continue
            row_key = result["row_key"]
            self.proxy_rotate_store.request(row_key, max_clicks=3, force=True)
            proxy, proxy_error = self._wait_for_value_result(
                self.proxy_rotate_store,
                row_key,
                "proxy",
                timeout_seconds=80,
            )
            if not proxy:
                result["ok"] = False
                result["status"] = "failed"
                result["error"] = proxy_error or "Proxy did not change."
                continue

            result["proxy"] = proxy
            self.store.remove_for_banned_account(row_key=row_key, proxy_address=proxy)
            result["status"] = "removed" if result.get("ok") else "partial"

        removed = sum(1 for item in results if item.get("status") == "removed")
        partial = sum(1 for item in results if item.get("status") == "partial")
        failed = sum(1 for item in results if item.get("status") == "failed")
        return {
            "ok": failed == 0,
            "count": len(results),
            "removed": removed,
            "partial": partial,
            "failed": failed,
            "warmup": 0,
            "results": results,
            "rows": self.store.list_tasks(limit=500),
            "message": f"Remove banned finished: {removed} removed, {partial} partial, {failed} failed.",
        }

    def warmup_banned_rows(self, rows=None):
        candidates = self._banned_candidates(rows)
        results = [self._warmup_one_banned_row(row) for row in candidates]
        warmup = sum(1 for item in results if item.get("status") == "warmup")
        failed = sum(1 for item in results if item.get("status") == "failed")
        return {
            "ok": failed == 0,
            "count": len(results),
            "warmup": warmup,
            "failed": failed,
            "results": results,
            "message": f"Warm Up status update finished: {warmup} updated, {failed} failed.",
        }

    def start(self):
        if self._server is not None:
            return

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _read_json(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                return json.loads(raw.decode("utf-8") or "{}")

            def _write_json(self, status_code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                apply_cors(self)
                self.end_headers()
                self.wfile.write(body)

            def _is_authorized(self, payload=None):
                if not outer.token:
                    return True
                payload = payload or {}
                token = str(payload.get("token", "") or self.headers.get("X-Nyxify-Token", "")).strip()
                return token == outer.token

            def do_OPTIONS(self):
                self._write_json(200, {"ok": True})

            def do_GET(self):
                if self.path == "/token":
                    return self._write_json(200, {"ok": True, "token": outer.token})

                if self.path == "/queue":
                    self._write_json(200, {"ok": True, "rows": outer.store.list_tasks(limit=500)})
                    return

                if self.path == "/status":
                    if outer.status_provider is None:
                        self._write_json(200, {"ok": True, "status": {"runner": "unavailable"}})
                    else:
                        self._write_json(200, {"ok": True, "status": outer.status_provider()})
                    return

                parsed_path = urlparse(self.path)
                if parsed_path.path == "/replace_banned/scan":
                    rows, updated_at = outer.replace_banned_scan_store.banned_rows()
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "rows": rows,
                            "count": len(rows),
                            "updated_at": updated_at,
                            "message": f"Found {len(rows)} banned SnapBoard row(s).",
                        },
                    )
                    return

                if self.path == "/otp/pending":
                    self._write_json(200, {"ok": True, "request": outer.store.get_pending_otp_request()})
                    return

                if self.path == "/email/pending":
                    self._write_json(200, {"ok": True, "request": outer.email_fetch_store.pop_pending()})
                    return

                if self.path == "/phone/pending":
                    self._write_json(200, {"ok": True, "request": outer.phone_fetch_store.pop_pending()})
                    return

                if self.path == "/sms/pending":
                    self._write_json(200, {"ok": True, "request": outer.sms_fetch_store.pop_pending()})
                    return

                if self.path == "/snapboard_refresh/pending":
                    self._write_json(200, {"ok": True, "request": outer.snapboard_refresh_store.pop_pending()})
                    return

                if self.path == "/config":
                    self._write_json(200, {"ok": True, "config": load_nyxify_config()})
                    return

                if self.path == "/username_update/pending":
                    self._write_json(200, {"ok": True, "request": outer.username_update_store.pop_pending()})
                    return

                if parsed_path.path == "/username_update/status":
                    params = parse_qs(parsed_path.query)
                    row_key = str((params.get("row_key") or [""])[0]).strip()
                    result = outer.username_update_store.get_result(row_key) if row_key else None
                    if result:
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "done": True,
                                "success": bool(result.get("success")),
                                "error": result.get("error", ""),
                            },
                        )
                    else:
                        self._write_json(200, {"ok": True, "done": False})
                    return

                if self.path == "/adspower_update/pending":
                    self._write_json(200, {"ok": True, "request": outer.adspower_update_store.pop_pending()})
                    return

                if parsed_path.path == "/adspower_update/status":
                    params = parse_qs(parsed_path.query)
                    row_key = str((params.get("row_key") or [""])[0]).strip()
                    result = outer.adspower_update_store.get_result(row_key) if row_key else None
                    if result:
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "done": True,
                                "success": bool(result.get("success")),
                                "error": result.get("error", ""),
                            },
                        )
                    else:
                        self._write_json(200, {"ok": True, "done": False})
                    return

                if self.path == "/adspower_name_update/pending":
                    self._write_json(200, {"ok": True, "request": outer.adspower_name_update_store.pop_pending()})
                    return

                if self.path == "/status_update/pending":
                    self._write_json(200, {"ok": True, "request": outer.status_update_store.pop_pending()})
                    return

                if parsed_path.path == "/status_update/status":
                    params = parse_qs(parsed_path.query)
                    row_key = str((params.get("row_key") or [""])[0]).strip()
                    result = outer.status_update_store.get_result(row_key) if row_key else None
                    if result:
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "done": True,
                                "success": bool(result.get("success")),
                                "error": result.get("error", ""),
                            },
                        )
                    else:
                        self._write_json(200, {"ok": True, "done": False})
                    return

                if parsed_path.path == "/adspower_name_update/status":
                    params = parse_qs(parsed_path.query)
                    row_key = str((params.get("row_key") or [""])[0]).strip()
                    result = outer.adspower_name_update_store.get_result(row_key) if row_key else None
                    if result:
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "done": True,
                                "success": bool(result.get("success")),
                                "error": result.get("error", ""),
                            },
                        )
                    else:
                        self._write_json(200, {"ok": True, "done": False})
                    return

                if parsed_path.path == "/email/status":
                    params = parse_qs(parsed_path.query)
                    row_key = str((params.get("row_key") or [""])[0]).strip()
                    result = outer.email_fetch_store.get_result(row_key) if row_key else None
                    if result:
                        email = str(result.get("email") or "").strip()
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "done": True,
                                "email": email,
                                "error": result.get("error", "") if not email else "",
                            },
                        )
                    else:
                        pending = outer.email_fetch_store.get_pending_info(row_key) if row_key else None
                        payload = {"ok": True, "done": False}
                        if pending:
                            payload.update(pending)
                        self._write_json(200, payload)
                    return

                if parsed_path.path == "/phone/status":
                    params = parse_qs(parsed_path.query)
                    row_key = str((params.get("row_key") or [""])[0]).strip()
                    result = outer.phone_fetch_store.get_result(row_key) if row_key else None
                    if result:
                        phone = str(result.get("phone") or "").strip()
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "done": True,
                                "phone": phone,
                                "error": result.get("error", "") if not phone else "",
                            },
                        )
                    else:
                        pending = outer.phone_fetch_store.get_pending_info(row_key) if row_key else None
                        payload = {"ok": True, "done": False}
                        if pending:
                            payload.update(pending)
                        self._write_json(200, payload)
                    return

                if parsed_path.path == "/sms/status":
                    params = parse_qs(parsed_path.query)
                    row_key = str((params.get("row_key") or [""])[0]).strip()
                    result = outer.sms_fetch_store.get_result(row_key) if row_key else None
                    if result:
                        code = str(result.get("code") or "").strip()
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "done": True,
                                "code": code,
                                "error": result.get("error", "") if not code else "",
                            },
                        )
                    else:
                        pending = outer.sms_fetch_store.get_pending_info(row_key) if row_key else None
                        payload = {"ok": True, "done": False}
                        if pending:
                            payload.update(pending)
                        self._write_json(200, payload)
                    return

                if parsed_path.path == "/snapboard_refresh/status":
                    params = parse_qs(parsed_path.query)
                    request_id = str((params.get("request_id") or [""])[0]).strip()
                    result = outer.snapboard_refresh_store.get_result(request_id)
                    if result:
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "done": True,
                                "request_id": result.get("request_id", ""),
                                "success": bool(result.get("success")),
                                "error": result.get("error", ""),
                            },
                        )
                    else:
                        pending = outer.snapboard_refresh_store.get_pending_info(request_id)
                        payload = {"ok": True, "done": False}
                        if pending:
                            payload.update(pending)
                        self._write_json(200, payload)
                    return

                if parsed_path.path == "/models":
                    usernames_dir = DEFAULT_FULL_AUTO_USERNAMES_DIR
                    signup_dir = ensure_signup_names_dir()
                    models = set()
                    for d in (usernames_dir, signup_dir):
                        try:
                            for f in d.glob("*.txt"):
                                models.add(f.stem)
                        except Exception:
                            pass
                    self._write_json(200, {"ok": True, "models": sorted(models)})
                    return

                if parsed_path.path == "/usernames":
                    params = parse_qs(parsed_path.query)
                    model = str((params.get("model") or [""])[0]).strip()
                    path = outer.full_auto_username_store._model_file(model) if model else None
                    lines = FullAutoUsernameStore._read_lines(path) if path else []
                    data_lines = [l.strip() for l in lines if l.strip() and not l.startswith("#") and not l.startswith(";")]
                    self._write_json(200, {"ok": True, "model": model, "usernames": data_lines})
                    return

                if parsed_path.path == "/signup_names":
                    params = parse_qs(parsed_path.query)
                    model = str((params.get("model") or [""])[0]).strip()
                    resolved = resolve_model_name(model)
                    names_dir = ensure_signup_names_dir()
                    path = names_dir / f"{resolved}.txt"
                    lines = []
                    try:
                        raw = path.read_text(encoding="utf-8-sig") if path.exists() else ""
                        lines = [l.strip() for l in raw.splitlines() if l.strip() and not l.startswith("#") and not l.startswith(";")]
                    except Exception:
                        pass
                    self._write_json(200, {"ok": True, "model": model, "resolved": resolved, "signup_names": lines})
                    return

                if parsed_path.path == "/proxy_ranking":
                    rows = []
                    if outer.proxy_ranking_store is not None:
                        try:
                            rows = outer.proxy_ranking_store.ranked()
                        except Exception as exc:
                            self._write_json(500, {"ok": False, "error": str(exc)})
                            return
                    self._write_json(200, {"ok": True, "rows": rows})
                    return

                if self.path == "/proxy/rotate_pending":
                    request = outer.proxy_rotate_store.pop_pending()
                    if request:
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "row_key": request.get("row_key"),
                                "max_clicks": request.get("max_clicks"),
                                "force": bool(request.get("force")),
                            },
                        )
                    else:
                        self._write_json(200, {"ok": True, "row_key": None})
                    return

                if parsed_path.path == "/proxy/rotate_status":
                    params = parse_qs(parsed_path.query)
                    row_key = str((params.get("row_key") or [""])[0]).strip()
                    result = outer.proxy_rotate_store.get_result(row_key) if row_key else None
                    if result:
                        self._write_json(200, {"ok": True, "done": True, "proxy": result.get("proxy"), "error": result.get("error")})
                    else:
                        self._write_json(200, {"ok": True, "done": False})
                    return

                self._write_json(404, {"ok": False, "error": "Not found"})

            def do_POST(self):
                payload = self._read_json()

                if not self._is_authorized(payload):
                    self._write_json(401, {"ok": False, "error": "Unauthorized request."})
                    return

                if self.path == "/queue/upsert":
                    entries = payload.get("entries") or []
                    count = 0
                    for entry in entries:
                        row_key = str((entry or {}).get("row_key", "")).strip()
                        model = str((entry or {}).get("model", "")).strip()
                        ip_address = str((entry or {}).get("ip_address", "")).strip()
                        proxy_address = str((entry or {}).get("proxy_address", "")).strip()
                        username = str((entry or {}).get("username", "")).strip()
                        email = str((entry or {}).get("email", "")).strip()
                        password = str((entry or {}).get("password", "")).strip()
                        adspower_id = str((entry or {}).get("adspower_id", "")).strip()
                        if not row_key or not model or not ip_address:
                            continue

                        _task_id, action = outer.store.upsert_task(
                            row_key=row_key,
                            model=model,
                            ip_address=ip_address,
                            proxy_address=proxy_address,
                            username=username,
                            email=email,
                            password=password,
                            adspower_id=adspower_id,
                            source="nyxify-extension",
                        )
                        if action in {"created", "updated"}:
                            count += 1

                    self._write_json(200, {"ok": True, "count": count, "message": "Nyxify queue synced locally."})
                    return

                if self.path == "/queue/clear":
                    count = outer.store.clear_all_tasks()
                    self._write_json(200, {"ok": True, "count": count, "message": "Nyxify queue cleared."})
                    return

                if self.path == "/queue/reset_failed":
                    count = outer.store.reset_failed_tasks()
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "count": count,
                            "rows": outer.store.list_tasks(limit=500),
                            "message": "Failed Nyxify rows reset to PENDING.",
                        },
                    )
                    return

                if self.path == "/queue/remove":
                    row_key = str(payload.get("row_key", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return

                    count = outer.store.remove_task_by_row_key(row_key)
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "count": count,
                            "rows": outer.store.list_tasks(limit=500),
                            "message": "Row removed from Nyxify queue.",
                        },
                    )
                    return

                if self.path == "/replace_banned/snapshot":
                    rows = outer.replace_banned_scan_store.update(payload.get("rows") or [])
                    banned = [
                        row for row in rows
                        if str(row.get("status") or "").strip().lower() == "banned"
                    ]
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "rows": banned,
                            "count": len(banned),
                            "message": f"Stored {len(rows)} SnapBoard row(s); {len(banned)} banned.",
                        },
                    )
                    return

                if self.path == "/replace_banned/replace":
                    rows = payload.get("rows")
                    result = outer.replace_banned_rows(rows=rows if isinstance(rows, list) else None)
                    self._write_json(200 if result.get("ok") else 207, result)
                    return

                if self.path == "/replace_banned/remove":
                    rows = payload.get("rows")
                    result = outer.remove_banned_rows(rows=rows if isinstance(rows, list) else None)
                    self._write_json(200 if result.get("ok") else 207, result)
                    return

                if self.path == "/replace_banned/warmup":
                    rows = payload.get("rows")
                    result = outer.warmup_banned_rows(rows=rows if isinstance(rows, list) else None)
                    self._write_json(200 if result.get("ok") else 207, result)
                    return

                if self.path == "/replace_banned/remove_result":
                    row_key = str(payload.get("row_key", "")).strip()
                    proxy = str(payload.get("proxy", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    count = outer.store.remove_for_banned_account(row_key=row_key, proxy_address=proxy)
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "count": count,
                            "message": "Remove banned result stored.",
                            "rows": outer.store.list_tasks(limit=500),
                        },
                    )
                    return

                if self.path == "/usernames":
                    model = str(payload.get("model", "")).strip()
                    usernames = payload.get("usernames", [])
                    if not model:
                        self._write_json(400, {"ok": False, "error": "Model is required."})
                        return
                    path = outer.full_auto_username_store._model_file(model)
                    text = "\n".join(str(u).strip() for u in (usernames or []) if str(u).strip())
                    path.write_text(text + ("\n" if text else ""), encoding="utf-8")
                    self._write_json(200, {"ok": True, "model": model, "count": len(usernames or [])})
                    return

                if self.path == "/signup_names":
                    model = str(payload.get("model", "")).strip()
                    names = payload.get("signup_names", [])
                    if not model:
                        self._write_json(400, {"ok": False, "error": "Model is required."})
                        return
                    resolved = resolve_model_name(model)
                    names_dir = ensure_signup_names_dir()
                    path = names_dir / f"{resolved}.txt"
                    text = "\n".join(str(n).strip() for n in (names or []) if str(n).strip())
                    path.write_text(text + ("\n" if text else ""), encoding="utf-8")
                    self._write_json(200, {"ok": True, "model": model, "resolved": resolved, "count": len(names or [])})
                    return

                if self.path == "/proxy/rotate_request":
                    row_key = str(payload.get("row_key", "")).strip()
                    max_clicks = payload.get("max_clicks")
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.proxy_rotate_store.request(row_key, max_clicks=max_clicks)
                    self._write_json(200, {"ok": True, "message": "Proxy rotation requested."})
                    return

                if self.path == "/proxy/rotate_result":
                    row_key = str(payload.get("row_key", "")).strip()
                    proxy = str(payload.get("proxy") or "").strip() or None
                    error = str(payload.get("error") or "").strip() or None
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.proxy_rotate_store.store_result(row_key, proxy=proxy, error=error)
                    self._write_json(200, {"ok": True, "message": "Proxy rotation result stored."})
                    return

                if self.path == "/email/request":
                    row_key = str(payload.get("row_key", "")).strip()
                    force_new = bool(payload.get("force_new"))
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.email_fetch_store.request(row_key, force_new=force_new)
                    self._write_json(200, {"ok": True, "message": "Email fetch requested."})
                    return

                if self.path == "/email/result":
                    row_key = str(payload.get("row_key", "")).strip()
                    email = str(payload.get("email", "")).strip()
                    error = str(payload.get("error", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    if email:
                        outer.store.update_task_email(row_key, email)
                    outer.email_fetch_store.store_result(row_key, email=email, error=error)
                    self._write_json(200, {"ok": True, "message": "Email fetch result stored."})
                    return

                if self.path == "/phone/request":
                    row_key = str(payload.get("row_key", "")).strip()
                    force_new = bool(payload.get("force_new"))
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.phone_fetch_store.request(row_key, force_new=force_new)
                    self._write_json(200, {"ok": True, "message": "Phone fetch requested."})
                    return

                if self.path == "/phone/result":
                    row_key = str(payload.get("row_key", "")).strip()
                    phone = str(payload.get("phone", "")).strip()
                    error = str(payload.get("error", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.phone_fetch_store.store_result(row_key, phone=phone, error=error)
                    self._write_json(200, {"ok": True, "message": "Phone fetch result stored."})
                    return

                if self.path == "/sms/request":
                    row_key = str(payload.get("row_key", "")).strip()
                    phone = str(payload.get("phone", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.sms_fetch_store.request(row_key, phone=phone)
                    self._write_json(200, {"ok": True, "message": "SMS fetch requested."})
                    return

                if self.path == "/sms/result":
                    row_key = str(payload.get("row_key", "")).strip()
                    code = str(payload.get("code", "")).strip()
                    error = str(payload.get("error", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.sms_fetch_store.store_result(row_key, code=code, error=error)
                    self._write_json(200, {"ok": True, "message": "SMS fetch result stored."})
                    return

                if self.path == "/snapboard_refresh/request":
                    reason = str(payload.get("reason", "")).strip()
                    request_id = outer.snapboard_refresh_store.request(reason=reason)
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "request_id": request_id,
                            "message": "SnapBoard refresh requested.",
                        },
                    )
                    return

                if self.path == "/snapboard_refresh/result":
                    request_id = str(payload.get("request_id", "")).strip()
                    success = bool(payload.get("success"))
                    error = str(payload.get("error", "")).strip()
                    stored = outer.snapboard_refresh_store.store_result(
                        success=success,
                        error=error,
                        request_id=request_id,
                    )
                    if not stored:
                        self._write_json(409, {"ok": False, "error": "SnapBoard refresh request is stale."})
                        return
                    self._write_json(200, {"ok": True, "message": "SnapBoard refresh result stored."})
                    return

                if self.path == "/full_auto/reserve":
                    config = load_nyxify_config()
                    if not config.get("full_auto_mode_enabled", False):
                        self._write_json(409, {"ok": False, "error": "Full Auto Mode is off."})
                        return

                    row_key = str(payload.get("row_key", "")).strip()
                    model = str(payload.get("model", "")).strip()
                    current_username = str(payload.get("current_username", "")).strip()
                    reason = str(payload.get("reason", "")).strip()
                    try:
                        reservation = outer.full_auto_username_store.reserve(
                            row_key=row_key,
                            model=model,
                            current_username=current_username,
                            reason=reason,
                        )
                    except Exception as exc:
                        self._write_json(400, {"ok": False, "error": str(exc)})
                        return

                    if reservation.get("reserved"):
                        outer.store.update_task_last_step_by_row_key(row_key, "getting_username")

                    if reservation.get("username"):
                        self._write_json(200, {"ok": True, **reservation})
                    else:
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                **reservation,
                                "message": f"No Full Auto username available for model {model or '-'}."
                            },
                        )
                    return

                if self.path == "/full_auto/commit":
                    row_key = str(payload.get("row_key", "")).strip()
                    reservation_id = str(payload.get("reservation_id", "")).strip()
                    username = str(payload.get("username", "")).strip()
                    model = str(payload.get("model", "")).strip()
                    success = bool(payload.get("success"))
                    error = str(payload.get("error", "")).strip()
                    try:
                        result = outer.full_auto_username_store.commit(
                            row_key=row_key,
                            reservation_id=reservation_id,
                            username=username,
                            model=model,
                            success=success,
                            error=error,
                        )
                    except Exception as exc:
                        self._write_json(400, {"ok": False, "error": str(exc)})
                        return

                    self._write_json(200, {"ok": True, **result})
                    return

                if self.path == "/username_update/request":
                    row_key = str(payload.get("row_key", "")).strip()
                    username = str(payload.get("username", "")).strip()
                    if not row_key or not username:
                        self._write_json(400, {"ok": False, "error": "Row key and username are required."})
                        return
                    outer.store.update_task_username(row_key, username)
                    outer.username_update_store.request(row_key, username)
                    self._write_json(200, {"ok": True, "message": "Username update requested."})
                    return

                if self.path == "/username_update/result":
                    row_key = str(payload.get("row_key", "")).strip()
                    success = bool(payload.get("success"))
                    error = str(payload.get("error", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.username_update_store.store_result(row_key, success, error=error)
                    self._write_json(200, {"ok": True, "message": "Username update result stored."})
                    return

                if self.path == "/adspower_update/request":
                    row_key = str(payload.get("row_key", "")).strip()
                    adspower_id = str(payload.get("adspower_id", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.adspower_update_store.request(row_key, adspower_id)
                    self._write_json(200, {"ok": True, "message": "AdsPower id update requested."})
                    return

                if self.path == "/adspower_update/result":
                    row_key = str(payload.get("row_key", "")).strip()
                    success = bool(payload.get("success"))
                    error = str(payload.get("error", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.adspower_update_store.store_result(row_key, success, error=error)
                    self._write_json(200, {"ok": True, "message": "AdsPower id update result stored."})
                    return

                if self.path == "/adspower_name_update/request":
                    row_key = str(payload.get("row_key", "")).strip()
                    adspower_name = str(payload.get("adspower_name", "")).strip()
                    if not row_key or not adspower_name:
                        self._write_json(400, {"ok": False, "error": "Row key and AdsPower name are required."})
                        return
                    outer.adspower_name_update_store.request(row_key, adspower_name)
                    self._write_json(200, {"ok": True, "message": "AdsPower name update requested."})
                    return

                if self.path == "/adspower_name_update/result":
                    row_key = str(payload.get("row_key", "")).strip()
                    success = bool(payload.get("success"))
                    error = str(payload.get("error", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.adspower_name_update_store.store_result(row_key, success, error=error)
                    self._write_json(200, {"ok": True, "message": "AdsPower name update result stored."})
                    return

                if self.path == "/status_update/request":
                    row_key = str(payload.get("row_key", "")).strip()
                    status = str(payload.get("status", "")).strip()
                    if not row_key or not status:
                        self._write_json(400, {"ok": False, "error": "Row key and status are required."})
                        return
                    outer.status_update_store.request(row_key, status)
                    self._write_json(200, {"ok": True, "message": "SnapBoard status update requested."})
                    return

                if self.path == "/status_update/result":
                    row_key = str(payload.get("row_key", "")).strip()
                    success = bool(payload.get("success"))
                    error = str(payload.get("error", "")).strip()
                    if not row_key:
                        self._write_json(400, {"ok": False, "error": "Row key is required."})
                        return
                    outer.status_update_store.store_result(row_key, success, error=error)
                    self._write_json(200, {"ok": True, "message": "SnapBoard status update result stored."})
                    return

                if self.path == "/otp/result":
                    row_key = str(payload.get("row_key", "")).strip()
                    code = str(payload.get("code", "")).strip()
                    error = str(payload.get("error", "")).strip()
                    if not row_key or not (code or error):
                        self._write_json(400, {"ok": False, "error": "Row key and code or error are required."})
                        return

                    count = outer.store.store_otp_code(row_key, code, error=error)
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "count": count,
                            "message": "OTP stored locally.",
                        },
                    )
                    return

                if self.path == "/config":
                    updates = {}
                    for key in (
                        "max_parallel_profiles",
                        "temporary_profile_name",
                        "adspower_group",
                        "extension_category",
                        "tag_one",
                        "tag_two",
                        "adspower_tags_enabled",
                        "proxy_blocker_enabled",
                        "proxy_checker_enabled",
                        "push_adspower_id_enabled",
                        "full_auto_mode_enabled",
                        "continuous_mode_enabled",
                        "keep_profile_open_after_signup",
                        "disable_extensions_enabled",
                        "launch_on_windows_startup",
                        "names_dir",
                        "cookie_warmup_enabled",
                        "cookie_warmup_sites",
                        "whox_check_enabled",
                        "whox_min_trust_score",
                        "whox_url",
                    ):
                        if key in payload:
                            updates[key] = payload.get(key)
                    # The banned-proxy list is edited from several places (this
                    # config save, the Proxy Ranking "Ban" button, the popup ban).
                    # An ordinary config push carries a possibly-stale copy from
                    # the extension, so only REPLACE the stored list when the
                    # caller is a deliberate banned-proxies editor and sets
                    # ``blocked_proxies_replace``. Otherwise leave it untouched so
                    # an incidental save (e.g. flipping a toggle) can't wipe bans
                    # added elsewhere — the "banned proxy cleared on restart" bug.
                    if payload.get("blocked_proxies_replace"):
                        if "blocked_proxies" in payload:
                            updates["blocked_proxies"] = payload.get("blocked_proxies")
                        elif "banned_proxies" in payload:
                            updates["blocked_proxies"] = payload.get("banned_proxies")
                    config = save_nyxify_config(updates)
                    self._write_json(200, {"ok": True, "config": config, "message": "Nyxify config saved locally."})
                    return

                if self.path == "/proxy_ranking/ban_many":
                    raw_values = payload.get("subnets")
                    if raw_values is None:
                        raw_values = payload.get("values")
                    if raw_values is None:
                        raw_values = payload.get("proxies")
                    if isinstance(raw_values, str):
                        values = [
                            item.strip()
                            for chunk in raw_values.splitlines()
                            for item in chunk.split(",")
                            if item.strip()
                        ]
                    elif isinstance(raw_values, list):
                        values = [str(item or "").strip() for item in raw_values if str(item or "").strip()]
                    else:
                        values = []

                    deduped = []
                    seen = set()
                    for value in values:
                        if value in seen:
                            continue
                        seen.add(value)
                        deduped.append(value)
                    if not deduped:
                        self._write_json(400, {"ok": False, "error": "Missing subnets/proxies to ban."})
                        return

                    current = load_nyxify_config()
                    blocked = list(current.get("blocked_proxies") or [])
                    added = []
                    for value in deduped:
                        if value not in blocked:
                            blocked.append(value)
                            added.append(value)
                    config = save_nyxify_config({
                        "blocked_proxies": blocked,
                        "proxy_blocker_enabled": True,
                    })
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "config": config,
                            "count": len(added),
                            "subnets": added,
                            "message": f"{len(added)} red proxy subnet(s) added to the Proxy Blocker.",
                        },
                    )
                    return

                if self.path == "/proxy_ranking/ban":
                    # Accepts a subnet (Proxy Ranking "Ban") or a full proxy value
                    # (popup ban) — either way it's appended verbatim to the
                    # blocked list. This is an ADDITIVE path: it never drops an
                    # existing ban, so bans from any surface accumulate durably.
                    value = str(
                        payload.get("subnet")
                        or payload.get("value")
                        or payload.get("proxy")
                        or ""
                    ).strip()
                    if not value:
                        self._write_json(400, {"ok": False, "error": "Missing subnet/proxy to ban."})
                        return
                    current = load_nyxify_config()
                    blocked = list(current.get("blocked_proxies") or [])
                    if value not in blocked:
                        blocked.append(value)
                    config = save_nyxify_config({
                        "blocked_proxies": blocked,
                        "proxy_blocker_enabled": True,
                    })
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "config": config,
                            "message": f"{value} added to the Proxy Blocker.",
                        },
                    )
                    return

                if self.path.startswith("/bot/"):
                    action_name = self.path.split("/bot/", 1)[1].strip("/")
                    action = outer.action_handlers.get(action_name)
                    if action is None:
                        self._write_json(404, {"ok": False, "error": "Unknown Nyxify action."})
                        return

                    try:
                        result = action(payload or {})
                    except Exception as exc:
                        self._write_json(500, {"ok": False, "error": str(exc) or "Nyxify action failed."})
                        return

                    if not isinstance(result, dict):
                        result = {"message": "Action completed."}
                    result.setdefault("ok", True)
                    self._write_json(200, result)
                    return

                self._write_json(404, {"ok": False, "error": "Not found"})

            def log_message(self, format, *args):
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server is None:
            return

        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
