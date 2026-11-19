# Put this computer's GPU in the pool, in one command.
#
#   join_pool.cmd                 install if needed, start the models, join the pool
#   join_pool.cmd -NoStart        use the services that are already running
#   join_pool.cmd -Leave          take this machine back out of the pool
#
# Everything the models need - the portable Python, the .venv, the packages and the
# ~10 GB of weights - is installed by install_all.ps1, which is what install.cmd
# runs. This script only calls it when this machine has never been set up, so a
# second run is instant.
#
# The join key is read from build\song_endpoint.key and sent only to the pool. It is
# never printed and never leaves this machine except as the header of that request:
# the key decides who may add a card, and sharing it would let anyone add theirs.

[CmdletBinding()]
param(
    [switch]$NoStart,
    [switch]$Leave
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root 'song_service\.venv\Scripts\python.exe'
$keyFile = Join-Path $root 'build\song_endpoint.key'
$tunnelFile = Join-Path $root 'build\tunnel_url.txt'
$poolJoin = 'https://song-pool.bpiersing.workers.dev/join'
$poolRemove = 'https://song-pool.bpiersing.workers.dev/remove'

function Say([string]$text) { Write-Host $text }

if (-not (Test-Path $keyFile)) {
    Say "Missing $keyFile"
    Say 'That file holds the key that lets a card into the pool. Without it this machine can'
    Say 'still run the models for itself; it just cannot join the shared pool.'
    exit 1
}

$key = (Get-Content -Raw -Path $keyFile).Trim()
if (-not $key) {
    Say "$keyFile is empty."
    exit 1
}

# --- leaving ----------------------------------------------------------------
# One command out, because a card in the pool is the card strangers' songs are sent
# to: whoever joined it must be able to take it back without editing anything.
if ($Leave) {
    $mine = ''
    if (Test-Path $tunnelFile) { $mine = (Get-Content -Raw -Path $tunnelFile).Trim() }
    if (-not $mine) {
        Say "No tunnel address in $tunnelFile, so there is no card to take out."
        exit 1
    }
    $body = @{ url = $mine } | ConvertTo-Json -Compress
    try {
        $answer = Invoke-RestMethod -Uri $poolRemove -Method Post -Body $body `
            -ContentType 'application/json' -Headers @{ 'x-join-key' = $key } -TimeoutSec 30
    } catch {
        Say "The pool refused the removal: $($_.Exception.Message)"
        exit 1
    }
    Say "left the pool. it now holds $($answer.count) card(s):"
    foreach ($machine in $answer.pool) { Say "  $machine" }
    exit 0
}

# --- the models --------------------------------------------------------------
if (-not (Test-Path $python)) {
    Say 'This machine has not been set up yet: installing Python, the packages and the weights.'
    Say 'That is the long step (roughly 10 GB) and it happens once.'
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'install_all.ps1') -Start
    if (-not (Test-Path $python)) {
        Say 'The install did not finish, so there is no interpreter to start the models with.'
        exit 1
    }
} elseif (-not $NoStart) {
    Say 'Starting the song model, the voice model, the site and the tunnel.'
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'tools\start_services.ps1') -Quiet
}

# --- this machine's address --------------------------------------------------
# The tunnel writes its address when it connects; a Quick Tunnel gets a new one on
# every start, which is why the pool is told again on every run.
$address = ''
$deadline = (Get-Date).AddMinutes(4)
while ((Get-Date) -lt $deadline) {
    if (Test-Path $tunnelFile) {
        $address = (Get-Content -Raw -Path $tunnelFile).Trim()
        if ($address) { break }
    }
    Start-Sleep -Seconds 3
}
if (-not $address) {
    Say "No tunnel address after four minutes ($tunnelFile)."
    Say 'Without a tunnel nothing outside this machine can reach the card, so there is nothing to join.'
    exit 1
}
Say "this machine's address: $address"

# --- does the card answer? ---------------------------------------------------
$answered = $false
$deadline = (Get-Date).AddMinutes(4)
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri "$address/health" -TimeoutSec 20
        if ($health.loaded) { $answered = $true; break }
        Say "the service is up but its model is still loading ($($health.warming))"
    } catch {
        Say "waiting for $address to answer: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 5
}
if (-not $answered) {
    Say 'The card never reported a loaded model, so the pool was not told about it.'
    Say 'A card counts only when /health says loaded: true.'
    exit 1
}
Say "card: $($health.gpu) - model: $($health.model)"

# --- the pool ----------------------------------------------------------------
$body = @{ url = $address; note = 'joined from join_pool' } | ConvertTo-Json -Compress
try {
    $answer = Invoke-RestMethod -Uri $poolJoin -Method Post -Body $body `
        -ContentType 'application/json' -Headers @{ 'x-join-key' = $key } -TimeoutSec 30
} catch {
    Say "The pool refused this card: $($_.Exception.Message)"
    Say 'A 403 here means the key in build\song_endpoint.key is not the key the pool holds.'
    exit 1
}

Say "joined. the pool now holds $($answer.count) card(s):"
foreach ($machine in $answer.pool) { Say "  $machine" }
Say ''
Say 'The site picks the card that joined last. Open https://browser.bpiersing.workers.dev/'
Say 'and the header names the card that is answering.'
