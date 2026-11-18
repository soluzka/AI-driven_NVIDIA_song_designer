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

# --- 1. the environment ------------------------------------------------------
# A venv's python is what every step below uses: pip, the package lists and the
# weight download all run through the same interpreter, which is the point of
# this script. uv is preferred because on Windows "python" is often the Microsoft
# Store stub, and --seed is what puts pip inside the new environment.
if (-not (Test-Path $python)) {
    Say 'no .venv yet: creating one with Python 3.11'
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        & $uv.Source venv $venv --python 3.11 --seed
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
} else {
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
    $weights = Join-Path $root 'song_service\models\ACE-Step-v1-3.5B'
    Say ("  {0,-24} {1}" -f 'ACE-Step weights', $(if (Test-Path $weights) { 'present' } else { 'MISSING' }))
    Say ("  {0,-24} {1}" -f 'shared marker', $(if (Test-Path $marker) { 'present (launcher will use this env)' } else { 'absent' }))
    Say ''
    Say 'Not installed here on purpose: voice_service (numpy 1.x + torch 2.6 conflict).'
    Say 'Nothing to change - rerun without -Check to install.'
    return
}

# --- 2. the card -------------------------------------------------------------
# PyPI's Windows torch wheel is the CPU build. A CPU build renders a three minute
# song in hours, so the CUDA wheel comes first and from the CUDA index.
Say ''
Say 'torch for the card (CUDA 12.6 wheels)'
Pip @('torch==2.14.1+cu126', 'torchaudio==2.14.1+cu126', '--index-url', $torchIndex)

# --- 3. the three services ---------------------------------------------------
Say ''
Say 'the site (5001)'
Pip @('-r', 'requirements.txt')

Say ''
Say 'the downloader (8080)'
Pip @('-r', 'downloader_service\requirements.txt')

Say ''
Say 'the song service (8090): ACE-Step, demucs, librosa'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Say '  warning: git is not on PATH, and ACE-Step installs from its GitHub repo'
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
