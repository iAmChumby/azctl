# azctl installer (Windows PowerShell): downloads the single-file app into
# %LOCALAPPDATA%\azctl\bin, creates an `azctl` command shim there, and puts
# that directory on the user PATH. Nothing outside it is touched by this
# script; azctl itself bootstraps its own Python deps into a private venv on
# first run.
#
# Usage:
#   .\install.ps1              install from the default branch
#   .\install.ps1 some-ref     install from a specific tag/branch/commit
param(
    [string]$Ref = $env:AZCTL_REF,
    [switch]$NoPathUpdate
)
$ErrorActionPreference = "Stop"

if (-not $Ref) { $Ref = "main" }

$Repo = "iAmChumby/azctl"
$Url = "https://raw.githubusercontent.com/$Repo/$Ref/azctl.py"
$DestDir = if ($env:AZCTL_HOME) { $env:AZCTL_HOME } else { Join-Path $env:LOCALAPPDATA "azctl\bin" }
$DestPy = Join-Path $DestDir "azctl.py"
$Shim = Join-Path $DestDir "azctl.cmd"

function Fail($msg) {
    [Console]::Error.WriteLine("azctl installer: $msg")
    exit 1
}

# Locate a working Python 3 launcher (py launcher, python3, or python).
$pythonCmd = $null
$pythonPrefixArgs = @()
$candidates = @(
    , @("py", "-3")
    , @("python3")
    , @("python")
)
foreach ($entry in $candidates) {
    $exe = Get-Command $entry[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    $probeArgs = @()
    if ($entry.Count -gt 1) { $probeArgs = @($entry[1..($entry.Count - 1)]) }
    $ver = & $exe.Source @probeArgs --version 2>$null
    if ($LASTEXITCODE -eq 0 -and "$ver" -match "^Python 3") {
        $pythonCmd = $exe.Source
        $pythonPrefixArgs = $probeArgs
        break
    }
}
if (-not $pythonCmd) {
    Fail "Python 3 is required but was not found (tried py -3, python3, python). Install it from https://www.python.org/downloads/"
}

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

Write-Host "Downloading azctl ($Ref) -> $DestPy ..."
try {
    Invoke-WebRequest -Uri $Url -OutFile "$DestPy.tmp" -UseBasicParsing
} catch {
    Fail "download failed: $Url ($_)"
}
if ((Get-Item "$DestPy.tmp").Length -eq 0) {
    Remove-Item "$DestPy.tmp" -Force -ErrorAction SilentlyContinue
    Fail "downloaded file is empty: $Url"
}
Move-Item -Force -Path "$DestPy.tmp" -Destination $DestPy

# The shim forwards every argument to the installed file through the same
# Python launcher that was verified above.
$prefix = ($pythonPrefixArgs | ForEach-Object { "`"$_`"" }) -join " "
$shimLine = "`"$pythonCmd`" $prefix `"%~dp0azctl.py`" %*"
@("@echo off", $shimLine) | Set-Content -Path $Shim -Encoding Ascii

if (-not $NoPathUpdate) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($userPath -split ";") -notcontains $DestDir) {
        $newPath = if ($userPath) { "$userPath;$DestDir" } else { $DestDir }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Host "Added $DestDir to your user PATH (new terminals will see it)."
    }
    if (($env:Path -split ";") -notcontains $DestDir) {
        $env:Path = "$env:Path;$DestDir"
    }
}

# Smoke test through the shim itself.
& $Shim --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    Remove-Item $Shim, $DestPy -Force -ErrorAction SilentlyContinue
    Fail "the downloaded azctl did not run correctly; installation aborted."
}

Write-Host ""
Write-Host "Installed: azctl ($Ref)"
Write-Host "  app:      $DestPy"
Write-Host "  command:  $Shim  (on PATH: $DestDir)"
Write-Host ""
Write-Host "Run 'azctl' for the dashboard, 'azctl status' for a read-only snapshot."
Write-Host "First launch installs Textual+psutil into a private venv"
Write-Host "(%LOCALAPPDATA%\azctl\venv); your system Python is never touched."
