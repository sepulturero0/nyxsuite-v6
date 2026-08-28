"""Process supervisor for the Nyx and Nyxify runners.

The bridge owns one :class:`RunnerSupervisor` with a :class:`ManagedRunner` per
product. Each ManagedRunner reuses the exact spawn/stop/PID helpers the legacy
tkinter UIs use (``core/process_utils``), so a runner launched by the bridge is
indistinguishable from one launched by the old UI:

* command = the frozen ``"<Name>.exe"`` when packaged, else ``[python, script]``
* spawned detached via :func:`start_background_process` with per-runner logs
* PID tracked in a pid file, with orphan re-adoption by process name / cmdline
* stop via :func:`stop_process_tree` (``taskkill /T /F`` on Windows)

This module only orchestrates processes; per-task automation logic (account
creation, Bitmoji creation) lives untouched in the runner scripts themselves.
This mirrors ``start_bot_process`` / ``stop_bot_process`` from ``ui_nyx.py`` and
``start_runner_process`` / ``stop_runner_process`` from ``ui_nyxify.py``.
"""

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from core.process_utils import (
    ROOT_DIR,
    clear_pid_file,
    find_process_ids_by_names,
    find_python_process_ids,
    force_kill_process_tree,
    is_pid_running,
    pid_matches,
    read_pid_file,
    resolve_python_executable,
    start_background_process,
    stop_process_tree,
    write_pid_file,
)

# How long stop() waits for a runner to die after SIGTERM/taskkill before
# escalating to a force kill — keeps the hotkey/dashboard Stop truly total.
STOP_CONFIRM_TIMEOUT_SECONDS = 3.0

ORPHAN_SCAN_TTL_SECONDS = 3.0

# How long a confirmed pid->is-ours verdict is trusted before re-checking. The
# check shells out (PowerShell/ps), so we cache it to keep resolve_pid cheap on
# the hot status path while still catching a PID that was recycled onto another
# process (e.g. chrome.exe) within a bounded window.
PID_IDENTITY_TTL_SECONDS = 20.0

# How long a (pid -> running) verdict is trusted before the next bounded
# subprocess probe. The bridge watcher polls status every ~0.5s per product, so
# without this every tick would shell out to PowerShell / tasklist on Windows.
# A short TTL still catches a PID that exits within ~1s.
LIVE_STATE_TTL_SECONDS = 0.75


@dataclass
class RunnerSpec:
    """Everything the supervisor needs to launch and track one runner."""

    name: str                                   # "nyx" | "nyxify"
    script_path: Path                           # ROOT_DIR/main.py | ROOT_DIR/nyxify_runner.py
    pid_file: Path
    stdout_path: Path
    stderr_path: Path
    script_match: str                           # cmdline substring for orphan detection ("main.py")
    exe_candidates: List[Path] = field(default_factory=list)   # frozen-build exe names, newest first
    process_names: List[str] = field(default_factory=list)     # exe basenames for orphan detection
    env_builder: Optional[Callable[[], dict]] = None           # returns env overrides (db path, flags, config)
    python_executable: Optional[Path] = None    # defaults to resolve_python_executable(gui=False)


