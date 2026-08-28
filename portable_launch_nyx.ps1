param(
    [string]$EntryScript = "bridge_app.py",
    [switch]$Console,
    [switch]$SetupOnly,
    [switch]$ForceSetup,
    [switch]$SkipBrowserInstall,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

$appName = "Snap Bitmoji Bot"
$pythonMinVersion = [Version]"3.10"
$requirementsPath = Join-Path $PSScriptRoot "requirements.txt"
# v6 line: keep every install-time path under the same "NyxSuite" namespace the
# bridge uses (core/process_utils APP_DATA_DIR_NAME), so a machine-local venv and
# app-data are discovered consistently and never collide with a stale v3 "Nyx"
# tree. Change this one constant to rebrand the data folder.
$appDataRoot = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "NyxSuite" } else { Join-Path $PSScriptRoot ".nyx_local" }
$projectVenvDir = Join-Path $PSScriptRoot "venv"
$machineLocalVenvDir = Join-Path $appDataRoot "venv"
$venvDir = $projectVenvDir
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPythonw = Join-Path $venvDir "Scripts\pythonw.exe"
$bootstrapDir = Join-Path $appDataRoot "bootstrap"
$bootstrapStatePath = Join-Path $bootstrapDir "portable_setup.json"
$bootstrapLogPath = Join-Path $bootstrapDir "portable_launch.log"
$logsDir = Join-Path $PSScriptRoot "logs"
$dataDir = Join-Path $PSScriptRoot "data"

function Write-LogMessage {
    param(
        [string]$Message,
        [string]$Level = "INFO"
    )

    New-Item -ItemType Directory -Force -Path $bootstrapDir | Out-Null
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $bootstrapLogPath -Value "$timestamp [$Level] $Message"
}

function Write-Status {
    param(
        [string]$Message,
        [string]$Color = "Cyan",
        [switch]$Force
    )

    Write-LogMessage -Message $Message

    if ($Quiet -and -not $Force) {
        return
    }

    Write-Host "[NYX] $Message" -ForegroundColor $Color
}

