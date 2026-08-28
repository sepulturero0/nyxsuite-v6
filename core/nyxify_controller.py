"""Headless Nyxify controller for the bridge.

Headless equivalent of the Nyxify parts of ``ui_nyxify.py``: supplies the
``status_provider`` and ``action_handlers`` that
:class:`core.nyxify_local_api.NyxifyLocalApiServer` consumes, with no tkinter
dependency. Reuses NyxifyTaskStore, AdsPowerManager, nyxify_runtime_config,
``runner_flags`` and the :class:`~core.runner_supervisor.RunnerSupervisor`.
Account-creation/signup automation is untouched; this only orchestrates the
runner process and answers queue/status queries.
"""

import threading

from core import runner_flags
from core.adspower_live import annotate_rows_with_open_state
from core.process_utils import APP_DATA_DIR, LOGS_DIR, ROOT_DIR
from core.runner_supervisor import RunnerSpec
from core.version import NYXIFY_VERSION_LABEL

NYXIFY_DB_PATH = APP_DATA_DIR / "data" / "nyxify_tasks.db"
PID_FILE = LOGS_DIR / "nyxify_runner.pid"
RUNNER_STDOUT = LOGS_DIR / "nyxify_runner_stdout.log"
RUNNER_STDERR = LOGS_DIR / "nyxify_runner_stderr.log"
RUNNER_SCRIPT = ROOT_DIR / "nyxify_runner.py"
# v6 ships its own runner exe; older runner names stay isolated (see nyx_controller).
# macOS frozen builds produce .app bundles or plain executables (no .exe).
RUNNER_EXECUTABLE_CANDIDATES = [
    ROOT_DIR / f"NyxifyRunner {NYXIFY_VERSION_LABEL}.exe",
    ROOT_DIR / f"NyxifyRunner {NYXIFY_VERSION_LABEL}.app",
    ROOT_DIR / f"NyxifyRunner {NYXIFY_VERSION_LABEL}",
]
RUNNER_EXECUTABLE_PROCESS_NAMES = [
    f"NyxifyRunner {NYXIFY_VERSION_LABEL}.exe",
    f"NyxifyRunner {NYXIFY_VERSION_LABEL}",
]


def _load_config() -> dict:
    try:
        from core.nyxify_runtime_config import load_nyxify_config

        return load_nyxify_config() or {}
    except Exception:
        return {}


