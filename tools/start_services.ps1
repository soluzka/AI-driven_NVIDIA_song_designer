<#
    Start everything the website needs on this computer, and do nothing if it is
    already running.

    Three processes, and the page finds them on its own:

      5001  app.py               the site itself: Flask, Socket.IO and the
                                 SQLite index
      8080  downloader_service   Flask + yt-dlp, no GPU, about 60 MB
      8090  song_service         ACE-Step on CUDA, warmed up so the first
                                 composed song does not also pay for the load

    Safe to run repeatedly: each service is probed first and only started when it
    is not answering. Run it at logon and the whole thing comes back after a
    reboot with no one clicking anything.

    A Quick Tunnel can be started too (-NoTunnel skips it), giving the model an
    https address from outside the home network; the address is published to the
    site so the page finds it without anyone pasting a new one.

    -Watch stays running afterwards and checks all three ports every two minutes,
    starting a service again if it has gone away. A logon entry only covers a
    reboot; this covers the machine staying on while a service dies, which is
    what happened on 8 Nov 2026 - the crash took both services and nothing was
    left to bring them back.

    Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\start_services.ps1
            powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File tools\start_services.ps1 -Watch
#>

[CmdletBinding()]
param(
    [int]$WebPort = 5001,
    [int]$DownloadPort = 8080,
    [int]$SongPort = 8090,
    [switch]$NoWarm,
    [switch]$NoTunnel,
    [switch]$Quiet,
    [switch]$Watch,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# One environment for every role that needs the card. install_all.ps1 fills .venv
# with the site, the downloader, the song service and the voice service's chatterbox
# and leaves this marker; when it is there those roles run from it instead of
# building a venv each. chatterbox's declared pins (numpy 1.x, torch 2.6) are
# metadata only: it loads and sings on the numpy 2.4.6 and torch 2.14.1 here.
$sharedPython = Join-Path $root '.venv\Scripts\python.exe'
$useShared = (Test-Path (Join-Path $root '.venv\all_services.json')) -and (Test-Path $sharedPython)

# A service that is already answering keeps the code it loaded at start-up, so a
# reinstall only takes effect after a restart. install_all.ps1 passes -Restart for
# exactly that reason: without it a corrected package sits in .venv while the old
# process keeps serving, which is how the py3langid fix looked like it had never
# installed. Only processes this launcher would have started are touched.
if ($Restart) {
    foreach ($role in @('site', 'downloader', 'song', 'voice')) {
        $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -like "*--role $role*" })
        foreach ($p in $procs) {
            try {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                if (-not $Quiet) { Say "stopped $role (pid $($p.ProcessId)) so the installed code loads" }
            } catch {
                if (-not $Quiet) {
                    Say "could not stop $role (pid $($p.ProcessId)): it belongs to another user"
                    Say '  log off that user, or stop it there, then rerun this launcher'
                }
            }
        }
    }
    # A socket frees as soon as the process goes; the card needs a moment to drop
    # the model before the next process claims it.
    Start-Sleep -Seconds 5
    # Command lines are hidden for processes owned by another account, so a role can
    # survive the loop above without ever appearing in it. A port that still answers
    # is that case, and saying so is the difference between "updated" and "updated,
    # but the old code is what is running".
    foreach ($check in @(@{ port = $WebPort; role = 'site'; path = '/' },
                         @{ port = $DownloadPort; role = 'downloader'; path = '/health' },
                         @{ port = $SongPort; role = 'song'; path = '/health' },
                         @{ port = 8095; role = 'voice'; path = '/health' })) {
        $code = & curl.exe -s -o NUL -w '%{http_code}' --max-time 20 "http://127.0.0.1:$($check.port)$($check.path)"
        if ($code -eq '200') {
            $owner = (Get-NetTCPConnection -LocalPort $check.port -State Listen -ErrorAction SilentlyContinue |
                      Select-Object -First 1).OwningProcess
            if (-not $Quiet) {
                Say "$($check.role) is still answering on $($check.port) (pid $owner): it is running the old code"
                Say '  stop that process, or log off and back on, for the new packages to load'
            }
        }
    }
}

