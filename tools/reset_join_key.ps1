# Set the key that connects this PC's GPU to the song site.
#
#   reset_join_key.cmd                 type the key you want, then Enter
#   reset_join_key.cmd -Key "abc..."   set it in one line
#   reset_join_key.cmd -Show           print the key this PC holds now
#   reset_join_key.cmd -DryRun         show it, change nothing
#
# The key is written where join.cmd and the launcher already read it -
# <app>\build\song_endpoint.key - and then handed to the two Workers, but only on a PC
# where wrangler is signed in to the Cloudflare account that owns them. That second half
# is the point: a card whose file says one thing while the Workers check another is
# refused with "Wrong join key", so it runs, serves songs, and the site's header still
# says the pool is empty. Writing the file alone is what made that state possible.
#
# It sets the GPU connect key: JOIN_KEY on the pool Worker, POOL_JOIN_KEY on the site.
# It never touches SONG_ENDPOINT_KEY, the song key, which signs publishing and is a
# different secret now. Without a Cloudflare login the file is still written and the
# script says plainly that the Workers kept their own key.

[CmdletBinding()]
param(
    [string]$Key = '',
    [string]$Url = '',
    [string]$App = '',
    [switch]$Show,
    [switch]$DryRun,
    [switch]$NoSync
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
$typedUrlPath = Join-Path $App 'build\model_endpoint_url.txt'

# --- an address instead of a key --------------------------------------------
# The same command sets the model endpoint when what is pasted is a URL. That is the
# address the page shows as "MODEL ENDPOINT" and every device is told to use, and naming
# it here is how a host from somewhere else - a card on another PC, a Kaggle one - becomes
# the model the site hands out. A key is still a key: no spaces, written to its own file.
# Writing a URL into the key file would leave a machine that runs and can never publish.
if (-not $Url -and $Key -match '^https?://') { $Url = $Key; $Key = '' }
if ($Url) {
    $Url = $Url.Trim().TrimEnd('/')
    if ($Url -notmatch '^https://[^\s]+$' -and $Url -notmatch '^http://[^\s]+$') {
        Say 'that is not an http(s) address'
        exit 1
    }
    $held = ''
    if (Test-Path $keyPath) { $held = ((Get-Content -Raw -Path $keyPath) -as [string]).Trim() }
    Say ''
    Say "model endpoint: $Url"
    if (-not $held) {
        Say 'this PC has no join key yet, so it is not allowed to name the site''s address.'
        Say 'Run this once with your join key, then paste the address.'
        exit 1
    }
    if ($DryRun) {
        Say '-DryRun: the address was checked and nothing was sent, and the site still names what it named.'
        exit 0
    }
    Say 'asking that address to answer ...'
    $alive = $false
    try {
        $probe = Invoke-WebRequest -UseBasicParsing -Method Get -Uri ($Url + '/health') -TimeoutSec 8
        $alive = ($probe.StatusCode -eq 200)
    } catch { $alive = $false }
    if (-not $alive) {
        Say 'it did not answer /health, so the site still names whatever it named before.'
        Say 'Nothing was changed.'
        exit 1
    }
    try {
        Invoke-RestMethod -Method Post `
            -Uri 'https://song-pool.bpiersing.workers.dev/state/site:endpoint' `
            -Headers @{ 'x-join-key' = $held } -ContentType 'application/json' `
            -Body (@{ value = $Url } | ConvertTo-Json -Compress) -TimeoutSec 25 | Out-Null
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $typedUrlPath) | Out-Null
        Set-Content -Path $typedUrlPath -Value $Url -Encoding ascii -NoNewline
        Say 'the site now names that address as the model endpoint, for every device.'
        Say "kept in $typedUrlPath, so a later run of this names it again."
        exit 0
    } catch {
        Say "the site refused it: $($_.Exception.Message)"
        Say 'Nothing was changed here; the key on this PC is probably not the one the pool Worker holds.'
        exit 1
    }
}

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
    Say '-DryRun: nothing was written and nothing was contacted'
    exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keyPath) | Out-Null
Set-Content -Path $keyPath -Value $Key -Encoding ascii -NoNewline
Say "written to $keyPath"

# --- and the Workers have to agree with it --------------------------------
# A join is checked against the pool Worker's JOIN_KEY and a publish against the site's
# key, so the file by itself can leave a machine that runs perfectly and never appears in
# the pool. Both secrets are set here from the value just written, with stdin taken from
# the file so the key is never retyped and a stray newline cannot get into the secret.
$script:nodePath = Join-Path $App 'tools\node\node.exe'
$script:wranglerPath = Join-Path $App 'build\wrangler-tool\node_modules\wrangler\bin\wrangler.js'
$script:npmPath = Join-Path $App 'tools\node\npm.cmd'

# --- wrangler, fetched once if it is missing --------------------------------
# Setting the key on the Workers is the one thing this script cannot do without
# wrangler, and an install does not carry it: without this a person types a key here,
# the Workers never hear it, and the pool refuses the address with a 403 that looks
# like a wrong key but is a key that was never set. npm sits beside node, so it is
# fetched from there once into build\wrangler-tool and kept.
function Ensure-Wrangler {
    if ((Test-Path $script:nodePath) -and (Test-Path $script:wranglerPath)) { return $true }
    if (-not (Test-Path $script:npmPath)) { return $false }
    $tool = Join-Path $App 'build\wrangler-tool'
    New-Item -ItemType Directory -Force -Path $tool | Out-Null
    Say '  fetching wrangler once (a minute or two): it is what sets the key on the Workers'
    $out = (& cmd /c ('"{0}" install --prefix "{1}" --no-audit --no-fund --loglevel=error wrangler@4 2>&1' -f $script:npmPath, $tool)) | Out-String
    if (Test-Path $script:wranglerPath) {
        Say '  wrangler: ready'
        return $true
    }
    $why = (($out -split "`n") | Where-Object { $_.Trim() } | Select-Object -Last 1)
    Say "  wrangler: could not be fetched - $why"
    return $false
}

$canSync = -not $NoSync -and (Ensure-Wrangler)

function Push-Secret([string]$label, [string]$name, [string[]]$extra) {
    $line = '"{0}" "{1}" secret put {2}' -f $script:nodePath, $script:wranglerPath, $name
    if ($extra -and $extra.Count) { $line += ' ' + ($extra -join ' ') }
    $line += ' < "{0}" 2>&1' -f $keyPath
    Push-Location $App
    $out = (& cmd /c $line) | Out-String
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -eq 0 -and $out -match 'Success') {
        Say "  $label : set"
        return $true
    }
    $why = (($out -split "`n") | Where-Object { $_.Trim() } | Select-Object -Last 1)
    if (-not $why) { $why = "wrangler exited with $code" }
    Say "  $label : not set - $why"
    return $false
}

Say ''
if ($NoSync) {
    Say '-NoSync: the Workers were not contacted and keep the key they hold.'
} elseif (-not $canSync) {
    Say 'The Workers were not contacted: there is no wrangler here and no npm to fetch it, so the'
    Say 'key is written on this PC only. Run this where wrangler can run, signed in to the'
    Say 'Cloudflare account that owns the Workers, and it sets both of them from the same key.'
} else {
    Say 'telling the Workers the same GPU connect key...'
    $poolOk = Push-Secret 'GPU connect key on the pool' 'JOIN_KEY' @('--config', 'wrangler.pool.jsonc')
    $siteOk = Push-Secret 'GPU connect key on the site' 'POOL_JOIN_KEY' @()
    if ($poolOk -and $siteOk) {
        Say ''
        Say 'this PC, the pool and the site now hold the same GPU connect key. Nothing else can'
        Say 'disagree with it: a card is refused only when one of the three was left behind, and'
        Say 'running this again is what puts them back in step.'
    } else {
        Say ''
        Say 'The key is written here but not on every Worker, so a card can still be refused until'
        Say 'it is. Run this again on the PC signed in to the Cloudflare account that owns them, or'
        Say 'set both to this key: JOIN_KEY on the pool Worker, POOL_JOIN_KEY on the site.'
    }
}

# --- and the header has to name this PC ----------------------------------
# "AI ready · CUDA · this PC" is the address the site publishes; every device reads it
# from GET /api/song/endpoint. The site's own KV runs out of writes daily, so the address
# goes where writes still land - beside the pool's clocks - and the site reads it there.
# This names this machine as the model. It is not a pool join: nothing is added to the
# list of GPUs that take work, which is the pool badge's business, not the header's.
$tunnelPath = Join-Path $App 'build\tunnel_url.txt'
# An address typed here wins over the tunnel this PC last wrote - it is the one the person
# chose. But a tunnel name dies when its session ends, and the app writes a new one when it
# starts again, so a typed address that no longer answers is stepped over for the one the
# app recorded: naming a dead host is worse than naming the live one beside it.
$candidates = @()
foreach ($candidate in @($typedUrlPath, $tunnelPath)) {
    if (-not (Test-Path $candidate)) { continue }
    $v = ((Get-Content -Raw -Path $candidate) -as [string]).Trim().TrimEnd('/')
    if ($v -match '^https?://' -and ($candidates -notcontains $v)) { $candidates += $v }
}
$tunnel = ''
if (-not $candidates.Count) {
    Say ''
    Say 'the header was not pointed at this PC: no tunnel address yet (build\tunnel_url.txt).'
    Say 'start the model once, then run this again.'
} else {
    Say ''
    Say 'pointing the site at this PC for "AI ready - this PC"...'
    # Only an address that answers is worth naming: build\tunnel_url.txt keeps the last
    # tunnel the app opened, and one left over from a session that ended resolves to
    # nothing. Nagging the header with it is worse than leaving it on the pool.
    foreach ($c in $candidates) {
        $alive = $false
        try {
            $probe = Invoke-WebRequest -UseBasicParsing -Method Get -Uri ($c + '/health') -TimeoutSec 6
            $alive = ($probe.StatusCode -eq 200)
        } catch { $alive = $false }
        if ($alive) { $tunnel = $c; break }
        Say "  $c did not answer, so it is not named."
    }
    if (-not $tunnel) {
        Say '  not pointed: none of those addresses answered, so the header keeps what it uses now.'
        Say '  the key was still written.'
    }
    if ($tunnel) {
        function Name-ThisPc([string]$address) {
            try {
                Invoke-RestMethod -Method Post `
                    -Uri 'https://song-pool.bpiersing.workers.dev/state/site:endpoint' `
                    -Headers @{ 'x-join-key' = $Key } -ContentType 'application/json' `
                    -Body (@{ value = $address } | ConvertTo-Json -Compress) -TimeoutSec 25 | Out-Null
                return 0
            } catch {
                try { return [int]$_.Exception.Response.StatusCode } catch { return -1 }
            }
        }
        $status = Name-ThisPc $tunnel
        # A 403 here is never about the address: the address answered a moment ago. It is
        # the key - this PC holds one the Worker does not. The key is right here, so the
        # Workers are told it once and the name is tried again: that is the repair, and it
        # is why typing a new key no longer ends in an error.
        if ($status -eq 403 -and $canSync) {
            Say '  the pool holds a different key, so the Workers are being told this one...'
            $retryPool = Push-Secret 'GPU connect key on the pool' 'JOIN_KEY' @('--config', 'wrangler.pool.jsonc')
            $retrySite = Push-Secret 'GPU connect key on the site' 'POOL_JOIN_KEY' @()
            if ($retryPool -and $retrySite) { $status = Name-ThisPc $tunnel }
        }
        if ($status -eq 0) {
            Say "  $tunnel"
            Say '  named as the model, so the header says this PC and not the pool.'
        } elseif ($status -eq 403) {
            Say '  not pointed: the pool still holds a different GPU connect key than this PC.'
            Say '  Run this once on the PC signed in to the Cloudflare account that owns the'
            Say '  Workers: it sets JOIN_KEY on the pool and POOL_JOIN_KEY on the site to this key.'
        } else {
            Say "  not pointed: the site did not accept the address (status $status)."
        }
    }
}

Say ''
Say 'the song key (SONG_ENDPOINT_KEY) was not touched.'
Say 'done. restart the song service so it reads the new key: it keeps the one it started with.'
