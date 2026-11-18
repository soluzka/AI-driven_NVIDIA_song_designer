<#
.SYNOPSIS
    One install script, one environment, the whole application.

.DESCRIPTION
    Fills the single .venv at the repository root with everything the running
    application needs:

        the site            5001   Flask + Socket.IO + SQLite index   (requirements.txt)
        the downloader      8080   Flask + yt-dlp                      (downloader_service)
        the song service    8090   ACE-Step + lyrics + demucs + librosa (song_service)

    and then downloads the ~10 GB of ACE-Step weights into song_service\models.

    voice_service is deliberately NOT folded in. chatterbox-tts pins numpy 1.26.4
    and torch 2.6.0, and the song service needs numpy 2.4.6 and torch 2.14.1+cu126;
    pip cannot hold both in one environment, and nothing in the singing path calls
    voice_service any more. It keeps its own venv and its own install.py.

.PARAMETER Check
    Report what this environment has and what is missing. Installs nothing.

.PARAMETER NoWeights
    Skip the ~10 GB model download (packages only).

.PARAMETER Start
    Start the services and the tunnel when the install finishes.

.PARAMETER Autostart
    Register the logon entry (tools\install_autostart.ps1), so the services come
    back after a reboot with nobody touching a terminal.

.EXAMPLE
    .\install_all.ps1 -Check
    .\install_all.ps1
    .\install_all.ps1 -NoWeights
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$NoWeights,
    [switch]$Start,
    [switch]$Autostart
)

# 'Continue', not 'Stop': pip, uv and the weight downloader all write progress to
# stderr, and under Stop a native command's stderr becomes a terminating error
# that aborts the install at the first line of output. Every failure that matters
# is checked through $LASTEXITCODE below instead.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$marker = Join-Path $venv 'all_services.json'
$buildDir = Join-Path $root 'build'
$log = Join-Path $buildDir 'install_all.log'
$torchIndex = 'https://download.pytorch.org/whl/cu126'

if (-not (Test-Path $buildDir)) { New-Item -ItemType Directory -Path $buildDir | Out-Null }

function Say([string]$text) {
    Write-Host $text
    try { Add-Content -Path $log -Value $text -Encoding utf8 } catch { }
}

function Probe([string]$module, [string]$expression) {
    $answer = & $python -c "import $module; print($expression)" 2>$null | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($answer -as [string])
}

function Pip([string[]]$arguments) {
    Say ("  pip install " + ($arguments -join ' '))
    & $python -m pip install --disable-pip-version-check --no-input @arguments
    if ($LASTEXITCODE -ne 0) {
        Say "  pip exited $LASTEXITCODE"
        throw "pip install failed: $($arguments -join ' ')"
    }
}

Say ''
Say 'The whole application in one environment'
Say '----------------------------------------'
Say "root:    $root"
Say "python:  $python"
Say ''

# --- 0. the two prerequisites nobody should have to install ------------------
# A Windows machine that has never developed anything has no Python 3.11 and no
# git, and both are required: pip needs the interpreter, and ACE-Step installs
# from its GitHub URL, which pip can only do when git is on PATH. Installing
# either by hand is a download, a wizard and sometimes an administrator prompt,
# which is exactly what this script exists to avoid - so both are fetched here
# as portable, per-user copies. The same shape as the cloudflared fetch in the
# launcher: pinned version, no elevation, no PATH edit that outlives the run.
$toolsBin = Join-Path $root 'tools\bin'
$gitDir = Join-Path $toolsBin 'git'
if (-not (Test-Path $toolsBin)) { New-Item -ItemType Directory -Path $toolsBin -Force | Out-Null }
if (Test-Path (Join-Path $gitDir 'cmd')) { $env:PATH = (Join-Path $gitDir 'cmd') + ';' + $env:PATH }
$env:PATH = $toolsBin + ';' + $env:PATH

$uvVersion = '0.13.9'
$uvZipUrl = "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip"
$gitVersion = '2.56.1'
$gitZipUrl = "https://github.com/git-for-windows/git/releases/download/v$gitVersion.windows.1/MinGit-$gitVersion-64-bit.zip"

