<#
.SYNOPSIS
    Runs the song model on this PC's NVIDIA card - and nothing else.

.DESCRIPTION
    The full application is the site, the downloader, the search index and the
    song service. This installs only the song service: six Python files, the
    CUDA build of torch, about 10 GB of weights, the service and a tunnel. No
    site, no downloader, no database, no repository clone.

    It is also self-contained on a PC that has never developed anything: if
    Python 3.11 and git are missing, portable copies are fetched into this
    folder, with no installer and no administrator prompt.

    What the card gets:

      NVIDIA, driver 560+   CUDA. torch is the +cu126 wheel and the model runs
                            on the card.
      anything else         the same model on the processor, minutes to hours
                            per song. There is no CUDA on AMD or Intel graphics.

    After it installs, the PC is usable two ways: paste the tunnel address it
    prints into the site's Song designer -> Connect my model, or give it the
    join key and it joins the pool every visitor's click can use.

    Every later run is a start, not an install, and the logon entry uses that
    path, so a reboot comes back by itself.

.PARAMETER Check
    Report the card, the disk and what is installed here. Changes nothing.

.PARAMETER StartOnly
    Skip the install and only start the service and the tunnel. This is what
    the logon entry runs.

.PARAMETER Watch
    Stay running and restart the service if it goes away.

.PARAMETER NoAutostart
    Do not register the logon entry.

.PARAMETER Key
    The pool's join key. Without it the model still runs and still prints a
    tunnel address, it is just not in the pool.

.PARAMETER Target
    Where everything lives. Defaults to %LOCALAPPDATA%\song-gpu.

.EXAMPLE
    .\song-gpu.ps1 -Check
    .\song-gpu.ps1
    .\song-gpu.ps1 -StartOnly -Watch
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$StartOnly,
    [switch]$Watch,
    [switch]$NoAutostart,
    [string]$Key = '',
    [string]$Target = ''
)

# Native tools (pip, uv, git, curl, cloudflared) all write progress to stderr.
# Under 'Stop' that becomes a terminating error and aborts the install at the
# first line of output, so failures are checked through $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$siteUrl = 'https://browser.bpiersing.workers.dev'
$poolUrl = 'https://song-pool.bpiersing.workers.dev'
$rawBase = 'https://raw.githubusercontent.com/soluzka/AI-driven_NVIDIA_song_designer/main'
$torchIndex = 'https://download.pytorch.org/whl/cu126'
$uvVersion = '0.13.9'
$gitVersion = '2.56.1'
$cloudflaredVersion = '2026.10.7'
$songPort = 8090

if (-not $Target) { $Target = Join-Path $env:LOCALAPPDATA 'song-gpu' }
$work = $Target
$svc = Join-Path $work 'song_service'
$tools = Join-Path $work 'tools'
$gitDir = Join-Path $tools 'git'
$venv = Join-Path $work '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$logDir = Join-Path $work 'logs'
$keyFile = Join-Path $work 'join.key'
$serviceLog = Join-Path $logDir 'song.out.log'
$serviceErr = Join-Path $logDir 'song.err.log'
$tunnelLog = Join-Path $logDir 'tunnel.log'
$tunnelUrlFile = Join-Path $logDir 'tunnel_url.txt'

