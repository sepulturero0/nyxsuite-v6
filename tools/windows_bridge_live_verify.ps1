#requires -version 5.1
<#
.SYNOPSIS
  One-time NyxSuite Windows bridge startup verifier.

.DESCRIPTION
  Runs the exact evidence collection needed after the Windows launcher fast-path
  fix:

  - toggles the bridge OFF/ON for several cycles
  - starts ON through portable_launch_nyx.ps1, the path used by the extension
  - measures shutdown response, port-down time, launcher return time, and
    dashboard-ready time
  - times key local endpoints
  - captures process snapshots
  - checks portable_launch.log for repeated setup/install lines
  - writes a diagnostics folder and zip under %LOCALAPPDATA%\NyxSuite\diagnostics

  It does not print or store the NyxSuite token.
#>
param(
    [int]$Cycles = 3,
    [string]$InstallRoot = "",
    [string]$OutDir = "",
    [int]$ReadyTimeoutSeconds = 30,
    [int]$DownTimeoutSeconds = 15,
    [int]$EndpointSamples = 10
)

$ErrorActionPreference = "Continue"

function Get-UtcStamp {
    return (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmmss'Z'")
}

function New-Directory {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
    }
}

function Write-Report {
    param([string]$Message)
    $line = "$((Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")) $Message"
    Write-Host $line
    Add-Content -LiteralPath $script:SummaryPath -Value $line
}

function Resolve-InstallRoot {
    param([string]$RequestedRoot)
    $candidates = @()
    if ($RequestedRoot) {
        $candidates += $RequestedRoot
    }
    $scriptDir = Split-Path -Parent $PSCommandPath
    if ($scriptDir) {
        $candidates += (Split-Path -Parent $scriptDir)
        $candidates += $scriptDir
    }
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA "NyxSuite\app")
    }
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
        foreach ($proc in $procs) {
            $cmd = [string]$proc.CommandLine
            if ($cmd -match '"([^"]*bridge_app\.py)"') {
                $candidates += (Split-Path -Parent $matches[1])
            }
            elseif ($cmd -match '([A-Za-z]:\\[^"]*bridge_app\.py)') {
                $candidates += (Split-Path -Parent $matches[1])
            }
        }
    }
    catch {
    }

    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        $bridge = Join-Path $candidate "bridge_app.py"
        $launcher = Join-Path $candidate "portable_launch_nyx.ps1"
        if ((Test-Path -LiteralPath $bridge) -and (Test-Path -LiteralPath $launcher)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return ""
}

function Read-AgentToken {
    param([string]$Root = "")
    $paths = @()
    if ($env:LOCALAPPDATA) {
        $paths += (Join-Path $env:LOCALAPPDATA "NyxSuite\agent_token.txt")
        $paths += (Join-Path $env:LOCALAPPDATA "NyxSuite\app\agent_token.txt")
    }
    if ($Root) {
        $paths += (Join-Path $Root "agent_token.txt")
    }
    foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path) {
            try {
                $token = (Get-Content -LiteralPath $path -Raw).Trim()
                if ($token) {
                    return $token
                }
            }
            catch {
            }
        }
    }
    return (Request-TokenFromLocalApi)
}

function Request-TokenFromLocalApi {
    foreach ($uri in @("http://127.0.0.1:8865/token", "http://127.0.0.1:8866/token")) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri $uri
            $payload = $response.Content | ConvertFrom-Json
            $token = [string]$payload.token
            if ($token) {
                return $token
            }
        }
        catch {
        }
    }
    return ""
}

function Test-TcpPortOpen {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(250, $false)
        if (-not $ok) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Get-BridgeReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Method Head -Uri "http://127.0.0.1:8870/"
        return ($response.StatusCode -eq 200)
    }
    catch {
        return $false
    }
}

function Wait-BridgeReady {
    param([int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Get-BridgeReady) {
            return $true
        }
        Start-Sleep -Milliseconds 200
    }
    return $false
}

function Wait-PortDown {
    param([int]$Port, [int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-TcpPortOpen -Port $Port)) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    }
    return $false
}

