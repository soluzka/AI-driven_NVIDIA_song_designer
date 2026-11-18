@echo off
rem Start the AI by hand, installing anything it still needs first.
rem
rem What this CMD does, in order, all in this window:
rem   1. builds the voice service's environment if it is missing (once, a few GB)
rem   2. starts the site, the downloader, the song service, the voice service and
rem      the tunnel from the installed copy
rem   3. re-announces this PC's address to the site and the pool, so neither is
rem      left holding an address the tunnel has since replaced
rem
rem The logon launcher publishes once, at start-up; step 3 is the part that is
rem missing from it, which is why a machine that is up could still be absent from
rem the list. The voice environment is built here too, because the launcher only
rem starts the voice service when its venv already exists.
rem
rem It uses the installed copy under %LOCALAPPDATA%\internet-gpu when that exists,
rem and the folder it sits in otherwise, so running it from the source tree does
rem not start a second set of services.
setlocal
set "APP=%LOCALAPPDATA%\internet-gpu\AI-driven_NVIDIA_song_designer-main"
if not exist "%APP%\tools\start_services.ps1" set "APP=%~dp0.."

echo Starting the AI from %APP%

if exist "%APP%\voice_service\install.py" if not exist "%APP%\voice_service\.venv\Scripts\python.exe" (
    echo.
    echo The voice service has no environment yet. Building it now, once.
    echo It downloads a few GB and prints pip output for several minutes.
    echo.
    "%APP%\.venv\Scripts\python.exe" -m venv "%APP%\voice_service\.venv"
    if errorlevel 1 echo   could not create voice_service\.venv; the rest will still start
    "%APP%\voice_service\.venv\Scripts\python.exe" "%APP%\voice_service\install.py"
    if errorlevel 1 echo   the voice environment did not finish; run this CMD again to repair it
)

echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%APP%\tools\start_services.ps1"
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%APP%\tools\republish_address.ps1"
echo.
echo Leave this window open only if you want to watch it; the services keep running.
pause
