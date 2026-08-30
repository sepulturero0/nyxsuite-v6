# Nyx Suite v6 Packaging

Builds Nyx Suite v6 release artifacts. The source-ZIP release works on Windows
and macOS/Linux; the Windows PyInstaller scripts also build the bridge and runner
executables when needed.

See [`V6_RELEASE.md`](V6_RELEASE.md) and the root [`RELEASE.md`](../RELEASE.md)
for the release checklist.

## Scripts

- `build_updater.ps1` — builds `dist/Updater.exe` (the update/rollback sidecar).
- `build_bridge.ps1` — builds `bridge.spec`, `nyx_bot.spec`, and `nyxify_runner.spec`,
  then assembles `release/v6/NyxSuite <version>/` (bridge + both runners + `Updater.exe`
  + `update_config.json` + `VERSION`). Runs `build_updater.ps1` first unless `-SkipUpdater`.
- `create_release_zip.sh` / `create_release_zip.ps1` — create the source-based
  update ZIP used by the dashboard updater on Windows and macOS.

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_bridge.ps1
```

## Requirements

- Windows
- AdsPower installed and running on the target machine
- A v6 venv at `venv\Scripts\python.exe` or `%LOCALAPPDATA%\NyxSuite\venv\Scripts\python.exe`,
  with `requirements.txt` + `PyInstaller` installed and the Playwright browser present
- The local license secret (`prepare_license_runtime_secret.ps1`, invoked automatically by the build)

## Release

Publish `NyxSuite-v<version>.zip` to the public `sepulturero0/nyxsuite-v6`
GitHub release. The `update_config.json` baked into the build points the in-app
updater at that same repo.

## Scope

Windows PyInstaller packaging is Windows-only. The source ZIP update path and
portable launchers (`run_nyx_suite.command` / `run_nyx_suite.sh`) support macOS
and Linux.
