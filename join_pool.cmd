@echo off
rem Put this computer's GPU in the pool, in one command.
rem
rem   join_pool.cmd            install if needed, start the models, join the pool
rem   join_pool.cmd -NoStart   join with the services that are already running
rem
rem join_pool.ps1 holds the work and the reasons; this file exists so the command is
rem one word to type, and so a double-click does the same thing.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0join_pool.ps1" %*
if errorlevel 1 pause
