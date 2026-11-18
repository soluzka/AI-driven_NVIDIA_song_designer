@echo off
rem Double-click this. Nothing else is typed, ever, and nothing has to be
rem installed first: if this PC has no Python 3.11 and no git, both are fetched
rem as portable copies into tools\bin (no installer, no administrator prompt).
rem
rem   one .venv with the site, the downloader and the song service
rem   the ACE-Step / Hugging Face weights (~10 GB, once)
rem   the services started now
rem   and the logon entry that brings them back by themselves after a reboot
rem
rem -ExecutionPolicy Bypass is why this works without a terminal: the machine's
rem policy blocks scripts by default, and the user is not asked to change it.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_all.ps1" -Start -Autostart
echo.
echo Done. The services are running and will come back at every logon.
echo Close this window - the site is already answering.
pause
