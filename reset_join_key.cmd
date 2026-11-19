@echo off
rem Set the key that lets this PC's GPU connect to the song site.
rem
rem Double-click it, or run it from any folder: like join.cmd, it carries no logic of its
rem own - it fetches the script from GitHub and runs that, so this one file is all you need.
rem
rem   reset_join_key.cmd                 type the key you want, then Enter
rem   reset_join_key.cmd -Key "abc..."   set it in one line
rem   reset_join_key.cmd -Show           print the key this PC holds now
rem
rem It writes <app>\build\song_endpoint.key - the GPU connect key - and, on a PC signed
rem in to the Cloudflare account that owns the Workers, sets the same key there too: JOIN_KEY
rem on the pool Worker and POOL_JOIN_KEY on the site. Without that half a card is refused
rem with "Wrong join key" and never appears in the pool. The song key is left alone.
setlocal
cd /d "%~dp0"
echo Fetching the reset script from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; $target = Join-Path $env:TEMP 'reset-join-key.ps1'; Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/soluzka/AI-driven_NVIDIA_song_designer/main/tools/reset_join_key.ps1' -OutFile $target; & powershell -NoProfile -ExecutionPolicy Bypass -File $target %*"
rem Pause on the way out either way: double-clicked, this window is the only place the
rem key and the result appear.
pause
