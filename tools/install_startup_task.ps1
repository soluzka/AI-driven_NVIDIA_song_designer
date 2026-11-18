<#
.SYNOPSIS
    Start the services at boot, before anyone logs in, with no window.

.DESCRIPTION
    The logon Run key (tools\install_autostart.ps1) needs somebody to sign in:
    the machine can sit at the lock screen with the GPU idle. This registers a
    Scheduled Task instead, triggered AtStartup, running as the current user with
    S4U - "whether the user is logged on or not", without storing a password.

    That needs administrator rights once, because a task that runs before a logon
    cannot be created by a standard user. There is no elevation prompt after that:
    the task and the watchdog it starts keep the site, the downloader, the song
    service and the tunnel up.

    Remove it with -Remove.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_startup_task.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_startup_task.ps1 -Remove
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$taskName = 'AI song services (boot)'

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Host 'This needs administrator rights once: a task that runs before logon cannot be created otherwise.'
    Write-Host 'Right-click PowerShell, Run as administrator, then:'
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\install_startup_task.ps1`""
    exit 1
}

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "removed: $taskName"
    exit 0
}

$launcher = Join-Path $PSScriptRoot 'start_services.ps1'
if (-not (Test-Path $launcher)) { throw "launcher not found: $launcher" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`" -Watch" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
# S4U: the task owns the user's profile without a stored password. Interactive
# logon is not required, which is the point - the GPU is serving before the lock
# screen is answered.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "registered: $taskName (AtStartup, S4U, no window)"
Write-Host '  Start-ScheduledTask -TaskName ''' + $taskName + '''    to run it now'
Write-Host '  Get-ScheduledTaskInfo -TaskName ''' + $taskName + '''   to see the last result'
Write-Host ''
Write-Host 'The logon Run key can stay as a second chance; the task and the watchdog'
Write-Host 'both check for a running launcher, so only one copy ever runs.'