class NyxifyController:
    """Headless equivalent of the Nyxify parts of NyxifyApp."""

    NAME = "nyxify"

    def __init__(self, supervisor, store=None, adspower=None):
        if store is None:
            from core.nyxify_task_store import NyxifyTaskStore

            store = NyxifyTaskStore(db_path=NYXIFY_DB_PATH)
        if adspower is None:
            from core.adspower import AdsPowerManager

            adspower = AdsPowerManager(ui_assume_presearch=True)
        self.store = store
        self.adspower = adspower
        self.supervisor = supervisor
        self.runner = supervisor.register(self._spec())
        # Per-product action lock: serialises start/stop/pause/resume/etc so a
        # double click or a concurrent hotkey never races a spawn against a kill.
        self._action_lock = threading.RLock()
        # Config read cache keyed by file mtime — the watcher calls status every
        # ~0.5s, so we avoid re-reading/parsing nyxify_config.json each tick.
        self._config_cache = {"mtime": None, "value": {}}

    def _spec(self) -> RunnerSpec:
        return RunnerSpec(
            name=self.NAME,
            script_path=RUNNER_SCRIPT,
            pid_file=PID_FILE,
            stdout_path=RUNNER_STDOUT,
            stderr_path=RUNNER_STDERR,
            script_match=str(RUNNER_SCRIPT),  # full path so v6 never adopts an older source runner
            exe_candidates=RUNNER_EXECUTABLE_CANDIDATES,
            process_names=RUNNER_EXECUTABLE_PROCESS_NAMES,
            env_builder=self._base_env,
        )

    def _cached_config(self) -> dict:
        """Config with an mtime cache so the hot status path never re-reads
        nyxify_config.json. Falls back to a fresh read when stat/load fails."""
        try:
            from core.nyxify_runtime_config import CONFIG_PATH

            try:
                mtime = CONFIG_PATH.stat().st_mtime_ns
            except OSError:
                return _load_config()
            cache = self._config_cache
            if cache["mtime"] == mtime:
                return cache["value"]
            value = _load_config()
            self._config_cache.update({"mtime": mtime, "value": value})
            return value
        except Exception:
            return {}

    def _base_env(self) -> dict:
        env = {
            "NYXIFY_TASK_DB_PATH": str(NYXIFY_DB_PATH),
            "NYXIFY_PAUSE_FILE": str(runner_flags.NYXIFY_PAUSE_FILE),
        }
        # Hand the runner the SAME local-API token the NyxifyLocalApiServer
        # resolves (env override, else the persistent agent token), so its
        # SnapBoard requests (email fetch, AdsPower id push, proxy rotate) are
        # authenticated from the first call instead of relying on env inheritance
        # — a stale inherited token otherwise 401s every SnapBoard call. The
        # runner still self-heals via /token if this is ever wrong.
        try:
            import os

            from core.agent_token import get_or_create_token

            token = (
                os.getenv("NYXIFY_LOCAL_API_TOKEN")
                or os.getenv("NYXSUITE_TOKEN")
                or get_or_create_token()
            )
            if token:
                env["NYXIFY_LOCAL_API_TOKEN"] = str(token)
        except Exception:
            pass
        return env

    # ------------------------------------------------------------------ status
    @staticmethod
    def _counts_from(tasks) -> dict:
        pending = sum(1 for r in tasks if r["status"] == "PENDING")
        waiting = sum(
            1
            for r in tasks
            if r["status"] == "PENDING" and str(r.get("last_step", "")).startswith("waiting_for_")
        )
        running = sum(1 for r in tasks if r["status"] == "RUNNING")
        failed = sum(1 for r in tasks if r["status"] == "FAILED")
        done = sum(1 for r in tasks if r["status"] == "DONE")
        return {
            "pending": pending,
            "waiting": waiting,
            "ready": max(0, pending - waiting),
            "running": running,
            "failed": failed,
            "done": done,
            "recent": len(tasks),
        }

    def _compute_bot(self, pid, paused) -> dict:
        """Derive the ``bot`` block. STARTING/STOPPING from the latched transition
        overlay win over the (possibly stale) pid check so a Start/Stop request is
        reflected immediately, then the SSE watcher confirms the final state."""
        transition = self.runner.transition()
        if transition == "starting":
            return {"state": "STARTING", "detail": "Nyxify runner is starting...", "pid": pid}
        if transition == "stopping":
            return {"state": "STOPPING", "detail": "Nyxify runner is stopping...", "pid": pid}
        if paused:
            detail = f"Nyxify runner is paused (PID {pid})." if pid else "Nyxify runner is paused."
            return {"state": "PAUSED", "detail": detail, "pid": pid if pid else None}
        if pid:
            return {"state": "RUNNING", "detail": "Nyxify runner is active.", "pid": pid}
        return {"state": "STOPPED", "detail": "Nyxify runner is not running.", "pid": None}

    def status_snapshot(self) -> dict:
        tasks = self.store.list_tasks(limit=500)
        live = annotate_rows_with_open_state(tasks, ("adspower_profile_id", "adspower_id"))
        pid = self.runner.resolve_pid()
        return {
            "rows": tasks,
            "counts": self._counts_from(tasks),
            "bot": self._compute_bot(pid, runner_flags.nyxify_is_paused()),
            "adspower_live": live,
            "config": self._cached_config(),
        }

    def light_status(self) -> dict:
        """Cheap status for an action ack: bot + counts, no rows and no AdsPower
        annotations. The SSE watcher pushes the full snapshot, so an ack never
        builds the expensive table just to confirm the button press."""
        tasks = self.store.list_tasks(limit=500)
        return {
            "bot": self._compute_bot(
                self.runner.resolve_pid(), runner_flags.nyxify_is_paused()
            ),
            "counts": self._counts_from(tasks),
            "config": self._cached_config(),
        }

    # ---------------------------------------------------------- action handlers
    def start(self, payload=None) -> dict:
        """Start Nyxify and return a fast ack. The latched "starting" state is
        reported immediately; the SSE watcher confirms the final state."""
        with self._action_lock:
            runner_flags.nyxify_set_paused(False)
            self.runner.mark_starting()
            pid = None
            try:
                pid, started = self.supervisor.start(
                    self.NAME, force_restart=bool((payload or {}).get("force_restart", False))
                )
                ack_status = self.light_status()
            finally:
                self.runner.clear_transition()
            return {
                "ok": True,
                "message": f"Nyxify runner started (PID {pid})."
                if started
                else f"Nyxify runner already running (PID {pid}).",
                "status": ack_status,
            }

    def pause(self, payload=None) -> dict:
        with self._action_lock:
            runner_flags.nyxify_set_paused(True)
            return {"ok": True, "message": "Nyxify runner paused.", "status": self.light_status()}

    def resume(self, payload=None) -> dict:
        with self._action_lock:
            runner_flags.nyxify_set_paused(False)
            pid, _started = self.supervisor.start(self.NAME, force_restart=False)
            return {"ok": True, "message": f"Nyxify runner resumed (PID {pid}).", "status": self.light_status()}

    def stop(self, payload=None) -> dict:
        """Stop Nyxify and return a fast ack. The latched "stopping" state is
        reported immediately; the supervisor still performs a complete
        process-tree termination, and the SSE watcher confirms "STOPPED"."""
        with self._action_lock:
            self.runner.mark_stopping()
            try:
                stopped = self.supervisor.stop(self.NAME)
                ack_status = self.light_status()
            finally:
                self.runner.clear_transition()
            runner_flags.nyxify_set_paused(False)
            return {
                "ok": True,
                "message": "Nyxify runner stopped." if stopped else "No Nyxify runner process was found.",
                "status": ack_status,
            }

    def reset_failed(self, payload=None) -> dict:
        count = self.store.reset_failed_tasks()
        return {
            "ok": True,
            "count": count,
            "message": f"Reset {count} failed Nyxify row(s).",
            "status": self.light_status(),
        }

    def clear_queue(self, payload=None) -> dict:
        count = self.store.clear_all_tasks()
        return {
            "ok": True,
            "count": count,
            "message": f"Cleared {count} Nyxify row(s).",
            "status": self.light_status(),
        }

    def delete_adspower_profile(self, payload=None) -> dict:
        from core.nyxify_cleanup import close_and_delete_profile

        profile_id = str((payload or {}).get("profile_id", "")).strip()
        row_key = str((payload or {}).get("row_key", "")).strip()
        if not profile_id:
            return {"ok": False, "error": "AdsPower profile id is required."}
        result = close_and_delete_profile(
            self.adspower,
            profile_id,
            log=None,
            row_key=row_key,
            reason="replace_banned",
        )
        if not result.get("deleted"):
            return {
                "ok": False,
                "error": result.get("delete_error") or "AdsPower profile deletion was not confirmed.",
                "result": result,
            }
        return {"ok": True, "message": f"AdsPower profile {profile_id} deleted.", "result": result}

    def action_handlers(self) -> dict:
        """The /bot/<action> handlers consumed by NyxifyLocalApiServer.

        Lifecycle + reset/clear are implemented here. delete_orphan_failed_profiles
        and rename_profile (which need AdsPower cleanup helpers) are layered on in
        Phase 3 with the dashboard controls that use them.
        """
        return {
            "start": self.start,
            "pause": self.pause,
            "resume": self.resume,
            "stop": self.stop,
            "reset_failed": self.reset_failed,
            "clear_queue": self.clear_queue,
            "delete_adspower_profile": self.delete_adspower_profile,
        }
