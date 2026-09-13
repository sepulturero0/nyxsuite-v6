#requires -version 5.1
<#
.SYNOPSIS
  NyxSuite Windows live performance probe v2 (read-only evidence collection).

.DESCRIPTION
  One-shot collector for the NyxSuite Windows performance investigation. It never
  edits application code and never logs secrets. It gathers everything needed in
  a single run:

    env        - install root, venv/python, launcher stage timings (the 3 probes
                 portable_launch_nyx.ps1 runs before starting the bridge)
    endpoints  - timed /bridge/status, :8865/status, :8866/status
    toggle     - multi-cycle bridge OFF then ON timing
    sample     - fast CPU/RAM process sampling
    all        - endpoints, toggle, sample, then interactive popup capture

  Popup open times cannot be automated reliably, so the script prompts for them
  at the end (open the popups during the sample window, then type the numbers).

.PARAMETER Mode
  env | endpoints | toggle | sample | all

.PARAMETER Seconds
  Duration of the process sampling window.

.PARAMETER IntervalMs
  Target process sampling interval (kept low; the fast sampler honors it).

.PARAMETER Cycles
  Number of OFF/ON toggle cycles to measure (default 3).

.PARAMETER OutDir
  Output directory. Defaults to %TEMP%\nyxsuite-perf-<utcstamp>.

.PARAMETER InstallRoot
  Folder containing bridge_app.py and portable_launch_nyx.ps1. Auto-detected
  from the running bridge when omitted.

.PARAMETER OnDelaySeconds
  Seconds to wait after OFF before ON (default 3).

.PARAMETER NoPrompt
  Skip the interactive popup/task-manager prompts.
#>
param(
    [ValidateSet("env", "endpoints", "toggle", "sample", "all")]
    [string]$Mode = "all",
    [int]$Seconds = 60,
    [int]$IntervalMs = 200,
    [int]$Cycles = 3,
    [string]$OutDir = "",
    [string]$InstallRoot = "",
    [double]$OnDelaySeconds = 3,
    [switch]$NoPrompt
)

$ErrorActionPreference = "Continue"

# Make the bridge log its internal startup timing during toggle cycles. Child
# launches (launcher -> pythonw bridge) inherit these.
$env:NYXSUITE_TIMING = "1"
if (-not $env:NYXSUITE_NO_OPEN) { $env:NYXSUITE_NO_OPEN = "1" }

function Get-UtcStamp {
    return (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmmss'Z'")
}

function Write-Utc {
    param([string]$Message, [string]$File = "")
    $line = "$((Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")) $Message"
    Write-Host $line
    if ($File) { Add-Content -LiteralPath $File -Value $line }
}

function Resolve-InstallRoot {
    param([string]$Value, [string]$Here)
    $candidates = @()
    if ($Value) { $candidates += $Value }

    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
        foreach ($p in $procs) {
            if ($p.CommandLine -and $p.CommandLine -match '"([^"]*bridge_app\.py)"') {
                $dir = Split-Path -Parent $matches[1]
                if ($dir) { $candidates += $dir }
            }
        }
    } catch { }

    $candidates += @(
        (Join-Path $env:LOCALAPPDATA "NyxSuite\app"),
        $Here,
        (Split-Path -Parent $Here)
    )

    foreach ($cand in $candidates) {
        if ($cand -and (Test-Path -LiteralPath (Join-Path $cand "bridge_app.py"))) { return $cand }
    }
    return ""
}

function Get-BridgeToken {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://127.0.0.1:8870/"
        if ($resp.Content -match '__NYX_TOKEN__="([^"]+)"') { return $matches[1] }
    } catch { }
    return ""
}

