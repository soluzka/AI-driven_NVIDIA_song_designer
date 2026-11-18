# Register the start-and-announce launcher at logon.
#
# Separate from tools\install_autostart.ps1 on purpose: that script points the logon
# entries at start_services.ps1, which publishes this machine's tunnel address once.
# This one points them at tools\start_ai_boot.ps1, which starts the same services,
# re-announces the address afterwards and then leaves one hidden watchdog behind.
#
# Both entries are written, as before, because on 8 Nov 2026 a boot produced no run
# from the Startup folder at all while the Run key worked.
#
# Remove with:
#   Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' 'AI song services'
#   Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'AI song services.cmd')
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_autostart_ai.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$boot = Join-Path $PSScriptRoot 'start_ai_boot.ps1'
if (-not (Test-Path $boot)) {
    throw "missing $boot - the boot launcher has to be beside this script"
}

$command = 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $boot + '"'

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
Set-ItemProperty -Path $runKey -Name 'AI song services' -Value $command
Write-Host "run key set: $runKey\AI song services"

$startup = [Environment]::GetFolderPath('Startup')
$cmd = Join-Path $startup 'AI song services.cmd'
Set-Content -Path $cmd -Encoding ascii -Value @(
    '@echo off',
    'rem Added by tools\install_autostart_ai.ps1: start the AI and re-announce it at logon.',
    'start "" /min ' + $command
)
Write-Host "startup entry: $cmd"

$watch = Get-CimInstance Win32_Process -Filter "Name like '%powershell%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*start_services.ps1*' -and $_.CommandLine -like '*-Watch*' }
if ($watch.Count -gt 0) {
    Write-Host "a watchdog is already running (pid $($watch[0].ProcessId)); the entries take over at the next logon"
} else {
    Write-Host 'run tools\start_ai.cmd once, or log off and back on, to start it now'
}
