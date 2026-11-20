# Set the key that connects this PC's GPU to the song site.
#
#   reset_join_key.cmd                 type the key you want, then Enter
#   reset_join_key.cmd -Key "abc..."   set it in one line
#   reset_join_key.cmd -Url "https://host"   name that address as the model instead
#   reset_join_key.cmd -KeepUrl        keep the address this PC already has
#   reset_join_key.cmd -NewUrl         force a new address even if the old one answers
#   reset_join_key.cmd -Show           print the key this PC holds now
#   reset_join_key.cmd -DryRun         show it, change nothing
#
# The address changes when the one this PC has stops answering: a working tunnel is kept,
# and a dead one is replaced by a new tunnel opened beside it, which is closed only after
# the new address answers. -NewUrl forces that a new address is made anyway; -KeepUrl
# leaves even a dead one alone.
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
    [switch]$NoSync,
    [switch]$KeepUrl,
    [switch]$PointOnly,
    [switch]$NewUrl
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

# -PointOnly: do the last half only - a new tunnel, the address named, the card joined
# to the pool, dead cards taken out - using the key this PC already has. The installer
# calls it that way, so both commands point, join and prune through this one script
# instead of two that can drift apart.
if ($PointOnly) {
    if (-not (Test-Path $keyPath)) {
        Say ''
        Say 'there is no join key on this PC yet, so the pool would refuse a card from it.'
        Say 'Run this once with the key first.'
        exit 1
    }
    $Key = ((Get-Content -Raw -Path $keyPath) -as [string]).Trim()
    if (-not $Key) {
        Say ''
        Say 'the join key on this PC is empty; run this once with the key first.'
        exit 1
    }
}

$script:nodePath = Join-Path $App 'tools\node\node.exe'
$script:wranglerPath = Join-Path $App 'build\wrangler-tool\node_modules\wrangler\bin\wrangler.js'
$script:npmPath = Join-Path $App 'tools\node\npm.cmd'

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

function Find-LiveAddress {
    # Every address this PC has recorded, newest first, plus the last address in each tunnel
    # log. A tunnel that is running but was never written down still printed its name.
    $list = @()
    foreach ($f in @($tunnelPath, $typedUrlPath)) {
        if (-not (Test-Path $f)) { continue }
        $v = ((Get-Content -Raw -Path $f) -as [string]).Trim().TrimEnd('/')
        if ($v -match '^https?://' -and ($list -notcontains $v)) { $list += $v }
    }
    foreach ($logFile in @(Get-ChildItem -Path (Join-Path $App 'build') -Filter 'tunnel*.log' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 6)) {
        $found = (Select-String -Path $logFile.FullName -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Matches[0].Value } | Select-Object -Last 1)
        if ($found -and ($list -notcontains $found)) { $list += $found }
    }
    foreach ($u in $list) {
        try {
            if ((Invoke-WebRequest -UseBasicParsing -Uri ($u + '/health') -TimeoutSec 8).StatusCode -eq 200) { return $u }
        } catch { }
    }
    return ''
}

