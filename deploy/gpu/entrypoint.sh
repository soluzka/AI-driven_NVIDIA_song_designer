#!/usr/bin/env bash
# Starts the whole application and the tunnel Cloudflare reaches it through.
#
# Three services, one environment:
#
#   5001  the site (Flask, Socket.IO, the SQLite index)
#   8080  the downloader (Flask + yt-dlp)
#   8090  the song service (ACE-Step on the GPU, the lyric model, demucs, librosa)
#
# and cloudflared: with TUNNEL_TOKEN it joins a named tunnel (one hostname for
# good); with QUICK_TUNNEL=1 it opens a throwaway trycloudflare.com address. With
# SONG_ENDPOINT_KEY set, whichever address it got is published to the Worker, so
# browsers find this machine by themselves and every model call goes through
# Cloudflare instead of needing an address pasted into a page.
set -euo pipefail

cd "${APP_DIR:-/app}"
mkdir -p "${HF_HOME:-/data/hf}" "${TORCH_HOME:-/data/torch}" song_service/logs song_service/models

SITE_PORT="${SITE_PORT:-5001}"
DOWNLOAD_PORT="${DOWNLOAD_PORT:-8080}"
SONG_PORT="${SONG_PORT:-8090}"
SITE_URL="${SITE_URL:-https://browser.bpiersing.workers.dev}"

pids=()

start() {
    local name="$1"; shift
    "$@" &
    local pid=$!
    pids+=("$pid")
    echo "[stack] $name started (pid $pid)"
}

shutdown() {
    echo "[stack] stopping"
    for pid in "${pids[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait || true
}
trap shutdown TERM INT

# Publishes this machine's address to the Worker's KV, which is what the page
# reads (/api/song/endpoint) and what worker.js proxies the model calls to.
publish() {
    local url="$1"
    [ -n "${SONG_ENDPOINT_KEY:-}" ] || return 0
    [ -n "$url" ] || return 0
    if curl -fsS -m 20 -X POST "$SITE_URL/api/song/endpoint" \
            -H "x-song-key: $SONG_ENDPOINT_KEY" \
            -H 'content-type: application/json' \
            -d "{\"url\":\"$url\"}" > /dev/null; then
        echo "[stack] published $url"
    else
        echo "[stack] could not publish $url to $SITE_URL"
    fi
}

start "site" env PORT="$SITE_PORT" python app.py --role site
start "downloader" bash -c "cd downloader_service && exec env PORT=$DOWNLOAD_PORT python app.py --role downloader"
start "song service" bash -c "cd song_service && exec env SONG_PORT=$SONG_PORT SONG_HOST=${SONG_HOST:-0.0.0.0} SONG_WARM=${SONG_WARM:-1} python app.py --role song"

# The site takes a few seconds; the song service imports torch and warms 3.5B
# parameters, which is a minute or two on a cold card. Neither blocks the tunnel.
if [ -n "${TUNNEL_TOKEN:-}" ] && [ "${QUICK_TUNNEL:-0}" != "1" ]; then
    start "cloudflared (named tunnel)" cloudflared tunnel --no-autoupdate --loglevel info run --token "$TUNNEL_TOKEN"
    if [ -n "${TUNNEL_HOSTNAME:-}" ]; then
        publish "https://$TUNNEL_HOSTNAME"
    fi
elif [ "${QUICK_TUNNEL:-0}" = "1" ]; then
    TUNNEL_LOG="${TMPDIR:-/tmp}/tunnel.log"
    rm -f "$TUNNEL_LOG"
    start "cloudflared (quick tunnel)" \
        cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$SONG_PORT" --logfile "$TUNNEL_LOG" --loglevel info
    for _ in $(seq 1 40); do
        url="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)"
        [ -n "$url" ] && break
        sleep 2
    done
    publish "${url:-}"
else
    echo "[stack] no TUNNEL_TOKEN and QUICK_TUNNEL is not 1: the services are local to this machine only"
fi

# If any process dies the container should not pretend to be healthy: -n returns
# on the first one, the trap stops the rest, and the restart policy brings it back.
wait -n
shutdown
exit 1
