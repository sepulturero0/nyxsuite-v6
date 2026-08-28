"""Nyx Suite bridge — one tray app for the whole suite.

A single lightweight process that:
  * holds a single-instance guard (RunnerLock on :8869)
  * owns one shared AdsPowerManager
  * supervises the Nyx and Nyxify runner subprocesses (RunnerSupervisor +
    NyxController / NyxifyController) — per-task automation is untouched
  * hosts the attach-aware product local APIs (Nyx :8865, Nyxify :8866)
  * serves the web dashboard (:8870) + /bridge/* actions
  * shows a pystray tray (Open Dashboard, per-product Start/Stop/Restart,
    Check Update, Roll back, Exit). The tray is best-effort: if pystray/PIL are
    unavailable the bridge still runs headless and serves the dashboard.

Run from source:  python bridge_app.py
Installed:        launched by the "Nyx Suite" tray exe / Start-Menu shortcut.

Closing the bridge does NOT stop the runners (same as the old UI's behaviour);
use the tray/dashboard Stop to halt a runner.
"""

import os
import socket
import sys
import threading
import time
import webbrowser

from pathlib import Path

from core.bridge_runtime_config import load_bridge_config, save_bridge_config
from core.agent_token import get_or_create_token
from core.process_utils import ensure_logs_dir
from core.runner_lock import RunnerLock
from core.runner_supervisor import RunnerSupervisor
from core.timing_diag import elapsed, log_timing, now
from core.webui_server import WebDashboardServer

SINGLE_INSTANCE_PORT = int(os.getenv("NYXSUITE_BRIDGE_PORT", "8869"))
NYX_API_PORT = int(os.getenv("NYX_LOCAL_API_PORT", "8865"))
NYXIFY_API_PORT = int(os.getenv("NYXIFY_LOCAL_API_PORT", "8866"))
DASHBOARD_PORT = int(os.getenv("NYXSUITE_DASHBOARD_PORT", "8870"))
DASHBOARD_URL = f"http://127.0.0.1:{DASHBOARD_PORT}/"


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def log(message: str) -> None:
    try:
        from core.logger import logger

        logger.info(message)
    except Exception:
        print(f"[bridge] {message}", flush=True)


def _ensure_data_dirs():
    """Create all expected data subdirectories on first launch."""
    from core.process_utils import ROOT_DIR
    data_dir = ROOT_DIR / "data"
    for sub in ("full_auto_usernames", "signup_names", "logs", "cache", "updates", "templates"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)