foreach ($dir in @($work, $tools, $logDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

function Say([string]$text) {
    Write-Host $text
    try { Add-Content -Path (Join-Path $logDir 'install.log') -Value $text -Encoding utf8 } catch { }
}

function Probe([string]$module, [string]$expression) {
    if (-not (Test-Path $python)) { return $null }
    $answer = & $python -c "import $module; print($expression)" 2>$null | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($answer -as [string])
}

function Answers {
    $code = & curl.exe -s -o NUL -w '%{http_code}' --max-time 15 "http://127.0.0.1:$songPort/health"
    return ($code -eq '200')
}

function Running {
    $found = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*--role song*' }
    return [bool]$found
}

# --- what this machine has ---------------------------------------------------
function Report-Card {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    $gpu = ''
    $driver = ''
    if ($smi) {
        $gpu = (& $smi.Source --query-gpu=name,memory.total --format=csv,noheader 2>$null | Select-Object -First 1)
        $driver = (& $smi.Source --query-gpu=driver_version --format=csv,noheader 2>$null | Select-Object -First 1)
    }
    $disk = Get-PSDrive -Name ($work.Substring(0, 1)) -ErrorAction SilentlyContinue
    $free = if ($disk) { [math]::Round($disk.Free / 1GB, 1) } else { $null }

    Say 'This machine:'
    if ($gpu) {
        Say ("  {0,-11} {1}" -f 'card', $gpu)
        Say ("  {0,-11} {1}" -f 'driver', $(if ($driver) { "$driver (560 or newer is what the wheel needs)" } else { 'version unknown - 560 or newer is required' }))
    } else {
        Say ("  {0,-11} {1}" -f 'card', 'no NVIDIA card: the model will run on the processor')
        Say '              CUDA exists only on NVIDIA. AMD, Intel and ARM graphics cannot use it.'
    }
    Say ("  {0,-11} {1}" -f 'disk free', $(if ($free) { "$free GB (about 20 GB is used by torch, the packages and the weights)" } else { 'unknown' }))
    Say ("  {0,-11} {1}" -f 'folder', $work)
}

if ($Check) {
    Say ''
    Say 'Song model only - no site, no downloader, no index'
    Report-Card
    Say ''
    Say 'Installed here:'
    $torch = Probe 'torch' "'%s cuda=%s' % (torch.__version__, torch.cuda.is_available())"
    Say ("  {0,-14} {1}" -f 'torch', $(if ($torch) { $torch } else { 'MISSING' }))
    $ace = Join-Path $svc 'models\ACE-Step-v1-3.5B'
    $qwen = Join-Path $svc 'models\Qwen2.5-1.5B-Instruct'
    Say ("  {0,-14} {1}" -f 'ACE-Step', $(if (Test-Path $ace) { 'present' } else { 'MISSING' }))
    Say ("  {0,-14} {1}" -f 'lyric model', $(if (Test-Path $qwen) { 'present' } else { 'MISSING' }))
    Say ("  {0,-14} {1}" -f 'service now', $(if (Answers) { 'answering on 8090' } elseif (Running) { 'starting' } else { 'not running' }))
    Say ''
    Say 'Nothing was changed. Run without -Check to install.'
    return
}

# --- the prerequisites -------------------------------------------------------
function Save-Zip([string]$url, [string]$target) {
    $partial = "$target.download"
    Invoke-WebRequest -Uri $url -OutFile $partial -UseBasicParsing -TimeoutSec 600
    if (Test-Path $target) { Remove-Item $target -Force -ErrorAction SilentlyContinue }
    Move-Item -Path $partial -Destination $target -Force
}

function Ensure-Uv {
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $exe = Join-Path $tools 'uv.exe'
    if (Test-Path $exe) { return $exe }
    Say "  uv is missing: fetching $uvVersion (~15 MB, no installer)"
    try {
        $zip = Join-Path $tools 'uv.zip'
        Save-Zip "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip" $zip
        Expand-Archive -Path $zip -DestinationPath $tools -Force
        Remove-Item $zip -ErrorAction SilentlyContinue
    } catch {
        Say "  could not fetch uv: $($_.Exception.Message)"
        return $null
    }
    if (Test-Path $exe) { return $exe }
    $nested = Get-ChildItem -Path $tools -Filter 'uv.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($nested) { return $nested.FullName }
    return $null
}

function Ensure-Git {
    if (Get-Command git -ErrorAction SilentlyContinue) { return $true }
    if (Test-Path (Join-Path $gitDir 'cmd\git.exe')) {
        $env:PATH = (Join-Path $gitDir 'cmd') + ';' + $env:PATH
        return $true
    }
    Say "  git is missing: fetching MinGit $gitVersion (~40 MB, no installer)"
    try {
        $zip = Join-Path $tools 'mingit.zip'
        Save-Zip "https://github.com/git-for-windows/git/releases/download/v$gitVersion.windows.1/MinGit-$gitVersion-64-bit.zip" $zip
        if (Test-Path $gitDir) { Remove-Item $gitDir -Recurse -Force -ErrorAction SilentlyContinue }
        Expand-Archive -Path $zip -DestinationPath $gitDir -Force
        Remove-Item $zip -ErrorAction SilentlyContinue
    } catch {
        Say "  could not fetch git: $($_.Exception.Message)"
        return $false
    }
    if (-not (Test-Path (Join-Path $gitDir 'cmd\git.exe'))) { return $false }
    $env:PATH = (Join-Path $gitDir 'cmd') + ';' + $env:PATH
    return $true
}

# --- the six files -----------------------------------------------------------
# Only the song service. The site, the downloader, the SQLite index and the
# scrapers are not downloaded at all, which is the difference between this and
# join.cmd: that one installs the whole application.
$files = @(
    'song_service/app.py',
    'song_service/lyrics.py',
    'song_service/model_lock.py',
    'song_service/models.json',
    'song_service/requirements.txt',
    'song_service/install.py'
)

function Ensure-Files {
    if (-not (Test-Path $svc)) { New-Item -ItemType Directory -Path $svc -Force | Out-Null }
    foreach ($relative in $files) {
        $name = Split-Path -Leaf $relative
        $path = Join-Path $svc $name
        if (Test-Path $path) { continue }
        Say "  fetching $name"
        try {
            Invoke-WebRequest -Uri "$rawBase/$relative" -OutFile $path -UseBasicParsing -TimeoutSec 120
        } catch {
            Say "  could not fetch ${name}: $($_.Exception.Message)"
            return $false
        }
        if (-not (Test-Path $path) -or (Get-Item $path).Length -lt 100) {
            Say "  $name came back empty or truncated"
            return $false
        }
    }
    return $true
}

# --- install -----------------------------------------------------------------
Say ''
Say 'Song model only - no site, no downloader, no index'
Say '-----------------------------------------------'
Report-Card
Say ''

if (-not $StartOnly) {
    if (-not [Environment]::Is64BitOperatingSystem) {
        Say 'This needs 64-bit Windows: torch has no 32-bit wheel.'
        exit 1
    }
    if ($PSVersionTable.PSVersion.Major -lt 5) {
        Say 'This needs Windows PowerShell 5.1 or newer (Windows 10 and 11 ship it).'
        exit 1
    }

    Say 'the six files'
    if (-not (Ensure-Files)) { Say 'stopping: the source files could not be fetched'; exit 1 }

    Say ''
    Say 'the environment'
    if (-not (Test-Path $python)) {
        $uvPath = Ensure-Uv
        if (-not $uvPath) { Say 'stopping: no Python could be fetched'; exit 1 }
        & $uvPath venv $venv --python 3.11 --seed
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $python)) { Say 'stopping: the environment could not be created'; exit 1 }
    }

    $torch = Probe 'torch' 'torch.__version__'
    if (-not $torch) {
        Say ''
        Say 'the packages (torch for the card first, then the model)'
        & $python -m pip install --disable-pip-version-check --no-input --upgrade pip wheel 'setuptools<81'
        & $python -m pip install --disable-pip-version-check --no-input torch==2.14.1+cu126 torchaudio==2.14.1+cu126 --index-url $torchIndex
        if ($LASTEXITCODE -ne 0) { Say '  the CUDA torch wheels did not install' }
        if (-not (Ensure-Git)) { Say 'stopping: ACE-Step installs from git and git could not be fetched'; exit 1 }
        & $python -m pip install --disable-pip-version-check --no-input -r (Join-Path $svc 'requirements.txt') --extra-index-url $torchIndex
        if ($LASTEXITCODE -ne 0) { Say '  the song packages did not all install; see the lines above' }
        $torch = Probe 'torch' 'torch.__version__'
        if (-not $torch) { Say 'stopping: torch is not importable in this environment'; exit 1 }
    } else {
        Say ''
        Say "packages: already installed (torch $torch)"
    }

    $ace = Join-Path $svc 'models\ACE-Step-v1-3.5B'
    if (-not (Test-Path $ace)) {
        Say ''
        Say 'the weights (~10 GB, once)'
        & $python (Join-Path $svc 'install.py') --weights
        if ($LASTEXITCODE -ne 0) { Say '  the download did not finish; rerun this file and it resumes' }
    } else {
        Say ''
        Say 'weights: already present'
    }
}

