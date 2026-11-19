@echo off
rem Set the join key that join.cmd asks for.
rem
rem   reset_join_key.cmd                 type the key you want, then Enter
rem   reset_join_key.cmd -Key "abc..."   set it in one line
rem   reset_join_key.cmd -Show           print the key this machine holds now
rem
rem It writes build\song_endpoint.key and updates both Workers that check the key, so the
rem machine joining with join.cmd has to paste this one. tools\reset_join_key.ps1 holds the
rem detail; this file exists so the command is one word to type.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\reset_join_key.ps1" %*
if errorlevel 1 pause
