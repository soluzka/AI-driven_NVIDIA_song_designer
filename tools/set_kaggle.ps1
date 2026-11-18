# Store the Kaggle credentials the Worker needs to start a free GPU.
#
# Kaggle's API is the only free service whose token can boot a GPU without a
# person: Colab has no such API at all. The Worker reads these two secrets and
# pushes the project's own boot kernel, which joins the pool by itself.
#
# Usage:
#   1. kaggle.com -> your avatar -> Settings -> API -> Create New Token
#      (this downloads kaggle.json, or shows a KGAT_... token)
#   2. Save the file as  build\kaggle.json  (it is already gitignored)
#   3. powershell -NoProfile -ExecutionPolicy Bypass -File tools\set_kaggle.ps1
#
# The token is never printed and never leaves this machine except into the
# Worker's secret store.

param(
    [string]$CredentialFile = (Join-Path $PSScriptRoot '..\build\kaggle.json'),
    [string]$JoinKeyFile = (Join-Path $PSScriptRoot '..\build\song_endpoint.key'),
    [string]$WorkerScript = (Join-Path $PSScriptRoot '..\build\wr.ps1')
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $CredentialFile)) {
    Write-Host "No Kaggle token file at $CredentialFile"
    Write-Host 'Download it from kaggle.com -> Settings -> API -> Create New Token, save it there, and run this again.'
    exit 1
}

$creds = Get-Content $CredentialFile -Raw | ConvertFrom-Json
$user = ($creds.username | ForEach-Object { "$_" }).Trim()
$key = ($creds.key | ForEach-Object { "$_" }).Trim()
if (-not $user -or -not $key) {
    Write-Host 'That file has no username/key in it. It should be the kaggle.json Kaggle gives you.'
    exit 1
}
if ($user -like 'PASTE_*' -or $key -like 'PASTE_*') {
    Write-Host "$CredentialFile still holds the placeholder text, not a real token."
    Write-Host 'Open it and replace the two PASTE_ values with your Kaggle username and key, or overwrite the file with the kaggle.json Kaggle downloaded.'
    exit 1
}

$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ('kaggle-' + [guid]::NewGuid().ToString('N') + '.txt')
try {
    Set-Content -Path $temporary -Value $key -Encoding ascii -NoNewline
    Get-Content $temporary | & $WorkerScript secret put KAGGLE_KEY
    Write-Host 'KAGGLE_KEY: stored'
    Set-Content -Path $temporary -Value $user -Encoding ascii -NoNewline
    Get-Content $temporary | & $WorkerScript secret put KAGGLE_USERNAME
    Write-Host "KAGGLE_USERNAME: stored ($user)"

    if (Test-Path $JoinKeyFile) {
        $joinKey = (Get-Content $JoinKeyFile -Raw).Trim()
        if ($joinKey) {
            Set-Content -Path $temporary -Value $joinKey -Encoding ascii -NoNewline
            Get-Content $temporary | & $WorkerScript secret put SONG_JOIN_KEY
            Write-Host 'SONG_JOIN_KEY: stored, so a started GPU joins the pool by itself'
        }
    } else {
        Write-Host "No $JoinKeyFile, so a started GPU will print its address instead of joining the pool"
    }
} finally {
    Remove-Item $temporary -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host 'Done. Deploy once so the Worker picks the secrets up:'
Write-Host '  powershell -NoProfile -ExecutionPolicy Bypass -File build\wr.ps1 deploy'
