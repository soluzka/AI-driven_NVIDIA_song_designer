# Build the two Kaggle datasets the offline kernel mounts.
#
# Kaggle ignores enableInternet on every kernel pushed through its API, so a pushed
# run can never clone the repository or download weights. The way round it is to
# put everything in dataset sources, which are mounted at /kaggle/input without any
# network at all:
#
#   <you>/song-gpu-code     repo (no .git, no models), packages unpacked for Linux,
#                           the ACE-Step source, and the cloudflared binary
#   <you>/song-gpu-weights  ACE-Step-v1-3.5B and Qwen2.5-1.5B-Instruct
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -WhatIfOnly   # sizes and plan
#   powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -Packages     # also unpack packages
#   powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -Create       # also create the datasets
#   powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -CodeOnly -Create  # code dataset only
#   powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -WeightsOnly -Create  # weights dataset only
#
# -Create needs a Kaggle token in build\kaggle.json (tools\set_kaggle.ps1 writes one)
# and uploads ~11 GB once. Nothing here is pushed to GitHub: the staging folder is
# gitignored.

param(
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$Stage = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path 'build\kaggle_pack'),
    # Kaggle's script runner is Python 3.13; the compiled packages must match it.
    [string]$PythonVersion = '3.13',
    [string]$Platform = 'x86_64-manylinux_2_28',
    [string]$Owner = '',
    [switch]$Packages,
    [switch]$Weights,
    [switch]$Create,
    # Repack and upload the code dataset alone: a code change does not need the 10.7 GB
    # of weights to be uploaded again, and a run that has internet can fetch those.
    [switch]$CodeOnly,
    # The mirror of -CodeOnly: a weights change does not need the repository re-uploaded.
    [switch]$WeightsOnly,
    [switch]$WhatIfOnly
)

$ErrorActionPreference = 'Stop'

function Say($message) { Write-Host $message }

# This script rebuilds everything it needs from the repository, so a fresh clone can
# run it with no ignored file present except the two it reads: build\kaggle.json (for
# -Create) and song_service\models (the weights). Checked up front so the failure is
# a sentence rather than a stack trace halfway through an 11 GB upload.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is not on PATH: install it (winget install --id=astral-sh.uv) and run this again'
}
if (-not (Get-Command robocopy -ErrorAction SilentlyContinue)) {
    throw 'robocopy is not on PATH: this packer runs on Windows'
}

$codeDir = Join-Path $Stage 'code'
$weightsDir = Join-Path $Stage 'weights'
$repoCopy = Join-Path $codeDir 'repo'

Say "stage: $Stage"
Say "repo : $Root"

if ($WhatIfOnly) {
    $models = Join-Path $Root 'song_service\models'
    $size = 0
    if (Test-Path $models) {
        $size = (Get-ChildItem $models -Recurse -File | Measure-Object -Property Length -Sum).Sum
    }
    Say ("weights to upload: {0:N2} GB" -f ($size / 1GB))
    Say "code dataset     : repository copy + packages + ACE-Step source + cloudflared"
    Say "kernel mounts    : /kaggle/input/song-gpu-code/repo, /kaggle/input/song-gpu-weights/..."
    exit 0
}

New-Item -ItemType Directory -Force -Path $codeDir, $weightsDir | Out-Null

# --- the repository ---------------------------------------------------------
# robocopy, because it copies a tree with exclusions without loading every file
# into PowerShell objects. Exit codes 0-7 are success for robocopy.
Say 'copying the repository (no .git, no models, no environments) ...'
$excludeDirs = @('.git', '.venv', 'node_modules', 'build', 'models', 'song_service\models', 'tools\node', '__pycache__', 'dist')
$args = @($Root, $repoCopy, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1',
          '/XD') + $excludeDirs + @('/XF', '*.log', '*.pyc')
& robocopy @args | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with $LASTEXITCODE" }