function Invoke-TimedRequest {
    param(
        [string]$Name,
        [string]$Method,
        [string]$Uri,
        [string]$OutPath,
        [string]$Token = "",
        [string]$Body = ""
    )
    $headers = @{}
    if ($Token) {
        $headers["X-Nyx-Token"] = $Token
        $headers["X-Nyxify-Token"] = $Token
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $code = 0
    $bytes = 0
    $errorText = ""
    try {
        if ($Body) {
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Method $Method -Uri $Uri -Headers $headers -ContentType "application/json" -Body $Body
        }
        else {
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Method $Method -Uri $Uri -Headers $headers
        }
        $code = [int]$response.StatusCode
        $bytes = ([string]$response.Content).Length
    }
    catch {
        $errorText = $_.Exception.Message.Replace(",", ";")
    }
    $sw.Stop()
    $line = "{0},{1},{2},{3},{4}" -f $Name, $code, [math]::Round($sw.Elapsed.TotalMilliseconds), $bytes, $errorText
    Add-Content -LiteralPath $OutPath -Value $line
    return @{
        code = $code
        ms = [math]::Round($sw.Elapsed.TotalMilliseconds)
        error = $errorText
    }
}

function Invoke-ProcessSnapshot {
    param([string]$Label)
    $out = Join-Path $script:OutDir "process-snapshots.csv"
    $patterns = @("bridge_app.py", "portable_launch_nyx.ps1", "python", "pythonw", "powershell", "chrome", "msedge", "adspower", "sunbrowser", "NyxSuite", "NyxBot", "NyxifyRunner")
    try {
        $rows = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
        foreach ($row in $rows) {
            $name = [string]$row.Name
            $cmd = [string]$row.CommandLine
            $joined = ($name + " " + $cmd).ToLowerInvariant()
            $match = $false
            foreach ($pattern in $patterns) {
                if ($joined.Contains($pattern.ToLowerInvariant())) {
                    $match = $true
                    break
                }
            }
            if (-not $match) {
                continue
            }
            $privateMb = 0
            try {
                $proc = Get-Process -Id $row.ProcessId -ErrorAction SilentlyContinue
                if ($proc) {
                    $privateMb = [math]::Round($proc.PrivateMemorySize64 / 1MB, 1)
                }
            }
            catch {
            }
            $safeCmd = $cmd.Replace("`r", " ").Replace("`n", " ").Replace(",", ";")
            Add-Content -LiteralPath $out -Value ("{0},{1},{2},{3},{4},{5}" -f $Label, (Get-Date).ToUniversalTime().ToString("o"), $row.ProcessId, $name, $privateMb, $safeCmd)
        }
    }
    catch {
        Add-Content -LiteralPath $out -Value ("{0},{1},ERROR,,,{2}" -f $Label, (Get-Date).ToUniversalTime().ToString("o"), $_.Exception.Message.Replace(",", ";"))
    }
}

function Invoke-EndpointTimings {
    $out = Join-Path $script:OutDir "endpoint-timings.csv"
    Set-Content -LiteralPath $out -Value "name,http_code,elapsed_ms,bytes,error"
    $targets = @(
        @{ name = "bridge-status-8870"; uri = "http://127.0.0.1:8870/bridge/status" },
        @{ name = "nyx-status-8865"; uri = "http://127.0.0.1:8865/status" },
        @{ name = "nyxify-status-8866"; uri = "http://127.0.0.1:8866/status" }
    )
    foreach ($target in $targets) {
        for ($i = 1; $i -le $EndpointSamples; $i++) {
            Invoke-TimedRequest -Name ($target.name + "-sample-" + $i) -Method "GET" -Uri $target.uri -OutPath $out | Out-Null
        }
    }
}

function Invoke-PortableLogAnalysis {
    param([string]$Phase)
    $logPath = Join-Path $env:LOCALAPPDATA "NyxSuite\bootstrap\portable_launch.log"
    $out = Join-Path $script:OutDir "portable-launch-log-$Phase.txt"
    if (-not (Test-Path -LiteralPath $logPath)) {
        Set-Content -LiteralPath $out -Value "portable_launch.log not found at $logPath"
        return @{
            path = $logPath
            preparing = 0
            installing = 0
            launching = 0
            lines = 0
        }
    }
    $tail = Get-Content -LiteralPath $logPath -Tail 160
    Set-Content -LiteralPath $out -Value $tail
    return @{
        path = $logPath
        preparing = @($tail | Where-Object { $_ -match "Preparing Snap Bitmoji Bot" }).Count
        installing = @($tail | Where-Object { $_ -match "Installing the Playwright Chromium runtime" }).Count
        launching = @($tail | Where-Object { $_ -match "Launching bridge_app.py" }).Count
        lines = @($tail).Count
    }
}

function Start-BridgeViaLauncher {
    param([string]$Root)
    $launcher = Join-Path $Root "portable_launch_nyx.ps1"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $launcher,
        "-EntryScript",
        "bridge_app.py",
        "-Quiet"
    ) -WorkingDirectory $Root -Wait -PassThru
    $sw.Stop()
    return @{
        exit_code = $proc.ExitCode
        launcher_ms = [math]::Round($sw.Elapsed.TotalMilliseconds)
    }
}