# --- start -------------------------------------------------------------------
if (-not (Answers)) {
    if (Running) {
        Say 'the service is already starting (torch import takes about 40 s)'
    } else {
        $env:SONG_PORT = "$songPort"
        $env:SONG_HOST = '0.0.0.0'
        $env:SONG_WARM = '1'
        Start-Process -FilePath $python -ArgumentList 'app.py', '--role', 'song' -WorkingDirectory $svc `
            -WindowStyle Hidden -RedirectStandardOutput $serviceLog -RedirectStandardError $serviceErr | Out-Null
        Say "service starting on 127.0.0.1:$songPort (log: $serviceLog)"
    }
} else {
    Say "service already answering on 127.0.0.1:$songPort"
}

# --- the tunnel --------------------------------------------------------------
$cloudflared = Join-Path $tools 'cloudflared.exe'
if (-not (Test-Path $cloudflared)) {
    Say "tunnel: fetching cloudflared $cloudflaredVersion (~55 MB, once)"
    try {
        $partial = "$cloudflared.download"
        Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/download/$cloudflaredVersion/cloudflared-windows-amd64.exe" `
            -OutFile $partial -UseBasicParsing -TimeoutSec 600
        $signature = Get-AuthenticodeSignature -FilePath $partial
        $signer = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { '' }
        if ($signature.Status -ne 'Valid' -or $signer -notmatch 'Cloudflare') {
            Say "tunnel: the download failed its signature check ($($signature.Status)); discarded"
            Remove-Item $partial -ErrorAction SilentlyContinue
        } else {
            Move-Item -Path $partial -Destination $cloudflared -Force
        }
    } catch {
        Say "tunnel: could not fetch cloudflared ($($_.Exception.Message))"
        Remove-Item "$cloudflared.download" -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path $cloudflared)) {
    Say 'tunnel: skipped, so this PC is reachable only on its own network'
} else {
    $live = @(Get-Process cloudflared -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $cloudflared })
    if ($live.Count -eq 0) {
        # An empty config, for the same reason the launcher writes one: a
        # config.yml in ~/.cloudflared has ingress rules that win over --url and
        # answer 404 for everything.
        $config = Join-Path $tools 'quick-tunnel.yml'
        if (-not (Test-Path $config)) { Set-Content -Path $config -Value '{}' -Encoding ascii }
        Remove-Item $tunnelLog -ErrorAction SilentlyContinue
        Start-Process -FilePath $cloudflared -WorkingDirectory $work -ArgumentList @(
            'tunnel', '--config', $config, '--url', "http://127.0.0.1:$songPort",
            '--no-autoupdate', '--logfile', $tunnelLog, '--loglevel', 'info'
        ) -WindowStyle Hidden | Out-Null
        Say 'tunnel: starting'
    }

    $url = ''
    for ($i = 0; $i -lt 25; $i++) {
        if (Test-Path $tunnelLog) {
            $url = (Select-String -Path $tunnelLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' |
                ForEach-Object { $_.Matches[0].Value } | Select-Object -Last 1)
            if ($url) { break }
        }
        Start-Sleep -Seconds 2
    }

    if (-not $url) {
        Say 'tunnel: no address yet, see logs\tunnel.log'
    } else {
        Set-Content -Path $tunnelUrlFile -Value $url -Encoding ascii
        Say ''
        Say "This PC's model is at: $url"
        Say 'Use it two ways:'
        Say "  1. no key: on the site, Song designer -> Connect my model, paste that address"
        Say '  2. with the join key: it goes into the pool and needs nothing pasted'

        # The key is asked for once and kept beside the model, never in the
        # repository. SONG_ENDPOINT_KEY wins, then -Key, then the saved file.
        if (-not $Key) { $Key = $env:SONG_ENDPOINT_KEY }
        if (-not $Key -and (Test-Path $keyFile)) { $Key = (Get-Content $keyFile -Raw).Trim() }
        if (-not $Key) {
            Say ''
            Say 'The join key is one string from the site owner. It lets this PC announce its'
            Say 'address to the pool and to the site; it is not a password to anything else.'
            $secure = Read-Host 'Paste the join key (Enter to skip)' -AsSecureString
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
            if ($plain) {
                $Key = $plain
                Set-Content -Path $keyFile -Value $Key -Encoding ascii -NoNewline
            }
        }

        if (-not $Key) {
            Say ''
            Say 'No key: this PC is not in the pool. The address above still works when pasted'
            Say 'into Connect my model.'
        } else {
            # The JSON goes through a file: curl.exe on Windows mangles quotes that
            # arrive inside an argument, which made the site answer "expected JSON".
            $payload = Join-Path $logDir 'publish.json'
            try {
                Set-Content -Path $payload -Encoding ascii -Value ('{"url":"' + $url + '"}')
                $answer = & curl.exe -s --max-time 20 -X POST "$siteUrl/api/song/endpoint" `
                    -H 'Content-Type: application/json' -H "x-song-key: $Key" --data-binary "@$payload"
                Say "site: $answer"
                $joined = & curl.exe -s --max-time 20 -X POST "$poolUrl/join" `
                    -H 'Content-Type: application/json' -H "x-join-key: $Key" --data-binary "@$payload"
                Say "pool: $joined"
            } finally {
                Remove-Item $payload -ErrorAction SilentlyContinue
            }
        }
    }
}

