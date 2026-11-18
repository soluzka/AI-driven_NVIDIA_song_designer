@echo off
rem Start the AI by hand, and re-announce this machine while doing it.
rem
rem The logon launcher publishes this PC's tunnel once, when it starts. If the
rem tunnel has been restarted since, or the site's list has aged, the address the
rem site holds can be an old one. This does the start and then says "still here,
rem this is my address" in the same run, so a machine that is up is never missing
rem from the list.
rem
rem Double-click it, or run it from any shell. It uses the installed copy under
rem %LOCALAPPDATA%\internet-gpu when that exists, and this folder otherwise, so
rem running it from the source tree does not start a second set of services.
setlocal
set "APP=%LOCALAPPDATA%\internet-gpu\AI-driven_NVIDIA_song_designer-main"
if not exist "%APP%\tools\start_services.ps1" set "APP=%~dp0.."

echo Starting the AI from %APP%
powershell -NoProfile -ExecutionPolicy Bypass -File "%APP%\tools\start_services.ps1"
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%APP%\tools\republish_address.ps1"
echo.
echo Leave this window open only if you want to watch it; the services keep running.
pause