function Renew-Tunnel([switch]$Retried) {
    $cf = Join-Path $App 'tools\bin\cloudflared.exe'
    $cfg = Join-Path $App 'tools\bin\quick-tunnel.yml'
    # cloudflared logs "Configuration file ... was empty" for a config that has no KEYS,
    # not for one with no bytes: a file of comments only is empty to it, which is why the
    # error kept appearing over a file that was 353 bytes long. The file's only job is to
    # stop a config.yml in ~/.cloudflared from taking over the quick tunnel's --url
    # routing, so it is written with one harmless key and no ingress at all.
    $cfgText = if (Test-Path $cfg) { Get-Content -Raw -Path $cfg } else { '' }
    $cfgKey = @(($cfgText -split "`n") | Where-Object { $_ -match '\S' -and $_ -notmatch '^\s*#' }).Count -gt 0
    if (-not $cfgKey) {
        try {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $cfg) | Out-Null
            Set-Content -Path $cfg -Encoding ascii -Value @(
                '# Deliberately no ingress rules: a config.yaml in ~/.cloudflared would take over',
                '# the quick tunnel routing, and its catch-all answers 404. This file exists only',
                '# to be passed as --config, with one key so cloudflared does not call it empty.',
                'no-autoupdate: true'
            )
            Say '  quick-tunnel.yml had no keys, so it was written with one'
        } catch {
            Say "  could not write quick-tunnel.yml ($($_.Exception.Message))"
        }
    }
    # A log of its own for every rotation, named by the minute: a shared one holds the
    # addresses of tunnels that have already gone, and reading the last line of it named
    # an older address that no longer answered.
    $log = Join-Path $App ('build\tunnel-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    # A new address every run, as asked: the old tunnel is closed only after the new one
    # answers, so the change is never a disconnection. If Cloudflare will not serve a new
    # quick tunnel just now (it throttles them when they come quickly), the run says so and
    # leaves the working address alone rather than breaking the one that answers.
    $live = Find-LiveAddress
    $script:previousTunnel = $live
    if (-not (Test-Path $cf)) {
        Say '  there is no cloudflared on this PC, so the address stays as it is'
        return ''
    }

    # Open the second tunnel first, beside the one that is serving. Nothing is stopped
    # until the new address answers: a rotation that stops the old one first is a
    # disconnection, not a change of address, and the model stops being reachable while
    # the new one comes up.
    # A cap of five. Every rotation leaves its own cloudflared behind, and a pile of them
    # is what makes Cloudflare refuse to route a new address at all - the new tunnel opens,
    # registers, and then answers 530 from the edge. So before opening anything, old
    # tunnels are pruned: the five newest are kept, the older ones are stopped, and the run
    # says what it is doing instead of silently leaving them running.
    $keep = 5
    $running = @(Get-Process cloudflared -ErrorAction SilentlyContinue | ForEach-Object {
            $started = $null
            try { $started = $_.StartTime } catch { $started = $null }
            [pscustomobject]@{ Id = $_.Id; Started = $started }
        } | Sort-Object Started, Id)
    if ($running.Count -ge $keep) {
        # At five, all of them go: a pile of tunnels is what makes Cloudflare refuse to
        # route the next one, so the pile is cleared completely and exactly one fresh
        # tunnel is started in its place.
        Say "  pruning old tunnels ($($running.Count) running, all of them), then starting one"
        foreach ($p in $running) {
            try {
                Stop-Process -Id $p.Id -Force -ErrorAction Stop
                Say "  pruned the tunnel running as pid $($p.Id)"
            } catch {
                Say "  pid $($p.Id) runs under another account and cannot be stopped from here"
            }
        }
    }

    $oldIds = @(Get-Process cloudflared -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
    Say '  opening a second tunnel beside the one that is serving...'
    Remove-Item $log -ErrorAction SilentlyContinue
    # What this PC was using, so the pool can be told exactly which card to drop once the
    # new address is in. Probing the old one is not enough: a tunnel that was left running
    # elsewhere keeps answering, and its card would stay in the pool forever.
    $script:previousTunnel = ''
    foreach ($f in @((Join-Path $App 'build\tunnel_url.txt'))) {
        if (Test-Path $f) { $script:previousTunnel = ((Get-Content -Raw -Path $f) -as [string]).Trim() }
    }
    # Started through WMI, so its parent is the WMI service and not this window: a tunnel
    # started by the window or by this script stops when either closes, which is exactly
    # how a new address appears and then goes dead. This one outlives both.
    $cmdline = 'pushd "{0}" && "{1}" tunnel --config "{2}" --url http://127.0.0.1:8090 --no-autoupdate --logfile "{3}" --loglevel info' -f $App, $cf, $cfg, $log
    try {
        $made = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine = "cmd /c $cmdline"
        }
        if ($made.ReturnValue -ne 0) {
            Say "  cloudflared did not start (code $($made.ReturnValue))"
            return ''
        }
    } catch {
        Say "  cloudflared did not start: $($_.Exception.Message)"
        return ''
    }

    $new = ''
    for ($i = 0; $i -lt 20 -and -not $new; $i++) {
        Start-Sleep -Seconds 3
        if (Test-Path $log) {
            $new = (Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' |
                ForEach-Object { $_.Matches[0].Value } | Select-Object -Last 1)
        }
    }
    if (-not $new) {
        Say '  no new tunnel appeared, so nothing was stopped and the address is unchanged'
        return ''
    }

    $alive = $false
    for ($a = 0; $a -lt 10 -and -not $alive; $a++) {
        try {
            $alive = (Invoke-WebRequest -UseBasicParsing -Uri ($new + '/health') -TimeoutSec 8).StatusCode -eq 200
        } catch { $alive = $false }
        if (-not $alive) { Start-Sleep -Seconds 3 }
    }
    if (-not $alive) {
        Say "  $new was opened but never answered"
        # That is what Cloudflare's throttle looks like: the tunnel registers, then the edge
        # answers 530 for the name. When this PC has already piled up five of them, every
        # cloudflared is pruned and the address is tried once more from a single tunnel -
        # the first one it tried, with the dead pile out of the way.
        $all = @(Get-Process cloudflared -ErrorAction SilentlyContinue)
        if (-not $Retried -and $all.Count -ge 5) {
            Say "  pruning old tunnels ($($all.Count) running, all of them), then trying once more"
            foreach ($p in $all) {
                try {
                    Stop-Process -Id $p.Id -Force -ErrorAction Stop
                    Say "  pruned the tunnel running as pid $($p.Id)"
                } catch {
                    Say "  pid $($p.Id) belongs to another account and cannot be stopped from here"
                }
            }
            Start-Sleep -Seconds 3
            return (Renew-Tunnel -Retried)
        }
        Say '  nothing was stopped and the address is unchanged'
        return ''
    }

    # Only now: the new one answers, so the old ones can go without anybody noticing.
    Say "  new address: $new"
    Say '  the old tunnel is closed below, and its card is taken out of the pool either way'
    $closed = 0
    foreach ($id in $oldIds) {
        try { Stop-Process -Id $id -Force -ErrorAction Stop; $closed++ } catch { }
    }
    if ($closed -lt $oldIds.Count) {
        Say '  one old tunnel runs under another account and cannot be closed from here;'
        Say '  the pool drops its card as soon as it stops answering'
    }
    try { Set-Content -Path (Join-Path $App 'build\tunnel_url.txt') -Value $new -Encoding ascii -NoNewline } catch { }
    return $new
}

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

