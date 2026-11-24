<#
.SYNOPSIS
  Copy this checkout into the installed copy of the app that join.cmd unpacked.

.DESCRIPTION
  public\join.cmd downloads and unpacks the app to
  %LOCALAPPDATA%\internet-gpu\AI-driven_NVIDIA_song_designer-main, and that
  unpacked folder is what actually runs on this PC. A fix committed in the
  checkout does not reach it until the files are copied across, which is what
  this does.

  What it changes:
    every file in the checkout that differs is copied over the installed one
  What it leaves alone:
    anything it has to delete - there is no mirror, so nothing is removed
    the installed .venv, song_service models, caches and logs
    the live index, %LOCALAPPDATA%\internet-gpu\video_index.db (outside the app)

.PARAMETER Target
  The installed copy to update. Defaults to the join.cmd location.

.PARAMETER List
  Show what would be copied without copying anything.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File build\update_install.ps1

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File build\update_install.ps1 -List

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File build\update_install.ps1 -Target D:\some\app
#>
[CmdletBinding()]
param(
    [string]$Target = (Join-Path $env:LOCALAPPDATA 'internet-gpu\AI-driven_NVIDIA_song_designer-main'),
    [switch]$List
)

$ErrorActionPreference = 'Stop'

# build\ -> the checkout that holds it.
$Repo = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path -LiteralPath (Join-Path $Repo 'ai_engine.py'))) {
    throw "This is not the checkout: $Repo\ai_engine.py is missing."
}
if (-not (Test-Path -LiteralPath (Join-Path $Target 'ai_engine.py'))) {
    throw "No installed copy at $Target. Install one with public\join.cmd, or pass -Target."
}
$Repo = (Resolve-Path -LiteralPath $Repo).Path
$Target = (Resolve-Path -LiteralPath $Target).Path

# The installed copy owns these; copying them would take a multi-gigabyte models
# folder or the virtualenv with it, and one run would be enough to fill the disk.
$ExcludeDirs = @('.git', '.venv', '.freebuff', 'node_modules', '__pycache__',
                 '.pytest_cache', 'build', 'models', 'logs', 'deploy')
$ExcludeFiles = @('*.pyc', '*.pyo', '*.log', '*.err')

$arguments = @($Repo, $Target, '/E',
               '/XD') + $ExcludeDirs + @('/XF') + $ExcludeFiles +
             @('/NFL', '/NDL', '/NP', '/R:1', '/W:1')
if ($List) { $arguments += '/L' }

Write-Host "Updating $Target"
Write-Host "From     $Repo"
Write-Host ''
& robocopy @arguments | Out-Host
$code = $LASTEXITCODE

# robocopy: 0 nothing to do, 1 files copied, 2 extras seen, 3 both; 8 and above
# are failures and must not be reported as a tidy zero.
if ($code -ge 8) {
    throw "Copy failed with robocopy code $code. Run it again once the cause is fixed."
}
Write-Host ''

# build\ is excluded above because it holds renders and stems, but build\measure is the
# small folder of measurement tools - and the installed copy is where the renders and
# logs are, so those tools are used from there. It is copied on its own, minus audio.
$Measure = Join-Path $Repo 'build\measure'
if (Test-Path -LiteralPath $Measure) {
    $measureArgs = @($Measure, (Join-Path $Target 'build\measure'), '/E',
                     '/XF', '*.wav', '*.pyc', '*.log', '/NFL', '/NDL', '/NP', '/R:1', '/W:1')
    if ($List) { $measureArgs += '/L' }
    Write-Host 'Measurement tools:'
    & robocopy @measureArgs | Out-Host
    if ($LASTEXITCODE -ge 8) {
        throw "Copying build\measure failed with robocopy code $LASTEXITCODE."
    }
    Write-Host ''
}

if ($List) {
    Write-Host 'List only: nothing was copied.'
    return
}
Write-Host 'Updated. Restart the services so the new code is loaded:'
Write-Host '  tools\start_ai.cmd'
Write-Host 'The live index was not touched. If it holds no videos, the app seeds it from'
Write-Host 'data\video_index.db on the next start.'