def _port_in_use(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False
    finally:
        sock.close()


class BridgeApp:
    def __init__(self):
        self.supervisor = RunnerSupervisor()
        self.adspower = None
        self.nyx = None
        self.nyxify = None
        self.nyx_api = None
        self.nyxify_api = None
        self.dashboard = None
        self._tray_icon = None
        self._last_tray_image = None
        self._transparent_tray_icon = False
        self._stop = threading.Event()
        self.token = ""

    # ------------------------------------------------------------------ build
    def _version(self) -> str:
        try:
            from core.version import NYX_VERSION

            return NYX_VERSION
        except Exception:
            return ""

    def build(self):
        _ensure_data_dirs()
        start = now()
        from core.adspower import AdsPowerManager
        from core.nyx_controller import NyxController
        from core.nyxify_controller import NyxifyController

        # Per-install token required on state-changing endpoints (env can override).
        self.token = os.getenv("NYXSUITE_TOKEN") or get_or_create_token()
        self._transparent_tray_icon = bool(load_bridge_config().get("transparent_tray_icon", False))
        self.adspower = AdsPowerManager()
        self.nyx = NyxController(self.supervisor, adspower=self.adspower)
        self.nyxify = NyxifyController(self.supervisor, adspower=self.adspower)
        log_timing("bridge.build", start, "bridge")

    def start_servers(self):
        start = now()
        from core.nyx_local_api import NyxLocalApiServer
        from core.nyxify_local_api import NyxifyLocalApiServer

        # Attach-aware: if a legacy tkinter UI already holds the product port,
        # do NOT re-bind — the dashboard talks to the already-running server.
        if _port_in_use("127.0.0.1", NYX_API_PORT):
            log(f"Nyx API :{NYX_API_PORT} already in use — attach mode (using existing server).")
        else:
            self.nyx_api = NyxLocalApiServer(
                self.nyx.store,
                host="127.0.0.1",
                port=NYX_API_PORT,
                token=os.getenv("NYX_LOCAL_API_TOKEN") or self.token,
                status_provider=self.nyx.light_status,
                action_handlers=self.nyx.action_handlers(),
            )
            self.nyx_api.start()
            log(f"Nyx local API on :{NYX_API_PORT}")

        if _port_in_use("127.0.0.1", NYXIFY_API_PORT):
            log(f"Nyxify API :{NYXIFY_API_PORT} already in use — attach mode (using existing server).")
        else:
            self.nyxify_api = NyxifyLocalApiServer(
                self.nyxify.store,
                host="127.0.0.1",
                port=NYXIFY_API_PORT,
                token=os.getenv("NYXIFY_LOCAL_API_TOKEN") or self.token,
                status_provider=self.nyxify.light_status,
                action_handlers=self.nyxify.action_handlers(),
            )
            self.nyxify_api.start()
            log(f"Nyxify local API on :{NYXIFY_API_PORT}")

        self.dashboard = WebDashboardServer(
            controllers={"nyx": self.nyx, "nyxify": self.nyxify},
            host="127.0.0.1",
            port=DASHBOARD_PORT,
            bridge_actions=self._bridge_actions(),
            bridge_settings_provider=self._bridge_settings_snapshot,
            version=self._version(),
            token=self.token,
        )
        self.dashboard.start()
        log(f"Dashboard on {DASHBOARD_URL}")
        try:
            from core.hotkeys import start_product_hotkeys

            # Dedicated key per product: Ctrl+F8 always controls Nyx and
            # Ctrl+F7 always controls Nyxify — no shared key, no dashboard-tab
            # dependent target, so stopping one product can never start the other.
            start_product_hotkeys({
                "f8": ("nyx", lambda _scope: self._toggle_product("nyx")),
                "f7": ("nyxify", lambda _scope: self._toggle_product("nyxify")),
            })
        except Exception as exc:
            log(f"Ctrl+F7/F8 stop/start hotkeys unavailable: {exc}")
        log_timing("bridge.start_servers", start, "bridge")

    def _bridge_actions(self) -> dict:
        return {
            "check_update": self._action_check_update,
            "apply_update": self._action_apply_update,
            "rollback": self._action_rollback,
            "list_backups": self._action_list_backups,

            "autostart": self._action_autostart,
            "set_autostart": self._action_set_autostart,
            "install_deps": self._action_install_deps,
            "install_deps_status": self._action_install_deps_status,
            "tray_icon": self._action_tray_icon,
            "set_tray_icon": self._action_set_tray_icon,
            "sync_extensions": self._action_sync_extensions,
            "hotkey_product": self._action_hotkey_product,
            "adspower_test": self._action_adspower_test,
            "shutdown": self._action_shutdown,
        }

    def _bridge_settings_snapshot(self) -> dict:
        return {
            "transparent_tray_icon": bool(getattr(self, "_transparent_tray_icon", False)),
        }

    def _action_hotkey_product(self, payload=None) -> dict:
        # Kept only so older cached dashboards calling this endpoint don't get an
        # error. Hotkeys are fixed now: Ctrl+F8 = Nyx, Ctrl+F7 = Nyxify.
        return {
            "ok": True,
            "message": "Hotkeys are fixed per product: Ctrl+F8 controls Nyx, Ctrl+F7 controls Nyxify.",
        }

    def _toggle_product(self, product) -> dict:
        """Hotkey action: stop ``product`` if it is active, else start it.

        Stop goes through the same controller.stop() the dashboard Stop button
        uses (full process-tree kill via the supervisor), so the hotkey stops the
        runner exactly as the button does."""
        product = str(product or "").strip().lower()
        if product not in {"nyx", "nyxify"}:
            product = "nyx"

        controller = getattr(self, product, None)
        if controller is None:
            return {"ok": False, "action": "stop", "product": product, "message": f"{product} controller is not ready."}

        try:
            snapshot = controller.status_snapshot()
            state = str(((snapshot or {}).get("bot") or {}).get("state") or "").strip().lower()
        except Exception:
            state = ""

        if state in {"running", "paused", "waiting", "blocked"}:
            result = controller.stop({})
            action = "stop"
        else:
            result = controller.start({})
            action = "start"

        if not isinstance(result, dict):
            result = {"ok": True}
        result = dict(result)
        result["action"] = action
        result["product"] = product
        result.setdefault("message", f"{product} {action} requested.")
        log(f"Hotkey {action} {product}: {result.get('message')}")
        return result

    def _action_adspower_test(self, payload=None) -> dict:
        """Run the AdsPower preflight probe with the currently-saved settings so
        the dashboard's "Test AdsPower connection" button can show OK or the
        actionable permission/unreachable error inline."""
        try:
            from core.adspower import AdsPowerManager
            result = AdsPowerManager().preflight_check()
            return {
                "ok": bool(result.get("ok")),
                "code": result.get("code"),
                "message": result.get("message"),
            }
        except Exception as exc:
            return {"ok": False, "code": "error", "message": f"AdsPower test failed: {exc}"}

    def _action_shutdown(self, payload=None) -> dict:
        log("Shutdown requested via bridge action.")
        threading.Thread(target=self._request_exit, daemon=True).start()
        return {"ok": True, "message": "Bridge shutting down."}

    def _action_check_update(self, payload=None) -> dict:
        try:
            from core.release_updater import (compare_versions, get_current_version,
                                              get_latest_release, load_update_config,
                                              _trim_release_notes)
        except Exception as exc:
            return {"ok": False, "message": f"Updater unavailable: {exc}"}
        cfg = load_update_config()
        repo, pattern = cfg.get("repo"), cfg.get("asset_pattern")
        current = get_current_version()
        if not repo or not pattern:
            return {"ok": False, "current": current, "message": "Update channel not configured (running from source)."}
        try:
            rel = get_latest_release(repo, pattern)
        except Exception as exc:
            return {"ok": False, "current": current, "message": f"Update check failed: {exc}"}
        available = compare_versions(current, rel.tag_name) < 0
        return {
            "ok": True,
            "current": current,
            "latest": rel.tag_name,
            "latest_name": rel.release_name or rel.tag_name,
            "update_available": available,
            "release_notes": _trim_release_notes(rel.body) if available else "",
            "release_url": rel.html_url or "",
            "message": (f"Update {rel.tag_name} available (current {current})." if available
                        else f"Up to date ({current})."),
        }

    def _action_apply_update(self, payload=None) -> dict:
        try:
            from core.release_updater import (apply_update_direct,
                                              get_latest_release, load_update_config)
            from core.release_updater import _log as _update_log
        except Exception as exc:
            return {"ok": False, "message": f"Updater unavailable: {exc}"}
        cfg = load_update_config()
        repo, pattern = cfg.get("repo"), cfg.get("asset_pattern")
        if not repo or not pattern:
            return {"ok": False, "message": "Update channel not configured (running from source)."}
        try:
            rel = get_latest_release(repo, pattern)
            _update_log(f"update started — {rel.tag_name}")
            # Stop runners before update.
            for name in self.supervisor.names():
                try:
                    self.supervisor.stop(name)
                except Exception:
                    pass
            result = apply_update_direct(rel.asset_url, rel.tag_name)
            if not result.get("ok"):
                _update_log(f"direct update failed: {result.get('message')}", "error")
                return {"ok": False, "message": result.get("message", "Update failed.")}
            _update_log(f"update completed — {result.get('message')}")
            self._relaunch_after_exit()
            threading.Timer(0.8, self._request_exit).start()
            return {"ok": True, "message": result.get("message", f"Updated to {rel.tag_name}.")}
        except Exception as exc:
            _update_log(f"update failed: {exc}", "error")
            return {"ok": False, "message": f"Update failed: {exc}"}

    def _action_rollback(self, payload=None) -> dict:
        try:
            from core.release_updater import sync_extensions, sync_source_dirs, _sync_root_files
            from core.update_backup import list_backups, backups_dir
            from core.process_utils import ROOT_DIR
        except Exception as exc:
            return {"ok": False, "message": f"Rollback unavailable: {exc}"}
        backups = list_backups()
        version = (payload or {}).get("version") or (backups[0] if backups else None)
        if not version:
            return {"ok": False, "message": "No version specified and no local backup to roll back to."}
        version = str(version).strip().lstrip("vV")

        # Stop runners before touching any files.
        for name in self.supervisor.names():
            try:
                self.supervisor.stop(name)
            except Exception:
                pass

        # Fast path: this version was snapshotted locally — restore from disk.
        backup_folder = backups_dir() / version
        if backup_folder.is_dir():
            try:
                sync_source_dirs(backup_folder, ROOT_DIR)
                sync_extensions(backup_folder, ROOT_DIR)
                _sync_root_files(backup_folder, ROOT_DIR)
                ver_src = backup_folder / "VERSION"
                if ver_src.exists():
                    (ROOT_DIR / "VERSION").write_text(
                        ver_src.read_text(encoding="utf-8-sig").strip(), encoding="ascii"
                    )
            except Exception as exc:
                return {"ok": False, "message": f"Rollback failed: {exc}"}
            self._relaunch_after_exit()
            threading.Timer(0.8, self._request_exit).start()
            return {"ok": True, "message": f"Rolling back to v{version}; the app will restart."}

        # No local snapshot for this version — download that published release
        # and apply it (this also snapshots the current build first, so a later
        # roll-forward still has a restore point, and preserves user data).
        try:
            from core.release_updater import (apply_update_direct, get_release_by_version,
                                              load_update_config)
            from core.release_updater import _log as _update_log
            cfg = load_update_config()
            repo, pattern = cfg.get("repo"), cfg.get("asset_pattern")
            if not repo or not pattern:
                return {"ok": False, "message": f"No local backup for v{version}, and no update channel is configured to download it."}
            rel = get_release_by_version(repo, pattern, version)
            _update_log(f"rollback download started — {rel.tag_name}")
            result = apply_update_direct(rel.asset_url, rel.tag_name)
            if not result.get("ok"):
                _update_log(f"rollback download failed: {result.get('message')}", "error")
                return {"ok": False, "message": result.get("message", "Rollback download failed.")}
            _update_log(f"rollback completed — {result.get('message')}")
        except Exception as exc:
            return {"ok": False, "message": f"Rollback failed: {exc}"}
        self._relaunch_after_exit()
        threading.Timer(0.8, self._request_exit).start()
        return {"ok": True, "message": f"Rolling back to v{version}; the app will restart."}

    def _action_list_backups(self, payload=None) -> dict:
        try:
            from core.update_backup import list_backups
        except Exception as exc:
            return {"ok": False, "backups": [], "available_versions": [], "message": str(exc)}
        try:
            backups = list_backups()
        except Exception:
            backups = []
        return {
            "ok": True,
            "backups": backups,
            "available_versions": self._available_release_versions(backups),
        }

    def _available_release_versions(self, local_backups=None) -> list:
        """Every version Roll Back can restore: all published releases (fetched
        over the network, cached briefly) plus any local-only snapshots, each
        flagged with whether it's available offline. Degrades to just the local
        backups when GitHub is unreachable."""
        local_set = {str(b).strip().lstrip("vV") for b in (local_backups or [])}
        cache = getattr(self, "_rollback_versions_cache", None)
        if cache is None:
            cache = {"at": 0.0, "versions": []}
            self._rollback_versions_cache = cache
        now = time.monotonic()
        if not cache["versions"] or (now - cache["at"]) > 300.0:
            try:
                from core.release_updater import (get_current_version, list_all_releases,
                                                  load_update_config)
                cfg = load_update_config()
                repo, pattern = cfg.get("repo"), cfg.get("asset_pattern")
                if repo and pattern:
                    current = str(get_current_version() or "").strip().lstrip("vV")
                    fetched = []
                    for rel in list_all_releases(repo, pattern):
                        tag = str(rel.tag_name or "").strip().lstrip("vV")
                        if tag:
                            fetched.append({
                                "version": tag,
                                "name": rel.release_name or rel.tag_name,
                                "current": tag == current,
                            })
                    cache.update({"at": now, "versions": fetched})
            except Exception:
                pass  # keep any prior list; local backups remain usable

        out, seen = [], set()
        for item in cache["versions"]:
            v = item.get("version")
            if not v or v in seen:
                continue
            seen.add(v)
            out.append({**item, "local": v in local_set})
        for v in sorted(local_set, reverse=True):
            if v not in seen:
                out.append({"version": v, "name": v, "current": False, "local": True})
        return out

    def _action_autostart(self, payload=None) -> dict:
        try:
            from core.startup import is_launch_on_startup

            return {"ok": True, "enabled": is_launch_on_startup()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _action_set_autostart(self, payload=None) -> dict:
        enabled = bool((payload or {}).get("enabled", False))
        try:
            from core.startup import set_launch_on_startup

            set_launch_on_startup(enabled)
            return {"ok": True, "enabled": enabled, "message": f"Start on login {'enabled' if enabled else 'disabled'}."}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _action_tray_icon(self, payload=None) -> dict:
        transparent = bool(getattr(self, "_transparent_tray_icon", False))
        return {"ok": True, "transparent": transparent}

    def _action_set_tray_icon(self, payload=None) -> dict:
        transparent = bool((payload or {}).get("transparent", False))
        try:
            saved = save_bridge_config({"transparent_tray_icon": transparent})
            transparent = bool(saved.get("transparent_tray_icon", False))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self._transparent_tray_icon = transparent
        self._repaint_tray_icon()
        label = "transparent" if transparent else "visible"
        return {
            "ok": True,
            "transparent": transparent,
            "message": f"Tray icon set to {label}.",
        }

    _install_deps_status = {"state": "idle", "output": ""}

    def _action_install_deps(self, payload=None) -> dict:
        if self._install_deps_status["state"] == "running":
            return {"ok": False, "message": "Already installing."}
        self._install_deps_status = {"state": "running", "output": ""}
        threading.Thread(target=self._run_install_deps, daemon=True).start()
        return {"ok": True, "message": "Install started. Check status endpoint for progress."}

    def _action_install_deps_status(self, payload=None) -> dict:
        return {"ok": True, **self._install_deps_status}

    def _run_install_deps(self):
        import subprocess, sys
        from pathlib import Path
        from core.process_utils import ROOT_DIR, resolve_python_executable
        python = resolve_python_executable(gui=False)
        req = ROOT_DIR / "requirements.txt"
        output_lines = [f"Using python: {python}"]
        failures = 0
        def run(cmd, label):
            nonlocal failures
            output_lines.append(f">>> {label}...")
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=ROOT_DIR)
                if r.stdout: output_lines.append(r.stdout.strip())
                if r.stderr: output_lines.append(r.stderr.strip())
                if r.returncode == 0:
                    output_lines.append(f"OK: {label}")
                else:
                    output_lines.append(f"FAILED (code {r.returncode}): {label}")
                    failures += 1
            except subprocess.TimeoutExpired:
                output_lines.append(f"TIMEOUT: {label}")
                failures += 1
            except Exception as e:
                output_lines.append(f"ERROR: {label}: {e}")
                failures += 1
        run([str(python), "-m", "pip", "install", "-r", str(req)], "pip install requirements")
        run([str(python), "-m", "playwright", "install", "chromium"], "playwright install chromium")
        state = "done" if failures == 0 else "failed"
        self._install_deps_status = {"state": state, "output": "\n".join(output_lines)}

    def _action_sync_extensions(self, payload=None) -> dict:
        """Sync extension directories from a user-specified source folder
        (or from ROOT_DIR by default) into the install root so the bridge
        always serves the latest extension code.

        Payload may contain:
            source_dir (str): path to a release folder containing nyx_extension/
                              and/or nyxify_extension/.  If omitted, ROOT_DIR is
                              used (the current install root).
        """
        import shutil
        from core.process_utils import ROOT_DIR
        source = (payload or {}).get("source_dir", "").strip()
        source_path = Path(source).resolve() if source else ROOT_DIR
        if not source_path.is_dir():
            return {"ok": False, "message": f"Source folder does not exist: {source_path}"}
        install_root = ROOT_DIR
        synced = []
        errors = []
        for ext_name in ("nyx_extension", "nyxify_extension"):
            src = source_path / ext_name
            if not src.is_dir():
                errors.append(f"{ext_name}: not found in {source_path}")
                continue
            dest = install_root / ext_name
            try:
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src, dest)
                synced.append(ext_name)
                log(f"Synced {ext_name} from {src} to {dest}")
            except Exception as exc:
                errors.append(f"{ext_name}: {exc}")
        message_parts = []
        if synced:
            message_parts.append(f"Synced: {', '.join(synced)}")
        if errors:
            message_parts.append(f"Errors: {'; '.join(errors)}")
        return {
            "ok": len(synced) > 0,
            "synced": synced,
            "errors": errors,
            "message": ". ".join(message_parts) or "No extensions synced.",
        }

    def _relaunch_after_exit(self, delay: float = 3.0):
        """Best-effort relaunch of the bridge after this instance exits.

        Source installs are launched by a one-shot launcher that does NOT
        auto-restart, so after an update/rollback the app would simply vanish
        and look broken. We spawn a detached helper that waits for this process
        to exit (releasing the single-instance lock on :8869) and relaunches
        ``bridge_app.py``. Frozen builds are relaunched by the sidecar instead.
        """
        import subprocess
        import sys

        if getattr(sys, "frozen", False):
            return
        try:
            from core.process_utils import ROOT_DIR, resolve_python_executable

            python = str(resolve_python_executable(gui=(os.name == "nt")))
            bridge = str(ROOT_DIR / "bridge_app.py")
            code = (
                "import time, subprocess; "
                f"time.sleep({float(delay)}); "
                f"subprocess.Popen([{python!r}, {bridge!r}], cwd={str(ROOT_DIR)!r}, close_fds=True)"
            )
            kwargs = {"cwd": str(ROOT_DIR), "close_fds": True}
            if os.name == "nt":
                kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                )
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([python, "-c", code], **kwargs)
            log("Scheduled bridge relaunch after exit.")
        except Exception as exc:
            log(f"Relaunch scheduling failed (relaunch manually): {exc}")

    def _request_exit(self):
        """Stop servers and end the process so the sidecar can swap files."""
        self.shutdown()
        icon = self._tray_icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        self._stop.set()

    # ------------------------------------------------------------------ tray
    def open_dashboard(self, *args):
        try:
            webbrowser.open(DASHBOARD_URL)
        except Exception as exc:
            log(f"Could not open dashboard: {exc}")

    def _safe(self, fn, label):
        try:
            fn()
            log(f"{label}: ok")
        except Exception as exc:
            log(f"{label} failed: {exc}")

    def run(self):
        """Show the tray (blocking) or fall back to a headless wait loop."""
        icon = self._build_tray_icon()
        if icon is None:
            log("Tray unavailable (pystray/PIL missing) — running headless. Ctrl+C to exit.")
            # Do NOT auto-open the dashboard — it opens only on demand (extension
            # "Open Dashboard" button or tray). Opt in with NYXSUITE_OPEN_ON_START=1.
            if os.getenv("NYXSUITE_OPEN_ON_START") == "1":
                self.open_dashboard()
            try:
                while not self._stop.is_set():
                    self._stop.wait(1.0)
            except KeyboardInterrupt:
                pass
            return
        self._tray_icon = icon
        # macOS: show only the menu-bar icon, never a Python rocket in the Dock.
        self._hide_macos_dock()
        # Live color-dot status: repaint the menu-bar dot as Nyx/Nyxify start/stop.
        self._start_tray_status_updater()
        # Dashboard opens only on demand (extension button / tray "Open Dashboard").
        if os.getenv("NYXSUITE_OPEN_ON_START") == "1":
            self.open_dashboard()
        icon.run(setup=lambda _icon: self._hide_macos_dock())  # blocking until icon.stop()

    def _hide_macos_dock(self):
        """Hide the Dock icon on macOS so only the menu-bar tray icon shows.

        pystray creates an NSApplication for the status-bar item but leaves the
        default (regular) activation policy, which puts a Python rocket in the
        Dock. Switching to the Accessory policy (the menu-bar-app pattern)
        removes the Dock icon while keeping the menu-bar icon. Some Python.app
        launches need the older Process Manager transform too, so use both.
        No-op elsewhere.
        """
        from core.macos_dock import hide_macos_dock_icon

        hide_macos_dock_icon(log)

    # ---- tray status indicator (color dot per running product) --------------
    # Distinct colors: Nyx = blue, Nyxify = gray. Both running shows a split
    # dot; stopped shows a faint hollow ring (near-invisible but keeps a
    # clickable menu-bar target). Works on macOS and Windows.
    _NYX_DOT_COLOR = (59, 130, 246, 255)      # blue
    _NYXIFY_DOT_COLOR = (160, 162, 170, 255)  # gray

    def _nyx_running(self) -> bool:
        try:
            return bool(self.supervisor.is_running("nyx"))
        except Exception:
            return False

    def _nyxify_running(self) -> bool:
        try:
            return bool(self.supervisor.is_running("nyxify"))
        except Exception:
            return False

    def _make_status_dot(self, nyx_running: bool, nyxify_running: bool):
        """Return a PIL color-dot image for the current running state.

        The menu-bar image is downscaled to ~22px, so the dot fills most of the
        canvas (small padding) to stay legible. Running states are bright solid
        fills; idle is a muted-but-visible hollow ring so the item is always
        findable and clickable."""
        from PIL import Image, ImageDraw

        # A small dot in a larger transparent canvas: when the menu bar scales
        # the image down to its ~22px height, the generous padding makes the
        # colored dot render tiny (~8px) rather than filling the bar.
        size = 44
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        pad = 14
        box = [pad, pad, size - pad, size - pad]
        if nyx_running and nyxify_running:
            # Split dot: left half = Nyx (blue), right half = Nyxify (gray).
            draw.pieslice(box, 90, 270, fill=self._NYX_DOT_COLOR)
            draw.pieslice(box, 270, 450, fill=self._NYXIFY_DOT_COLOR)
        elif nyx_running:
            draw.ellipse(box, fill=self._NYX_DOT_COLOR)
        elif nyxify_running:
            draw.ellipse(box, fill=self._NYXIFY_DOT_COLOR)
        else:
            # Idle: a faint hollow ring — muted "off" state, still visible/clickable.
            draw.ellipse(box, outline=(120, 122, 130, 190), width=3)
        return img

    def _make_tray_image(self, nyx_running: bool, nyxify_running: bool):
        if sys.platform == "darwin" and bool(getattr(self, "_transparent_tray_icon", False)):
            from PIL import Image

            return Image.new("RGBA", (44, 44), (0, 0, 0, 0))
        return self._make_status_dot(nyx_running, nyxify_running)

    def _repaint_tray_icon(self):
        if self._tray_icon is None:
            return
        state = (self._nyx_running(), self._nyxify_running())
        self._apply_tray_image(
            self._make_tray_image(*state),
            self._tray_title(*state),
        )

    def _apply_tray_image(self, image, title):
        """Set the tray icon image/title, marshaling to the macOS main thread.

        pystray's macOS backend calls AppKit ``setImage_`` directly, but AppKit
        UI mutations must run on the main thread — setting ``icon.icon`` from our
        background poll thread silently fails to repaint. Dispatch it onto the
        main run loop on macOS; set it directly elsewhere (Windows/Linux are
        fine from any thread)."""
        icon = self._tray_icon
        if icon is None:
            return
        self._last_tray_image = image

        def _apply():
            try:
                icon.icon = image
            except Exception:
                pass
            try:
                icon.title = title
            except Exception:
                pass
            # macOS renders menu-bar images as monochrome "template" masks by
            # default (tinted black in light mode / white in dark mode). Force
            # the status-item button's NSImage to non-template so our color dot
            # shows in full color.
            if sys.platform == "darwin":
                self._force_color_tray_image()

        if sys.platform == "darwin":
            try:
                from Foundation import NSOperationQueue

                NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)
                return
            except Exception:
                pass
        _apply()

    def _force_color_tray_image(self):
        """On macOS, force the status-item button's image to render in color.

        Builds an NSImage straight from the current PIL dot and installs it as a
        NON-template image on the button, bypassing any monochrome/template
        tinting the menu bar applies. Runs on the main thread (called from the
        dispatched block)."""
        try:
            import io

            import AppKit
            import Foundation

            button = self._tray_icon._status_item.button()
            if button is None:
                if not getattr(self, "_tray_diag_logged", False):
                    self._tray_diag_logged = True
                    log("Tray dot diag: status-item button is None")
                return

            # Rebuild the NSImage from the PIL image we last painted so we own a
            # known-good color, non-template image regardless of pystray's cache.
            pil = getattr(self, "_last_tray_image", None)
            if pil is None:
                state = (self._nyx_running(), self._nyxify_running())
                pil = self._make_tray_image(*state)
            buf = io.BytesIO()
            pil.save(buf, "png")
            nsimg = AppKit.NSImage.alloc().initWithData_(Foundation.NSData(buf.getvalue()))
            nsimg.setTemplate_(False)
            button.setImage_(nsimg)
            try:
                button.setContentTintColor_(None)
            except Exception:
                pass
        except Exception as exc:
            if not getattr(self, "_tray_color_warned", False):
                self._tray_color_warned = True
                log(f"Could not force color tray icon: {exc}")

    def _tray_title(self, nyx_running: bool, nyxify_running: bool) -> str:
        parts = []
        if nyx_running:
            parts.append("Nyx running")
        if nyxify_running:
            parts.append("Nyxify running")
        return "Nyx Suite — " + (", ".join(parts) if parts else "idle")

    def _refresh_tray_menu(self):
        """Rebuild the tray menu so the Start/Stop enabled-state and the status
        lines track the live runner state.

        pystray only re-evaluates a menu's dynamic (callable) ``enabled`` and
        label properties when :meth:`update_menu` is called — on macOS *and*
        Windows opening the menu does NOT regenerate it (the native menu is
        built once and cached, see pystray's own docstring). So when a runner
        is started or stopped from the dashboard or a Ctrl+F7/F8 hotkey — i.e.
        not from a tray click, which pystray refreshes for us — the menu goes
        stale and can show an enabled "Start X" while X is already running (or
        vice versa). Re-running update_menu on every observed state change keeps
        it honest. On macOS ``setMenu_`` is an AppKit mutation, so marshal it
        onto the main thread exactly like the icon repaint."""
        icon = self._tray_icon
        if icon is None:
            return

        def _apply():
            try:
                icon.update_menu()
            except Exception:
                pass

        if sys.platform == "darwin":
            try:
                from Foundation import NSOperationQueue

                NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)
                return
            except Exception:
                pass
        _apply()

    def _start_tray_status_updater(self):
        """Poll the runner state and repaint the dot + rebuild the menu when it
        changes, so both the menu-bar dot and the Start/Stop items stay in sync
        with runners started/stopped from anywhere (tray, dashboard, hotkey)."""
        def loop():
            last = None
            # Small initial delay so the pystray run loop is up before the first
            # main-thread dispatch.
            self._stop.wait(1.5)
            while not self._stop.is_set():
                try:
                    state = (self._nyx_running(), self._nyxify_running())
                    if self._tray_icon is not None and state != last:
                        self._apply_tray_image(
                            self._make_tray_image(*state),
                            self._tray_title(*state),
                        )
                        # Keep the Start/Stop enabled-state and status lines
                        # honest — repainting the dot alone leaves the menu
                        # frozen at its last-built state (the "shows Start while
                        # already running" report).
                        self._refresh_tray_menu()
                        last = state
                except Exception:
                    pass
                self._stop.wait(1.5)

        threading.Thread(target=loop, daemon=True).start()

    def _build_tray_icon(self):
        if os.getenv("NYXSUITE_NO_TRAY") == "1":
            return None  # headless/server mode: serve the dashboard without a tray icon
        try:
            import pystray
        except Exception:
            return None

        try:
            image = self._make_tray_image(self._nyx_running(), self._nyxify_running())
        except Exception:
            # PIL unavailable / draw failed — fall back to the bundled icon.
            try:
                from core.ui_shared import load_tray_image

                image = load_tray_image()
            except Exception:
                image = None
        if image is None:
            return None

        def item(label, fn, **kwargs):
            return pystray.MenuItem(label, lambda icon, _it=None: self._safe(fn, label), **kwargs)

        def status_line(name, running_fn):
            # Disabled header row that shows the live state at a glance.
            return pystray.MenuItem(
                lambda _i, _n=name, _f=running_fn: f"{_n}:  {'● running' if _f() else '○ stopped'}",
                None,
                enabled=False,
            )

        # Flat menu: both products' Start and Stop are visible directly (no
        # submenu to hover into). Start is enabled only when stopped, Stop only
        # when running, so the actionable control is obvious.
        def restart(controller):
            controller.stop({})
            controller.start({"force_restart": True})

        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", lambda icon, _it=None: self.open_dashboard()),
            pystray.Menu.SEPARATOR,
            status_line("Nyx", self._nyx_running),
            item("Start Nyx", lambda: self.nyx.start({}), enabled=lambda _i: not self._nyx_running()),
            item("Stop Nyx", lambda: self.nyx.stop({}), enabled=lambda _i: self._nyx_running()),
            item("Restart Nyx", lambda: restart(self.nyx)),
            pystray.Menu.SEPARATOR,
            status_line("Nyxify", self._nyxify_running),
            item("Start Nyxify", lambda: self.nyxify.start({}), enabled=lambda _i: not self._nyxify_running()),
            item("Stop Nyxify", lambda: self.nyxify.stop({}), enabled=lambda _i: self._nyxify_running()),
            item("Restart Nyxify", lambda: restart(self.nyxify)),
            pystray.Menu.SEPARATOR,
            item("Check for Update", lambda: self._bridge_actions()["check_update"]()),
            item("Roll back to previous", lambda: self._bridge_actions()["rollback"]()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._on_exit),
        )
        return pystray.Icon(
            "nyx_suite", image,
            self._tray_title(self._nyx_running(), self._nyxify_running()),
            menu,
        )

    def _on_exit(self, icon=None, item=None):
        log("Bridge exiting (runners left running).")
        self.shutdown()
        if icon is not None:
            icon.stop()
        self._stop.set()

    def shutdown(self):
        for server in (self.dashboard, self.nyx_api, self.nyxify_api):
            try:
                if server is not None:
                    server.stop()
            except Exception:
                pass