# --- come back by itself -----------------------------------------------------
if (-not $NoAutostart) {
    # The script is run from a temporary copy by song-gpu.cmd, and TEMP is
    # cleaned: the logon entry points at a copy that stays.
    $resident = Join-Path $work 'song-gpu.ps1'
    if ($PSCommandPath -and $PSCommandPath -ne $resident) {
        try { Copy-Item -Path $PSCommandPath -Destination $resident -Force } catch { }
    }
    if (Test-Path $resident) {
        $command = 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $resident + '" -StartOnly -Watch'
        try {
            Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Song GPU agent' -Value $command
            Say 'logon entry: Song GPU agent (no administrator, no window)'
        } catch {
            Say "could not register the logon entry: $($_.Exception.Message)"
        }
    }
}

if ($Watch) {
    $others = @(Get-CimInstance Win32_Process -Filter "Name like '%powershell%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*song-gpu.ps1*' -and $_.CommandLine -like '*-Watch*' -and $_.ProcessId -ne $PID })
    if ($others.Count -gt 0) { return }
    Say 'watching: the service is restarted if it goes away'
    while ($true) {
        Start-Sleep -Seconds 120
        if (-not (Answers) -and -not (Running)) {
            Say 'service is down, restarting'
            Start-Process -FilePath 'powershell' -WindowStyle Hidden -ArgumentList @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $work 'song-gpu.ps1'), '-StartOnly'
            ) | Out-Null
        }
    }
}

$health = & curl.exe -s --max-time 15 "http://127.0.0.1:$songPort/health"
Say ''
Say "health: $(if ($health) { $health } else { 'not answering yet - the model loads for about a minute' })"
