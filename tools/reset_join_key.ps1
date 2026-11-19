# Reset the join key that public/join.cmd asks for.
#
#   reset_join_key.cmd                    type the key you want, install it
#   reset_join_key.cmd -Key "abc..."      set it without being asked
#   reset_join_key.cmd -Show              print the key this machine holds now
#   reset_join_key.cmd -DryRun            show what would be set, change nothing live
#
# The key is shown because it is yours: it is the string you hand to whoever joins a
# machine, so you have to be able to read it off. It is written to build\song_endpoint.key
# (git-ignored) and set on the one thing that checks it:
#
#   browser  SONG_ENDPOINT_KEY  what the site verifies when a machine publishes its address
#
# That is the key that lets a GPU connect to the website: with it, a machine can tell the
# site "send the songs to me". After a reset the old key is refused with 403 and the next
# publish has to use the new one.

[CmdletBinding()]
param(
    [string]$Key = '',
    [string]$KeyFile = '',
    [switch]$Show,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# This file is also fetched raw from GitHub by reset_join_key.cmd, so it can run from
# %TEMP% with no project around it. In that case it has to find the installed app itself.
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'song_service'))) {
    foreach ($candidate in @(
        (Join-Path $env:LOCALAPPDATA 'internet-gpu\AI-driven_NVIDIA_song_designer-main'),
        (Join-Path $env:LOCALAPPDATA 'internet-gpu')
    )) {
        if (Test-Path (Join-Path $candidate 'song_service')) { $root = $candidate; break }
    }
}
# Not $keyFile: PowerShell variable names are case-insensitive, so that would overwrite
# the -KeyFile parameter with this path and make every run read the old key from disk.
$keyPath = Join-Path $root 'build\song_endpoint.key'
$wrangler = Join-Path $root 'build\wr.ps1'

function Say([string]$text) { Write-Host $text }

if (-not (Test-Path (Join-Path $root 'song_service'))) {
    Say "no installed app found (looked beside this script and in $env:LOCALAPPDATA\internet-gpu)"
    Say 'Run join.cmd on this PC first, or run this from the project folder.'
    exit 1
}

if ($Show -and -not $Key -and -not $KeyFile) {
    if (-not (Test-Path $keyPath)) { Say 'this machine has no key yet'; exit 1 }
    $current = (Get-Content -Raw -Path $keyPath).Trim()
    Say 'the join key this machine holds now:'
    Say ''
    Say $current
    Say ''
    Say 'that is the string join.cmd asks for. Change it with: reset_join_key.cmd'
    exit 0
}

if ($KeyFile) {
    if (-not (Test-Path $KeyFile)) { Say "no file at $KeyFile"; exit 1 }
    $Key = (Get-Content -Raw -Path $KeyFile).Trim()
    Say "read the key from $KeyFile"
}

if (-not $Key) {
    # The key is typed, not generated: it is handed to somebody who has to paste it into
    # join.cmd, and a string a person can pass on is easier to live with than a random
    # one nobody can remember. Enter with nothing typed makes a random key instead.
    Say 'Type the join key to use, or press Enter for a random one.'
    Say 'Any text works, with no spaces. Shorter is easier to type and easier to guess.'
    $typed = Read-Host 'Join key'
    if ($typed -and $typed.Trim()) {
        $Key = $typed.Trim()
        Say 'using the key you typed'
    } else {
        $bytes = New-Object byte[] 36
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $rng.GetBytes($bytes)
        $Key = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        Say 'made a new random key'
    }
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
Say 'the join key:'
Say ''
Say $Key
Say ''

if ($DryRun) {
    Say '-DryRun: nothing was written and the Worker was not touched'
    Say 'the key that works right now is unchanged'
    exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keyPath) | Out-Null
Set-Content -Path $keyPath -Value $Key -Encoding ascii -NoNewline
Say "written to build\song_endpoint.key ($($Key.Length) characters)"

Push-Location $root
try {
    # The bundled wrapper is in build\wr.ps1 in a full checkout; a copy that came out of
    # the GitHub zip has no build folder, so wrangler on PATH or npx is the fallback.
    $tool = $null
    if (Test-Path $wrangler) { $tool = $wrangler }
    elseif (Get-Command wrangler -ErrorAction SilentlyContinue) { $tool = 'wrangler' }
    elseif (Get-Command npx -ErrorAction SilentlyContinue) { $tool = 'npx' }
    if (-not $tool) {
        Say 'no wrangler found: install Node (npx wrangler) or run this from the project folder.'
        Say 'the local key file has the new key; the site was not told about it.'
        exit 1
    }
    $wranglerArgs = @('secret', 'put', 'SONG_ENDPOINT_KEY', '--config', 'wrangler.jsonc')
    if ($tool -eq 'npx') { $wranglerArgs = @('wrangler') + $wranglerArgs }
    Say 'setting SONG_ENDPOINT_KEY on wrangler.jsonc ...'
    $Key | & $tool @wranglerArgs 2>&1 | ForEach-Object {
        if ($_ -match 'Success|success|Uploaded|created|updated') { Say '  ' + $_ }
    }
    if ($LASTEXITCODE -ne 0) {
        Say "  wrangler exited $LASTEXITCODE"
        Say '  the file now holds the new key, so publishing will be refused until this succeeds'
        exit 1
    }
} finally {
    Pop-Location
}

Say ''
Say 'done. that is the key a machine needs to connect its GPU to the site.'
Say 'The machine already connected keeps its connection; the next publish uses this key.'