function Save-Zip([string]$url, [string]$target) {
    $partial = "$target.download"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $url -OutFile $partial -UseBasicParsing -TimeoutSec 600
    if (Test-Path $target) { Remove-Item $target -Force -ErrorAction SilentlyContinue }
    Move-Item -Path $partial -Destination $target -Force
}

# uv is what creates the 3.11 interpreter and the venv. On Windows "python" is
# usually the Microsoft Store stub, and py.exe cannot always start the 3.11 that
# uv installed, so uv is the one interpreter manager this script relies on.
function Ensure-Uv {
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $exe = Join-Path $toolsBin 'uv.exe'
    if (Test-Path $exe) { return $exe }
    Say "  uv is missing: fetching $uvVersion (~15 MB, once, no installer)"
    try {
        $zip = Join-Path $toolsBin 'uv.zip'
        Save-Zip $uvZipUrl $zip
        Expand-Archive -Path $zip -DestinationPath $toolsBin -Force
        Remove-Item $zip -ErrorAction SilentlyContinue
    } catch {
        Say "  could not fetch uv: $($_.Exception.Message)"
        return $null
    }
    if (-not (Test-Path $exe)) {
        $nested = Get-ChildItem -Path $toolsBin -Filter 'uv.exe' -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($nested) { $exe = $nested.FullName } else { return $null }
    }
    Say '  uv in place'
    return $exe
}

# MinGit: the portable git, 40 MB, unzips into tools\bin\git. pip's git+https
# support shells out to git, and without it the ACE-Step line of the install
# fails with "Cannot find command 'git'" after everything else has succeeded.
function Ensure-Git {
    if (Get-Command git -ErrorAction SilentlyContinue) { return $true }
    if (Test-Path (Join-Path $gitDir 'cmd\git.exe')) {
        $env:PATH = (Join-Path $gitDir 'cmd') + ';' + $env:PATH
        return $true
    }
    Say "  git is missing: fetching MinGit $gitVersion (~40 MB, once, no installer)"
    try {
        $zip = Join-Path $toolsBin 'mingit.zip'
        Save-Zip $gitZipUrl $zip
        if (Test-Path $gitDir) { Remove-Item $gitDir -Recurse -Force -ErrorAction SilentlyContinue }
        Expand-Archive -Path $zip -DestinationPath $gitDir -Force
        Remove-Item $zip -ErrorAction SilentlyContinue
    } catch {
        Say "  could not fetch git: $($_.Exception.Message)"
        return $false
    }
    if (-not (Test-Path (Join-Path $gitDir 'cmd\git.exe'))) { return $false }
    $env:PATH = (Join-Path $gitDir 'cmd') + ';' + $env:PATH
    Say '  git in place'
    return $true
}

# What the machine itself has: the card, the driver, and room on disk for ~20 GB
# of wheels and weights. Reported before anything is downloaded, because "no
# NVIDIA GPU" and "2 GB free" are the two answers that turn an install into
# something to stop rather than wait out.
function Report-Machine {
    $disk = Get-PSDrive -Name ($root.Substring(0, 1)) -ErrorAction SilentlyContinue
    $freeGb = if ($disk) { [math]::Round($disk.Free / 1GB, 1) } else { $null }
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    $gpu = ''
    $driver = ''
    if ($smi) {
        $gpu = (& $smi.Source --query-gpu=name,memory.total --format=csv,noheader 2>$null | Select-Object -First 1)
        $driver = (& $smi.Source --query-gpu=driver_version --format=csv,noheader 2>$null | Select-Object -First 1)
    }
    Say 'This machine:'
    Say ("  {0,-12} {1}" -f 'gpu', $(if ($gpu) { $gpu } else { 'none detected (CPU only - a 3 minute song takes hours)' }))
    Say ("  {0,-12} {1}" -f 'driver', $(if ($driver) { $driver } else { 'n/a - an NVIDIA driver 560 or newer is what makes the card usable' }))
    Say ("  {0,-12} {1}" -f 'disk free', $(if ($freeGb) { "$freeGb GB (about 20 GB is used by torch, the packages and the weights)" } else { 'unknown' }))
    if ($freeGb -and $freeGb -lt 20) {
        Say '  warning: less than 20 GB free - free some space or run with -NoWeights first'
    }
    if (-not $gpu) {
        Say '  warning: no NVIDIA card found. The CUDA torch wheel still installs and runs on the'
        Say '           processor; songs render from the same model, only much more slowly.'
    }
    Say ''
}

