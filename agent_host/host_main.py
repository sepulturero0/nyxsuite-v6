#!/usr/bin/env python3
"""Native messaging host for Nyx Suite extension <-> bridge agent.

Protocol (stdin/stdout):
  Read:  4 bytes (uint32 LE) = message length, then that many bytes = UTF-8 JSON
  Write: same format for response

Messages (extension -> host):
  {"type": "ping"}               -> {"ok": true, "version": "..."}
  {"type": "connect"}            -> {"ok": true, "token": "..."}  (agent running)
  {"type": "get_token"}          -> {"ok": true, "token": "..."}
  {"type": "start_agent"}        -> {"ok": true, "message": "started"} or {"ok": false, "error": "..."}
  {"type": "agent_status"}       -> {"ok": true, "running": bool, "pid": int|null}

Start agent: runs bridge_app.py with NYXSUITE_NO_TRAY=1 as a subprocess.
Token: stored at the agent's token file; we read it and hand back.
"""

import json
import os
import plistlib
import struct
import subprocess
import sys
from pathlib import Path

# Ensure the project root is on sys.path so core.* modules are importable
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from core.agent_token import read_token as _read_agent_token, TOKEN_FILE as _AGENT_TOKEN_FILE

MACOS_LAUNCHD_LABEL = "com.nyxsuite.bridge"


def _read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    length = struct.unpack("<I", raw)[0]
    payload = sys.stdin.buffer.read(length)
    return json.loads(payload.decode("utf-8"))


