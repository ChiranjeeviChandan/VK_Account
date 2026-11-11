# Installs Felix for the current Windows user: a private Python environment and
# a `felix` command. Run it again after `git pull` to update.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
#   $env:FELIX_PREFIX   where the environment goes (default %LOCALAPPDATA%\Felix\app)

$here = Split-Path -Parent $PSScriptRoot
$prefix = if ($env:FELIX_PREFIX) { $env:FELIX_PREFIX } else { Join-Path $env:LOCALAPPDATA "Felix\app" }

function Stop-Install([string] $message) {
    Write-Host $message -ForegroundColor Red
    exit 1
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

$extra = $python.Args
Write-Host "Installing Felix into $prefix"
& $python.Exe @extra -m venv $prefix
if ($LASTEXITCODE -ne 0) { Stop-Install "Python could not create an environment in $prefix." }

$venvPython = Join-Path $prefix "Scripts\python.exe"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet --upgrade $here
if ($LASTEXITCODE -ne 0) { Stop-Install "Installing Felix failed. Check your internet connection and run this again." }

$scripts = Join-Path $prefix "Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (-not (($userPath -split ";") -contains $scripts)) {
    $newPath = if ($userPath) { $userPath.TrimEnd(";") + ";" + $scripts } else { $scripts }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Added $scripts to your PATH. Open a new terminal window to use the 'felix' command."
}

$version = & (Join-Path $scripts "felix.exe") --version
Write-Host "Installed $version."
Write-Host ""
Write-Host "Next:"
Write-Host "  felix demo      look around with sample data"
Write-Host "  felix setup     connect your own accounts"
Write-Host "  felix start     run Felix and open the dashboard"