def main():
    ensure_logs_dir()

    # Auto-register the native messaging host so browser extensions can connect.
    try:
        from agent_host.install_host import register
        register()
    except Exception as exc:
        log(f"Native messaging host registration skipped: {exc}")

    guard = RunnerLock("127.0.0.1", SINGLE_INSTANCE_PORT)
    if not guard.acquire():
        if _env_truthy("NYXSUITE_NO_OPEN"):
            log(
                f"Another Nyx Suite bridge already holds :{SINGLE_INSTANCE_PORT}. "
                "Leaving the existing dashboard tab alone because NYXSUITE_NO_OPEN=1."
            )
        else:
            log(f"Another Nyx Suite bridge already holds :{SINGLE_INSTANCE_PORT}. Opening the dashboard instead.")
            try:
                webbrowser.open(DASHBOARD_URL)
            except Exception:
                pass
        return
    try:
        # Post-update launch watchdog: if the new build has been crash-looping,
        # roll back to the previous version and exit so the sidecar relaunches it.
        try:
            from core.update_backup import confirm_or_rollback

            verdict = confirm_or_rollback()
            if verdict and verdict[0] == "rollback":
                log("Update failed verification repeatedly — rolled back to the previous version; exiting for the sidecar to relaunch it.")
                return
        except Exception as exc:
            log(f"update watchdog skipped: {exc}")

        app = BridgeApp()
        startup_marker = now()
        app.build()
        app.start_servers()
        log_timing("bridge.startup", startup_marker, "bridge")
        log("Nyx Suite bridge ready.")
        app.run()
    finally:
        guard.release()


if __name__ == "__main__":
    main()