Report-Machine

# -Check installs nothing, so it must not create an environment on the way to
# reporting one: a bare machine would download uv and a 3.11 interpreter merely
# to be told what it does not have.
if ($Check -and -not (Test-Path $python)) {
    Say 'no .venv yet: nothing is installed here.'
    Say 'Run install.cmd (or this script without -Check) to install everything.'
    return
}

# --- 1. the environment ------------------------------------------------------
# A venv's python is what every step below uses: pip, the package lists and the
# weight download all run through the same interpreter, which is the point of
# this script. --seed is what puts pip inside the new environment.
if (-not (Test-Path $python)) {
    Say 'no .venv yet: creating one with Python 3.11'
    $uvPath = Ensure-Uv
    if ($uvPath) {
        & $uvPath venv $venv --python 3.11 --seed
        if ($LASTEXITCODE -ne 0) { throw 'uv could not create the environment' }
    } else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) {
            & $launcher.Source -3.11 -m venv $venv
        } else {
            & python -3.11 -m venv $venv
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $python)) {
            throw 'could not create .venv - install Python 3.11 or uv first'
        }
    }
    Say '  environment created'
} elseif (-not $Check) {
    Say '.venv already exists: installing into it'
}

if ($Check) {
    Say ''
    Say 'What this environment has:'
    $rows = @(
        @{ name = 'flask (site)';            value = (Probe 'flask' 'flask.__version__') },
        @{ name = 'flask_socketio (site)';   value = (Probe 'flask_socketio' "getattr(flask_socketio, '__version__', 'ok')") },
        @{ name = 'yt_dlp (downloader)';     value = (Probe 'yt_dlp.version' 'yt_dlp.version.__version__') },
        @{ name = 'torch';                   value = (Probe 'torch' 'torch.__version__') },
        @{ name = 'cuda usable';             value = (Probe 'torch' "'yes' if torch.cuda.is_available() else 'NO - cpu only'") },
        @{ name = 'torchaudio';              value = (Probe 'torchaudio' 'torchaudio.__version__') },
        @{ name = 'transformers';            value = (Probe 'transformers' 'transformers.__version__') },
        @{ name = 'librosa';                 value = (Probe 'librosa' 'librosa.__version__') },
        @{ name = 'scipy';                   value = (Probe 'scipy' 'scipy.__version__') },
        @{ name = 'demucs';                  value = (Probe 'demucs' "'ok'") },
        @{ name = 'soundfile';               value = (Probe 'soundfile' 'soundfile.__version__') },
        @{ name = 'numpy';                   value = (Probe 'numpy' 'numpy.__version__') }
    )
    foreach ($row in $rows) {
        $value = if ($row.value) { $row.value } else { 'MISSING' }
        Say ("  {0,-24} {1}" -f $row.name, $value)
    }
    Say ("  {0,-24} {1}" -f 'git (ACE-Step)', $(if (Get-Command git -ErrorAction SilentlyContinue) { 'ok' } else { 'MISSING - install_all fetches a portable copy' }))
    Say ("  {0,-24} {1}" -f 'uv (venv, Python 3.11)', $(if (Get-Command uv -ErrorAction SilentlyContinue) { 'ok' } else { 'MISSING - install_all fetches a portable copy' }))
    $weights = Join-Path $root 'song_service\models\ACE-Step-v1-3.5B'
    Say ("  {0,-24} {1}" -f 'ACE-Step weights', $(if (Test-Path $weights) { 'present' } else { 'MISSING' }))
    Say ("  {0,-24} {1}" -f 'shared marker', $(if (Test-Path $marker) { 'present (launcher will use this env)' } else { 'absent' }))
    Say ''
    Say 'Not installed here on purpose: voice_service (numpy 1.x + torch 2.6 conflict).'
    Say 'Nothing to change - rerun without -Check to install.'
    return
}

# --- 2. the card -------------------------------------------------------------
# pip, wheel and setuptools first: a Python 3.11 that uv just downloaded ships an
# older setuptools, and ACE-Step's setup.py needs one below 81 to build.
Say ''
Say 'build tools'
Pip @('--upgrade', 'pip', 'wheel', 'setuptools<81')