function Get-BridgeUp {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Method Head -Uri "http://127.0.0.1:8870/"
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Get-VenvPython {
    param([string]$Root)
    $cands = @(
        (Join-Path $Root "venv\Scripts\python.exe"),
        (Join-Path $Root ".venv\Scripts\python.exe"),
        (Join-Path $env:LOCALAPPDATA "NyxSuite\venv\Scripts\python.exe")
    )
    foreach ($c in $cands) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    return ""
}

function Invoke-EnvReport {
    param([string]$Dir, [string]$Root)
    $out = Join-Path $Dir "env-report.txt"
    Write-Utc "install_root=$Root" $out
    $versionFile = Join-Path $Root "VERSION"
    if (Test-Path -LiteralPath $versionFile) {
        Write-Utc ("version=" + ((Get-Content -LiteralPath $versionFile -TotalCount 1).Trim())) $out
    }
    Write-Utc ("host=$env:COMPUTERNAME user=$env:USERNAME") $out
    Write-Utc ("os=" + [System.Environment]::OSVersion.VersionString) $out
    Write-Utc ("powershell=" + $PSVersionTable.PSVersion.ToString()) $out
    Write-Utc ("cores=" + [Environment]::ProcessorCount) $out

    $py = Get-VenvPython -Root $Root
    Write-Utc "venv_python=$py" $out
    if (-not $py) {
        Write-Utc "no venv python found; launcher stages not measurable" $out
        return
    }
    try { Write-Utc ("python=" + ((& $py -c "import sys; print(sys.version.split()[0])" 2>$null) | Select-Object -First 1)) $out } catch { }

    # Baseline: PowerShell startup cost (paid on every native-host start).
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & powershell.exe -NoProfile -Command "exit 0" | Out-Null
    $sw.Stop()
    Write-Utc ("stage_powershell_startup_ms=" + [math]::Round($sw.Elapsed.TotalMilliseconds)) $out

    # The three probes portable_launch_nyx.ps1 runs before Start-EntryScript.
    $checks = @(
        @{ name = "probe1_import_sys"; args = 'import sys; print(sys.executable)' },
        @{ name = "probe2_deps"; args = 'import certifi, greenlet, playwright.async_api, requests; import pywinauto, win32ui' },
        @{ name = "probe3_playwright_chromium"; args = 'import os; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); path=p.chromium.executable_path; p.stop(); raise SystemExit(0 if path and os.path.exists(path) else 1)' }
    )
    foreach ($c in $checks) {
        for ($run = 1; $run -le 2; $run++) {
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            & $py -c $c.args 2>$null | Out-Null
            $rc = $LASTEXITCODE
            $sw.Stop()
            Write-Utc ("{0}_run{1} rc={2} ms={3}" -f $c.name, $run, $rc, [math]::Round($sw.Elapsed.TotalMilliseconds)) $out
        }
    }
    Write-Host "Wrote $out"
}

function Invoke-LauncherOverheadTiming {
    param([string]$Dir, [string]$Root)
    $out = Join-Path $Dir "launcher-overhead.txt"
    $launcher = Join-Path $Root "portable_launch_nyx.ps1"
    if (-not (Test-Path -LiteralPath $launcher)) {
        Write-Utc "launcher not found at $launcher" $out
        return
    }
    # A no-op entry script lets the launcher run its full fast path (PowerShell
    # startup + script load + readiness probes + pythonw launch) without starting
    # the real bridge, isolating launcher overhead from bridge startup.
    $noop = Join-Path $Dir "noop_entry.py"
    Set-Content -LiteralPath $noop -Value "import time`ntime.sleep(0.2)`n" -Encoding UTF8
    for ($run = 1; $run -le 3; $run++) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $launcher -EntryScript $noop -Quiet | Out-Null
        $sw.Stop()
        Write-Utc ("launcher_overhead_run{0}_ms={1}" -f $run, [math]::Round($sw.Elapsed.TotalMilliseconds)) $out
    }
    Write-Host "Wrote $out"
}

function Show-BridgeTiming {
    param([string]$Dir, [string]$Root)
    $out = Join-Path $Dir "bridge-internal-timing.txt"
    $candidates = @(
        (Join-Path $Root "logs\nyx_bot.log"),
        (Join-Path $env:LOCALAPPDATA "NyxSuite\logs\nyx_bot.log"),
        (Join-Path $env:LOCALAPPDATA "NyxSuite\app\logs\nyx_bot.log")
    )
    foreach ($log in $candidates) {
        if (Test-Path -LiteralPath $log) {
            Write-Utc "bridge_log=$log" $out
            $timing = Select-String -LiteralPath $log -Pattern 'timing \|' -ErrorAction SilentlyContinue |
                Select-Object -Last 40
            foreach ($t in $timing) { Write-Utc ([string]$t.Line) $out }
            return
        }
    }
    Write-Utc "no bridge log found; candidates: $($candidates -join '; ')" $out
    Write-Host "Wrote $out"
}