def _write_message(msg):
    data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _get_agent_pid(lock_port=8869):
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        return sock.connect_ex(("127.0.0.1", lock_port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _macos_launchd_agent_path():
    return (
        Path.home() / "Library" / "Application Support" / "NyxSuite"
        / "launchd" / f"{MACOS_LAUNCHD_LABEL}.plist"
    )


def _write_macos_launchd_agent(path: Path, cmd, cwd: Path, env: dict):
    clean_env = {
        str(key): str(value)
        for key, value in (env or {}).items()
        if value not in (None, "")
    }
    plist = {
        "Label": MACOS_LAUNCHD_LABEL,
        "ProgramArguments": [str(part) for part in cmd],
        "WorkingDirectory": str(cwd),
        "RunAtLoad": True,
        "KeepAlive": False,
        "EnvironmentVariables": clean_env,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist, sort_keys=False))
    return path


def _run_launchctl(args):
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _start_agent_via_launchd(cmd, env, root: Path):
    """Start the bridge as a per-user launchd job on macOS.

    Chrome's native-messaging host process is a poor TCC parent for GUI
    automation: children can inherit Chrome's Accessibility trust state. Letting
    launchd spawn the bridge gives macOS a stable Python/Nyx Suite process to
    grant, which is what the AdsPower GUI fallback needs.
    """
    path = _write_macos_launchd_agent(
        path=_macos_launchd_agent_path(),
        cmd=cmd,
        cwd=root,
        env=env,
    )
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{MACOS_LAUNCHD_LABEL}"
    _run_launchctl(["bootout", service])
    result = _run_launchctl(["bootstrap", domain, str(path)])
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        return {
            "ok": False,
            "error": f"launchd start failed: {message or result.returncode}",
        }
    _run_launchctl(["kickstart", "-k", service])
    return {"ok": True, "message": "Agent started via launchd."}


def _start_agent():
    root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    # Show the menu-bar / system-tray icon when the bridge is started from the
    # extension so the user can manage it without a terminal. (No console window
    # appears: on Windows we spawn with CREATE_NO_WINDOW, on macOS the dock icon
    # is suppressed in bridge_app so only the menu-bar icon shows.)
    env.pop("NYXSUITE_NO_TRAY", None)
    env["NYXSUITE_NO_OPEN"] = "1"
    if getattr(sys, "frozen", False):
        candidates = ["bridge_app.exe", "bridge_app", "bridge_app.app"]
        exe = ""
        for name in candidates:
            candidate = str((Path(sys.executable).resolve().parent / name).resolve())
            if Path(candidate).exists():
                exe = candidate
                break
            app_binary = str((Path(sys.executable).resolve().parent / name / "Contents" / "MacOS" / "bridge_app").resolve())
            if Path(app_binary).exists():
                exe = app_binary
                break
        if not exe:
            return {"ok": False, "error": "bridge_app executable not found"}
        cmd = [exe]
    else:
        # Launch the bridge with the project's venv Python, NOT the interpreter
        # running this host. On macOS the host is started by Chrome via the
        # system python3 (manifest shebang), which lacks the bridge's deps
        # (pystray, playwright, requests). resolve_python_executable() finds the
        # .venv/venv interpreter the portable launcher created on first setup.
        try:
            from core.process_utils import resolve_python_executable
            python = str(resolve_python_executable(gui=False))
        except Exception:
            python = sys.executable
        cmd = [python, str(root / "bridge_app.py")]
    try:
        if sys.platform == "darwin":
            launchd_result = _start_agent_via_launchd(cmd, env, root)
            if launchd_result.get("ok"):
                return launchd_result
            # Fall back to the original direct spawn so Connect still has a
            # chance to work on systems where launchctl is unavailable.
        # Detach the bridge's stdio from THIS host's pipes. Chrome connects the
        # native host's stdin/stdout to its native-messaging channel; if the
        # spawned bridge inherits them, its startup writes (logs, register()
        # prints) land in Chrome's pipe and — once Chrome closes the one-shot
        # channel — trigger SIGPIPE/garbage, which Chrome reports as
        # "Native host has exited". Redirecting to DEVNULL fixes that. The bridge
        # logs to its own file, so nothing useful is lost.
        popen_kwargs = {
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **popen_kwargs)
        return {"ok": True, "message": "Agent started."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main():
    msg = _read_message()
    if msg is None:
        sys.exit(0)
    msg_type = msg.get("type", "")

    if msg_type == "ping":
        try:
            from core.version import NYX_VERSION as _v
        except Exception:
            _v = ""
        _write_message({"ok": True, "version": _v})
    elif msg_type == "get_token":
        token = _read_agent_token()
        _write_message({"ok": bool(token), "token": token} if token else {"ok": False, "error": "Agent token not found. Start the agent first."})
    elif msg_type == "connect":
        running = _get_agent_pid()
        if not running:
            result = _start_agent()
            if not result.get("ok"):
                _write_message(result)
                return
            import time
            time.sleep(0.5)
        token = _read_agent_token()
        if not token:
            _write_message({"ok": False, "error": "Agent started but token not yet available. Try again."})
        else:
            _write_message({"ok": True, "token": token, "running": True})
    elif msg_type == "start_agent":
        result = _start_agent()
        _write_message(result)
    elif msg_type == "stop_agent":
        running = _get_agent_pid()
        if not running:
            _write_message({"ok": True, "message": "Agent not running."})
            return
        try:
            import urllib.request
            token = _read_agent_token()
            req = urllib.request.Request(
                "http://127.0.0.1:8870/bridge/shutdown",
                data=b"{}",
                headers={"Content-Type": "application/json", "X-Nyx-Token": token or ""},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            _write_message({"ok": True, "message": "Agent stopped."})
        except Exception as exc:
            _write_message({"ok": False, "error": str(exc)})
    elif msg_type == "agent_status":
        token = _read_agent_token()
        running = _get_agent_pid()
        _write_message({"ok": True, "running": running, "token": token or ""})
    else:
        _write_message({"ok": False, "error": f"Unknown message type: {msg_type}"})


def _classify_failure(exc):
    message = str(exc).lower()
    if any(word in message for word in ("localappdata", "venv", "pystray", "playwright", "requests", "import")):
        return "python"
    if any(word in message for word in ("already in use", "address", "bind", "connect", "socket", "port")):
        return "network/port"
    if any(word in message for word in ("denied", "permission", "read-only", "ro", "locked")):
        return "permissions"
    if any(word in message for word in ("manifest", "register", "native", "chrome")):
        return "native messaging"
    return "unknown"


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Reply with a real error instead of dying mid-handshake — otherwise
        # Chrome only reports the generic "Native host has exited". Also log the
        # traceback + an actionable category so the failure can be diagnosed.
        import traceback

        try:
            _write_message({"ok": False, "error": f"native host crashed: {exc}"})
        except Exception:
            pass
        try:
            log_path = Path(__file__).resolve().parent / "host_error.log"
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"native host failure [{_classify_failure(exc)}]: {exc}\n")
                handle.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        sys.exit(1)
