param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
Set-Location 'D:/scrape_videos/scrape_videos/scrape/scrape/my_app/scrape'

# --no-check belongs to this wrapper, not to wrangler: take it out before forwarding.
$forward = @($Args | Where-Object { $_ -ne '--no-check' })
$skipCheck = $Args -contains '--no-check'

& tools\node\node.exe build\wrangler-tool\node_modules\wrangler\bin\wrangler.js @forward
$code = $LASTEXITCODE

# A site deploy is only finished once the page it just published has been measured. The
# voice fit once shipped a rumble nothing caught until it was heard, so a deploy now renders
# the test tone through the deployed fitRender and fails on it. Skipped for the pool worker
# (--config) and for anything that is not a deploy, and --no-check skips it on purpose.
if ($code -eq 0 -and -not $skipCheck -and $forward.Count -gt 0 -and $forward[0] -eq 'deploy' -and -not ($forward -contains '--config')) {
    Write-Host ''
    Write-Host 'Checking the voice fit low end on the deployed page...'
    # FIT_CHECK_URL points the check at a preview or a deliberately broken page;
    # without it the check measures the production address it just deployed.
    $checkArgs = @('tools\check_fit_low_end.py')
    if ($env:FIT_CHECK_URL) { $checkArgs += @('--url', $env:FIT_CHECK_URL) }
    & '.venv\Scripts\python.exe' @checkArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'The deploy went out, but the low-end check failed: the page that is live can rumble.' -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

exit $code
