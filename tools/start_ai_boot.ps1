# The logon half of tools\start_ai.cmd: no window, no pause, and the address is
# announced again after the services are up, so the site is never left holding a
# tunnel that has since been replaced.
#
# It also accepts and ignores arguments: Explorer appends the songsvc:// address to
# a scheme launch, and a launcher that treats that as a parameter fails before it
# starts anything.

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    $Rest
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot

$services = Join-Path $root 'tools\start_services.ps1'
if (-not (Test-Path $services)) { exit 1 }

& $services

$republish = Join-Path $root 'tools\republish_address.ps1'
if (Test-Path $republish) {
    try { & $republish } catch { }
}

# One watchdog, hidden, so anything that dies during the session comes back.
Start-Process -FilePath 'powershell' -WindowStyle Hidden -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $services, '-Watch'
) | Out-Null
