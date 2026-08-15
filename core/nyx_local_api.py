import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.nyx_runtime_config import load_nyx_config, save_nyx_config
from core.local_http import apply_cors
from core.task_store import CONTINUOUS_TASK_PRIORITY

QUEUE_LIST_LIMIT = 500


class NyxLocalApiServer:

    def __init__(self, store, host="127.0.0.1", port=8865, token="", status_provider=None, action_handlers=None):
        self.store = store
        self.host = host
        self.port = int(port)
        self.token = str(token or "")
        self.status_provider = status_provider
        self.action_handlers = action_handlers or {}
        self._server = None
        self._thread = None

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

                payload = payload if isinstance(payload, dict) else {}
                token = str(payload.get("token", "") or self.headers.get("X-Nyx-Token", "")).strip()
                return token == outer.token

            def _queue_rows(self):
                return outer.store.list_tasks(limit=QUEUE_LIST_LIMIT)

            def do_OPTIONS(self):
                self._write_json(200, {"ok": True})

            def do_GET(self):
                if self.path == "/token":
                    return self._write_json(200, {"ok": True, "token": outer.token})

                if self.path == "/queue":
                    rows = self._queue_rows()
                    self._write_json(200, {"ok": True, "rows": rows})
                    return

                if self.path == "/status":
                    if outer.status_provider is None:
                        self._write_json(200, {"ok": True, "status": {"runner": "unavailable"}})
                    else:
                        self._write_json(200, {"ok": True, "status": outer.status_provider()})
                    return

                if self.path == "/config":
                    # Never expose the raw AdsPower API key to the browser —
                    # redact it and hand back a boolean the UI uses to show
                    # "key set / leave blank to keep".
                    cfg = dict(load_nyx_config())
                    cfg["adspower_api_key_set"] = bool(str(cfg.get("adspower_api_key") or "").strip())
                    cfg["adspower_api_key"] = ""
                    self._write_json(200, {"ok": True, "config": cfg})
                    return

                if self.path == "/bitmoji/catalog":
                    from core.bitmoji_config import (
                        DEFAULT_BASE_AVATAR, load_catalog_raw, render_param_map,
                        feature_order as get_feature_order, feature_groups as get_feature_groups,
                    )
                    raw = load_catalog_raw()
                    self._write_json(200, {
                        "ok": True,
                        "catalog": raw.get("features", {}),
                        "feature_order": raw.get("feature_order") or get_feature_order(),
                        "groups": raw.get("groups") or get_feature_groups(),
                        "base_avatar": raw.get("base_avatar") or DEFAULT_BASE_AVATAR,
                        "render_params": render_param_map(),
                    })
                    return

                if self.path == "/bitmoji/models":
                    from core.bitmoji_config import load_models, model_presets
                    try:
                        from snap_selectors.selectors import BITMOJI_SELECTORS
                        model_names = list(BITMOJI_SELECTORS.get("models", {}).keys())
                    except Exception:
                        model_names = []
                    self._write_json(200, {
                        "ok": True,
                        "models": load_models(),
                        "model_names": model_names,
                        "model_presets": model_presets(),
                    })
                    return

                if self.path == "/bitmoji/outfits":
                    from core.bitmoji_outfit_config import load_outfit_config
                    self._write_json(200, {
                        "ok": True,
                        "outfits": load_outfit_config(),
                    })
                    return

                self._write_json(404, {"ok": False, "error": "Not found"})

            def do_POST(self):
                payload = self._read_json()

                if not self._is_authorized(payload):
                    self._write_json(401, {"ok": False, "error": "Unauthorized request."})
                    return

                if self.path == "/queue/run_now":
                    profile_id = str(payload.get("profile_id", "")).strip()
                    model = str(payload.get("model", "")).strip()
                    username = str(payload.get("username", "")).strip()
                    password = str(payload.get("password", "")).strip()
                    if not profile_id or not model:
                        self._write_json(400, {"ok": False, "error": "Profile ID and model are required."})
                        return

                    _task_id, action = outer.store.upsert_task(
                        profile_id=profile_id,
                        model=model,
                        gender="female",
                        status="PENDING",
                        source="nyxify_continuous",
                        username=username,
                        password=password,
                        priority=CONTINUOUS_TASK_PRIORITY,
                    )
                    count = 0 if action in {"ignored_done", "ignored_missing"} else 1
                    start_result = None
                    if count:
                        finish_action = outer.action_handlers.get("finish_remaining")
                        if finish_action is not None:
                            try:
                                start_result = finish_action({})
                            except Exception as exc:
                                self._write_json(
                                    500,
                                    {
                                        "ok": False,
                                        "count": count,
                                        "action": action,
                                        "error": str(exc) or "Could not start Nyx.",
                                    },
                                )
                                return
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "count": count,
                            "action": action,
                            "priority": CONTINUOUS_TASK_PRIORITY,
                            "start_result": start_result,
                            "message": "Nyx run-now task queued.",
                        },
                    )
                    return

                if self.path == "/queue/upsert":
                    entries = payload.get("entries") or []
                    allow_done_requeue = payload.get("allow_done_requeue")
                    count = 0
                    skipped_done = 0
                    skipped_missing = 0
                    for entry in entries:
                        profile_id = str((entry or {}).get("profile_id", "")).strip()
                        model = str((entry or {}).get("model", "")).strip()
                        username = str((entry or {}).get("username", "")).strip()
                        password = str((entry or {}).get("password", "")).strip()
                        if not profile_id or not model:
                            continue

                        _, action = outer.store.upsert_task(
                            profile_id=profile_id,
                            model=model,
                            gender="female",
                            status="PENDING",
                            ignore_done_override=False if allow_done_requeue else None,
                            source="extension_popup",
                            username=username,
                            password=password,
                        )
                        if action == "ignored_done":
                            skipped_done += 1
                            continue
                        if action == "ignored_missing":
                            skipped_missing += 1
                            continue
                        count += 1

                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "count": count,
                            "skipped_done": skipped_done,
                            "skipped_missing": skipped_missing,
                            "message": "Nyx queue synced locally.",
                        }
                    )
                    return

                if self.path == "/queue/clear":
                    count = outer.store.clear_all_tasks()
                    self._write_json(200, {"ok": True, "count": count, "message": "Nyx local queue cleared."})
                    return

                if self.path == "/queue/prune_completed":
                    result = outer.store.prune_completed_tasks_keep_latest(keep=150)
                    rows = self._queue_rows()
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "result": result,
                            "rows": rows,
                            "message": f"Deleted {result.get('deleted', 0)} old DONE row(s), keeping the newest {result.get('keep', 150)}.",
                        }
                    )
                    return

                if self.path == "/queue/bitmoji_status":
                    entries = payload.get("entries") or []
                    statuses = outer.store.get_bitmoji_statuses(entries)
                    self._write_json(200, {"ok": True, "statuses": statuses})
                    return

                if self.path == "/queue/rerun_failed":
                    start_result = None
                    start_action = outer.action_handlers.get("start")
                    if start_action is not None:
                        # Reuse the dashboard path so Rerun Failed respects a
                        # paused or stopped runner instead of silently starting it.
                        start_result = start_action({
                            "min_pending_override": None,
                            "force_restart": False,
                            "reset_failed": True,
                        })
                        count = int(start_result.get("reset_failed_count") or 0) if isinstance(start_result, dict) else 0
                        message = (
                            start_result.get("message")
                            if isinstance(start_result, dict)
                            else "Failed Nyx rows reset to PENDING."
                        )
                    else:
                        count = outer.store.reset_failed_tasks()
                        message = "Failed Nyx rows reset to PENDING."
                    rows = self._queue_rows()
                    self._write_json(200, {"ok": True, "count": count, "rows": rows, "start_result": start_result, "message": message})
                    return

                if self.path == "/queue/reset_stuck":
                    reset_action = outer.action_handlers.get("reset_stuck")
                    if reset_action is not None:
                        result = reset_action(payload or {})
                        if not isinstance(result, dict):
                            result = {"ok": True, "message": "Stuck Nyx rows reset to PENDING."}
                        result.setdefault("ok", True)
                        result["rows"] = self._queue_rows()
                        self._write_json(200, result)
                    else:
                        count = outer.store.reset_stuck_tasks()
                        rows = self._queue_rows()
                        self._write_json(200, {"ok": True, "count": count, "rows": rows, "message": "Stuck Nyx rows reset to PENDING."})
                    return

                if self.path == "/queue/mark_done":
                    profile_id = str(payload.get("profile_id", "")).strip()
                    if not profile_id:
                        self._write_json(400, {"ok": False, "error": "Profile ID is required."})
                        return

                    updated = outer.store.update_status_by_profile_id(
                        profile_id,
                        "DONE",
                        step="manual_marked_done",
                        error=""
                    )
                    if not updated:
                        self._write_json(404, {"ok": False, "error": "Profile not found in Nyx queue."})
                        return

                    rows = self._queue_rows()
                    self._write_json(200, {"ok": True, "rows": rows, "message": "Profile marked DONE."})
                    return

                if self.path == "/queue/remove":
                    profile_id = str(payload.get("profile_id", "")).strip()
                    if not profile_id:
                        self._write_json(400, {"ok": False, "error": "Profile ID is required."})
                        return

                    count = outer.store.remove_task_by_profile_id(profile_id)
                    rows = self._queue_rows()
                    self._write_json(200, {"ok": True, "count": count, "rows": rows, "message": "Profile removed from Nyx queue."})
                    return

                if self.path == "/queue/remove_missing_profile":
                    removed_rows = outer.store.remove_missing_profile_tasks()
                    rows = self._queue_rows()
                    removed_count = len(removed_rows)
                    if not removed_count:
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "count": 0,
                                "rows": rows,
                                "message": "No FAILED missing-profile row was found.",
                            }
                        )
                        return

                    profile_ids = [
                        str(row.get("profile_id", "")).strip()
                        for row in removed_rows
                        if str(row.get("profile_id", "")).strip()
                    ]
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "count": removed_count,
                            "profile_ids": profile_ids,
                            "rows": rows,
                            "message": f"Removed {removed_count} missing profile row(s) from the Nyx queue.",
                        }
                    )
                    return

                if self.path == "/queue/relaunch":
                    profile_id = str(payload.get("profile_id", "")).strip()
                    if not profile_id:
                        self._write_json(400, {"ok": False, "error": "Profile ID is required."})
                        return

                    updated = outer.store.relaunch_task_by_profile_id(profile_id)
                    if updated == "running":
                        self._write_json(409, {"ok": False, "error": "Profile is currently RUNNING. Wait for it to finish before relaunching."})
                        return

                    if updated != "requeued":
                        self._write_json(404, {"ok": False, "error": "Profile not found in Nyx queue."})
                        return

                    rows = self._queue_rows()
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "rows": rows,
                            "message": "Profile relaunched back to PENDING only. Active runs and queue order were left untouched.",
                        }
                    )
                    return

                if self.path == "/config":
                    if "launch_on_windows_startup" in payload and payload.get("launch_on_windows_startup") is not None:
                        startup_action = outer.action_handlers.get("set_launch_on_startup")
                        if startup_action is not None:
                            startup_result = startup_action(
                                {"enabled": bool(payload.get("launch_on_windows_startup"))}
                            )
                            if isinstance(startup_result, dict) and startup_result.get("ok") is False:
                                self._write_json(
                                    500,
                                    {"ok": False, "error": startup_result.get("error") or "Could not update Windows startup."},
                                )
                                return

                    config_updates = {
                        "pending_threshold": payload.get("pending_threshold"),
                        "max_parallel_profiles": payload.get("max_parallel_profiles"),
                        "ignore_done_profiles": payload.get("ignore_done_profiles"),
                        "outfit_style": payload.get("outfit_style"),
                        "outfit_custom_preset_id": payload.get("outfit_custom_preset_id"),
                        "automation_speed": payload.get("automation_speed"),
                        "hair_randomizer_enabled": payload.get("hair_randomizer_enabled"),
                        "launch_on_windows_startup": payload.get("launch_on_windows_startup"),
                        "hubstaff_control_enabled": payload.get("hubstaff_control_enabled"),
                        "hubstaff_stop_mode": payload.get("hubstaff_stop_mode"),
                        "hubstaff_timer_minutes": payload.get("hubstaff_timer_minutes"),
                        "hubstaff_cli_path": payload.get("hubstaff_cli_path"),
                        "adspower_control_mode": payload.get("adspower_control_mode"),
                        "adspower_host": payload.get("adspower_host"),
                        "adspower_port": payload.get("adspower_port"),
                    }
                    # Only overwrite the API key when a new, non-empty value is
                    # provided (the dashboard omits the masked field when blank),
                    # so a routine config save never wipes the saved key.
                    if str(payload.get("adspower_api_key") or "").strip():
                        config_updates["adspower_api_key"] = payload.get("adspower_api_key")
                    config = save_nyx_config(config_updates)
                    safe_config = dict(config)
                    safe_config["adspower_api_key_set"] = bool(str(safe_config.get("adspower_api_key") or "").strip())
                    safe_config["adspower_api_key"] = ""
                    self._write_json(200, {"ok": True, "config": safe_config, "message": "Nyx config saved locally."})
                    return

                if self.path == "/bitmoji/models":
                    from core.bitmoji_config import save_models
                    models = payload.get("models") if isinstance(payload, dict) else None
                    if not isinstance(models, dict):
                        self._write_json(400, {"ok": False, "error": "models must be a mapping."})
                        return
                    try:
                        saved = save_models(models)
                    except ValueError as exc:
                        self._write_json(400, {"ok": False, "error": str(exc) or "Invalid models payload."})
                        return
                    self._write_json(200, {"ok": True, "models": saved, "message": "Bitmoji models saved."})
                    return

                if self.path == "/bitmoji/outfits":
                    from core.bitmoji_outfit_config import load_outfit_config, save_outfit_config
                    outfits = payload.get("outfits") if isinstance(payload, dict) else None
                    if not isinstance(outfits, dict):
                        self._write_json(400, {"ok": False, "error": "outfits must be a mapping."})
                        return
                    try:
                        save_outfit_config(outfits)
                    except ValueError as exc:
                        self._write_json(400, {"ok": False, "error": str(exc) or "Invalid outfits payload."})
                        return
                    self._write_json(200, {"ok": True, "outfits": load_outfit_config(), "message": "Bitmoji outfits saved."})
                    return

                if self.path.startswith("/bot/"):
                    action_name = self.path.split("/bot/", 1)[1].strip("/")
                    action = outer.action_handlers.get(action_name)
                    if action is None:
                        self._write_json(404, {"ok": False, "error": "Unknown Nyx action."})
                        return

                    try:
                        result = action(payload or {})
                    except Exception as exc:
                        self._write_json(500, {"ok": False, "error": str(exc) or "Nyx action failed."})
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
