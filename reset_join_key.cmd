@echo off
rem Set the join key that join.cmd asks for.
rem
rem   reset_join_key.cmd                 type the key you want, then Enter
rem   reset_join_key.cmd -Key "abc..."   set it in one line
rem   reset_join_key.cmd -Show           print the key this machine holds now
rem
rem It writes build\song_endpoint.key and sets the key the site checks when a machine
rem publishes its address, so the next machine to connect has to use this one.
rem tools\reset_join_key.ps1 holds the detail; this file exists so the command is one word
rem to type.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\reset_join_key.ps1" %*
rem Pause on the way out either way: double-clicked, this window is the only place the
rem key and the result appear, and it used to vanish the instant the script succeeded.
pause
