param(
    [string]$Version = "",
    [string]$OutputDir = ""
)

<#
.SYNOPSIS
Creates the cross-platform NyxSuite release ZIP containing everything
needed to run from source on Windows, macOS, or Linux:

  NyxSuite/
  ├── core/                  (Python modules — bridge, runners, updater, etc.)
  ├── webui/                 (dashboard HTML/JS/CSS)
  ├── agent_host/            (native-messaging host)
  ├── nyx_extension/         (browser extension)
  ├── nyxify_extension/      (browser extension)
  ├── data/                  (configs, databases, templates)
  ├── agent_token.txt
  ├── bridge_app.py          (main entry point — launch this)
  ├── main.py                (Nyx runner)
  ├── nyxify_runner.py       (Nyxify runner)
  ├── requirements.txt
  ├── run_nyx_suite.bat      (Windows launcher)
  ├── run_nyx_suite.sh       (macOS/Linux terminal launcher)
  ├── run_nyx_suite.command  (macOS Finder launcher)
  ├── portable_launch_nyx.ps1
  ├── portable_launch_nyx.sh
  ├── .env.example
  ├── VERSION
  └── update_config.json

No PyInstaller binaries — runs via ``python bridge_app.py``.
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# ---- resolve version ----
if (-not $Version) {
    $venvPython = Join-Path $root "venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        throw "No venv python found at $venvPython. Either create the venv or pass -Version explicitly."
    }
    $Version = (& $venvPython -c "from core.version import NYX_VERSION; import sys; sys.stdout.write(NYX_VERSION)").Trim()
}
if (-not $Version) { throw "Could not determine version." }
$label = "v$Version"

$archiveName = "NyxSuite-$label"
$zipName = "$archiveName.zip"

