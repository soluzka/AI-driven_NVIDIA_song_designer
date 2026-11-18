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
setlocal
cd /d "%~dp0"
echo Joining this PC's GPU to the song site...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; $target = Join-Path $env:TEMP 'join-song-gpu.ps1'; Invoke-WebRequest -UseBasicParsing -Uri 'https://browser.bpiersing.workers.dev/join.ps1' -OutFile $target; & powershell -NoProfile -ExecutionPolicy Bypass -File $target"
echo.
echo Finished. This PC stays in the pool and rejoins at every logon.
pause