function Invoke-BridgeCycle {
    param([int]$CycleNumber, [string]$Root, [string]$Token)
    $cycleOut = Join-Path $script:OutDir "bridge-cycles.csv"
    $endpointOut = Join-Path $script:OutDir "cycle-requests.csv"
    if (-not (Test-Path -LiteralPath $cycleOut)) {
        Set-Content -LiteralPath $cycleOut -Value "cycle,shutdown_http_code,shutdown_response_ms,bridge-off-port-down-ms,launcher_exit_code,launcher_return_ms,bridge-on-ms,ready,notes"
        Set-Content -LiteralPath $endpointOut -Value "name,http_code,elapsed_ms,bytes,error"
    }

    Invoke-ProcessSnapshot -Label ("cycle-{0}-before-off" -f $CycleNumber)
    $shutdown = Invoke-TimedRequest -Name ("cycle-$CycleNumber-shutdown") -Method "POST" -Uri "http://127.0.0.1:8870/bridge/shutdown" -OutPath $endpointOut -Token $Token -Body "{}"
    $offSw = [System.Diagnostics.Stopwatch]::StartNew()
    $down = Wait-PortDown -Port 8870 -TimeoutSeconds $DownTimeoutSeconds
    $offSw.Stop()
    $offMs = [math]::Round($offSw.Elapsed.TotalMilliseconds)
    Invoke-ProcessSnapshot -Label ("cycle-{0}-after-off" -f $CycleNumber)

    Start-Sleep -Seconds 2

    $onSw = [System.Diagnostics.Stopwatch]::StartNew()
    $launcher = Start-BridgeViaLauncher -Root $Root
    $ready = Wait-BridgeReady -TimeoutSeconds $ReadyTimeoutSeconds
    $onSw.Stop()
    $onMs = [math]::Round($onSw.Elapsed.TotalMilliseconds)
    Invoke-ProcessSnapshot -Label ("cycle-{0}-after-on" -f $CycleNumber)

    $notes = ""
    if (-not $down) {
        $notes += "port_down_timeout "
    }
    if (-not $ready) {
        $notes += "ready_timeout "
    }
    $line = "{0},{1},{2},{3},{4},{5},{6},{7},{8}" -f $CycleNumber, $shutdown.code, $shutdown.ms, $offMs, $launcher.exit_code, $launcher.launcher_ms, $onMs, $ready, $notes.Trim()
    Add-Content -LiteralPath $cycleOut -Value $line
    Write-Report ("cycle={0} bridge-off-port-down-ms={1} launcher-return-ms={2} bridge-on-ms={3} ready={4}" -f $CycleNumber, $offMs, $launcher.launcher_ms, $onMs, $ready)
}

$stamp = Get-UtcStamp
if (-not $OutDir) {
    $base = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "NyxSuite\diagnostics" } else { Join-Path $env:TEMP "NyxSuite\diagnostics" }
    $OutDir = Join-Path $base ("bridge-live-verify-" + $stamp)
}
New-Directory -Path $OutDir
$script:OutDir = $OutDir
$script:SummaryPath = Join-Path $OutDir "summary.txt"
Set-Content -LiteralPath $script:SummaryPath -Value "NyxSuite Windows bridge live verification $stamp"

$root = Resolve-InstallRoot -RequestedRoot $InstallRoot
if (-not $root) {
    Write-Report "ERROR install root not found. Re-run with -InstallRoot pointing at the folder containing bridge_app.py and portable_launch_nyx.ps1."
    exit 2
}
$token = Read-AgentToken -Root $root
Write-Report "install-root=$root"
Write-Report "cycles=$Cycles endpoint-samples=$EndpointSamples ready-timeout-s=$ReadyTimeoutSeconds down-timeout-s=$DownTimeoutSeconds"
Write-Report "token-present=$([bool]$token)"

$beforeLog = Invoke-PortableLogAnalysis -Phase "before"
Write-Report ("portable-log-before path={0} preparing-tail-count={1} installing-tail-count={2} launching-tail-count={3}" -f $beforeLog.path, $beforeLog.preparing, $beforeLog.installing, $beforeLog.launching)

Set-Content -LiteralPath (Join-Path $OutDir "process-snapshots.csv") -Value "label,utc,pid,name,private_mb,command"
Invoke-ProcessSnapshot -Label "initial"

for ($cycle = 1; $cycle -le $Cycles; $cycle++) {
    Invoke-BridgeCycle -CycleNumber $cycle -Root $root -Token $token
}

Invoke-EndpointTimings
Invoke-ProcessSnapshot -Label "final"

$afterLog = Invoke-PortableLogAnalysis -Phase "after"
Write-Report ("portable-log-after path={0} preparing-tail-count={1} installing-tail-count={2} launching-tail-count={3}" -f $afterLog.path, $afterLog.preparing, $afterLog.installing, $afterLog.launching)

$zipPath = $OutDir.TrimEnd("\") + ".zip"
try {
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -Force -LiteralPath $zipPath
    }
    Compress-Archive -LiteralPath (Join-Path $OutDir "*") -DestinationPath $zipPath -Force
    Write-Report "zip=$zipPath"
}
catch {
    Write-Report ("zip-error=" + $_.Exception.Message)
}

Write-Host ""
Write-Host "Done. Send back this folder or zip:" -ForegroundColor Green
Write-Host "  $OutDir"
Write-Host "  $zipPath"
