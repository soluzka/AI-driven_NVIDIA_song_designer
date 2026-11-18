# Store the Kaggle credentials the site needs to start a free GPU.
#
# Kaggle's API is the only free service whose token can boot a GPU without a
# person; Colab has no such API. The Worker reads these secrets, pushes this
# project's own boot kernel, and the machine that starts joins the pool by itself.
#
# What to do, once per machine:
#   1. Sign in at https://www.kaggle.com/settings/api
#   2. Press "Create New Token" - Kaggle downloads kaggle.json
#   3. Put that file here as  build\kaggle.json  (or fill in the placeholder that
#      is already there with the username and key from that same page)
#   4. powershell -NoProfile -ExecutionPolicy Bypass -File tools\set_kaggle.ps1
#   5. powershell -NoProfile -ExecutionPolicy Bypass -File build\wr.ps1 deploy
#
# The value is never printed and never echoed: it goes from the file, through
# this script's standard input, into wrangler's secret store. build\kaggle.json is
# gitignored, so it cannot reach GitHub.

param(
    [string]$CredentialFile = (Join-Path $PSScriptRoot '..\build\kaggle.json'),
    [string]$JoinKeyFile = (Join-Path $PSScriptRoot '..\build\song_endpoint.key')
)

$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$node = Join-Path $projectRoot 'tools\node\node.exe'
$wrangler = Join-Path $projectRoot 'build\wrangler-tool\node_modules\wrangler\bin\wrangler.js'

if (-not (Test-Path $node)) { throw "No bundled node at $node" }
if (-not (Test-Path $wrangler)) { throw "No wrangler at $wrangler - run build\wr.ps1 deploy once first" }

if (-not (Test-Path $CredentialFile)) {
    Write-Host "No Kaggle token file at $CredentialFile"
    Write-Host 'Sign in at https://www.kaggle.com/settings/api, press Create New Token, and save it there.'
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
    Write-Host 'Replace the two PASTE_ values with your Kaggle username and key, or overwrite the file with the kaggle.json Kaggle downloaded.'
    exit 1
}

$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ('kaggle-' + [guid]::NewGuid().ToString('N') + '.txt')

# wrangler reads a secret from standard input. Start-Process can feed it a file,
# which keeps the value out of the command line, the console and any error text -
# piping through the wrapper script failed before it reached wrangler and printed
# the key in the failure.
function Set-WorkerSecret {
    param([string]$Name, [string]$Value)

    Set-Content -Path $temporary -Value $Value -Encoding ascii -NoNewline
    $process = Start-Process -FilePath $node `
        -ArgumentList @($wrangler, 'secret', 'put', $Name) `
        -WorkingDirectory $projectRoot `
        -RedirectStandardInput $temporary `
        -NoNewWindow -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        Write-Host "$Name : failed (wrangler exit $($process.ExitCode))"
        return $false
    }
    Write-Host "$Name : stored"
    return $true
}

try {
    $ok = Set-WorkerSecret -Name 'KAGGLE_KEY' -Value $key
    if ($ok) { $ok = Set-WorkerSecret -Name 'KAGGLE_USERNAME' -Value $user }
    if ($ok) { Write-Host "  (account: $user)" }

    if (Test-Path $JoinKeyFile) {
        $joinKey = (Get-Content $JoinKeyFile -Raw).Trim()
        if ($joinKey) {
            if (Set-WorkerSecret -Name 'SONG_JOIN_KEY' -Value $joinKey) {
                Write-Host '  a started GPU will join the pool by itself'
            }
        }
    } else {
        Write-Host "No $JoinKeyFile, so a started GPU will print its address instead of joining the pool"
    }
} finally {
    Remove-Item $temporary -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host 'Next, deploy so the Worker picks the secrets up:'
Write-Host '  powershell -NoProfile -ExecutionPolicy Bypass -File build\wr.ps1 deploy'