# --- ACE-Step source --------------------------------------------------------
$aceDir = Join-Path $codeDir 'ace-step'
if (-not (Test-Path (Join-Path $aceDir 'acestep'))) {
    Say 'fetching the ACE-Step source (needed because pip cannot install it from git offline) ...'
    $tarball = Join-Path $Stage 'ace-step.tar.gz'
    Invoke-WebRequest -Uri 'https://codeload.github.com/ace-step/ACE-Step/tar.gz/refs/heads/main' -OutFile $tarball
    $extract = Join-Path $Stage 'ace-step-x'
    if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $extract | Out-Null
    # The tar first on PATH in Git Bash cannot open a D:\ path; Windows' own tar can.
    & "$env:SystemRoot\System32\tar.exe" -xzf $tarball -C $extract
    if ($LASTEXITCODE -ne 0) { throw "tar failed with $LASTEXITCODE" }
    $inner = Get-ChildItem $extract -Directory | Select-Object -First 1
    Move-Item $inner.FullName $aceDir
    Remove-Item $tarball -Force
    Remove-Item $extract -Recurse -Force
}
Say "ACE-Step source: $aceDir"

# --- cloudflared ------------------------------------------------------------
$cloudflared = Join-Path $codeDir 'cloudflared-linux-amd64'
if (-not (Test-Path $cloudflared)) {
    Say 'fetching cloudflared for Linux (the tunnel binary the kernel uses) ...'
    Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-amd64' -OutFile $cloudflared
}
Say ("cloudflared: {0:N1} MB" -f ((Get-Item $cloudflared).Length / 1MB))