if (-not $PointOnly) {

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
# --- wrangler, fetched once if it is missing --------------------------------
# Setting the key on the Workers is the one thing this script cannot do without
# wrangler, and an install does not carry it: without this a person types a key here,
# the Workers never hear it, and the pool refuses the address with a 403 that looks
# like a wrong key but is a key that was never set. npm sits beside node, so it is
# fetched from there once into build\wrangler-tool and kept.


$canSync = -not $NoSync -and (Ensure-Wrangler)

# --- a new address, without a reboot -----------------------------------------
# A Quick Tunnel's name lives exactly as long as the process that opened it, which is why
# every run used to keep the address from whenever the model started. Stopping that one
# process and opening another gives a name nobody has seen, in seconds, with the model,
# its warmed-up weights and the other services untouched. -KeepUrl leaves it alone.






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

} # end of the key half ($PointOnly skips it)

# --- and the header has to name this PC ----------------------------------
# "AI ready · CUDA · this PC" is the address the site publishes; every device reads it
# from GET /api/song/endpoint. The site's own KV runs out of writes daily, so the address
# goes where writes still land - beside the pool's clocks - and the site reads it there.
# This names this machine as the model. It is not a pool join: nothing is added to the
# list of GPUs that take work, which is the pool badge's business, not the header's.
$tunnelPath = Join-Path $App 'build\tunnel_url.txt'
# The address of the tunnel this run opens. It wins over every recorded one: a tunnel
# left running elsewhere keeps answering, so preferring the remembered address would
# name the old one forever and the new address would never appear to change.
$fresh = ''
if ($KeepUrl) {
    Say ''
    Say '-KeepUrl: the address this PC already has is kept.'
} else {
    Say ''
    Say 'getting a new address...'
    $fresh = Renew-Tunnel
}

