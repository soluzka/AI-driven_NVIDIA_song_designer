<#
.SYNOPSIS
    Joins this PC's NVIDIA card to the song site's GPU pool.

.DESCRIPTION
    Downloaded and run by join.cmd, so nobody types a command:

      1. checks the card
      2. fetches this application (a zip of the repository)
      3. installs it into one environment, with the CUDA wheels and the models
      4. asks once for the join key and stores it where the launcher publishes from
      5. starts the services and registers the logon entry

    After that the PC is in the pool: the site picks it up on the next click, and a
    reboot rejoins by itself.

.PARAMETER Target
    Where to unpack the app. Defaults to %LOCALAPPDATA%\internet-gpu.

.PARAMETER Key
    The join key. Without it the script asks, hidden, once. The key is what lets a
    machine publish its address; it is not the model, and it is not an account.

.PARAMETER Source
    The app archive. Defaults to the public repository zip.
#>
[CmdletBinding()]
param(
    [string]$Target = (Join-Path $env:LOCALAPPDATA 'internet-gpu'),
    [string]$Key = '',
    [string]$Source = 'https://codeload.github.com/soluzka/AI-driven_NVIDIA_song_designer/zip/refs/heads/main',
    [switch]$Offline
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

function Say([string]$text) { Write-Host $text }

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
if (-not $Offline) {
    Say "app:   fetching the current sources to $Target"
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    $zip = Join-Path $Target 'internet.zip'
    $downloaded = $false
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Source -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $Target -Force
        Remove-Item $zip -ErrorAction SilentlyContinue
        $downloaded = $true
    } catch {
        Say "app:   could not download the archive ($($_.Exception.Message))"
        Remove-Item $zip -ErrorAction SilentlyContinue
        if (-not (Test-Path (Join-Path $appDir 'install_all.ps1'))) {
            Say '       ask for the project folder and run install.cmd inside it'
            exit 1
        }
        Say '       using the copy already on disk instead'
    }
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
# A pasted address is not a join key: it is the model endpoint, the one the page shows as
# "MODEL ENDPOINT" and every device is told to use. It goes to its own file, and
# reset_join_key.cmd makes the site name it. Storing an address in the key file would make
# a machine that runs perfectly and can never publish or join.
if ($Key -match '^https?://') {
    $endpoint = $Key.Trim().TrimEnd('/')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $urlFile) | Out-Null
    Set-Content -Path $urlFile -Value $endpoint -Encoding ascii -NoNewline
    Say "model endpoint: $endpoint"
    Say 'saved. reset_join_key.cmd points the site at it; the join key is separate.'
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
        Say 'that was an address, not a key: saved here, and reset_join_key.cmd names it on the site.'
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keyFile) | Out-Null
        Set-Content -Path $keyFile -Value $plain -Encoding ascii -NoNewline
    }
}

# --- 4. install, start, stay -------------------------------------------------
Say ''
Say 'installing: the whole application in one environment - the site, the downloader,'
Say 'the song service, the CUDA wheels and about 10 GB of models. This takes 20-60'
Say 'minutes and about 20 GB of disk. Every step is printed below; leave it running.'
Say ''
& (Join-Path $appDir 'install_all.ps1') -Start -Autostart

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
Say ''
Say 'The site finds this PC on the next click, and it rejoins at every logon'
