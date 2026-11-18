# Tell the site, and the pool, which address this machine is on right now.
#
# The launcher publishes once, at start-up. A tunnel that is restarted after that,
# or a listing that has aged, leaves the site holding an address nothing answers.
# This is the "still here" half on its own, so it can be run by hand or from a
# scheduled run without starting anything again.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\republish_address.ps1
#         powershell ... -SiteUrl https://something.example

[CmdletBinding()]
param(
    [string]$SiteUrl = 'https://browser.bpiersing.workers.dev'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$keyFile = Join-Path $root 'build\song_endpoint.key'
$urlFile = Join-Path $root 'build\tunnel_url.txt'
$tunnelLog = Join-Path $root 'tunnel.log'

if (-not (Test-Path $keyFile)) {
    Write-Host 'no build\song_endpoint.key here, so the site cannot be told anything'
    exit 1
}

$url = ''
if (Test-Path $urlFile) { $url = (Get-Content $urlFile -Raw).Trim() }
if (-not $url -and (Test-Path $tunnelLog)) {
    $match = Select-String -Path $tunnelLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' |
        ForEach-Object { $_.Matches[0].Value } | Select-Object -Last 1
    if ($match) { $url = $match }
}
if (-not $url) {
    Write-Host 'no tunnel address yet: start the services first (tools\start_ai.cmd)'
    exit 1
}

$key = (Get-Content $keyFile -Raw).Trim()
Write-Host "address: $url"

# JSON through a file: curl.exe on Windows mangles quotes passed as an argument.
$payload = Join-Path $root 'build\tunnel_publish.json'
try {
    Set-Content -Path $payload -Encoding ascii -Value ('{"url":"' + $url + '"}')

    $site = & curl.exe -s --max-time 20 -X POST "$SiteUrl/api/song/endpoint" `
        -H 'Content-Type: application/json' -H "x-song-key: $key" --data-binary "@$payload"
    Write-Host "site: $site"

    $pool = & curl.exe -s --max-time 20 -X POST 'https://song-pool.bpiersing.workers.dev/join' `
        -H 'Content-Type: application/json' -H "x-join-key: $key" --data-binary "@$payload"
    Write-Host "pool: $pool"

    $health = & curl.exe -s --max-time 20 "$url/health"
    if ($health) { Write-Host "this machine answers: $health" }
    else { Write-Host "the address does not answer yet: the model may still be warming" }
} finally {
    Remove-Item $payload -ErrorAction SilentlyContinue
}