function Invoke-EndpointTimings {
    param([string]$Dir)
    $token = Get-BridgeToken
    $targets = @(
        @{ name = "dashboard_status_8870"; url = "http://127.0.0.1:8870/bridge/status?token=$token" },
        @{ name = "nyx_status_8865"; url = "http://127.0.0.1:8865/status" },
        @{ name = "nyxify_status_8866"; url = "http://127.0.0.1:8866/status" }
    )
    $out = Join-Path $Dir "endpoint-timings.txt"
    foreach ($t in $targets) {
        $vals = @()
        Write-Utc "=== $($t.name) ===" $out
        for ($i = 1; $i -le 20; $i++) {
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            $code = 0; $size = 0
            try {
                $headers = @{}
                if ($token) { $headers["X-Nyx-Token"] = $token; $headers["X-Nyxify-Token"] = $token }
                $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri $t.url -Headers $headers
                $code = [int]$resp.StatusCode
                $size = $resp.RawContentLength
            } catch {
                if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
            }
            $sw.Stop()
            $ms = [math]::Round($sw.Elapsed.TotalMilliseconds, 1)
            $vals += $ms
            Write-Utc ("attempt=$i code=$code total_ms=$ms size=$size") $out
            Start-Sleep -Milliseconds 500
        }
        $sorted = $vals | Sort-Object
        $min = $sorted[0]
        $max = $sorted[-1]
        $median = $sorted[[int][math]::Floor($sorted.Count / 2)]
        Write-Utc ("summary min_ms=$min median_ms=$median max_ms=$max") $out
    }
    Write-Host "Wrote $out"
}

function Invoke-ToggleTiming {
    param([string]$Dir, [string]$Root, [int]$CycleCount, [double]$DelaySeconds)
    $out = Join-Path $Dir "toggle-timing.txt"
    $launcher = Join-Path $Root "portable_launch_nyx.ps1"
    Write-Utc "install_root=$Root launcher_exists=$((Test-Path -LiteralPath $launcher))" $out

    for ($cycle = 1; $cycle -le $CycleCount; $cycle++) {
        Write-Utc "----- cycle $cycle -----" $out
        $token = Get-BridgeToken
        Write-Utc "token_present=$([bool]$token)" $out

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $headers = @{ "Content-Type" = "application/json" }
            if ($token) { $headers["X-Nyx-Token"] = $token; $headers["X-Nyxify-Token"] = $token }
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Method Post -Uri "http://127.0.0.1:8870/bridge/shutdown" `
                -Headers $headers -Body "{`"token`":`"$token`"}" | Out-Null
            $sw.Stop()
            Write-Utc ("OFF shutdown_response_ms=" + [math]::Round($sw.Elapsed.TotalMilliseconds, 1)) $out
        } catch {
            $sw.Stop()
            Write-Utc ("OFF shutdown_error=" + $_.Exception.Message) $out
        }

        $down = [System.Diagnostics.Stopwatch]::StartNew()
        while ($down.Elapsed.TotalSeconds -lt 30) {
            if (-not (Get-BridgeUp)) { break }
            Start-Sleep -Milliseconds 100
        }
        Write-Utc ("OFF port_down_ms=" + [math]::Round($down.Elapsed.TotalMilliseconds, 1)) $out

        Start-Sleep -Seconds $DelaySeconds

        if (-not (Test-Path -LiteralPath $launcher)) {
            Write-Utc "ON skipped: launcher not found at $launcher" $out
            return
        }

        $on = [System.Diagnostics.Stopwatch]::StartNew()
        Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $launcher,
            "-EntryScript", "bridge_app.py", "-Quiet"
        ) | Out-Null
        $ready = $false
        while ($on.Elapsed.TotalSeconds -lt 60) {
            if (Get-BridgeUp) { $ready = $true; break }
            Start-Sleep -Milliseconds 100
        }
        $on.Stop()
        if ($ready) {
            Write-Utc ("ON dashboard_ready_ms=" + [math]::Round($on.Elapsed.TotalMilliseconds, 1)) $out
        } else {
            Write-Utc "ON dashboard_did_not_come_up_within_60s" $out
        }
        Start-Sleep -Seconds 1
    }
    Write-Host "Wrote $out"
}

