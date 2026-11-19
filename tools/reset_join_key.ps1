# Set the key that connects this PC's GPU to the song site.
#
#   reset_join_key.cmd                 type the key you want, then Enter
#   reset_join_key.cmd -Key "abc..."   set it in one line
#   reset_join_key.cmd -Show           print the key this PC holds now
#   reset_join_key.cmd -DryRun         show it, change nothing
#
# This works for anybody, on any PC that has the app installed: it needs no Cloudflare
# login and touches nothing but the file. It writes the key where join.cmd and the
# launcher already read it - <app>\build\song_endpoint.key - and nothing else on this
# machine, no server, no website.
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

Say ''
Say 'done. this PC will use that key the next time it starts and publishes.'
Say 'Nothing was sent anywhere: the website was not asked, and it still holds the key its'
Say 'owner set.'