# A logon run has no console to read afterwards, so everything this run prints
# is also written to a file: "an error flashed up" is not diagnosable. A second
# run cannot take the same transcript, so failure here is not fatal.
try {
    Start-Transcript -Path (Join-Path $root 'build\launcher_run.log') -Append | Out-Null
} catch { }

# A line per run, so "the site started it" can be checked instead of assumed.
try {
    Add-Content -Path (Join-Path $root 'build\launcher_trace.txt') `
        -Value ('{0:yyyy-MM-dd HH:mm:ss} launcher ran' -f (Get-Date))
} catch { }

function Say([string]$text) {
    if (-not $Quiet) { Write-Host $text }
}

# Keep songsvc:// pointing at this copy of the launcher, every run. A moved
# folder, a restored machine or a first start all end the same way: the site's
# Start the AI button works without anyone running another script. Registering
# twice is harmless, so it simply checks and repairs.
function Ensure-Protocol {
    $launcher = Join-Path $root 'tools\start_services.ps1'
    if (-not (Test-Path $launcher)) { return }
    $wanted = 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launcher + '"'
    $base = 'HKCU:\Software\Classes\songsvc'
    try {
        $current = (Get-ItemProperty -Path "$base\shell\open\command" -ErrorAction SilentlyContinue).'(default)'
        if ($current -eq $wanted) { return }
        New-Item -Path $base -Force | Out-Null
        Set-ItemProperty -Path $base -Name '(default)' -Value 'URL:Song services launcher'
        New-ItemProperty -Path $base -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
        New-Item -Path "$base\shell\open\command" -Force | Out-Null
        Set-ItemProperty -Path "$base\shell\open\command" -Name '(default)' -Value $wanted
        Say 'registered songsvc:// for the Start the AI button'
    } catch {
        Say "could not register songsvc://: $($_.Exception.Message)"
    }
}

Ensure-Protocol

function Trace([string]$text) {
    try {
        Add-Content -Path (Join-Path $root 'build\launcher_trace.txt') `
            -Value ('{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $text)
    } catch { }
}

# The services answer /health; the site answers / with its page, so the path the
# probe asks for is a parameter rather than assumed.
function Answers([int]$port, [string]$path = '/health') {
    # Twenty seconds, not three. While the model is being loaded onto the card the
    # machine is busy enough that a three-second probe times out, and calling a
    # healthy-but-loading service "down" is what made the watchdog start a rival
    # copy at 21:32 on 13 Nov: the new one could not bind, the warm restarted, and
    # the AI took another five minutes to appear.
    $code = & curl.exe -s -o NUL -w '%{http_code}' --max-time 20 "http://127.0.0.1:$port$path"
    return ($code -eq '200')
}

# Health only answers once Flask is listening, which for the song service is
# after torch imports - about 40 s. Probing alone therefore says "not running"
# during exactly the window a second click lands in, and the second click used
# to start a second copy that then failed to bind 8090. Asking whether the
# process exists covers that window: one launcher run, one service.
#
# The marker has to be an argument, not a path. A venv's python.exe re-execs the
# interpreter named in its pyvenv.cfg, so the process that ends up owning the
# port reports the base uv python on its command line - no "song_service" and no
# ".venv" in it at all. Matching the folder therefore found nothing, every click
# started another copy, and each new copy spent forty seconds importing torch
# before failing to bind. "--role song" survives the re-exec and cannot be
# confused with the other two, which are all also called app.py.
function Starting([string]$marker) {
    $found = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$marker*" }
    return [bool]$found
}

# Nothing here waits for a model. Importing torch and warming 3.5B parameters
# takes a minute or two, and a launcher that sits through it looks hung: the
# process is started and this returns, health is checked separately.