function Get-ProcessLabel {
    param([string]$CommandLine, [string]$Name)
    $text = ("$Name $CommandLine").ToLowerInvariant()
    if ($text -match "bridge_app\.py" -or $text -match "nyxsuite") { return "bridge" }
    if ($text -match "nyxify_runner\.py" -or $text -match "nyxifyrunner") { return "nyxify_runner" }
    if ($text -match "main\.py" -or $text -match "nyxbot") { return "nyx_runner" }
    if ($text -match "chrome\.exe" -and ($text -match "--extension-process" -or $text -match "chrome-extension://")) { return "chrome_extension" }
    if ($text -match "chrome\.exe" -or $text -match "msedge\.exe") { return "chrome" }
    if ($text -match "sunbrowser") { return "sunbrowser" }
    if ($text -match "adspower") { return "adspower" }
    return "other"
}

function Invoke-ProcessSampling {
    param([string]$Dir, [int]$DurationSeconds, [int]$Interval)
    $out = Join-Path $Dir "process-samples.jsonl"
    $notes = Join-Path $Dir "sample-notes.txt"
    $prevCpu = @{}
    $lastTick = $null
    $cmdCache = @{}
    $nameFilter = "python|pythonw|chrome|msedge|SunBrowser|AdsPower|NyxSuite|NyxBot|NyxifyRunner"
    $lastCmdRefresh = [datetime]::MinValue
    $deadline = (Get-Date).AddSeconds($DurationSeconds)
    $cores = [Environment]::ProcessorCount
    Write-Utc "sampling started (${DurationSeconds}s, target ${Interval}ms, $cores cores)" $notes
    Write-Host ""
    Write-Host ">>> OPEN the Nyxify popup 5x and the Nyx popup 5x NOW." -ForegroundColor Yellow
    Write-Host ">>> For each open, note open->usable time (phone stopwatch is fine)." -ForegroundColor Yellow
    Write-Host ""

    while ((Get-Date) -lt $deadline) {
        $tickStart = Get-Date
        $nowUtc = [DateTime]::UtcNow

        if (($tickStart - $lastCmdRefresh).TotalSeconds -ge 2) {
            $lastCmdRefresh = $tickStart
            try {
                $cim = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match $nameFilter }
                $fresh = @{}
                foreach ($p in $cim) { $fresh[[int]$p.ProcessId] = [string]$p.CommandLine }
                $cmdCache = $fresh
            } catch { }
        }

        $targets = Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessName -match $nameFilter }
        foreach ($gp in $targets) {
            $procId = $gp.Id
            $cpuSec = $null
            try { $cpuSec = $gp.CPU } catch { $cpuSec = $null }
            $rss = 0
            try { $rss = [int64]$gp.WorkingSet64 } catch { $rss = 0 }
            $pct = 0.0
            if ($cpuSec -ne $null -and $prevCpu.ContainsKey($procId) -and $lastTick) {
                $dt = ($tickStart - $lastTick).TotalSeconds
                $delta = $cpuSec - $prevCpu[$procId]
                if ($delta -lt 0) { $delta = 0 }
                if ($dt -gt 0) { $pct = [math]::Round(($delta / $dt) * 100.0, 1) }
            }
            if ($cpuSec -ne $null) { $prevCpu[$procId] = $cpuSec }
            $cmd = ""
            if ($cmdCache.ContainsKey($procId)) { $cmd = $cmdCache[$procId] }
            $label = Get-ProcessLabel -CommandLine $cmd -Name ([string]$gp.ProcessName)
            $record = [ordered]@{
                ts_utc  = $nowUtc.ToString("o")
                pid     = $procId
                label   = $label
                cpu_pct = $pct
                rss_mb  = [math]::Round($rss / 1MB, 1)
                name    = [string]$gp.ProcessName
                command = $cmd
            }
            ($record | ConvertTo-Json -Compress) | Add-Content -LiteralPath $out
        }
        $elapsed = ((Get-Date) - $tickStart).TotalMilliseconds
        $sleep = [int]($Interval - $elapsed)
        if ($sleep -gt 0) { Start-Sleep -Milliseconds $sleep }
        $lastTick = $tickStart
    }
    Write-Host "Wrote $out"
}

