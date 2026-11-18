#!/usr/bin/env bash
# One command on any GPU machine: this installs the whole application into one
# environment, fetches the models, starts all three services and joins the
# Cloudflare tunnel — so a GPU box anywhere becomes the AI the site uses.
#
# Cloudflare cannot install anything on your machine; it provides the door. This
# script is the install, and cloudflared is what makes the box reachable from the
# Worker at a stable address.
#
#   ./deploy/gpu/install.sh --check        report what is here, install nothing
#   ./deploy/gpu/install.sh                install, then start everything
#   ./deploy/gpu/install.sh --no-weights   packages only, skip the ~10 GB
#   ./deploy/gpu/install.sh --install-only stop before starting the services
#
# Environment it reads:
#   TUNNEL_TOKEN        a named tunnel's token (cloudflared tunnel token song-ai).
#                       Without it a throwaway trycloudflare.com address is used.
#   QUICK_TUNNEL=1      force the throwaway address even when a token is present
#   SONG_ENDPOINT_KEY   the key the Worker holds; with it the address is published
#                       to the site so browsers find this machine on their own
#   SITE_URL            where to publish (default: the live site)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
TORCH_INDEX="https://download.pytorch.org/whl/cu126"
SITE_URL="${SITE_URL:-https://browser.bpiersing.workers.dev}"

CHECK=0
WEIGHTS=1
START=1
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=1 ;;
        --no-weights) WEIGHTS=0 ;;
        --install-only) START=0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }

probe() {  # probe <module> <expression>
    "$PY" -c "import $1; print($2)" 2>/dev/null | tail -1 || true
}

# --- 0. what this machine has ------------------------------------------------
if command -v nvidia-smi >/dev/null 2>&1; then
    say "gpu:      $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
    say "gpu:      no nvidia-smi — the models will run on the CPU, hours per song"
fi

if [ "$CHECK" = "1" ]; then
    if [ ! -x "$PY" ]; then
        say "env:      $VENV is missing — run without --check to build it"
        exit 0
    fi
    say "env:      $VENV"
    say "torch:    $(probe torch "'%s cuda=%s' % (torch.__version__, torch.cuda.is_available())")"
    for module in flask flask_socketio yt_dlp librosa scipy demucs soundfile transformers; do
        say "  $(printf '%-12s' "$module")$(probe "$module" "'ok'" | sed 's/^$/MISSING/')"
    done
    if [ -d "$ROOT/song_service/models/ACE-Step-v1-3.5B" ]; then
        say "weights:  present"
    else
        say "weights:  MISSING (run without --no-weights)"
    fi
    exit 0
fi

# --- 1. one environment ------------------------------------------------------
if [ ! -x "$PY" ]; then
    say "creating $VENV"
    if command -v uv >/dev/null 2>&1; then
        uv venv "$VENV" --python 3.11 --seed
    else
        python3 -m venv "$VENV"
    fi
fi
say "installing into $VENV"

# torch first and from the CUDA index: PyPI's wheel is the CPU build, and a CPU
# build turns a three minute song into an afternoon.
"$PY" -m pip install --disable-pip-version-check torch==2.14.1+cu126 torchaudio==2.11.0+cu126 \
    --index-url "$TORCH_INDEX"
"$PY" -m pip install --disable-pip-version-check -r requirements.txt
"$PY" -m pip install --disable-pip-version-check -r downloader_service/requirements.txt
"$PY" -m pip install --disable-pip-version-check -r song_service/requirements.txt \
    --extra-index-url "$TORCH_INDEX"

for tool in git ffmpeg; do
    command -v "$tool" >/dev/null 2>&1 || say "warning: $tool is missing; ACE-Step installs from git and demucs reads audio with ffmpeg"
done

# --- 2. the models -----------------------------------------------------------
if [ "$WEIGHTS" = "1" ]; then
    say "downloading the ACE-Step checkpoint (~10 GB, once)"
    "$PY" song_service/install.py --weights || say "weights did not finish; rerun this script"
fi

say "torch: $(probe torch "'%s cuda=%s' % (torch.__version__, torch.cuda.is_available())")"

if [ "$START" = "0" ]; then
    say "installed. Start it with: APP_DIR=$ROOT $ROOT/deploy/gpu/entrypoint.sh"
    exit 0
fi

# --- 3. start everything and open the door -----------------------------------
# The same entrypoint the container uses, so the Docker path and this one run the
# services identically. APP_DIR points it at this checkout instead of /app.
export APP_DIR="$ROOT"
export SONG_HOST="${SONG_HOST:-127.0.0.1}"
export SONG_WARM="${SONG_WARM:-1}"

if [ -n "${TUNNEL_TOKEN:-}" ] && [ "${QUICK_TUNNEL:-0}" != "1" ]; then
    say "tunnel: named${TUNNEL_HOSTNAME:+ at https://$TUNNEL_HOSTNAME}"
else
    say "tunnel: quick (a trycloudflare.com address, published when it connects)"
fi
[ -n "${SONG_ENDPOINT_KEY:-}" ] && say "publishing: yes, to $SITE_URL" || say "publishing: no SONG_ENDPOINT_KEY, so the address stays local"

say "starting the services and the tunnel (Ctrl-C stops all of them)"
exec "$ROOT/deploy/gpu/entrypoint.sh"
