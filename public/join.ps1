<#
.SYNOPSIS
    Joins this PC's NVIDIA card to the song site's GPU pool.

.DESCRIPTION
    Downloaded and run by join.cmd, so nobody types a command:

      1. checks the card
      2. fetches this application (a zip of the repository)
      3. installs it into one environment, with the CUDA wheels and the models -
         or, when this PC is already installed here, keeps that environment and
         the models and only restarts the services on the new files
      4. asks once for the join key and stores it where the launcher publishes from
      5. starts the services and registers the logon entry

    After that the PC is in the pool: the site picks it up on the next click, and a
    reboot rejoins by itself.

.PARAMETER Target
    Where to unpack the app. Defaults to %LOCALAPPDATA%\internet-gpu.

.PARAMETER Key
    The join key. Without it the script asks, hidden, once. The key is what lets a
    machine publish its address; it is not the model, and it is not an account.

.PARAMETER NoWindow
    Do not open the second console that follows the tunnel and the error logs.

.PARAMETER Source
    The app archive. Defaults to the public repository zip.
#>
[CmdletBinding()]
param(
    [string]$Target = (Join-Path $env:LOCALAPPDATA 'internet-gpu'),
    [string]$Key = '',
    [string]$Source = 'https://codeload.github.com/soluzka/AI-driven_NVIDIA_song_designer/zip/refs/heads/main',
    [switch]$Offline,
    [switch]$NoWindow
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

function Say([string]$text) { Write-Host $text }

# Names an address as the model endpoint - the one the page shows as "MODEL ENDPOINT"
# and hands to every device. Only a machine holding the join key may do it, and only
# an address that answers: a host left over from a session that ended resolves to
# nothing, and pointing the site at it leaves every device with a model that cannot
# render. Removed in cc749c6 together with the pasted-address handling below, which
# is why a pasted address then went into the key file and joined nothing.
function Set-ModelEndpoint([string]$address, [string]$appRoot) {
    $keyFile = Join-Path $appRoot 'build\song_endpoint.key'
    if (-not (Test-Path $keyFile)) {
        Say '       saved. There is no join key on this PC yet, so the site was not told.'
        Say '       Run reset_join_key.cmd with the key and it names this address.'
        return
    }
    $held = ((Get-Content -Raw -Path $keyFile) -as [string]).Trim()
    Say '       asking that address to answer ...'
    $alive = $false
    try {
        $probe = Invoke-WebRequest -UseBasicParsing -Method Get -Uri ($address + '/health') -TimeoutSec 8
        $alive = ($probe.StatusCode -eq 200)
    } catch { $alive = $false }
    if (-not $alive) {
        Say '       it did not answer /health, so the site was not pointed at it.'
        return
    }
    try {
        Invoke-RestMethod -Method Post `
            -Uri 'https://song-pool.bpiersing.workers.dev/state/site:endpoint' `
            -Headers @{ 'x-join-key' = $held } -ContentType 'application/json' `
            -Body (@{ value = $address } | ConvertTo-Json -Compress) -TimeoutSec 25 | Out-Null
        Say '       the site now names it as the model endpoint, for every device.'
    } catch {
        Say "       the site refused it: $($_.Exception.Message)"
    }
}

Say 'Joining this PC''s GPU to the song site'
Say '------------------------------------'

# --- 1. the card -------------------------------------------------------------
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $gpu = & $smi.Source --query-gpu=name,memory.total --format=csv,noheader 2>$null | Select-Object -First 1
    Say "gpu:   $gpu"
} else {
    Say 'gpu:   no nvidia-smi on PATH'
    $answer = Read-Host '       Continue anyway? The models will run on the CPU (hours per song) [y/N]'
    if ($answer -notmatch '^[Yy]') { Say 'stopped'; exit 1 }
}

# --- 2. the application ------------------------------------------------------
$appDir = Join-Path $Target 'AI-driven_NVIDIA_song_designer-main'
# Downloaded on every run, not only when the folder is missing. The old check meant a
# copy from an older commit kept reinstalling itself forever, so a corrected
# dependency pin never arrived and the same pip error came back on every attempt.
# -Offline uses whatever is already on disk.
if ($Offline) {
    Say "app:   -Offline: using the files already in $appDir, which are not updated by this run"
} else {
    Say "app:   fetching the current sources to $Target"
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    $zip = Join-Path $Target 'internet.zip'
    $staging = Join-Path $Target 'update-staging'
    $downloaded = $false
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Source -OutFile $zip
        if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
        Expand-Archive -Path $zip -DestinationPath $staging -Force
        Remove-Item $zip -ErrorAction SilentlyContinue
        $downloaded = $true
    } catch {
        Say "app:   could not fetch or unpack the sources ($($_.Exception.Message))"
        Remove-Item $zip -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
        # Falling back to the copy on disk looks exactly like an update that worked:
        # the folder keeps the files it already had and nothing says they are stale.
        # Stop instead; -Offline is the deliberate way to use what is already here.
        if (Test-Path (Join-Path $appDir 'install_all.ps1')) {
            Say '       the files already here were NOT updated - run it again for the current ones,'
            Say '       or pass -Offline on purpose to use this copy as it is'
        } else {
            Say '       nothing was downloaded and nothing is installed here yet'
            Say '       ask for the project folder and run install.cmd inside it'
        }
        exit 1
    }

    # Unpacked beside the install and compared file by file rather than extracted
    # straight over it, because Expand-Archive stamps every file with the archive's
    # own time: a folder that was just refreshed still shows old dates, so there is no
    # way to tell an update that happened from one that did not. Copying only what
    # differs also makes the count below true.
    $root = Get-ChildItem -LiteralPath $staging -Directory | Select-Object -First 1
    if ($root) { $source = $root.FullName; $destRoot = Join-Path $Target $root.Name }
    else { $source = $staging; $destRoot = $Target }
    $updated = 0
    $added = 0
    $same = 0
    foreach ($file in (Get-ChildItem -LiteralPath $source -Recurse -File)) {
        $dest = Join-Path $destRoot $file.FullName.Substring($source.Length).TrimStart('\')
        if (Test-Path -LiteralPath $dest) {
            if ((Get-FileHash -LiteralPath $file.FullName).Hash -eq (Get-FileHash -LiteralPath $dest).Hash) {
                $same++
                continue
            }
            $updated++
        } else {
            $added++
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $dest -Force
    }
    Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
    Say ("app:   {0} files updated, {1} added, {2} already current" -f $updated, $added, $same)
    if ($downloaded -and -not (Test-Path (Join-Path $appDir 'install_all.ps1'))) {
        # A zip whose root folder is named differently (a release, a branch rename).
        $found = Get-ChildItem -Path $Target -Directory |
            Where-Object { Test-Path (Join-Path $_.FullName 'install_all.ps1') } |
            Select-Object -First 1
        if ($found) { $appDir = $found.FullName }
    }
}
if (-not (Test-Path (Join-Path $appDir 'install_all.ps1'))) {
    Say "app:   no install_all.ps1 under $Target"
    exit 1
}
Say "app:   $appDir"

# --- 3. the join key ---------------------------------------------------------
# It is stored where the launcher already publishes from, so nothing else needs to
# know about it. Read-Host -AsSecureString keeps it off the screen; the plain value
# only ever exists in this process and in that 64-byte file.
$keyFile = Join-Path $appDir 'build\song_endpoint.key'
$urlFile = Join-Path $appDir 'build\model_endpoint_url.txt'
# A pasted address is not a join key: it is the model endpoint, the one the page
# shows as "MODEL ENDPOINT" and every device is told to use. It goes to its own
# file, and this names it on the site. Storing an address in the key file would
# make a machine that runs perfectly and can never publish or join.
if ($Key -match '^https?://') {
    $endpoint = $Key.Trim().TrimEnd('/')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $urlFile) | Out-Null
    Set-Content -Path $urlFile -Value $endpoint -Encoding ascii -NoNewline
    Say "model endpoint: $endpoint"
    Set-ModelEndpoint $endpoint $appDir
    $Key = ''
}
if ($Key) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keyFile) | Out-Null
    Set-Content -Path $keyFile -Value $Key -Encoding ascii -NoNewline
} elseif (-not (Test-Path $keyFile)) {
    Say ''
    Say 'The join key is one string from whoever runs the site. It is the key to the list'
    Say 'of machines the site is allowed to send songs to, which is why it is handed out'
    Say 'rather than printed on the page: anyone holding it can put a machine in that list.'
    Say 'Press Enter to skip. The install still finishes, the model still runs, and the'
    Say 'address it prints can be pasted into Connect my model on the site by hand.'
    $secure = Read-Host 'Paste the join key (hidden), or press Enter to skip' -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if (-not $plain) {
        Say 'no key given: this PC will run the AI for itself, not for the site'
    } elseif ($plain -match '^https?://') {
        $endpoint = $plain.Trim().TrimEnd('/')
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $urlFile) | Out-Null
        Set-Content -Path $urlFile -Value $endpoint -Encoding ascii -NoNewline
        Say "model endpoint: $endpoint"
        Say 'that was an address, not a key: nothing was written into the key file.'
        Set-ModelEndpoint $endpoint $appDir
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keyFile) | Out-Null
        Set-Content -Path $keyFile -Value $plain -Encoding ascii -NoNewline
    }
}

# --- 4. install, or update what is already here, then start ------------------
# The update path is for a PC where everything really is here: the environments
# (install_all.ps1's .venv\all_services.json) and the ACE-Step weights. Then the
# files fetched above are already the current ones and only a restart is needed,
# where running the installer again would spend 20-60 minutes arriving at the same
# place. Anything missing - no install, no weights, a half-finished one - takes the
# full install below, which puts back whatever is not there.
$marker = Join-Path $appDir '.venv\all_services.json'
$weights = Join-Path $appDir 'song_service\models\ACE-Step-v1-3.5B'
if ((Test-Path $marker) -and (Test-Path $weights)) {
    Say ''
    Say 'update: this PC is already installed here, so its environments and models stay.'
    Say 'update: nothing is reinstalled; only the files above changed.'
    Say 'update: restarting the services so the files just fetched are what runs.'
    Say ''
    & (Join-Path $appDir 'tools\start_services.ps1') -Restart
} else {
    Say ''
    Say 'installing: this PC does not have the whole application here yet, so it is put'
    Say 'in one environment - the site, the downloader, the song service, the CUDA wheels'
    Say 'and whatever of the ~10 GB of models is missing. This takes 20-60 minutes and'
    Say 'about 20 GB of disk. Every step is printed below; leave it running.'
    Say ''
    & (Join-Path $appDir 'install_all.ps1') -Start -Autostart
}

# --- the tunnel in a window of its own --------------------------------------
# The tunnel is started hidden by tools\start_services.ps1 and its address only
# ever exists in tunnel.log, so a join whose tunnel refused, or rotated to a new
# address, looked exactly like a join that had done nothing. This opens the second
# console: it follows the tunnel's log and the services' error logs, which is where
# "it did not start" and "it started and then failed" look different. Closing the
# window stops nothing - the tunnel and the services are separate processes.
if (-not $NoWindow) {
    $follow = @(
        (Join-Path $appDir 'tunnel.log'),
        (Join-Path $appDir 'run_web.err'),
        (Join-Path $appDir 'build\downloader_err.log'),
        (Join-Path $appDir 'song_service\logs\service_err.log'),
        (Join-Path $appDir 'voice_service\logs\service_err.log')
    ) | Where-Object { Test-Path -LiteralPath $_ }
    if (@($follow).Count -gt 0) {
        # The ICMP proxy lines cloudflared writes name this machine's LAN and IPv6
        # addresses; they say nothing is routed to them, and they are dropped from
        # what is shown here all the same.
        $following = (@($follow) | ForEach-Object {
            "Get-Content -Path '$_' -Wait -Tail 8 -ErrorAction SilentlyContinue | " +
                "Where-Object { `$_ -notmatch 'ICMP proxy' }"
        }) -join '; '
        Start-Process -FilePath 'powershell' -ArgumentList @(
            '-NoProfile', '-NoExit', '-Command', $following) | Out-Null
        Say 'a second window is following the tunnel log and the error logs'
        Say '  closing it stops nothing; the tunnel and the services keep running'
    }
}