# --- downloader ----------------------------------------------------------
if (Answers $DownloadPort) {
    Say "downloader already on 127.0.0.1:$DownloadPort"
    } elseif (Starting '*--role downloader*') {
    Say 'downloader is still starting, leaving it alone'
} else {
    $service = Join-Path $root 'downloader_service'
    # install_all.ps1's one environment, when it has been run; otherwise the
    # service's own venv is created below as before.
    $python = if ($useShared) { $sharedPython } else { Join-Path $service '.venv\Scripts\python.exe' }
    if (-not (Test-Path $python)) {
        Say 'downloader: creating its environment (a minute or two)'
        $venvDir = Join-Path $service '.venv'
        $venvLog = Join-Path $root 'build\downloader_venv.log'
        # `python` on PATH on this machine is the Microsoft Store stub, which only
        # prints "Python was not found". Building the venv by its bare name
        # therefore failed silently and left a service that never started, so try
        # each interpreter that is actually here. `uv venv --seed` matters: without
        # --seed there is no pip in the venv and the install below cannot run.
        #
        # The preference is relaxed around these calls on purpose: uv and py write
        # progress to stderr, and under this script's Stop preference a native
        # command's stderr becomes a terminating error that aborted the whole
        # launcher on the first try - before the site or the tunnel were reached.
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            if (Get-Command uv -ErrorAction SilentlyContinue) {
                & uv venv $venvDir --python 3.11 --seed *> $venvLog
            }
            if (-not (Test-Path $python) -and (Get-Command py -ErrorAction SilentlyContinue)) {
                & py -3.11 -m venv $venvDir *>> $venvLog
            }
            if (-not (Test-Path $python)) {
                & python -m venv $venvDir *>> $venvLog
            }
        } catch {
            Say "downloader: building its environment failed: $($_.Exception.Message)"
        } finally {
            $ErrorActionPreference = $previous
        }
        if (-not (Test-Path $python)) {
            Say "downloader: no Python could build its environment (see build\downloader_venv.log)"
        }
    }
    if (-not (Test-Path $python)) {
        Say 'downloader: not started for that reason'
    } else {
        if (-not (Get-ChildItem (Join-Path $service '.venv\Lib\site-packages') -Filter 'flask*' -ErrorAction SilentlyContinue)) {
            Say 'downloader: installing its packages'
            $previous = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                & $python -m pip install -q --disable-pip-version-check -r (Join-Path $service 'requirements.txt') *>> $venvLog
            } catch {
                Say "downloader: installing its packages failed: $($_.Exception.Message)"
            } finally {
                $ErrorActionPreference = $previous
            }
        }
        $env:PORT = "$DownloadPort"
        Start-Process -FilePath $python -ArgumentList 'app.py', '--role', 'downloader' -WorkingDirectory $service `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $root 'build\downloader_out.log') `
            -RedirectStandardError (Join-Path $root 'build\downloader_err.log') | Out-Null
        Say "downloader starting on 127.0.0.1:$DownloadPort (log: build\downloader_err.log)"
    }
}

# --- song service, on CUDA ----------------------------------------------
if (Answers $SongPort) {
    Say "song service already on 127.0.0.1:$SongPort"
    } elseif (Starting '*--role song*') {
    Say 'song service is still starting (torch import takes about 40 s), leaving it alone'
} else {
    $service = Join-Path $root 'song_service'
    # install_all.ps1's one environment, when it has been run; otherwise the
    # service's own venv is created below as before.
    $python = if ($useShared) { $sharedPython } else { Join-Path $service '.venv\Scripts\python.exe' }
    if (-not (Test-Path $python)) {
        Say 'song service: run song_service\install.py first (it installs the CUDA torch)'
        exit 1
    }

    # SONG_WARM puts the 3.5B model on the card now, in the background, instead
    # of on the first compose. LYRICS_DEVICE is left unset so app.py picks cuda
    # when torch can see a card and cpu when it cannot.
    $env:SONG_PORT = "$SongPort"
    $env:SONG_WARM = if ($NoWarm) { '0' } else { '1' }
    $songProc = Start-Process -FilePath $python -ArgumentList 'app.py', '--role', 'song' -WorkingDirectory $service `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $service 'logs\service_out.log') `
        -RedirectStandardError (Join-Path $service 'logs\service_err.log') -PassThru
    # Above normal: the first five minutes after a boot are a race between torch
    # importing, the 3.5B weights being paged onto the card and the site fitting
    # its own models over 12 000 videos. Without this the warm is what loses that
    # race, and the model takes several minutes longer to be usable.
    try { $songProc.PriorityClass = 'AboveNormal' } catch { }
    Say "song service starting on 127.0.0.1:$SongPort, warming CUDA (log: song_service\logs\service_out.log)"
}

