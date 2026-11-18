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
rem The installer above builds it too; this repeats the check here so the window
rem says plainly whether it is there, and repairs it if the install stopped early.
echo.
echo Voice service:
set "APP=%LOCALAPPDATA%\internet-gpu\AI-driven_NVIDIA_song_designer-main"
if not exist "%APP%\voice_service\install.py" goto :novoice
if exist "%APP%\voice_service\.venv\Scripts\python.exe" (
    echo   its environment is in place
    goto :done
)
echo   no environment yet: building it now, once. This downloads a few GB.
"%APP%\.venv\Scripts\python.exe" -m venv "%APP%\voice_service\.venv"
if errorlevel 1 echo   could not create voice_service\.venv; the rest of the app is installed
"%APP%\voice_service\.venv\Scripts\python.exe" "%APP%\voice_service\install.py"
if errorlevel 1 echo   the voice environment did not finish; run this file again to repair it
goto :done

:novoice
echo   voice_service\install.py is not in %APP%, so there was nothing to build

:done
echo.
echo Finished. This PC stays in the pool and rejoins at every logon.
pause