function Get-InteractivePopupData {
    param([string]$Dir)
    if ($NoPrompt) { return }
    Write-Host ""
    Write-Host "=== Popup timing capture ===" -ForegroundColor Cyan
    Write-Host "Enter open->usable times in ms, comma-separated. Leave blank if not measured."
    $nyxify = Read-Host "Nyxify 5 values"
    $nyx = Read-Host "Nyx 5 values"
    $taskmgr = Read-Host "Chrome Task Manager top-CPU entry (name/site | pid | cpu) [optional]"

    $md = Join-Path $Dir "popup-timings.md"
    $lines = @("# Windows Popup Measurements", "", "Nyxify raw ms: $nyxify", "Nyx raw ms: $nyx", "")
    foreach ($entry in @(@("Nyxify", $nyxify), @("Nyx", $nyx))) {
        $ext = $entry[0]
        $raw = $entry[1]
        $nums = @()
        if ($raw) {
            foreach ($v in ($raw -split ",")) {
                $t = $v.Trim()
                if ($t -match '^\d+(\.\d+)?$') { $nums += [double]$t }
            }
        }
        if ($nums.Count -gt 0) {
            $sorted = $nums | Sort-Object
            $median = $sorted[[int][math]::Floor($sorted.Count / 2)]
            $lines += "## $ext"
            $lines += "- count: $($nums.Count)"
            $lines += "- median_ms: $median"
            $lines += "- min_ms: $($sorted[0])"
            $lines += "- max_ms: $($sorted[-1])"
            $lines += ""
        }
    }
    if ($taskmgr) { $lines += "## Chrome Task Manager"; $lines += $taskmgr }
    ($lines -join "`n") | Set-Content -LiteralPath $md -Encoding UTF8
    if ($taskmgr) {
        Set-Content -LiteralPath (Join-Path $Dir "chrome-task-manager-snapshot.txt") -Value $taskmgr -Encoding UTF8
    }
    Write-Host "Wrote $md"
}

# --------------------------------------------------------------------------
if (-not $OutDir) { $OutDir = Join-Path $env:TEMP ("nyxsuite-perf-" + (Get-UtcStamp)) }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
if (-not $InstallRoot) { $InstallRoot = Resolve-InstallRoot -Value "" -Here $PSScriptRoot }

Write-Host "NyxSuite Windows performance probe v2"
Write-Host "  Mode:        $Mode"
Write-Host "  OutDir:      $OutDir"
Write-Host "  InstallRoot: $InstallRoot"
Write-Utc "probe v2 start mode=$Mode out=$OutDir install=$InstallRoot" (Join-Path $OutDir "probe-notes.txt")

switch ($Mode) {
    "env"       {
        Invoke-EnvReport -Dir $OutDir -Root $InstallRoot
        Invoke-LauncherOverheadTiming -Dir $OutDir -Root $InstallRoot
    }
    "endpoints" { Invoke-EndpointTimings -Dir $OutDir }
    "toggle"    { Invoke-ToggleTiming -Dir $OutDir -Root $InstallRoot -CycleCount $Cycles -DelaySeconds $OnDelaySeconds }
    "sample"    { Invoke-ProcessSampling -Dir $OutDir -DurationSeconds $Seconds -Interval $IntervalMs }
    "all" {
        Invoke-EnvReport -Dir $OutDir -Root $InstallRoot
        Invoke-LauncherOverheadTiming -Dir $OutDir -Root $InstallRoot
        Invoke-EndpointTimings -Dir $OutDir
        Invoke-ToggleTiming -Dir $OutDir -Root $InstallRoot -CycleCount $Cycles -DelaySeconds $OnDelaySeconds
        Show-BridgeTiming -Dir $OutDir -Root $InstallRoot
        Invoke-ProcessSampling -Dir $OutDir -DurationSeconds $Seconds -Interval $IntervalMs
        Get-InteractivePopupData -Dir $OutDir
    }
}

Write-Host ""
Write-Host "Done. Evidence directory: $OutDir"
Write-Host "Files: env-report.txt, launcher-overhead.txt, endpoint-timings.txt, toggle-timing.txt, bridge-internal-timing.txt, process-samples.jsonl, popup-timings.md"