class ManagedRunner:
    def __init__(self, spec: RunnerSpec):
        self.spec = spec
        self._pid_cache = {"at": 0.0, "pid": None, "scanning": False}
        # PIDs we launched this process lifetime — trusted without a lookup.
        self._spawned_pids: set = set()
        # pid -> (is_ours: bool, checked_at: float) short-lived verification cache.
        self._identity_cache: dict = {}
        # (pid, running) verdict cache — keeps the hot status path from shelling
        # out on every 0.5s watcher tick (see LIVE_STATE_TTL_SECONDS).
        self._live_cache = {"at": 0.0, "pid": None, "running": False}
        # Latching "starting"/"stopping" overlay so a Start/Stop request is
        # acknowledged immediately while the SSE watcher confirms the final state.
        self._transition = "idle"
        # Per-runner action lock: serialises start/stop/restart so a double click
        # or a concurrent hotkey cannot double-spawn or interleave a kill.
        self._lock = threading.RLock()

    def _pid_is_ours(self, pid: Optional[int]) -> bool:
        """Confirm ``pid`` is actually this runner (not a recycled PID).

        A pid file left by a previous session can point at a PID Windows has
        since handed to an unrelated process. Trusting it would let stop() run
        ``taskkill /T /F`` against, say, the user's Chrome. We only trust a PID
        we spawned ourselves or one whose live image/cmdline matches the spec.
        """
        if not pid:
            return False
        if pid in self._spawned_pids:
            return True
        now = time.monotonic()
        cached = self._identity_cache.get(pid)
        if cached and (now - cached[1]) < PID_IDENTITY_TTL_SECONDS:
            return cached[0]
        ok = pid_matches(pid, self.spec.process_names, self.spec.script_match)
        self._identity_cache[pid] = (ok, now)
        return ok

    def _running_verdict(self, pid: Optional[int]) -> bool:
        """Cached ``is_pid_running`` verdict so the hot status/SSE path does not
        shell out (PowerShell/tasklist) on every 0.5s watcher tick.

        Within :data:`LIVE_STATE_TTL_SECONDS` the last verdict is trusted; past
        that a single bounded probe refreshes it. A timed-out probe fails closed
        (treated as not running), which is safe for the PID-identity guard.
        """
        if not pid:
            return False
        now = time.monotonic()
        cache = self._live_cache
        if (
            cache["pid"] == pid
            and (now - float(cache.get("at") or 0.0)) < LIVE_STATE_TTL_SECONDS
        ):
            return cache["running"]
        running = is_pid_running(pid)
        cache.update({"at": now, "pid": pid, "running": running})
        return running

    # ------------------------------------------------------- transition overlay
    def mark_starting(self) -> None:
        self._transition = "starting"

    def mark_stopping(self) -> None:
        self._transition = "stopping"

    def clear_transition(self) -> None:
        self._transition = "idle"

    def transition(self) -> str:
        """The latched overlay state: ``"starting"``, ``"stopping"`` or ``"idle"``."""
        return self._transition

    def logical_state(self) -> str:
        """Effective state for the UI, layering the latched transition over the
        live process check: ``starting`` / ``stopping`` / ``running`` / ``stopped``."""
        if self._transition == "starting":
            return "starting"
        if self._transition == "stopping":
            return "stopping"
        return "running" if self.is_running() else "stopped"

    def resolve_exe(self) -> Optional[Path]:
        for candidate in self.spec.exe_candidates:
            try:
                if Path(candidate).exists():
                    return Path(candidate)
            except Exception:
                continue
        return None

    def _find_pids(self) -> List[int]:
        pids: List[int] = []
        if self.spec.script_match:
            for pid in find_python_process_ids(self.spec.script_match):
                if pid not in pids:
                    pids.append(pid)
        if self.spec.process_names:
            for pid in find_process_ids_by_names(self.spec.process_names):
                if pid not in pids:
                    pids.append(pid)
        return pids

    def resolve_pid(self) -> Optional[int]:
        # Fast path: a live pid file is authoritative and cheap — but only once
        # we've confirmed the PID is still OUR runner and wasn't recycled onto an
        # unrelated process (see _pid_is_ours).
        pid = read_pid_file(self.spec.pid_file)
        if pid and self._running_verdict(pid) and self._pid_is_ours(pid):
            return pid
        if pid:
            # Dead, or alive but recycled onto another process — drop the stale
            # pid file so nothing downstream trusts or kills it.
            clear_pid_file(self.spec.pid_file)
        # No live pid file. The orphan scan shells out to PowerShell (~1-2s), so
        # run it on a background thread and return the cached result immediately,
        # keeping the status/SSE path responsive. A bridge-started runner always
        # writes its pid file, so this path only matters for externally-started
        # (orphan) runners.
        cache = self._pid_cache
        now = time.monotonic()
        if (now - float(cache.get("at") or 0.0)) >= ORPHAN_SCAN_TTL_SECONDS and not cache.get("scanning"):
            cache["scanning"] = True
            threading.Thread(target=self._scan_orphans, daemon=True).start()
        return cache.get("pid")

    def _scan_orphans(self) -> None:
        found = None
        for detected in self._find_pids():
            if is_pid_running(detected):
                found = detected
                break
        if found:
            write_pid_file(self.spec.pid_file, found)
            # _find_pids only returns processes already matched to our runner by
            # image/cmdline, so prime the identity cache to skip a redundant lookup.
            self._identity_cache[found] = (True, time.monotonic())
        self._pid_cache.update({"at": time.monotonic(), "pid": found, "scanning": False})

    def is_running(self) -> bool:
        pid = self.resolve_pid()
        if pid is None:
            # Remember the "no live PID" verdict so consecutive calls don't
            # re-read the (now-absent) pid file / re-trigger an orphan scan.
            self._live_cache.update({"at": time.monotonic(), "pid": None, "running": False})
            return False
        return self._running_verdict(pid)

    def _build_command(self) -> list:
        exe = self.resolve_exe()
        if getattr(sys, "frozen", False) and exe is not None:
            return [str(exe)]
        python = self.spec.python_executable or resolve_python_executable(gui=False)
        return [str(python), str(self.spec.script_path)]

    def start(self, force_restart: bool = False, extra_env: Optional[dict] = None):
        """Spawn the runner, serialised against Stop/Restart. Returns (pid, started)."""
        with self._lock:
            return self._start_locked(force_restart=force_restart, extra_env=extra_env)

    def _start_locked(self, force_restart: bool = False, extra_env: Optional[dict] = None):
        """Spawn the runner. Returns (pid, started). Mirrors start_bot_process()."""
        live = [pid for pid in self._find_pids() if is_pid_running(pid)]
        existing = read_pid_file(self.spec.pid_file)
        # Only adopt/kill the pid-file PID if it's confirmed to be our runner —
        # never a number Windows recycled onto an unrelated process.
        if existing and is_pid_running(existing) and self._pid_is_ours(existing) and existing not in live:
            live.insert(0, existing)

        if live:
            if force_restart:
                for pid in live:
                    stop_process_tree(pid)
                clear_pid_file(self.spec.pid_file)
            else:
                write_pid_file(self.spec.pid_file, live[0])
                return live[0], False

        env = os.environ.copy()
        if self.spec.env_builder:
            try:
                env.update({k: str(v) for k, v in (self.spec.env_builder() or {}).items()})
            except Exception:
                pass
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items()})

        process = start_background_process(
            self._build_command(),
            cwd=ROOT_DIR,
            stdout_path=self.spec.stdout_path,
            stderr_path=self.spec.stderr_path,
            env=env,
        )
        write_pid_file(self.spec.pid_file, process.pid)
        self._spawned_pids.add(process.pid)
        self._identity_cache[process.pid] = (True, time.monotonic())
        return process.pid, True

    def stop(self) -> bool:
        """Kill the runner process tree (serialised vs Start/Restart)."""
        with self._lock:
            return self._stop_locked()

    def _stop_locked(self) -> bool:
        """Kill the runner process tree. Returns True if anything was stopped.

        Confirms every candidate actually died; a runner that survives SIGTERM
        (e.g. wedged inside a blocking GUI/browser call) is force-killed so a
        Stop — from the dashboard button or the global hotkey — is always total."""
        candidates: List[int] = []
        pid = self.resolve_pid()
        if pid:
            candidates.append(pid)
        for live in self._find_pids():
            if live not in candidates:
                candidates.append(live)

        if not candidates:
            clear_pid_file(self.spec.pid_file)
            return False

        stopped = False
        for candidate in candidates:
            try:
                stop_process_tree(candidate)
                stopped = True
            except Exception:
                continue

        deadline = time.monotonic() + STOP_CONFIRM_TIMEOUT_SECONDS
        survivors = [c for c in candidates if is_pid_running(c)]
        while survivors and time.monotonic() < deadline:
            time.sleep(0.15)
            survivors = [c for c in candidates if is_pid_running(c)]
        for survivor in survivors:
            try:
                force_kill_process_tree(survivor)
                stopped = True
            except Exception:
                continue

        clear_pid_file(self.spec.pid_file)
        return stopped

    def restart(self, extra_env: Optional[dict] = None):
        with self._lock:
            self._stop_locked()
            return self._start_locked(force_restart=True, extra_env=extra_env)


