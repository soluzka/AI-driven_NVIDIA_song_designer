# Reset the join key that public/join.cmd asks for.
#
#   reset_join_key.cmd                    type the key you want, install it
#   reset_join_key.cmd -Key "abc..."      set it without being asked
#   reset_join_key.cmd -Show              print the key this machine holds now
#   reset_join_key.cmd -DryRun            show what would be set, change nothing live
#
# The key is shown because it is yours: it is the string you hand to whoever joins a
# machine, so you have to be able to read it off. It is also written to
# build\song_endpoint.key (git-ignored) and pushed to the two Workers that check it:
#
#   song-pool  JOIN_KEY           what /join and /remove verify
#   browser    SONG_ENDPOINT_KEY  what the site verifies when a machine publishes an address
#
# Machines already in the pool stay in it; the key is only checked when a machine joins.
# Anyone joining from now on pastes the new key into join.cmd, and the old one gets 403.

[CmdletBinding()]
param(
    [string]$Key = '',
    [string]$KeyFile = '',
    [switch]$Show,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
# Not $keyFile: PowerShell variable names are case-insensitive, so that would overwrite
# the -KeyFile parameter with this path and make every run read the old key from disk.
$keyPath = Join-Path $root 'build\song_endpoint.key'
$wrangler = Join-Path $root 'build\wr.ps1'

function Say([string]$text) { Write-Host $text }

if (-not (Test-Path (Join-Path $root 'song_service'))) {
    Say "this is not the project folder: $root has no song_service"
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
    Say 'It needs at least 24 characters and no spaces - it is what join.cmd asks for.'
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

if ($Key.Length -lt 24) {
    Say 'that key is too short: it has to be at least 24 characters'
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
    Say '-DryRun: nothing was written and neither Worker was touched'
    Say 'the key that works right now is unchanged'
    exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keyPath) | Out-Null
Set-Content -Path $keyPath -Value $Key -Encoding ascii -NoNewline
Say "written to build\song_endpoint.key ($($Key.Length) characters)"

Push-Location $root
try {
    foreach ($target in @(
        @{ Config = 'wrangler.pool.jsonc'; Name = 'JOIN_KEY' },
        @{ Config = 'wrangler.jsonc'; Name = 'SONG_ENDPOINT_KEY' }
    )) {
        Say "setting $($target.Name) on $($target.Config) ..."
        $Key | & $wrangler secret put $target.Name --config $target.Config 2>&1 | ForEach-Object {
            if ($_ -match 'Success|success|Uploaded|created|updated') { Say '  ' + $_ }
        }
        if ($LASTEXITCODE -ne 0) {
            Say "  wrangler exited $LASTEXITCODE"
            Say '  the file now holds the new key, so joins will be refused until this succeeds'
            exit 1
        }
    }
} finally {
    Pop-Location
}

Say ''
Say 'done. this is the key to paste into join.cmd when a machine joins.'
Say 'Machines already in the pool stay in it.'
