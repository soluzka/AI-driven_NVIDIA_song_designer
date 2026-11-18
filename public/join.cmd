@echo off
rem Download this file from the site, double-click it, and this PC becomes one of
rem the machines the AI runs on. It installs the whole application, not a part:
rem the site, the downloader, the song service, the voice service, the models and
rem the tunnel. Nothing is typed in a terminal:
rem
rem   the agent script is fetched from the site
rem   the app is fetched, installed into one environment, started
rem   the voice service gets an environment of its own (chatterbox needs torch 2.6)
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

rem The voice service is the one part that needs an environment of its own:
rem chatterbox pins torch 2.6 with numpy 1.x, which cannot live in the shared one.
rem None of the Python handling is done here. install_voice.ps1 fetches uv if the
rem machine has none, lets uv download a 3.11 interpreter, and builds the venv with
rem it - so this line works on a PC that has never had Python installed.
echo.
echo Voice service:
set "APP=%LOCALAPPDATA%\internet-gpu\AI-driven_NVIDIA_song_designer-main"
if not exist "%APP%\tools\install_voice.ps1" goto :novoice
powershell -NoProfile -ExecutionPolicy Bypass -File "%APP%\tools\install_voice.ps1" -AppDir "%APP%"
goto :done

:novoice
echo   tools\install_voice.ps1 is not in %APP%, so there was nothing to build

:done
echo.
echo Finished. This PC stays in the pool and rejoins at every logon.
pause