# --- 5. did it actually connect? ---------------------------------------------
# The installer prints its own progress, but "is the site listing this PC" is a
# different question, and not one a person can answer from the install output.
# Ask the pool, which is the list the site reads, and say which answer came back.
Say ''
$tunnelFile = Join-Path $appDir 'build\tunnel_url.txt'
$mine = if (Test-Path $tunnelFile) { (Get-Content $tunnelFile -Raw).Trim() } else { '' }
try {
    $pool = Invoke-RestMethod -Uri 'https://song-pool.bpiersing.workers.dev/pool' -TimeoutSec 25
    $urls = @($pool.machines | ForEach-Object { $_.url })
    if ($mine -and ($urls -contains $mine)) {
        Say "connected: the site lists this PC at $mine"
    } elseif ($urls.Count -gt 0) {
        Say "the pool is answering, and lists $($urls.Count) machine(s):"
        foreach ($u in $urls) { Say "  $u" }
        Say "this PC's address was $($(if ($mine) { $mine } else { 'not published' }))"
    } else {
        Say 'the pool lists no machines yet: the install finished, but the announcement did not land.'
        Say 'Run tools\start_services.ps1 once more; it republishes the address.'
    }
} catch {
    Say "could not reach the pool to confirm the join: $($_.Exception.Message)"
}
# --- 6. what it is doing now -------------------------------------------------
# "did it join" is answered above; "is it working" is not. These are the checks a
# person would otherwise make one at a time in a terminal: every service's own
# health, the address through Cloudflare rather than only on this machine, and the
# errors the logs have recorded since the services last started.
Say ''
Say 'services'
foreach ($service in @(@{ name = 'site'; port = 5001; path = '/' },
                       @{ name = 'downloader'; port = 8080; path = '/health' },
                       @{ name = 'song service'; port = 8090; path = '/health' },
                       @{ name = 'voice service'; port = 8095; path = '/health' })) {
    $code = & curl.exe -s -o NUL -w '%{http_code}' --max-time 5 "http://127.0.0.1:$($service.port)$($service.path)"
    Say ('  {0,-14} {1,-5} {2}' -f $service.name, $service.port, $(if ($code) { $code } else { 'no answer' }))
}

