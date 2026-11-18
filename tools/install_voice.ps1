# Build the voice service's environment on a machine that has never had Python.
#
# The same bootstrap install_all.ps1 uses, on its own so the join CMD can call it:
# a portable uv (15 MB, per-user, no elevation) downloads a CPython 3.11 and makes
# the venv with it. Nothing here assumes a python, a py launcher, or a venv already
# exists - if none of those are there, uv is fetched and the interpreter with it.
#
# Then voice_service\install.py runs inside that venv, which installs chatterbox and
# puts the +cu126 torch wheels back after pip has laid down the CPU ones.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_voice.ps1
#         powershell ... -AppDir "C:\path\to\AI-driven_NVIDIA_song_designer-main"

[CmdletBinding()]
param(
    [string]$AppDir = ''
)

$ErrorActionPreference = 'Stop'
if (-not $AppDir) { $AppDir = Split-Path -Parent $PSScriptRoot }

$toolsBin = Join-Path $AppDir 'tools\bin'
$voiceDir = Join-Path $AppDir 'voice_service'
$installer = Join-Path $voiceDir 'install.py'
$venv = Join-Path $voiceDir '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

$uvVersion = '0.13.9'
$uvZipUrl = "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip"

function Say([string]$text) { Write-Host $text }

if (-not (Test-Path $installer)) {
    Say "no $installer, so there is nothing to install"
    exit 1
}

if (Test-Path $python) {
    Say 'the voice environment is already in place'
    exit 0
}

function Find-Uv {
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $local = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path $local) { return $local }
    $inBin = Join-Path $toolsBin 'uv.exe'
    if (Test-Path $inBin) { return $inBin }
    return ''
}

function Get-Uv {
    $uv = Find-Uv
    if ($uv) { return $uv }

    if (-not (Test-Path $toolsBin)) {
        New-Item -ItemType Directory -Path $toolsBin -Force | Out-Null
    }
    Say "  fetching uv $uvVersion (~15 MB, once, no installer, no admin)"
    $zip = Join-Path $toolsBin 'uv.zip'
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $uvZipUrl -OutFile $zip -UseBasicParsing -TimeoutSec 600
    Expand-Archive -Path $zip -DestinationPath $toolsBin -Force
    Remove-Item $zip -ErrorAction SilentlyContinue

    $uv = Find-Uv
    if (-not $uv) {
        $nested = Get-ChildItem -Path $toolsBin -Filter 'uv.exe' -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($nested) { $uv = $nested.FullName }
    }
    if (-not $uv) { throw 'could not get uv, so no interpreter could be created' }
    return $uv
}

Say 'voice service: building its environment'
$uv = Get-Uv
Say "  uv: $uv"

# --python 3.11 makes uv download that CPython if this machine has none; --seed
# puts pip in the venv, which install.py uses.
Say '  creating voice_service\.venv on Python 3.11 (uv downloads the interpreter if needed)'
& $uv venv $venv --python 3.11 --seed
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $python)) {
    throw 'uv could not create voice_service\.venv'
}

Say '  installing chatterbox and the +cu126 torch build (a few GB, prints pip output)'
& $python $installer
if ($LASTEXITCODE -ne 0) {
    Say '  the voice environment did not finish; run this again to repair it'
    exit $LASTEXITCODE
}

Say 'voice service: ready on 127.0.0.1:8095 the next time the AI starts'
exit 0
