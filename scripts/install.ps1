# Installs Felix for the current Windows user: a private Python environment and
# a `felix` command. Run it again after `git pull` to update.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
#   $env:FELIX_PREFIX   where the environment goes (default %LOCALAPPDATA%\Felix\app)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $PSScriptRoot
$prefix = if ($env:FELIX_PREFIX) { $env:FELIX_PREFIX } else { Join-Path $env:LOCALAPPDATA "Felix\app" }

$python = $null
foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
    $exe = $candidate[0]
    $rest = @($candidate | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    & $exe @rest -c "import sys; raise SystemExit(sys.version_info < (3, 10))" 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}
if (-not $python) {
    Write-Error "Felix needs Python 3.10 or newer. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this again."
}

$exe = $python[0]
$rest = @($python | Select-Object -Skip 1)
Write-Host "Installing Felix into $prefix"
& $exe @rest -m venv $prefix
if ($LASTEXITCODE -ne 0) { Write-Error "Python could not create an environment in $prefix." }

$venvPython = Join-Path $prefix "Scripts\python.exe"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet --upgrade $here
if ($LASTEXITCODE -ne 0) { Write-Error "Installing Felix failed. Check your internet connection and run this again." }

$scripts = Join-Path $prefix "Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not (($userPath -split ";") -contains $scripts)) {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $scripts), "User")
    Write-Host "Added $scripts to your PATH. Open a new terminal window to use the 'felix' command."
}

Write-Host ("Installed " + (& (Join-Path $scripts "felix.exe") --version) + ".")
Write-Host ""
Write-Host "Next:"
Write-Host "  felix demo      look around with sample data"
Write-Host "  felix setup     connect your own accounts"
Write-Host "  felix start     run Felix and open the dashboard"
