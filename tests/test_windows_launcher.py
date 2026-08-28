from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_run_nyx_suite_bat_runs_from_its_own_folder_and_pauses_on_failure():
    launcher = (ROOT / "run_nyx_suite.bat").read_text(encoding="ascii").lower()

    assert 'cd /d "%~dp0"' in launcher
    assert 'set "exit_code=%errorlevel%"' in launcher
    assert 'if not "%exit_code%"=="0" pause' in launcher
    assert "exit /b %exit_code%" in launcher


def test_windows_powershell_launcher_quotes_entry_script_for_spaced_paths():
    launcher = (ROOT / "portable_launch_nyx.ps1").read_text(encoding="ascii")

    assert "function Quote-ProcessArgument" in launcher
    assert "Quote-ProcessArgument -Value $Path" in launcher
    assert "Start-Process -FilePath $pythonExecutable -ArgumentList @($scriptArgument)" in launcher


def test_powershell_launcher_unifies_v6_paths_under_nyxsuite():
    launcher = (ROOT / "portable_launch_nyx.ps1").read_text(encoding="ascii")

    # v6 path unification: install/app-data must live under NyxSuite (matching
    # core/process_utils APP_DATA_DIR_NAME), so the machine-local venv the
    # launcher creates is the same one the native host and bridge resolve.
    assert 'Join-Path $env:LOCALAPPDATA "NyxSuite"' in launcher


def test_powershell_launcher_logs_actionable_failures():
    launcher = (ROOT / "portable_launch_nyx.ps1").read_text(encoding="ascii")

    # Actionable, categorized failure logging for Python / ports / permissions /
    # native messaging / AdsPower.
    assert "function Get-FailureCategory" in launcher
    assert '"python"' in launcher
    assert '"port"' in launcher
    assert '"permission"' in launcher
    assert '"native_messaging"' in launcher
    assert '"adspower"' in launcher
    assert "Actionable fixes" in launcher
    assert 'Failure category: "' in launcher


def test_host_main_bat_prefers_and_falls_back_to_nyxsuite_venvs():
    launcher = (ROOT / "agent_host" / "host_main.bat").read_text(encoding="ascii")

    # Project venv first, then the machine-local v6 venv under NyxSuite, so the
    # native messaging host runs with the same interpreter the launcher built.
    assert 'if exist "%HD%..\\venv\\Scripts\\python.exe"' in launcher
    assert '"%HD%..\\venv\\Scripts\\python.exe" "%HD%host_main.py"' in launcher
    assert 'if exist "%LOCALAPPDATA%\\NyxSuite\\venv\\Scripts\\python.exe"' in launcher
    assert '"%LOCALAPPDATA%\\NyxSuite\\venv\\Scripts\\python.exe" "%HD%host_main.py"' in launcher
    # Output stays clean (host protocol) — no text-echo statements.
    assert "@echo off" in launcher
