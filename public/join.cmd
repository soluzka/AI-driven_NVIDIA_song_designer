@echo off
rem Download this file from the site, double-click it, and this PC becomes one of
rem the machines the AI runs on. It installs the whole application, not a part:
rem the site, the downloader, the song service, its models and the tunnel.
rem Nothing is typed in a terminal:
rem
rem   the agent script is fetched from the site
rem   the app is fetched, installed into one environment, started
rem   the machine publishes its own address, so the site uses it immediately
rem   the logon entry is registered, so a reboot rejoins on its own
rem
rem Expect about 20 GB of downloads and 20-60 minutes on a normal connection.
rem The window prints every step; leave it open until it says Done.
rem
rem The script is fetched three times from the site and then from the repository,
rem with curl as a second opinion, because one missed lookup ended the whole join:
rem "The remote name could not be resolved" and the window closed having done
rem nothing. A resolver that has just come back from sleep answers a second later.
setlocal
cd /d "%~dp0"
echo Joining this PC's GPU to the song site...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $target = Join-Path $env:TEMP 'join-song-gpu.ps1'; $sources = @('https://browser.bpiersing.workers.dev/join.ps1', 'https://raw.githubusercontent.com/soluzka/internet/main/public/join.ps1', 'https://raw.githubusercontent.com/soluzka/AI-driven_NVIDIA_song_designer/main/public/join.ps1'); $got = $false; foreach ($source in $sources) { if ($got) { break }; for ($attempt = 1; $attempt -le 3; $attempt++) { try { Invoke-WebRequest -UseBasicParsing -Uri $source -OutFile $target -TimeoutSec 30 } catch { Write-Host ('setup script: attempt ' + $attempt + ' from ' + $source + ' failed - ' + $_.Exception.Message) }; if ((Test-Path $target) -and ((Get-Item $target).Length -gt 1000)) { $got = $true; break }; try { & curl.exe -s -f -L --max-time 60 -o $target $source; if ((Test-Path $target) -and ((Get-Item $target).Length -gt 1000)) { $got = $true; break } } catch { }; Start-Sleep -Seconds 4 } }; if (-not $got) { Write-Host 'Could not download the setup script: neither the site nor GitHub resolved.'; Write-Host 'Check the connection and run this file again.'; exit 1 }; & powershell -NoProfile -ExecutionPolicy Bypass -File $target"
echo.
echo Finished. This PC stays in the pool and rejoins at every logon.
pause
