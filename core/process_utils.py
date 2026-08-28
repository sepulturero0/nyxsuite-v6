import os
import signal
import subprocess
import sys
from pathlib import Path

# v6 ("Nyx Suite") line: isolated app-data namespace so it never collides with
# the frozen v3 install, which uses %LOCALAPPDATA%/Nyx. Change this one constant
# to rebrand the data folder.
APP_DATA_DIR_NAME = "NyxSuite"

# Cap every PowerShell / tasklist / ps subprocess. A hung Windows process
# manager (or a slow machine) must never block the bridge watcher or a
# Start/Stop action indefinitely — without a bound a single CIM query can hang
# the whole dashboard. A timed-out lookup fails closed (returns "not running" /
# empty), which is the safe outcome for the PID-identity guard.
SUBPROCESS_TIMEOUT_SECONDS = 8.0


def get_root_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def get_app_data_dir():
    if getattr(sys, "frozen", False):
        local_appdata = os.getenv("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / APP_DATA_DIR_NAME
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / APP_DATA_DIR_NAME
        xdg_data_home = os.getenv("XDG_DATA_HOME")
        if xdg_data_home:
            return Path(xdg_data_home) / APP_DATA_DIR_NAME
        if os.name != "nt":
            return Path.home() / ".local" / "share" / APP_DATA_DIR_NAME
        return Path(sys.executable).resolve().parent
    return get_root_dir()


ROOT_DIR = get_root_dir()
APP_DATA_DIR = get_app_data_dir()
LOGS_DIR = APP_DATA_DIR / "logs"


def windows_creation_flags():

    if os.name != "nt":
        return 0

    return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP


def resolve_python_executable(gui=False):
    candidates = []

    env_override = os.getenv("NYX_PYTHONW_EXECUTABLE" if gui else "NYX_PYTHON_EXECUTABLE")
    if env_override:
        candidates.append(Path(env_override))

    if os.name == "nt":
        appdata_root = Path(os.getenv("LOCALAPPDATA", "")) / APP_DATA_DIR_NAME
        candidates.extend(
            [
                appdata_root / "venv" / "Scripts" / ("pythonw.exe" if gui else "python.exe"),
                ROOT_DIR / "venv" / "Scripts" / ("pythonw.exe" if gui else "python.exe"),
                ROOT_DIR / ".venv" / "Scripts" / ("pythonw.exe" if gui else "python.exe"),
            ]
        )
    else:
        python_name = "pythonw" if gui else "python3"
        candidates.extend(
            [
                ROOT_DIR / ".venv" / "bin" / python_name,
                ROOT_DIR / ".venv" / "bin" / "python",
                ROOT_DIR / "venv" / "bin" / python_name,
                ROOT_DIR / "venv" / "bin" / "python",
            ]
        )

    candidates.append(Path(sys.executable))

    seen = set()
    for candidate in candidates:
        candidate_path = Path(candidate)
        key = str(candidate_path).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate_path.exists():
            return candidate_path

    return Path(sys.executable)


def find_python_process_ids(script_name):
    normalized_script = str(script_name or "").strip()
    if not normalized_script:
        return []

    if os.name == "nt":
        command = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') "
            f"-and $_.CommandLine -like '*{normalized_script}*' }} | "
            "Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                check=False,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                creationflags=windows_creation_flags(),
            )
            raw = (result.stdout or "").strip()
            if not raw:
                return []

            import json

            parsed = json.loads(raw)
            if isinstance(parsed, int):
                return [parsed]
            if isinstance(parsed, list):
                return [int(pid) for pid in parsed if pid]
        except Exception:
            return []
        return []

    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except Exception:
        return []

    process_ids = []
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command_line = parts
        command_lower = command_line.lower()
        if "python" not in command_lower:
            continue
        if normalized_script.lower() not in command_lower:
            continue
        try:
            process_ids.append(int(pid_text))
        except Exception:
            continue
    return process_ids


def _parse_process_id_json(raw):
    raw_text = str(raw or "").strip()
    if not raw_text:
        return []

    try:
        import json

        parsed = json.loads(raw_text)
    except Exception:
        return []

    if isinstance(parsed, int):
        return [parsed]
    if isinstance(parsed, list):
        process_ids = []
        for pid in parsed:
            try:
                process_ids.append(int(pid))
            except Exception:
                continue
        return process_ids
    return []


def find_process_ids_by_names(process_names):
    normalized_names = []
    seen = set()
    for process_name in process_names or []:
        name = Path(str(process_name or "").strip()).name
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized_names.append(name)

    if not normalized_names:
        return []

    if os.name == "nt":
        quoted_names = ", ".join(
            "'" + name.lower().replace("'", "''") + "'" for name in normalized_names
        )
        command = (
            f"$names = @({quoted_names}); "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $names -contains $_.Name.ToLowerInvariant() } | "
            "Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                check=False,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                creationflags=windows_creation_flags(),
            )
            return _parse_process_id_json(result.stdout)
        except Exception:
            return []

    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "comm="],
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except Exception:
        return []

    wanted = {name.lower() for name in normalized_names}
    process_ids = []
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command_name = parts
        if Path(command_name).name.lower() not in wanted:
            continue
        try:
            process_ids.append(int(pid_text))
        except Exception:
            continue
    return process_ids