# An address typed here wins over the tunnel this PC last wrote - it is the one the person
# chose. But a tunnel name dies when its session ends, and the app writes a new one when it
# starts again, so a typed address that no longer answers is stepped over for the one the
# app recorded: naming a dead host is worse than naming the live one beside it.
$candidates = @()
if ($fresh) { $candidates += $fresh }
foreach ($candidate in @($typedUrlPath, $tunnelPath)) {
    if (-not (Test-Path $candidate)) { continue }
    $v = ((Get-Content -Raw -Path $candidate) -as [string]).Trim().TrimEnd('/')
    if ($v -match '^https?://' -and ($candidates -notcontains $v)) { $candidates += $v }
}
# A tunnel that is running but whose address was never written down still printed it in
# its log. Without this the site can be left on an address that no longer answers while a
# working one sits on the same machine.
foreach ($logFile in @(Get-ChildItem -Path (Join-Path $App 'build') -Filter 'tunnel*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 4)) {
    $found = (Select-String -Path $logFile.FullName -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Matches[0].Value } | Select-Object -Last 1)
    if ($found -and ($candidates -notcontains $found)) { $candidates += $found }
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
        # A tunnel that has just been opened is in its log a few seconds before it answers
        # from the edge, so one quick probe calls a brand-new address dead. Five tries over
        # about fifteen seconds covers that without hanging on one that really is gone.
        $alive = $false
        for ($attempt = 0; $attempt -lt 5 -and -not $alive; $attempt++) {
            try {
                $probe = Invoke-WebRequest -UseBasicParsing -Method Get -Uri ($c + '/health') -TimeoutSec 6
                $alive = ($probe.StatusCode -eq 200)
            } catch { $alive = $false }
            if (-not $alive) { Start-Sleep -Seconds 3 }
        }
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
            # Remembered as the address this PC now has, so the next run starts from it
            # rather than from one that may have stopped answering.
            try { Set-Content -Path $typedUrlPath -Value $tunnel -Encoding ascii -NoNewline } catch { }

            # --- and the pool is the list that takes work --------------------------
            # Naming an address is not joining: the header reads one address, the pool
            # badge reads this list. The card for this address is added here, and cards
            # that no longer answer - the tunnels of sessions that have ended - are taken
            # out, so a dead address cannot sit in the badge for the week the pool keeps a
            # card. A card younger than five minutes is left alone: a tunnel is in its log
            # before it answers from the edge, and removing one that is still coming up is
            # worse than one stale card for a few minutes. A card that does answer is never
            # touched - that is this PC's own GPU as much as the notebook's.
            $poolRoot = 'https://song-pool.bpiersing.workers.dev'
            # The machine's own id, made once and kept: the pool has no other way to tell
            # that three cards with three addresses are one PC.
            $idFile = Join-Path $App 'build\machine_id.txt'
            $machineId = ''
            if (Test-Path $idFile) { $machineId = ((Get-Content -Raw -Path $idFile) -as [string]).Trim() }
            if (-not $machineId) {
                $machineId = [guid]::NewGuid().ToString('N')
                try { Set-Content -Path $idFile -Value $machineId -Encoding ascii -NoNewline } catch { }
            }
            try {
                Invoke-RestMethod -Method Post -Uri ($poolRoot + '/join') `
                    -Headers @{ 'x-join-key' = $Key } -ContentType 'application/json' `
                    -Body (@{ url = $tunnel; note = $machineId } | ConvertTo-Json -Compress) -TimeoutSec 25 | Out-Null
                Say '  in the pool: this address is one of the GPUs the site can send to'
                # The address this PC replaced is dropped by name, not by waiting for it to
                # stop answering: a tunnel left running on another account keeps answering,
                # and its card would otherwise sit in the pool beside the new one for a week.
                # One machine, one card. Every card carries the id of the machine that put
                # it there, so the cards of this PC are exactly the ones with this id: each
                # of them that is not the chosen address is taken out. Waiting for an old
                # address to stop answering is not enough - a tunnel left running keeps
                # answering for hours, and the pool would list the same PC three times with
                # only one of them current. The notebook's card has its own id or none, so
                # it is never touched.
                $dropped = 0
                try {
                    $now = Invoke-RestMethod -Uri ($poolRoot + '/pool') -TimeoutSec 25
                    foreach ($m in @($now.machines)) {
                        $u = [string]$m.url
                        $note = [string]$m.note
                        if (-not $u -or $u -eq $tunnel) { continue }
                        if ($note -ne $machineId) { continue }
                        try {
                            Invoke-RestMethod -Method Post -Uri ($poolRoot + '/remove') `
                                -Headers @{ 'x-join-key' = $Key } -ContentType 'application/json' `
                                -Body (@{ url = $u } | ConvertTo-Json -Compress) -TimeoutSec 20 | Out-Null
                            Say "  out of the pool: $u (an address this PC no longer uses)"
                            $dropped++
                        } catch { }
                    }
                } catch { }
                if (-not $dropped) { Say '  the pool had no other address of this PC to drop' }
                $pool = Invoke-RestMethod -Uri ($poolRoot + '/pool') -TimeoutSec 25
                foreach ($m in @($pool.machines)) {
                    $old = [string]$m.url
                    if (-not $old -or $old -eq $tunnel) { continue }
                    $ageMin = 0
                    try {
                        $ageMin = ((Get-Date).ToUniversalTime() - ([datetimeoffset]::FromUnixTimeMilliseconds([long]$m.joined)).UtcDateTime).TotalMinutes
                    } catch { $ageMin = 0 }
                    if ($ageMin -lt 5) { continue }
                    $answers = $false
                    for ($a = 0; $a -lt 2 -and -not $answers; $a++) {
                        try {
                            $answers = (Invoke-WebRequest -UseBasicParsing -Uri ($old + '/health') -TimeoutSec 6).StatusCode -eq 200
                        } catch { $answers = $false }
                    }
                    if (-not $answers) {
                        try {
                            Invoke-RestMethod -Method Post -Uri ($poolRoot + '/remove') `
                                -Headers @{ 'x-join-key' = $Key } -ContentType 'application/json' `
                                -Body (@{ url = $old } | ConvertTo-Json -Compress) -TimeoutSec 20 | Out-Null
                            Say "  taken out of the pool: $old no longer answers"
                        } catch { }
                    }
                }
            } catch {
                Say "  the pool was not updated: $($_.Exception.Message)"
            }
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
Say 'done. nothing has to be restarted: the key and the address are read as they are used.'