function Get-FileSha256 {
    param(
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Get-SetupState {
    if (-not (Test-Path -LiteralPath $bootstrapStatePath)) {
        return @{}
    }

    try {
        $raw = Get-Content -LiteralPath $bootstrapStatePath -Raw | ConvertFrom-Json
        $state = @{}

        if ($raw) {
            foreach ($property in $raw.PSObject.Properties) {
                $state[$property.Name] = $property.Value
            }
        }

        return $state
    }
    catch {
        Write-LogMessage -Level "WARN" -Message "Ignoring unreadable bootstrap state file."
        return @{}
    }
}

function Save-SetupState {
    param(
        [hashtable]$State
    )

    New-Item -ItemType Directory -Force -Path $bootstrapDir | Out-Null
    ($State | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $bootstrapStatePath -Encoding UTF8
}

function Set-PreferredVenv {
    param(
        [string]$Path
    )

    $state = Get-SetupState
    $state.preferred_venv_dir = $Path
    Save-SetupState -State $state
}

function Initialize-VenvPathPreference {
    $state = Get-SetupState
    $preferredPath = [string]($state.preferred_venv_dir)

    if (-not $preferredPath) {
        return
    }

    if ($preferredPath -eq $machineLocalVenvDir) {
        Set-VenvPath -Path $machineLocalVenvDir
        if (Test-VenvPythonAvailable) {
            return
        }
    }

    if ($preferredPath -eq $projectVenvDir) {
        Set-VenvPath -Path $projectVenvDir
        if (Test-VenvPythonAvailable) {
            return
        }
    }

    Set-VenvPath -Path $projectVenvDir
}

function Set-VenvPath {
    param(
        [string]$Path
    )

    $script:venvDir = $Path
    $script:venvPython = Join-Path $script:venvDir "Scripts\python.exe"
    $script:venvPythonw = Join-Path $script:venvDir "Scripts\pythonw.exe"
}

function Get-VenvLabel {
    if ($venvDir -eq $projectVenvDir) {
        return "project"
    }

    return "machine-local"
}

function Try-RemoveDirectory {
    param(
        [string]$Path
    )

    try {
        Remove-Item -Recurse -Force -LiteralPath $Path -ErrorAction Stop
        return $true
    }
    catch {
        Write-LogMessage -Level "WARN" -Message ("Could not remove {0}: {1}" -f $Path, $_.Exception.Message)
        return $false
    }
}

function Get-ArgumentArray {
    param(
        [object]$Value
    )

    $result = @()

    if ($null -eq $Value) {
        return $result
    }

    if ($Value -is [string]) {
        if ($Value) {
            $result += $Value
        }
        return $result
    }

    if ($Value -is [System.Collections.IEnumerable]) {
        foreach ($item in $Value) {
            if ($null -ne $item -and [string]$item -ne "") {
                $result += [string]$item
            }
        }
        return $result
    }

    $result += [string]$Value
    return $result
}

function Quote-ProcessArgument {
    param(
        [string]$Value
    )

    return '"' + $Value.Replace('"', '\"') + '"'
}

function Test-VenvPythonAvailable {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        return $false
    }

    try {
        & $venvPython -c "import sys; print(sys.executable)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Test-VenvDependenciesAvailable {
    if (-not (Test-VenvPythonAvailable)) {
        return $false
    }

    try {
        & $venvPython -c "import certifi, greenlet, playwright.async_api, requests; import sys; exec('import pywinauto, win32ui' if sys.platform == 'win32' else '')" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Test-WindowsWin32UiAvailable {
    if (-not (Test-VenvPythonAvailable)) {
        return $false
    }

    if (-not $IsWindows -and $env:OS -ne "Windows_NT") {
        return $true
    }

    try {
        & $venvPython -c "import win32ui" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Get-PythonArchitectureBits {
    try {
        $bits = & $venvPython -c "import struct; print(struct.calcsize('P') * 8)" 2>$null
        return [int]($bits | Select-Object -First 1)
    }
    catch {
        return 64
    }
}

function Get-MfcRuntimeCandidates {
    $dllName = "mfc140u.dll"
    $candidates = @()
    $bits = Get-PythonArchitectureBits

    if ($env:WINDIR) {
        if ($bits -eq 32) {
            $candidates += Join-Path $env:WINDIR "SysWOW64\$dllName"
        }
        $candidates += Join-Path $env:WINDIR "System32\$dllName"
    }

    $roots = @()
    if ($env:ProgramFiles) {
        $roots += Join-Path $env:ProgramFiles "Microsoft Office\root\vfs"
    }
    $programFilesX86 = ${env:ProgramFiles(x86)}
    if ($programFilesX86) {
        $roots += Join-Path $programFilesX86 "Microsoft Office\root\vfs"
    }

    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) {
            continue
        }
        try {
            $candidates += Get-ChildItem -LiteralPath $root -Recurse -Filter $dllName -File -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName
        }
        catch {
            continue
        }
    }

    return $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
}

function Repair-WindowsMfcRuntime {
    if (-not $IsWindows -and $env:OS -ne "Windows_NT") {
        return $true
    }

    if (Test-WindowsWin32UiAvailable) {
        return $true
    }

    $pythonwinDir = Join-Path $venvDir "Lib\site-packages\pythonwin"
    if (-not (Test-Path -LiteralPath $pythonwinDir)) {
        New-Item -ItemType Directory -Force -Path $pythonwinDir | Out-Null
    }

    $target = Join-Path $pythonwinDir "mfc140u.dll"
    foreach ($candidate in Get-MfcRuntimeCandidates) {
        Write-Status "Repairing pywin32 MFC runtime from $candidate."
        try {
            Copy-Item -Force -LiteralPath $candidate -Destination $target
        }
        catch {
            Write-LogMessage -Level "WARN" -Message ("Could not copy {0}: {1}" -f $candidate, $_.Exception.Message)
            continue
        }

        if (Test-WindowsWin32UiAvailable) {
            Write-Status "pywin32 MFC runtime repaired." -Color "Green"
            return $true
        }

        Remove-Item -Force -LiteralPath $target -ErrorAction SilentlyContinue
        Write-LogMessage -Level "WARN" -Message "Copied MFC runtime did not satisfy win32ui import; trying next candidate."
    }

    return $false
}

function Test-PlaywrightChromiumAvailable {
    if (-not (Test-VenvPythonAvailable)) {
        return $false
    }

    try {
        & $venvPython -c "import os; from playwright.sync_api import sync_playwright; p = sync_playwright().start(); path = p.chromium.executable_path; p.stop(); raise SystemExit(0 if path and os.path.exists(path) else 1)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Test-LaunchEnvironmentReady {
    param(
        [switch]$SkipBrowsers
    )

    if (-not (Test-VenvDependenciesAvailable)) {
        return $false
    }

    if ($SkipBrowsers) {
        return $true
    }

    return (Test-PlaywrightChromiumAvailable)
}

function Remove-VenvIfBroken {
    if (-not (Test-Path -LiteralPath $venvDir)) {
        return
    }

    if (Test-VenvPythonAvailable) {
        return
    }

    Write-Status "Existing virtual environment looks unusable on this machine. Recreating it."

    if (Try-RemoveDirectory -Path $venvDir) {
        return
    }

    if ($venvDir -eq $projectVenvDir) {
        Write-Status "Project virtual environment is locked or inaccessible. Falling back to a machine-local virtual environment." -Color "Yellow"
        Set-VenvPath -Path $machineLocalVenvDir
        Set-PreferredVenv -Path $machineLocalVenvDir

        if ((Test-Path -LiteralPath $venvDir) -and (-not (Test-VenvPythonAvailable))) {
            Write-Status "Existing machine-local virtual environment also looks unusable. Recreating it."
            if (-not (Try-RemoveDirectory -Path $venvDir)) {
                throw "Could not remove the unusable machine-local virtual environment at $venvDir. Close Python/Nyx processes and try again."
            }
        }

        return
    }

    throw "Could not remove the unusable virtual environment at $venvDir. Close Python/Nyx processes and try again."
}

function Refresh-PathFromRegistry {
    $machinePath = [System.Environment]::GetEnvironmentVariable("PATH", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $env:PATH = (@($machinePath, $userPath) | Where-Object { $_ }) -join ";"
}

function Find-PythonInCommonPaths {
    $roots = @()

    if ($env:LOCALAPPDATA) {
        $roots += Join-Path $env:LOCALAPPDATA "Programs\Python"
    }

    $roots += @(
        "C:\Python313",
        "C:\Python312",
        "C:\Python311",
        "C:\Python310",
        "C:\Program Files\Python313",
        "C:\Program Files\Python312",
        "C:\Program Files\Python311",
        "C:\Program Files\Python310"
    )

    foreach ($base in $roots) {
        if (-not $base -or -not (Test-Path -LiteralPath $base)) {
            continue
        }

        $candidates = @($base)

        if ((Get-Item -LiteralPath $base).PSIsContainer) {
            $candidates += Get-ChildItem -LiteralPath $base -Filter "Python3*" -Directory -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                Select-Object -ExpandProperty FullName
        }

        foreach ($candidateDir in ($candidates | Select-Object -Unique)) {
            $candidateExe = Join-Path $candidateDir "python.exe"

            if (-not (Test-Path -LiteralPath $candidateExe)) {
                continue
            }

            try {
                $versionText = & $candidateExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
                if ($LASTEXITCODE -ne 0 -or -not $versionText) {
                    continue
                }

                $version = [Version]($versionText | Select-Object -First 1)
                if ($version -ge $pythonMinVersion) {
                    return @{
                        Command = $candidateExe
                        Arguments = @()
                        Version = $version.ToString()
                    }
                }
            }
            catch {
                continue
            }
        }
    }

    return $null
}

function Try-FindPythonOnPath {
    $candidates = @("py", "python") |
        ForEach-Object { Get-Command $_ -ErrorAction SilentlyContinue } |
        Where-Object { $_ } |
        Select-Object -ExpandProperty Source -Unique

    foreach ($candidate in $candidates) {
        $isPyLauncher = [System.IO.Path]::GetFileName($candidate).Equals(
            "py.exe",
            [System.StringComparison]::OrdinalIgnoreCase
        )

        $versionArgs = if ($isPyLauncher) {
            @("-3.13", "-3.12", "-3.11", "-3.10", "-3")
        }
        else {
            @("")
        }

        foreach ($versionArg in $versionArgs) {
            try {
                $callArgs = @($versionArg, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))") |
                    Where-Object { $_ -ne "" }
                $versionText = & $candidate @callArgs 2>$null

                if ($LASTEXITCODE -ne 0 -or -not $versionText) {
                    continue
                }

                $version = [Version]($versionText | Select-Object -First 1)
                if ($version -ge $pythonMinVersion) {
                    $launcherArgs = if ($isPyLauncher -and $versionArg) { @($versionArg) } else { @() }
                    return @{
                        Command = $candidate
                        Arguments = $launcherArgs
                        Version = $version.ToString()
                    }
                }
            }
            catch {
                continue
            }
        }
    }

    return $null
}

function Install-PythonViaWinget {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw (
            "Python $pythonMinVersion or newer was not found on this machine, " +
            "and winget is not available to install it automatically. " +
            "Install Python from https://python.org, then run this launcher again."
        )
    }

    Write-Status "Python was not found. Installing Python 3.12 with winget."
    & $winget.Source install --id Python.Python.3.12 --source winget --accept-source-agreements --accept-package-agreements --silent

    if ($LASTEXITCODE -ne 0) {
        throw (
            "Automatic Python installation failed. " +
            "Install Python 3.10 or newer from https://python.org, then run this launcher again."
        )
    }

    Refresh-PathFromRegistry
}

function Find-SystemPython {
    $python = Try-FindPythonOnPath
    if ($python) {
        return $python
    }

    $python = Find-PythonInCommonPaths
    if ($python) {
        return $python
    }

    Install-PythonViaWinget

    $python = Try-FindPythonOnPath
    if ($python) {
        return $python
    }

    $python = Find-PythonInCommonPaths
    if ($python) {
        return $python
    }

    throw "Python $pythonMinVersion or newer is still unavailable after automatic setup."
}

function New-VenvIfNeeded {
    if (Test-VenvPythonAvailable) {
        return
    }

    $python = Find-SystemPython
    Write-Status "Creating a $(Get-VenvLabel) virtual environment with Python $($python.Version)."
    $pythonInvocationArgs = @(Get-ArgumentArray -Value $python.Arguments)
    $pythonInvocationArgs += @("-m", "venv", $venvDir)
    & $python.Command @pythonInvocationArgs

    if ($LASTEXITCODE -ne 0 -or -not (Test-VenvPythonAvailable)) {
        throw "Failed to create the virtual environment."
    }
}

function Ensure-PipReady {
    $state = Get-SetupState
    $venvExeHash = Get-FileSha256 -Path $venvPython
    $toolStateKey = "pip_tools_for_$venvExeHash"

    if ((-not $ForceSetup) -and $state[$toolStateKey]) {
        return
    }

    Write-Status "Upgrading pip tooling."
    & $venvPython -m pip install --upgrade pip setuptools wheel

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip tooling."
    }

    $state[$toolStateKey] = $true
    Save-SetupState -State $state
}

function Ensure-Requirements {
    $state = Get-SetupState
    $requirementsHash = Get-FileSha256 -Path $requirementsPath
    $venvExeHash = Get-FileSha256 -Path $venvPython
    $dependenciesReady = Test-VenvDependenciesAvailable

    if (
        (-not $ForceSetup) -and
        $dependenciesReady -and
        $state.requirements_hash -eq $requirementsHash -and
        $state.venv_python_hash -eq $venvExeHash
    ) {
        return
    }

    Write-Status "Installing Python packages from requirements.txt."
    & $venvPython -m pip install -r $requirementsPath

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Python packages."
    }

    if (-not (Repair-WindowsMfcRuntime)) {
        throw (
            "pywin32 could not import win32ui because the Microsoft MFC runtime " +
            "mfc140u.dll is unavailable for this Python environment. Install the " +
            "Microsoft Visual C++ Redistributable, or copy mfc140u.dll into " +
            "$venvDir\Lib\site-packages\pythonwin, then run the launcher again."
        )
    }

    if (-not (Test-VenvDependenciesAvailable)) {
        throw "Python package verification failed after installing requirements."
    }

    $state.requirements_hash = $requirementsHash
    $state.venv_python_hash = $venvExeHash
    Save-SetupState -State $state
}

function Ensure-PlaywrightBrowsers {
    $state = Get-SetupState
    $requirementsHash = Get-FileSha256 -Path $requirementsPath
    $browserStateKey = "playwright_browsers_for_$requirementsHash"
    $browsersReady = Test-PlaywrightChromiumAvailable

    if ((-not $ForceSetup) -and $browsersReady -and $state[$browserStateKey]) {
        return
    }

    Write-Status "Installing the Playwright Chromium runtime."
    & $venvPython -m playwright install chromium

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install the Playwright Chromium runtime."
    }

    $state[$browserStateKey] = $true
    Save-SetupState -State $state
}

function Mark-SetupCompleted {
    $state = Get-SetupState
    $state.setup_completed = $true
    $state.setup_completed_at = (Get-Date).ToString("o")
    Save-SetupState -State $state
}

function Ensure-ProjectFolders {
    foreach ($path in @($appDataRoot, $logsDir, $dataDir, $bootstrapDir)) {
        if (-not (Test-Path -LiteralPath $path)) {
            New-Item -ItemType Directory -Force -Path $path | Out-Null
        }
    }
}

function Resolve-EntryScriptPath {
    param(
        [string]$Value
    )

    if ([System.IO.Path]::IsPathRooted($Value)) {
        $resolved = $Value
    }
    else {
        $resolved = Join-Path $PSScriptRoot $Value
    }

    if (-not (Test-Path -LiteralPath $resolved)) {
        throw "Entry script not found: $Value"
    }

    return (Resolve-Path -LiteralPath $resolved).Path
}

function Start-EntryScript {
    param(
        [string]$Path,
        [bool]$RunInConsole
    )

    if ($RunInConsole) {
        Write-Status "Running $([System.IO.Path]::GetFileName($Path))."
        & $venvPython $Path

        if ($LASTEXITCODE -ne 0) {
            throw "$([System.IO.Path]::GetFileName($Path)) exited with code $LASTEXITCODE."
        }

        return 0
    }

    $pythonExecutable = if (Test-Path -LiteralPath $venvPythonw) { $venvPythonw } else { $venvPython }
    Write-Status "Launching $([System.IO.Path]::GetFileName($Path))."
    $scriptArgument = Quote-ProcessArgument -Value $Path
    Start-Process -FilePath $pythonExecutable -ArgumentList @($scriptArgument) -WorkingDirectory $PSScriptRoot | Out-Null
    return 0
}

function Get-FailureCategory {
    param(
        [string]$Message
    )

    $combined = $Message.ToLowerInvariant()

    if ($combined -match "venv|virtualenv|site-packages|pip|resolution|requirement|import certifi|import playwright|mfc140|win32ui|pythonrevision|python") {
        return "python"
    }

    if ($combined -match "already in use|only one usage|port|bind|address|socket|:8865|:8866|:8870|:8869") {
        return "port"
    }

    if ($combined -match "denied|access is denied|permission|acl|robo|read-only|locked") {
        return "permission"
    }

    if ($combined -match "native messaging|manifest|host.*regist|chrome.*host|register") {
        return "native_messaging"
    }

    if ($combined -match "adspower|ads power|50325|proxy|local api|connection refused") {
        return "adspower"
    }

    return "unknown"
}

$script:failureFixes = @{
    python = @("Open the .bat launcher, not the .ps1, so the console stays visible.", "Allow the installer to create the machine-local venv (default: on first run).", "If pip install failed, check internet access and use `python -m pip install -r requirements.txt`.")
    port = @("A previous Nyx Suite instance may still be running (Check the tray / Task Manager).", "Close the running instance, then run the launcher again.", "Re-check that another firewall/proxy isn't holding the local port.")
    permission = @("Run the launcher from a folder your user can write to.", "Check that the .venv/venv and data folders are not read-only or locked.", "Temporarily disable antivirus that may be locking the project folder.")
    native_messaging = @("Re-run the launcher once; it auto-registers the native host on startup.", "In Chrome, re-load the extension and re-run Setup & Install.", "Ensure the native host manifest path matches the installed location.")
    adspower = @("Install/reopen AdsPower and confirm the Local API is running on the default port.", "Verify the AdsPower API key and host match in Nyx Config.", "Allow AdsPower through the firewall so the bridge can reach its Local API.")
    unknown = @("Use the .bat launcher instead of double-clicking the .ps1 file.", "Make sure internet access is available for package installation.", "Install Python 3.10 or newer and run the launcher again.")
}

try {
    Ensure-ProjectFolders
    Initialize-VenvPathPreference
    $entryPath = $null

    if ((-not $SetupOnly) -and (-not $ForceSetup)) {
        $entryPath = Resolve-EntryScriptPath -Value $EntryScript

        if (Test-LaunchEnvironmentReady -SkipBrowsers:$SkipBrowserInstall.IsPresent) {
            $entryExitCode = Start-EntryScript -Path $entryPath -RunInConsole:$Console.IsPresent
            exit $entryExitCode
        }
    }

    $setupStatusMessage = if ($ForceSetup) {
        "Repairing $appName on this Windows installation."
    }
    elseif ($SetupOnly) {
        "Installing $appName requirements for this Windows installation."
    }
    else {
        "Preparing $appName for this Windows installation."
    }

    Write-Status $setupStatusMessage
    Remove-VenvIfBroken
    New-VenvIfNeeded
    Set-PreferredVenv -Path $venvDir
    Ensure-PipReady
    Ensure-Requirements

    if (-not $SkipBrowserInstall) {
        Ensure-PlaywrightBrowsers
    }

    Mark-SetupCompleted

    if ($SetupOnly) {
        Write-Status "Setup complete." -Color "Green" -Force
        exit 0
    }

    $entryExitCode = Start-EntryScript -Path $entryPath -RunInConsole:$Console.IsPresent
    exit $entryExitCode
}
catch {
    $errorMessage = $_.Exception.Message
    Write-LogMessage -Level "ERROR" -Message $errorMessage
    Write-LogMessage -Level "ERROR" -Message $_.ScriptStackTrace

    $category = Get-FailureCategory -Message $errorMessage
    Write-LogMessage -Level "ERROR" -Message ("Failure category: " + $category)

    Write-Status "Portable launch failed." -Color "Red" -Force

    if (-not $Quiet) {
        Write-Host $errorMessage -ForegroundColor Red
        Write-Host ""
        Write-Host "Actionable fixes ($category):" -ForegroundColor Yellow
        $i = 1
        foreach ($fix in $script:failureFixes[$category]) {
            Write-Host ("  {0}. {1}" -f $i, $fix) -ForegroundColor Yellow
            $i += 1
        }
        if ($category -eq "unknown") {
            Write-Host "  $i. If the fix is unclear, open the bootstrap log below."
        }
        Write-Host ""
        Write-Host "Bootstrap log: $bootstrapLogPath" -ForegroundColor Yellow
    }

    exit 1
}