# PyPI's Windows torch wheel is the CPU build. A CPU build renders a three minute
# song in hours, so the CUDA wheel comes first and from the CUDA index.
Say ''
Say 'torch for the card (CUDA 12.6 wheels)'
Pip @('torch==2.14.1+cu126', 'torchaudio==2.11.0+cu126', '--index-url', $torchIndex)
$cudaNow = Probe 'torch' 'torch.cuda.is_available()'
if ($cudaNow -eq 'False') {
    Say '  torch is installed but sees no card. The AI will run on the processor:'
    Say '  install the NVIDIA driver (560 or newer) from nvidia.com and rerun -Check.'
} else {
    Say '  torch sees the card'
}

# --- 3. the three services ---------------------------------------------------
Say ''
Say 'the site (5001)'
Pip @('-r', 'requirements.txt')

Say ''
Say 'the downloader (8080)'
Pip @('-r', 'downloader_service\requirements.txt')

Say ''
Say 'the song service (8090): ACE-Step, demucs, librosa'
# ACE-Step installs as git+https://..., and pip shells out to git for that. The
# portable copy fetched in step 0 is already on PATH; if that failed, say so here
# instead of letting pip fail four hundred lines into a build.
if (-not (Ensure-Git)) {
    throw 'git is required for ACE-Step (pip installs it from GitHub) and could not be fetched'
}
Pip @('-r', 'song_service\requirements.txt', '--extra-index-url', $torchIndex)

# --- 4. the weights ----------------------------------------------------------
if ($NoWeights) {
    Say ''
    Say 'weights: skipped (-NoWeights)'
} else {
    Say ''
    Say 'the ACE-Step checkpoint (~10 GB, once)'
    & $python (Join-Path $root 'song_service\install.py') --weights
    if ($LASTEXITCODE -ne 0) { Say '  the weight download did not finish; rerun with the script when ready' }
}

# --- 5. what came out --------------------------------------------------------
Say ''
Say 'check'
$cuda = Probe 'torch' "'%s cuda=%s' % (torch.__version__, torch.cuda.is_available())"
$cudaOk = $cuda -and $cuda.EndsWith('cuda=True')
Say ("  torch:     " + $(if ($cuda) { $cuda } else { 'MISSING' }))
foreach ($pair in @(@('flask', 'flask'), @('flask_socketio', 'flask_socketio'), @('yt_dlp', 'yt_dlp'),
        @('librosa', 'librosa'), @('scipy', 'scipy'), @('demucs', 'demucs'), @('soundfile', 'soundfile'))) {
    $ok = Probe $pair[0] "'ok'"
    Say ("  {0,-16}{1}" -f $pair[1], $(if ($ok) { 'ok' } else { 'MISSING' }))
}

# The launcher reads this marker and runs the site, the downloader and the song
# service from this one environment instead of creating three more.
$info = [ordered]@{
    shared     = $true
    created    = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    python     = $python
    site       = 5001
    downloader = 8080
    song       = 8090
    voice      = 'separate venv: numpy 1.x + torch 2.6 cannot share this one'
}
$info | ConvertTo-Json | Set-Content -Path $marker -Encoding utf8
Say ''
Say "wrote $marker"
if (-not $cudaOk) {
    Say ''
    Say 'The model is installed, but torch cannot see a card, so songs render on the CPU:'
    Say 'minutes to hours instead of seconds. Nothing else is missing - an NVIDIA driver'
    Say '560 or newer is the only fix, and it is a normal download, not an administrator task.'
}
Say ''
Say 'Next:'
Say '  install.cmd                    double-click: install, start, and keep it running'
Say '  .\install_all.ps1 -Start -Autostart   the same thing from a shell'
Say '  .\tools\start_services.ps1     starts all three from this environment'
Say '  .\install_all.ps1 -Check       re-reports at any time'

if ($Autostart) {
    Say ''
    Say 'registering the logon entry: the services return by themselves after a reboot'
    & (Join-Path $root 'tools\install_autostart.ps1')
}

if ($Start) {
    Say ''
    Say 'starting the services'
    & (Join-Path $root 'tools\start_services.ps1')
}