# --- the packages -----------------------------------------------------------
# Unpacked, not wheels: the kernel puts this directory on sys.path, so it never runs
# pip and never needs an index. torch is left out on purpose - Kaggle's image has
# it, and the repository pins a CUDA-index build that cannot be reproduced offline.
if ($Packages) {
    $pylibs = Join-Path $codeDir 'pylibs'
    $filtered = Join-Path $Stage 'requirements-offline.txt'
    $lines = @()
    foreach ($source in @('song_service\requirements.txt', 'voice_service\requirements-shared.txt')) {
        foreach ($line in Get-Content (Join-Path $Root $source)) {
            $trimmed = $line.Trim()
            if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
            $name = ($trimmed -split '[=<>!\[\s]', 2)[0].ToLower()
            # torch is in Kaggle's image. chatterbox-tts is left out for a different
            # reason: it pins transformers 5.2 while ACE-Step needs 4.57, so both can
            # never share one environment - and the voice model's weights come from
            # the Hub at runtime, which an offline kernel cannot reach anyway.
            # cutlet comes with a source-only C extension (mojimoji) that has no Linux
            # wheel, and ACE-Step imports it lazily for Japanese romanisation only, so
            # it is left out: Japanese lyrics fall back to what the tokenizer can do.
            if ($name -in @('torch', 'torchaudio', 'torchvision', 'chatterbox-tts', 'cutlet')) { continue }
            $lines += $trimmed
        }
    }
    # transformers 4.57.6 (pinned by ACE-Step) refuses to import with a tokenizers
    # older than 0.23.1: a run bundled with 0.22.1 warmed all the way to
    # warm_failed="tokenizers>=0.23.1,<0.24.0 is required", so the model never loaded.
    # 0.23.3 is the version Kaggle's own image carries, so nothing else shifts.
    $lines = @($lines | Where-Object { $_ -notmatch '^tokenizers' })
    $lines += 'tokenizers==0.23.3'
    Set-Content -Path $filtered -Value $lines -Encoding utf8
    Say ("unpacking {0} requirements for Python {1} / {2} (this is the slow part) ..." -f $lines.Count, $PythonVersion, $Platform)
    if (Test-Path $pylibs) { Remove-Item $pylibs -Recurse -Force }
    # --no-deps on purpose: resolving would pull torch back in through
    # pytorch_lightning and demucs, and a resolved torch drags 3 GB of CUDA wheels for
    # a card that already has it. The list is curated instead, exactly like the boot
    # cell on the card installs it.
    & uv pip install --target $pylibs --python-version $PythonVersion --python-platform $Platform `
        --no-deps --only-binary :all: --no-cache -r $filtered
    if ($LASTEXITCODE -ne 0) { throw "uv pip install failed with $LASTEXITCODE" }
    Say ("packages: {0:N0} entries, {1:N0} MB" -f (Get-ChildItem $pylibs).Count,
        ((Get-ChildItem $pylibs -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))
}

# --- the weights ------------------------------------------------------------
# Uploaded from song_service\models itself, with a dataset-metadata.json written next
# to the models (gitignored). A staging copy would be another 11 GB on disk, and a
# hardlink farm is refused on this volume, so the folder is used as it is.
$weightsSource = Join-Path $Root 'song_service\models'
if ($Weights) {
    foreach ($name in @('ACE-Step-v1-3.5B', 'Qwen2.5-1.5B-Instruct')) {
        $path = Join-Path $weightsSource $name
        if (Test-Path $path) {
            Say ("weights {0}: {1:N2} GB" -f $name,
                ((Get-ChildItem $path -Recurse -File | Measure-Object Length -Sum).Sum / 1GB))
        } else {
            Say "missing weights: $path"
        }
    }
}

# --- dataset metadata -------------------------------------------------------
function Write-Metadata($path, $slug, $title) {
    # Private: a dataset is listed publicly with the owner's name on it otherwise, and
    # the weights are only there to be mounted by this account's own kernels.
    $metadata = @{
        title = $title
        id = $slug
        isPrivate = $true
        licenses = @(@{ name = 'CC0-1.0' })
    } | ConvertTo-Json -Depth 5
    # No BOM. `Set-Content -Encoding utf8` in Windows PowerShell 5.1 prepends one, and
    # the Kaggle CLI answers a BOM-prefixed metadata file with
    # "Expecting value: line 1 column 1 (char 0)" and uploads nothing at all.
    $file = Join-Path $path 'dataset-metadata.json'
    [System.IO.File]::WriteAllText($file, $metadata, (New-Object System.Text.UTF8Encoding($false)))
}

if (-not $Owner) {
    if (Test-Path (Join-Path $Root 'build\kaggle.json')) {
        $cred = Get-Content (Join-Path $Root 'build\kaggle.json') -Raw | ConvertFrom-Json
        $Owner = ([string]$cred.username).ToLower() -replace '[^a-z0-9_-]', ''
    }
}
Say "owner handle: $Owner"

if ($Owner) {
    Write-Metadata $codeDir "$Owner/song-gpu-code" 'song gpu code'
    Write-Metadata $weightsSource "$Owner/song-gpu-weights" 'song gpu weights'
}

Say ''
Say 'next:'
Say "  powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -Packages -Weights -Create"

if ($Create) {
    if (-not $Owner) { throw 'no Kaggle owner handle: run tools\set_kaggle.ps1 first' }
    $cred = Get-Content (Join-Path $Root 'build\kaggle.json') -Raw | ConvertFrom-Json
    $env:KAGGLE_USERNAME = $cred.username
    # The token Kaggle's settings page issues today is a KGAT_ token, and CLI 2.x only
    # reads that as KAGGLE_API_TOKEN. Passed as KAGGLE_KEY it is rejected and the CLI
    # prints "generate an API token" instead of uploading anything.
    $env:KAGGLE_API_TOKEN = $cred.key
    # Create the dataset, or add a version if it already exists. A version cannot be
    # added to a dataset that is not there, which is how one 10.6 GB upload was spent
    # on nothing: the first create failed, the fallback uploaded anyway, and the
    # version call was rejected at the end because the dataset had never been made.
    $existing = (& uv tool run --from kaggle --with six kaggle datasets list -m 2>$null) -join "`n"
    $pairs = @()
    if (-not $WeightsOnly) { $pairs += , @($codeDir, 'song-gpu-code') }
    if (-not $CodeOnly) { $pairs += , @($weightsSource, 'song-gpu-weights') }
    foreach ($pair in $pairs) {
        $path = $pair[0]; $slug = "$Owner/" + $pair[1]
        $known = $existing -match [regex]::Escape($slug)
        $verb = if ($known) { 'adding a version to ' } else { 'creating ' }
        Say ($verb + $slug + ' (this uploads; it takes a while) ...')
        if ($known) {
            & uv tool run --from kaggle --with six kaggle datasets version -p $path --dir-mode zip -m 'offline pack'
        } else {
            & uv tool run --from kaggle --with six kaggle datasets create -p $path --dir-mode zip
        }
        if ($LASTEXITCODE -ne 0) { throw "the upload of $slug failed with $LASTEXITCODE" }
    }
}
