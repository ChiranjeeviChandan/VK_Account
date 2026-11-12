# Installs Felix for the current Windows user: a private Python environment, a
# `felix` command and Start menu shortcuts. Run it again after `git pull` to update.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
#   $env:FELIX_PREFIX         where the environment goes (default %LOCALAPPDATA%\Felix\app)
#   $env:FELIX_NO_SHORTCUTS   set to 1 to leave the Start menu alone

$here = Split-Path -Parent $PSScriptRoot
$prefix = if ($env:FELIX_PREFIX) { $env:FELIX_PREFIX } else { Join-Path $env:LOCALAPPDATA "Felix\app" }
$scripts = Join-Path $prefix "Scripts"
$felix = Join-Path $scripts "felix.exe"

function Stop-Install([string] $message) {
    Write-Host $message -ForegroundColor Red
    exit 1
}

# True when the semicolon-separated list of folders contains Felix's own.
function Test-HasFelix([string] $folders) {
    foreach ($folder in ($folders -split ";")) {
        if ($folder.Trim().TrimEnd("\") -ieq $scripts.TrimEnd("\")) { return $true }
    }
    return $false
}

# The program this installer was started from ("cmd", "powershell", "explorer", ...),
# or nothing when that cannot be told.
function Get-StartedFrom {
    try {
        $me = Get-CimInstance Win32_Process -Filter "ProcessId = $PID" -ErrorAction Stop
        return (Get-Process -Id $me.ParentProcessId -ErrorAction Stop).ProcessName.ToLower()
    } catch {
        return ""
    }
}

# Find a Python that is new enough. The Microsoft Store placeholder named python.exe
# fails this check and is skipped.
$candidates = @(
    @{ Exe = "py"; Args = @("-3") },
    @{ Exe = "python"; Args = @() },
    @{ Exe = "python3"; Args = @() }
)
$python = $null
foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
    $extra = $candidate.Args
    try {
        & $candidate.Exe @extra -c "import sys; raise SystemExit(sys.version_info < (3, 10))" *> $null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    } catch {
        continue
    }
}
if (-not $python) {
    Stop-Install "Felix needs Python 3.10 or newer. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this again."
}

# Windows will not replace a program that is running, so an update needs Felix stopped.
if (Test-Path (Join-Path $scripts "python.exe")) {
    $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($prefix + "\", [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    })
    if ($running.Count -gt 0) {
        Stop-Install "Felix is running, so it cannot be updated. Stop it first (close its window, or run: felix stop), then run this again."
    }
}

$extra = $python.Args
Write-Host "Installing Felix into $prefix"
& $python.Exe @extra -m venv $prefix
if ($LASTEXITCODE -ne 0) { Stop-Install "Python could not create an environment in $prefix." }

$venvPython = Join-Path $scripts "python.exe"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet --upgrade $here
if ($LASTEXITCODE -ne 0) { Stop-Install "Installing Felix failed. Check your internet connection and run this again." }
if (-not (Test-Path $felix)) { Stop-Install "The install finished, but $felix is missing. Run this again." }

# New terminal windows find `felix` through your own PATH.
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (-not (Test-HasFelix $userPath)) {
    $newPath = if ($userPath) { $userPath.TrimEnd(";") + ";" + $scripts } else { $scripts }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
}

# A window that was already open keeps the PATH it started with. Inside PowerShell the
# change below is enough. But when PowerShell was started only to run this script (the
# command at the top does that), the change ends with it, and the window you typed in
# still does not know `felix`.
$knownHere = Test-HasFelix $env:Path
if (-not $knownHere) { $env:Path = $env:Path.TrimEnd(";") + ";" + $scripts }
$startedForThis = [bool]([Environment]::GetCommandLineArgs() | Where-Object { $_ -like "*install.ps1*" })
$startedFrom = if ($startedForThis) { Get-StartedFrom } else { "" }
$needsNewWindow = (-not $knownHere) -and $startedForThis

# Start menu shortcuts, so that starting Felix does not need a terminal at all.
$shortcuts = $false
if (-not $env:FELIX_NO_SHORTCUTS) {
    try {
        $folder = Join-Path ([Environment]::GetFolderPath("Programs")) "Felix"
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        $shell = New-Object -ComObject WScript.Shell
        $entries = @(
            @{ Name = "Felix"; Arguments = "start --keep-open"; Note = "Run Felix and open the dashboard" },
            @{ Name = "Felix demo"; Arguments = "demo --keep-open"; Note = "Look around with sample data. Nothing is sent." }
        )
        foreach ($entry in $entries) {
            $link = $shell.CreateShortcut((Join-Path $folder ($entry.Name + ".lnk")))
            $link.TargetPath = $felix
            $link.Arguments = $entry.Arguments
            $link.WorkingDirectory = $env:USERPROFILE
            $link.Description = $entry.Note
            $link.Save()
        }
        $shortcuts = $true
    } catch {
        Write-Host "The Start menu shortcuts could not be created: $($_.Exception.Message)"
    }
}

$version = & $felix --version
Write-Host "Installed $version."
Write-Host ""
if ($shortcuts) {
    Write-Host "Start Felix from the Start menu: 'Felix demo' to look around with sample data,"
    Write-Host "'Felix' to run it for real. Or use a terminal:"
    Write-Host ""
}
if ($needsNewWindow) {
    Write-Host "Open a NEW terminal window and type:"
} else {
    Write-Host "Next:"
}
Write-Host "  felix demo      look around with sample data"
Write-Host "  felix setup     connect your own accounts"
Write-Host "  felix start     run Felix and open the dashboard"
if ($needsNewWindow) {
    Write-Host ""
    Write-Host "This window was opened before Felix was installed, so it does not know the"
    Write-Host "'felix' command yet. To use Felix in this window anyway, type its full name:"
    Write-Host ""
    if ($startedFrom -ne "cmd") { Write-Host "  & `"$felix`" demo" }
    if ($startedFrom -eq "cmd" -or $startedFrom -notmatch "^(powershell|pwsh)") { Write-Host "  `"$felix`" demo" }
    if ($startedFrom -ne "cmd" -and $startedFrom -notmatch "^(powershell|pwsh)") {
        Write-Host ""
        Write-Host "(The first line is for PowerShell, the second for Command Prompt.)"
    }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (([Security.Principal.WindowsPrincipal] $identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "Note: this window has administrator rights. Felix does not need them. Start Felix"
    Write-Host "from the Start menu or a normal window, so that what it does on this laptop does"
    Write-Host "not run as administrator."
}

# Started by double-click or "Run with PowerShell": the window would close at once.
if ($startedFrom -eq "explorer") { Read-Host "Press Enter to close this window" | Out-Null }