class RunnerSupervisor:
    """Holds one ManagedRunner per product and exposes start/stop/restart/status."""

    def __init__(self):
        self._runners: dict = {}

    def register(self, spec: RunnerSpec) -> ManagedRunner:
        runner = ManagedRunner(spec)
        self._runners[spec.name] = runner
        return runner

    def get(self, name: str) -> Optional[ManagedRunner]:
        return self._runners.get(name)

    def names(self) -> List[str]:
        return list(self._runners.keys())

    def start(self, name: str, **kwargs):
        return self._runners[name].start(**kwargs)

    def stop(self, name: str) -> bool:
        return self._runners[name].stop()

    def restart(self, name: str, **kwargs):
        return self._runners[name].restart(**kwargs)

    def is_running(self, name: str) -> bool:
        runner = self._runners.get(name)
        return bool(runner and runner.is_running())

    def transition(self, name: str) -> str:
        runner = self._runners.get(name)
        return runner.transition() if runner else "idle"

    def logical_state(self, name: str) -> str:
        runner = self._runners.get(name)
        return runner.logical_state() if runner else "stopped"

    def status(self) -> dict:
        return {
            name: {
                "running": runner.is_running(),
                "pid": runner.resolve_pid(),
                "transition": runner.transition(),
                "state": runner.logical_state(),
            }
            for name, runner in self._runners.items()
        }