# --- the voice service, on the same card ---------------------------------
# The cloning model that sings the AI's lyrics in the recorded voice. Started here
# so "Sing the lyrics" works without a second terminal; without it the song service
# still renders backing tracks, it just has no singer to lay over them.
$voicePort = 8095
if (Answers $voicePort) {
    Say "voice service already on 127.0.0.1:$voicePort"
} elseif (Starting '*--role voice*') {
    Say 'voice service is still starting, leaving it alone'
} else {
    $voice = Join-Path $root 'voice_service'
    # Its own venv if someone built one, otherwise the shared environment, which
    # install_all.ps1 now fills with chatterbox: no second Python, no Python
    # installed by hand, and the service comes up like the other three.
    $voicePython = Join-Path $voice '.venv\Scripts\python.exe'
    if (-not (Test-Path $voicePython)) { $voicePython = $sharedPython }
    $voiceLogs = Join-Path $voice 'logs'
    if (-not (Test-Path $voiceLogs)) { New-Item -ItemType Directory -Path $voiceLogs -Force | Out-Null }
    if ((Test-Path $voicePython) -and (Test-Path (Join-Path $voice 'app.py'))) {
        $env:VOICE_PORT = "$voicePort"
        Start-Process -FilePath $voicePython -ArgumentList 'app.py', '--role', 'voice' -WorkingDirectory $voice `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $voice 'logs\service_out.log') `
            -RedirectStandardError (Join-Path $voice 'logs\service_err.log') | Out-Null
        Say "voice service starting on 127.0.0.1:$voicePort (log: voice_service\logs\service_err.log)"
    } else {
        Say 'voice service not installed yet (no shared environment: run install_all.ps1)'
    }
}

# --- the site -------------------------------------------------------------
# app.py is the page itself, and it is the one process whose absence is noticed
# first, so it is started here with the two AI services rather than by hand.
# The marker is this root's venv python: every service is called app.py, so the
# name alone would match all three.
$sitePython = Join-Path $root '.venv\Scripts\python.exe'
$siteMarker = '*--role site*'

