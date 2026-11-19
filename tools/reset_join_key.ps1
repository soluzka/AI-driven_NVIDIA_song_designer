# Set the key that connects this PC's GPU to the song site.
#
#   reset_join_key.cmd                 type the key you want, then Enter
#   reset_join_key.cmd -Key "abc..."   set it in one line
#   reset_join_key.cmd -Show           print the key this PC holds now
#   reset_join_key.cmd -DryRun         show it, change nothing
#
# This works for anybody, on any PC that has the app installed: it needs no Cloudflare
# login and touches no server. It writes the key where join.cmd and the launcher already
# read it - <app>\build\song_endpoint.key - and then asks the site whether it accepts that
# key, so "saved" and "connected" are not the same thing in the output.
#
# It changes this PC only. Nobody can change the key the site itself holds from a
# download: that is a secret on the Worker, and only the Cloudflare account that owns it
# can set it.

[CmdletBinding()]
param(
    [string]$Key = '',
    [string]$App = '',
    [switch]$Show,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Say([string]$text) { Write-Host $text }

# --- where the app is --------------------------------------------------------
# join.ps1 unpacks it here. This file can also be fetched raw and run from %TEMP%, so it
# must not trust where it happens to be lying.
$root = Split-Path -Parent $PSScriptRoot
if (-not $App) {
    if (Test-Path (Join-Path $root 'song_service')) {
        $App = $root
    } else {
        foreach ($candidate in @(
            (Join-Path $env:LOCALAPPDATA 'internet-gpu\AI-driven_NVIDIA_song_designer-main'),
            (Join-Path $env:LOCALAPPDATA 'internet-gpu')
        )) {
            if (Test-Path (Join-Path $candidate 'song_service')) { $App = $candidate; break }
        }
    }
}
if (-not $App -or -not (Test-Path (Join-Path $App 'song_service'))) {
    Say 'I could not find the installed app on this PC.'
    Say 'It is normally in %LOCALAPPDATA%\internet-gpu\AI-driven_NVIDIA_song_designer-main.'
    $typed = Read-Host 'Paste the folder that holds song_service (or press Enter to stop)'
    if (-not $typed) { Say 'stopped'; exit 1 }
    $App = $typed.Trim('"').Trim()
    if (-not (Test-Path (Join-Path $App 'song_service'))) {
        Say "no song_service under $App"
        exit 1
    }
}
Say "app:  $App"

$keyPath = Join-Path $App 'build\song_endpoint.key'
$tunnelPath = Join-Path $App 'build\tunnel_url.txt'

if ($Show -and -not $Key) {
    if (-not (Test-Path $keyPath)) { Say 'this PC has no key yet'; exit 1 }
    Say ''
    Say 'the key this PC holds now:'
    Say ''
    Say ((Get-Content -Raw -Path $keyPath).Trim())
    exit 0
}

if (-not $Key) {
    Say ''
    Say 'Type the key that connects this PC to the site.'
    Say 'Any text works, with no spaces. Shorter is easier to type and easier to guess.'
    $typed = Read-Host 'Key'
    if (-not $typed) { Say 'no key given, nothing changed'; exit 1 }
    $Key = $typed.Trim()
}
if ($Key.Length -lt 4) {
    Say 'that key is too short: use at least 4 characters'
    exit 1
}
if ($Key -match '\s') {
    Say 'the key cannot contain spaces or line breaks'
    exit 1
}

Say ''
Say 'the key:'
Say ''
Say $Key
Say ''

if ($DryRun) {
    Say '-DryRun: nothing was written and the site was not contacted'
    exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keyPath) | Out-Null
Set-Content -Path $keyPath -Value $Key -Encoding ascii -NoNewline
Say "written to $keyPath"

# --- does the site accept it? ------------------------------------------------
# The site answers 200 when this key is the one it holds, and 403 when it is not. It knows
# the answer; this PC can only guess. The address is only sent so the check is the same
# request the launcher makes.
$address = if (Test-Path $tunnelPath) { (Get-Content -Raw -Path $tunnelPath).Trim() } else { '' }
if (-not $address) {
    Say ''
    Say 'no tunnel address yet, so the key could not be checked. Start the app (start.cmd in'
    Say 'the app folder), then run this again to see whether the site accepts the key.'
    exit 0
}
try {
    $body = @{ url = $address } | ConvertTo-Json -Compress
    $reply = Invoke-WebRequest -UseBasicParsing -Uri 'https://browser.bpiersing.workers.dev/api/song/endpoint' `
        -Method Post -Body $body -ContentType 'application/json' `
        -Headers @{ 'x-song-key' = $Key } -TimeoutSec 25
    if ($reply.StatusCode -ge 200 -and $reply.StatusCode -lt 300) {
        Say ''
        Say "the site accepted the key and knows this PC at $address"
    }
} catch {
    $status = $null
    if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    Say ''
    if ($status -eq 403) {
        Say 'the site refused this key (403): it is not the key the site holds.'
        Say 'The key is saved on this PC, but publishing will fail until it matches.'
        Say 'Only the owner of the site can change which key it holds.'
    } else {
        Say "could not check the key against the site: $($_.Exception.Message)"
    }
}
Say ''
Say 'done.'