Say ''
if ($mine) {
    $edge = & curl.exe -s -o NUL -w '%{http_code}' --max-time 15 "$mine/health"
    Say "tunnel: $mine"
    Say "  through Cloudflare: $(if ($edge) { $edge } else { 'no answer' })"
} else {
    Say 'tunnel: nothing published, so the site has no address for this PC'
}

Say ''
Say 'errors in the logs, last 400 lines of each'
$hits = @()
foreach ($log in @('run_web.err', 'run_web.log', 'build\downloader_err.log',
                   'song_service\logs\service_err.log', 'voice_service\logs\service_err.log')) {
    $path = Join-Path $appDir $log
    if (Test-Path $path) {
        $hits += @(Get-Content -Path $path -Tail 400 -ErrorAction SilentlyContinue |
            Where-Object { $_ -match 'Traceback|Exception|\bERROR\b|\bCRITICAL\b|" 5\d\d ' } |
            ForEach-Object { '  ' + (Split-Path $log -Leaf) + ': ' + $_.Trim() })
    }
}
if ($hits.Count) {
    $hits | Select-Object -Last 8 | ForEach-Object { Say $_ }
    if ($hits.Count -gt 8) { Say "  ... and $($hits.Count - 8) more" }
} else {
    Say '  none'
}
Say ''
Say 'The site finds this PC on the next click, and it rejoins at every logon'