if (Answers $WebPort '/') {
    Say "site already on 127.0.0.1:$WebPort"
} elseif (Starting $siteMarker) {
    Say 'site is still starting, leaving it alone'
} elseif (-not (Test-Path $sitePython)) {
    Say "site: $sitePython is missing (create it with python -m venv .venv)"
} else {
    $env:PORT = "$WebPort"
    Start-Process -FilePath $sitePython -ArgumentList 'app.py', '--role', 'site' -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $root 'run_web.log') `
        -RedirectStandardError (Join-Path $root 'run_web.err') | Out-Null
    Say "site starting on 127.0.0.1:$WebPort (log: run_web.log)"
}

# --- the tunnel -------------------------------------------------------------
# A Quick Tunnel gives the model an https address the internet can reach, which
# is what a phone on mobile data needs. Its name changes on every start, so the
# address is written to build\tunnel_url.txt and published to the site: the page
# reads it from there, and nobody pastes a new one into each device.
$cloudflared = Join-Path $root 'tools\bin\cloudflared.exe'
$tunnelLog = Join-Path $root 'tunnel.log'
$siteUrl = 'https://browser.bpiersing.workers.dev'

# A fresh clone has no cloudflared.exe: it is a 55 MB binary and gitignored, so
# "clone, run, the phone works" used to need a manual download first. Fetch it
# once, at a pinned version, and only run it if Windows calls the signature valid
# and names Cloudflare - a download straight into an executed path is not
# something to do on trust. "latest" is deliberately not used: that is a moving
# target nobody reviewing this script can check.
$cloudflaredVersion = '2026.10.7'
$cloudflaredUrl = "https://github.com/cloudflare/cloudflared/releases/download/$cloudflaredVersion/cloudflared-windows-amd64.exe"

function Get-Cloudflared([string]$target) {
    $partial = "$target.download"
    try {
        Say "tunnel: cloudflared is missing, fetching $cloudflaredVersion (~55 MB, once)"
        # Progress off: this runs under a hidden window at logon, and the progress
        # bar makes Invoke-WebRequest several times slower for no benefit.
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $cloudflaredUrl -OutFile $partial -UseBasicParsing -TimeoutSec 300

        $signature = Get-AuthenticodeSignature -FilePath $partial
        $signer = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { '' }
        if ($signature.Status -ne 'Valid' -or $signer -notmatch 'Cloudflare') {
            Say "tunnel: the download failed its signature check (status $($signature.Status)), so it was discarded"
            Remove-Item $partial -ErrorAction SilentlyContinue
            return $false
        }

        Move-Item -Path $partial -Destination $target -Force
        Say "tunnel: cloudflared $cloudflaredVersion in place"
        return $true
    } catch {
        Say "tunnel: could not fetch cloudflared ($($_.Exception.Message)); the model stays on this network"
        Remove-Item $partial -ErrorAction SilentlyContinue
        return $false
    }
}

if ($NoTunnel) {
    Say 'tunnel: skipped (-NoTunnel)'
} elseif (-not (Test-Path $cloudflared) -and -not (Get-Cloudflared $cloudflared)) {
    Say 'tunnel: cloudflared is missing, so the model stays on this network'
} else {
    $live = @(Get-Process cloudflared -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $cloudflared })
    if ($live.Count -gt 0) {
        Say 'tunnel: already running'
    } else {
        Remove-Item $tunnelLog -ErrorAction SilentlyContinue
        Start-Process -FilePath $cloudflared -WorkingDirectory $root -ArgumentList @(
            'tunnel', '--config', 'tools\bin\quick-tunnel.yml',
            '--url', 'http://127.0.0.1:8090', '--no-autoupdate',
            '--logfile', 'tunnel.log', '--loglevel', 'info'
        ) -WindowStyle Hidden | Out-Null
        Say 'tunnel: starting'
    }

    # The address reaches the log either way: a fresh tunnel writes it as it
    # connects, and one already up wrote it when it started.
    $tunnelUrl = ''
    for ($t = 0; $t -lt 20; $t++) {
        if (Test-Path $tunnelLog) {
            $tunnelUrl = (Select-String -Path $tunnelLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' |
                ForEach-Object { $_.Matches[0].Value } | Select-Object -Last 1)
            if ($tunnelUrl) { break }
        }
        Start-Sleep -Seconds 2
    }

    if (-not $tunnelUrl) {
        Say 'tunnel: no address yet, see tunnel.log'
    } else {
        try { Set-Content -Path (Join-Path $root 'build\tunnel_url.txt') -Value $tunnelUrl -Encoding ascii } catch { }
        try { Set-Clipboard -Value $tunnelUrl } catch { }
        Say "tunnel: $tunnelUrl"

        $noPoolFile = Join-Path $root 'build\no_pool.txt'
        $keyFile = Join-Path $root 'build\song_endpoint.key'
        if (Test-Path $noPoolFile) {
            # A marker this install can set so it is not added to the pool on every
            # start. The pool is what the site renders on when no address has been
            # pasted, so a machine that joins itself is a machine nobody chose - and
            # it made the site say "AI ready" to someone who had connected nothing.
            Say 'tunnel: not published (build\no_pool.txt is set); paste the address above with Connect my model'
        } elseif (Test-Path $keyFile) {
            $key = (Get-Content $keyFile -Raw).Trim()
            if ($key) {
                # The JSON goes through a file: curl.exe on Windows mangles quotes
                # that arrive inside an argument, and the site answered "expected
                # JSON" while the address looked perfectly fine on screen.
                $payload = Join-Path $root 'build\tunnel_publish.json'
                try {
                    Set-Content -Path $payload -Encoding ascii `
                        -Value ('{"url":"' + $tunnelUrl + '"}')
                    $answer = & curl.exe -s --max-time 15 -X POST "$siteUrl/api/song/endpoint" `
                        -H 'Content-Type: application/json' -H "x-song-key: $key" `
                        --data-binary "@$payload"
                    Say "tunnel: published to the site: $answer"

                    # The pool has its own Worker too, and it is asked directly as
                    # well: if the site side is ever down, this machine is still in
                    # the list the site reads its GPU addresses from.
                    $poolUrl = 'https://song-pool.bpiersing.workers.dev/join'
                    $poolAnswer = & curl.exe -s --max-time 15 -X POST $poolUrl `
                        -H 'Content-Type: application/json' -H "x-join-key: $key" `
                        --data-binary "@$payload"
                    Say "tunnel: joined the pool: $poolAnswer"
                } finally {
                    Remove-Item $payload -ErrorAction SilentlyContinue
                }
            }
        } else {
            Say 'tunnel: build\song_endpoint.key is missing, so the site was not told the address'
        }
    }
}