def ensure_logs_dir():

    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR


def is_pid_running(pid):

    if not pid:
        return False

    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                creationflags=windows_creation_flags()
            )
            return str(pid) in result.stdout
        except Exception:
            return False

    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _process_identity(pid):
    """Return ``(image_name_lower, command_line_lower)`` for ``pid``.

    Returns ``("", "")`` when the process cannot be inspected. Used to confirm a
    PID actually belongs to one of our runners before trusting or killing it —
    Windows (and, less aggressively, POSIX) recycle PIDs, so a stale pid file
    whose number now points at an unrelated process (commonly ``chrome.exe``)
    must never be tree-killed. See :func:`pid_matches`.
    """
    if not pid:
        return "", ""

    if os.name == "nt":
        command = (
            f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | "
            "Select-Object Name, CommandLine | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                check=False,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                creationflags=windows_creation_flags(),
            )
            raw = (result.stdout or "").strip()
            if not raw:
                return "", ""
            import json

            data = json.loads(raw)
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                return "", ""
            name = str(data.get("Name") or "").strip().lower()
            cmdline = str(data.get("CommandLine") or "").strip().lower()
            return name, cmdline
        except Exception:
            return "", ""

    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except Exception:
        return "", ""
    cmdline = (result.stdout or "").strip()
    if not cmdline:
        return "", ""
    # First token is the executable path; its basename is the image name.
    first = cmdline.split(None, 1)[0]
    image = Path(first).name.lower()
    return image, cmdline.lower()


def pid_matches(pid, process_names=None, cmdline_substring=""):
    """True if the live process ``pid`` looks like one of our runners.

    Matches when the process image basename is in ``process_names`` OR its
    command line contains ``cmdline_substring`` (e.g. the full path to
    ``main.py``). Fails **closed**: if the identity can't be read, returns
    ``False`` so a PID we cannot confirm is ours is never killed. This is the
    guard that stops a recycled pid-file PID (now ``chrome.exe``) from being
    tree-killed when the GUI starts/stops runners.
    """
    if not pid:
        return False
    names = {
        Path(str(name or "").strip()).name.lower()
        for name in (process_names or [])
        if str(name or "").strip()
    }
    substring = str(cmdline_substring or "").strip().lower()
    if not names and not substring:
        return False
    image, cmdline = _process_identity(pid)
    if not image and not cmdline:
        return False
    if names and image in names:
        return True
    if substring and cmdline and substring in cmdline:
        return True
    return False


def read_pid_file(pid_file):

    try:
        if Path(pid_file).exists():
            return int(Path(pid_file).read_text(encoding="utf-8").strip())
    except Exception:
        return None

    return None


def write_pid_file(pid_file, pid):

    ensure_logs_dir()
    Path(pid_file).write_text(str(pid), encoding="utf-8")


def clear_pid_file(pid_file):

    pid_path = Path(pid_file)
    if pid_path.exists():
        pid_path.unlink()


def stop_process_tree(pid):

    if not pid:
        return False

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                creationflags=windows_creation_flags()
            )
            return True
        except Exception:
            return False

    try:
        process_group_id = os.getpgid(int(pid))
        os.killpg(process_group_id, signal.SIGTERM)
        return True
    except Exception:
        try:
            os.kill(int(pid), signal.SIGTERM)
            return True
        except Exception:
            return False


def force_kill_process_tree(pid):
    """SIGKILL escalation for a process (group) that survived stop_process_tree.

    Windows' ``taskkill /T /F`` is already forceful, so re-running it is the
    escalation there; on POSIX this sends SIGKILL to the process group (falling
    back to the single pid) for runners stuck in a call that swallowed SIGTERM."""
    if not pid:
        return False

    if os.name == "nt":
        return stop_process_tree(pid)

    try:
        process_group_id = os.getpgid(int(pid))
        os.killpg(process_group_id, signal.SIGKILL)
        return True
    except Exception:
        try:
            os.kill(int(pid), signal.SIGKILL)
            return True
        except Exception:
            return False


def start_background_process(command, cwd, stdout_path, stderr_path, env=None):

    ensure_logs_dir()

    with open(stdout_path, "a", encoding="utf-8") as stdout_handle, open(stderr_path, "a", encoding="utf-8") as stderr_handle:
        popen_kwargs = {
            "cwd": str(cwd),
            "stdout": stdout_handle,
            "stderr": stderr_handle,
            "env": env,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = windows_creation_flags()
        else:
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(
            command,
            **popen_kwargs,
        )

    return process
