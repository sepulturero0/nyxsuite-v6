@echo off
REM Native messaging host launcher. Prefer the project venv Python, then the
REM machine-local v6 venv (%LOCALAPPDATA%\NyxSuite\venv - the same tree the
REM portable launcher creates), then the py launcher, then python on PATH.
REM Output must stay clean (host protocol only), so nothing is echoed.
set "HD=%~dp0"
if exist "%HD%..\venv\Scripts\python.exe" (
  "%HD%..\venv\Scripts\python.exe" "%HD%host_main.py"
  goto :eof
)
if exist "%LOCALAPPDATA%\NyxSuite\venv\Scripts\python.exe" (
  "%LOCALAPPDATA%\NyxSuite\venv\Scripts\python.exe" "%HD%host_main.py"
  goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%HD%host_main.py"
  goto :eof
)
python "%HD%host_main.py"
