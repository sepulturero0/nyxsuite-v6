param(
    [switch]$SkipUpdater
)

# Builds the Nyx Suite v6 line: the bridge tray app (with the webui/ dashboard
# bundled) plus the Nyx and Nyxify runner exes, then assembles release\v6\.
#
# REQUIRES (Windows): a v6 venv with deps installed, Playwright browsers for the
# runners, and the local license secret + .env copied in. See packaging\V6_RELEASE.md.
# This script was authored from the v3 build pattern; verify the first build.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $root "dist"
$releaseRoot = Join-Path $root "release"
$packageRoot = Join-Path $releaseRoot "v6"
$bridgeSpec = Join-Path $PSScriptRoot "bridge.spec"
$nyxBotSpec = Join-Path $PSScriptRoot "nyx_bot.spec"
$nyxifyRunnerSpec = Join-Path $PSScriptRoot "nyxify_runner.spec"
$updaterStaged = Join-Path $distRoot "Updater.exe"

function Test-PythonLauncher($path) {
    if (!(Test-Path $path)) { return $false }
    try {
        $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        & $path -c "import sys" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false } finally { $ErrorActionPreference = $prev }
}

function Resolve-PackagingPython {
    $candidates = @( (Join-Path $root "venv\Scripts\python.exe") )
    if ($env:LOCALAPPDATA) { $candidates += Join-Path $env:LOCALAPPDATA "NyxSuite\venv\Scripts\python.exe" }
    foreach ($c in $candidates) { if (Test-PythonLauncher $c) { return $c } }
    throw "No working v6 packaging Python found. Create the venv (see packaging\V6_RELEASE.md)."
}

$venvPython = Resolve-PackagingPython

& $venvPython (Join-Path $root "scripts\sync_version.py")
if ($LASTEXITCODE -ne 0) { throw "scripts/sync_version.py failed (exit $LASTEXITCODE)" }

$label = (& $venvPython -c "from core.version import NYX_VERSION_LABEL;import sys;sys.stdout.write(NYX_VERSION_LABEL)").Trim()
$version = (& $venvPython -c "from core.version import NYX_VERSION;import sys;sys.stdout.write(NYX_VERSION)").Trim()
if (-not $label -or -not $version) { throw "Could not read version from core/version.py" }

$bridgeName = "NyxSuite $label"
$nyxBotName = "NyxBot $label"
$nyxifyRunnerName = "NyxifyRunner $label"
$releaseApp = Join-Path $packageRoot $bridgeName

if (-not $SkipUpdater) {
    $buildUpdater = Join-Path $PSScriptRoot "build_updater.ps1"
    if (Test-Path $buildUpdater) { & $buildUpdater }
}

& $venvPython -m PyInstaller --noconfirm --clean $bridgeSpec
& $venvPython -m PyInstaller --noconfirm --clean $nyxBotSpec
& $venvPython -m PyInstaller --noconfirm --clean $nyxifyRunnerSpec

New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null
if (Test-Path $releaseApp) { Remove-Item -Recurse -Force $releaseApp }
New-Item -ItemType Directory -Force -Path $releaseApp | Out-Null

# Bridge (carries webui/ via the spec Tree) + both runner exes into one folder.
Copy-Item -Recurse -Force (Join-Path $distRoot "$bridgeName\*") $releaseApp
Copy-Item -Recurse -Force (Join-Path $distRoot "$nyxBotName\*") $releaseApp
Copy-Item -Recurse -Force (Join-Path $distRoot "$nyxifyRunnerName\*") $releaseApp

foreach ($folder in @("data", "logs")) {
    $t = Join-Path $releaseApp $folder
    if (!(Test-Path $t)) { New-Item -ItemType Directory -Force -Path $t | Out-Null }
}
if (Test-Path (Join-Path $root ".env.example")) {
    Copy-Item -Force (Join-Path $root ".env.example") (Join-Path $releaseApp ".env.example")
}
$version | Set-Content -Path (Join-Path $releaseApp "VERSION") -Encoding ASCII -NoNewline

if (Test-Path $updaterStaged) {
    Copy-Item -Force $updaterStaged (Join-Path $releaseApp "Updater.exe")
    Copy-Item -Force $updaterStaged (Join-Path $releaseApp "Updater.new.exe")
} else {
    Write-Warning "Updater.exe not found at $updaterStaged. Run packaging\build_updater.ps1 first for the in-app updater/rollback."
}

# v6 update_config.json -> the public source/release repo (create it on GitHub; keep the
# name here in sync). skip_paths includes local_update_backups so backups are
# never overwritten or shipped.
$updateConfig = @"
{
  "app": "nyxsuite",
  "repo": "sepulturero0/nyxsuite-v6",
  "asset_pattern": "NyxSuite-v*.zip",
  "exe_to_relaunch": "$bridgeName.exe",
  "skip_paths": ["data", "logs", ".env", "local_update_backups"]
}
"@
[System.IO.File]::WriteAllText((Join-Path $releaseApp "update_config.json"), $updateConfig, [System.Text.UTF8Encoding]::new($false))

@"
@echo off
cd /d "%~dp0"
start "" "$bridgeName.exe"
"@ | Set-Content -Path (Join-Path $releaseApp "start_suite.bat") -Encoding ASCII

Write-Host "Nyx Suite v6 release prepared at: $releaseApp"

# ---- create the clean release ZIP for GitHub Releases ----
$createZipScript = Join-Path $PSScriptRoot "create_release_zip.ps1"
if (Test-Path $createZipScript) {
    Write-Host ""
    Write-Host "Creating clean release ZIP via create_release_zip.ps1 ..."
    & $createZipScript -Version $version
} else {
    Write-Warning "create_release_zip.ps1 not found; skipping clean release ZIP."
    Write-Host "Next: compress it to 'NyxSuite-$label.zip' and publish release '$label' to sepulturero0/nyxsuite-v6."
}