# ---- resolve output dir ----
if (-not $OutputDir) {
    $OutputDir = Join-Path $root "dist"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

# ---- temp staging ----
$tmp = Join-Path $root "tmp_release_zip"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
$stage = Join-Path $tmp $archiveName
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Write-Host "[create_release_zip] Assembling $archiveName ..."

# ---- Python source directories ----
$dirs = @(
    "core",
    "webui",
    "agent_host",
    "utils",
    "snap_selectors",
    "scripts",
    "ui_templates"
)
foreach ($d in $dirs) {
    $src = Join-Path $root $d
    $dest = Join-Path $stage $d
    if (Test-Path $src) {
        Write-Host "  + $d/"
        Copy-Item -Recurse -Force $src $dest
        Remove-Item -Recurse -Force (Join-Path $dest "__pycache__") -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force (Join-Path $dest ".git") -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force (Join-Path $dest ".pytest_cache") -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force (Join-Path $dest "*.pyc") -ErrorAction SilentlyContinue
    }
}

# ---- native-host manifest: ship the generic template ----
# register() rewrites the manifest path per-machine on the user's PC, so never
# ship a machine-specific absolute path in the release.
$agentManifest = Join-Path $stage "agent_host\com.nyxsuite.agent.json"
if (Test-Path $agentManifest) {
    try {
        $m = Get-Content -Raw $agentManifest | ConvertFrom-Json
        $m.path = "agent_host/host_main.py"
        $json = $m | ConvertTo-Json -Depth 10
        # BOM-less UTF-8 — a BOM would break json.loads in install_host.register().
        [System.IO.File]::WriteAllText($agentManifest, $json, [System.Text.UTF8Encoding]::new($false))
        Write-Host "  ~ reset agent_host manifest path to template"
    } catch { }
}

# ---- browser extensions ----
foreach ($ext in @("nyx_extension", "nyxify_extension")) {
    $src = Join-Path $root $ext
    $dest = Join-Path $stage $ext
    if (Test-Path $src) {
        Write-Host "  + $ext/"
        Copy-Item -Recurse -Force $src $dest
        Remove-Item -Recurse -Force (Join-Path $dest "__pycache__") -ErrorAction SilentlyContinue
    }
}

# ---- data/ (template defaults, exclude runtime DBs) ----
$dataSrc = Join-Path $root "data"
$dataDest = Join-Path $stage "data"
if (Test-Path $dataSrc) {
    Write-Host "  + data/"
    Copy-Item -Recurse -Force $dataSrc $dataDest
    # Remove machine-local runtime database files — they are created on first launch
    Get-ChildItem -Recurse -Path $dataDest -Filter "*.db" | Remove-Item -Force
    Write-Host "    (removed *.db runtime databases)"
} else {
    New-Item -ItemType Directory -Force -Path $dataDest | Out-Null
}

# ---- root-level Python & resource files ----
# NOTE: SETUP_README.txt lives inside packaging/ — copy from there.
$setupReadme = Join-Path $root "packaging\SETUP_README.txt"
if (Test-Path $setupReadme) {
    Copy-Item -Force $setupReadme (Join-Path $stage "SETUP_README.txt")
    Write-Host "  + SETUP_README.txt"
}

$rootFiles = @(
    "bridge_app.py",
    "main.py",
    "nyxify_runner.py",
    "requirements.txt",
    "run_nyx_suite.bat",
    "run_nyx_suite.sh",
    "run_nyx_suite.command",
    "portable_launch_nyx.ps1",
    "portable_launch_nyx.sh",
    ".env.example",
    "icons8-origami-50.ico",
    "icons8-origami-50.png",
    "icons8-origami-50-gray.ico",
    "icons8-origami-50-gray.png"
)
foreach ($f in $rootFiles) {
    $src = Join-Path $root $f
    if (Test-Path $src) {
        Copy-Item -Force $src (Join-Path $stage $f)
        Write-Host "  + $f"
    }
}

# ---- VERSION ----
$versionFile = Join-Path $stage "VERSION"
$Version | Set-Content -Path $versionFile -Encoding ASCII -NoNewline
Write-Host "  + VERSION ($Version)"

# ---- update_config.json ----
$updateConfig = @"
{
  "app": "nyxsuite",
  "repo": "sepulturero0/nyxsuite-v6",
  "asset_pattern": "NyxSuite-v*.zip",
  "exe_to_relaunch": "",
  "data_preserve_paths": [
    "data/*.db",
    "data/bridge_config.json",
    "data/nyx_config.json",
    "data/nyxify_config.json",
    "data/bitmoji_models.json",
    "data/bitmoji_outfits.json",
    "data/full_auto_usernames/*",
    "data/signup_names/*",
    "data/logs/*"
  ]
}
"@
$configPath = Join-Path $stage "update_config.json"
[System.IO.File]::WriteAllText($configPath, $updateConfig, [System.Text.UTF8Encoding]::new($false))
Write-Host "  + update_config.json"

# ---- SECURITY: never publish the license-GENERATION activator ----
# tools/ and the run_activator_ui.* launchers are admin-only (they sign licenses
# with the local secret). They are not in the copied dir/file lists above, but we
# strip-and-assert here so a future edit can never leak them into the public ZIP.
$forbidden = @(
    (Join-Path $stage "tools"),
    (Join-Path $stage "activator.html"),
    (Join-Path $stage "activator_server.py"),
    (Join-Path $stage "run_activator_ui.ps1"),
    (Join-Path $stage "run_activator_ui.bat"),
    (Join-Path $stage "run_activator_ui.sh"),
    (Join-Path $stage "run_activator_ui.command"),
    # License secrets are git-ignored locally but Copy-Item core/ would otherwise
    # pull them into the public ZIP — a license-bypass risk. Strip them.
    # - license_runtime_secret.py : legacy symmetric HMAC secret (v1).
    # - license_signing_key.py    : RSA PRIVATE signing key (v2) — catastrophic if leaked.
    (Join-Path $stage "core\license_runtime_secret.py"),
    (Join-Path $stage "core\license_signing_key.py")
)
foreach ($f in $forbidden) {
    if (Test-Path $f) { Remove-Item -Recurse -Force $f; Write-Host "  - stripped (never publish): $f" }
}
$leak = Get-ChildItem -Recurse -Path $stage -Filter "*activator*" -ErrorAction SilentlyContinue
if ($leak) { throw "Refusing to build: activator/license-generation files found in release stage: $($leak.FullName -join ', ')" }
$secretLeak = Get-ChildItem -Path $stage -Recurse -Include "*runtime_secret*","*signing_key*" -ErrorAction SilentlyContinue
if ($secretLeak) { throw "Refusing to build: license secret/private key found in release stage: $($secretLeak.FullName -join ', ')" }

# ---- create ZIP (forward-slash entry names — cross-platform safe) ----
# Compress-Archive on Windows PowerShell 5.1 stores Windows-style backslash path
# separators in the ZIP. Python's zipfile on macOS/Linux treats '\' as a literal
# filename character, so the subfolders are never created and the in-app updater
# syncs 0 files (the macOS "Updated to X.Y.Z (source dirs: 0 ...)" bug). We build
# the archive manually so every entry uses '/' separators.
$zipPath = Join-Path $OutputDir $zipName
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

Add-Type -AssemblyName System.IO.Compression | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null
$stageFull = (Resolve-Path $stage).Path.TrimEnd('\')
$prefix = Split-Path $stageFull -Leaf
$fs = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::CreateNew)
try {
    $zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        Get-ChildItem -Path $stage -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($stageFull.Length + 1) -replace '\\', '/'
            $entryName = "$prefix/$rel"
            $entry = $zip.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
            $es = $entry.Open()
            try {
                $in = [System.IO.File]::OpenRead($_.FullName)
                try { $in.CopyTo($es) } finally { $in.Dispose() }
            } finally { $es.Dispose() }
        }
    } finally { $zip.Dispose() }
} finally { $fs.Dispose() }

# ---- cleanup ----
Remove-Item -Recurse -Force $tmp

Write-Host ""
Write-Host "[create_release_zip] Done: $zipPath"
Write-Host "[create_release_zip] Size: $((Get-Item $zipPath).Length / 1KB) KB"
Write-Host ""
Write-Host "Upload to GitHub Releases:"
Write-Host "  gh release upload $label `"$zipPath`" --repo sepulturero0/nyxsuite-v6"
