@echo off
rem Double-click this to run the song model on this PC's NVIDIA card - and only
rem the model. No site, no downloader, no search index, no repository clone.
rem
rem It fetches its own Python 3.11 and git if the machine has neither, installs
rem the CUDA build of torch, downloads the ~10 GB of weights, starts the service
rem and a tunnel, and registers a logon entry so it comes back by itself.
rem
rem No administrator prompt, and nothing has to be installed first.
setlocal
cd /d "%TEMP%"
echo Downloading the song-only installer...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; $target = Join-Path $env:TEMP 'song-gpu.ps1'; Invoke-WebRequest -UseBasicParsing -Uri 'https://browser.bpiersing.workers.dev/song-gpu.ps1' -OutFile $target; & powershell -NoProfile -ExecutionPolicy Bypass -File $target"
echo.
echo Done. Close this window: the model keeps running in the background.
pause