$songHealth = & curl.exe -s --max-time 20 "http://127.0.0.1:$SongPort/health"
if ($songHealth) { Say "song now: $songHealth" } else { Say 'song: still loading, check again in a minute' }

# --- watchdog ------------------------------------------------------------
# Only one watchdog should exist: two would fight over the same 8090 and each
# would restart a service the other had just started.
function Watchers {
    Get-CimInstance Win32_Process -Filter "Name like '%powershell%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*start_services.ps1*' -and $_.CommandLine -like '*-Watch*' -and $_.ProcessId -ne $PID }
}

if ($Watch) {
    $others = @(Watchers)
    if ($others.Count -gt 0) {
        Trace "watchdog already running (pid $($others[0].ProcessId)), not adding another"
        Say 'watchdog already running'
        return
    }

    Trace 'watchdog started'
    Say 'watching all four services every 2 minutes'

    while ($true) {
        Start-Sleep -Seconds 120

        # A service that is answering needs nothing. One whose process exists but
        # is not answering is still importing torch or waiting on the model, so
        # it is left alone; only a port with no process behind it is restarted.
        $down = @()
        if (-not (Answers $WebPort '/') -and -not (Starting $siteMarker)) { $down += 'site' }
        if (-not $NoTunnel -and -not @(Get-Process cloudflared -ErrorAction SilentlyContinue).Count) { $down += 'tunnel' }
        if (-not (Answers $DownloadPort) -and -not (Starting '*--role downloader*')) { $down += 'downloader' }
        if (-not (Answers $SongPort) -and -not (Starting '*--role song*')) { $down += 'song service' }
        # The voice service is part of the install, so it is watched too: it is the
        # one that loads torch on first use and can die without saying anything.
        if (-not (Answers 8095) -and -not (Starting '*--role voice*')) { $down += 'voice service' }

        if ($down.Count -gt 0) {
            $which = $down -join ' and '
            Trace "watchdog: $which down, restarting"
            Say "$which down, restarting"
            Start-Process -FilePath 'powershell' -WindowStyle Hidden -ArgumentList @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Quiet'
            ) | Out-Null
        }
    }
}
