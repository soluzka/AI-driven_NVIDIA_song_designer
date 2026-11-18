#!/usr/bin/env bash
# One line on a fresh Linux GPU box, served from the site:
#
#   curl -fsSL https://browser.bpiersing.workers.dev/join.sh | bash -s -- \
#     --key <join key> [--token <tunnel token>] [--hostname ai.example.com]
#
# It fetches the app, installs one environment with the CUDA wheels, downloads the
# models, installs the systemd unit and starts the stack — then the machine
# publishes its own address to Cloudflare 2 and the site uses it. Nothing else is
# typed, and it comes back after every reboot.
#
# The weights are ~10 GB. Cloudflare can *store* that in R2 and stream it (see the
# /weights route in pool-worker.js) but no Worker can run it: a Worker is a 128 MB
# isolate with no GPU. This script is what turns a GPU box into an endpoint.
set -euo pipefail

ARCHIVE="${ARCHIVE:-https://codeload.github.com/soluzka/internet/zip/refs/heads/main}"
DEST="${DEST:-/opt/internet}"
KEY="${SONG_ENDPOINT_KEY:-}"
TOKEN="${TUNNEL_TOKEN:-}"
HOSTNAME_="${TUNNEL_HOSTNAME:-}"
QUICK="${QUICK_TUNNEL:-0}"

while [ $# -gt 0 ]; do
    case "$1" in
        --key) KEY="$2"; shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --hostname) HOSTNAME_="$2"; shift 2 ;;
        --quick) QUICK=1; shift ;;
        --dest) DEST="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }

if command -v nvidia-smi >/dev/null 2>&1; then
    say "gpu: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
    say 'gpu: no nvidia-smi — the models would run on the CPU, hours per song'
fi

# git and ffmpeg are the only system packages the stack needs; the CUDA libraries
# travel inside the torch wheels.
missing=()
for tool in git ffmpeg curl unzip; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ ${#missing[@]} -gt 0 ]; then
    say "installing: ${missing[*]}"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y > /dev/null 2>&1 || true
        apt-get install -y "${missing[@]}" > /dev/null 2>&1 || {
            say "could not install ${missing[*]} — run this as root, or install them first"
            exit 1
        }
    else
        say "install ${missing[*]} first (no apt-get here)"
        exit 1
    fi
fi

say "app: fetching to $DEST"
mkdir -p "$DEST"
tmp="$(mktemp -d)"
curl -fsSL "$ARCHIVE" -o "$tmp/app.zip"
unzip -q -o "$tmp/app.zip" -d "$DEST"
rm -rf "$tmp"
ROOT="$(find "$DEST" -maxdepth 2 -name install_all.ps1 -printf '%h\n' | head -1)"
if [ -z "$ROOT" ]; then
    say "the archive did not contain install_all.ps1"
    exit 1
fi
say "app: $ROOT"

export SONG_ENDPOINT_KEY="$KEY"
export TUNNEL_TOKEN="$TOKEN"
export TUNNEL_HOSTNAME="$HOSTNAME_"
export QUICK_TUNNEL="$QUICK"
export SITE_URL="${SITE_URL:-https://browser.bpiersing.workers.dev}"

say 'installing the environment and the models (this is the long part)'
"$ROOT/deploy/gpu/install.sh" --install-only

say 'installing the boot service'
"$ROOT/deploy/gpu/install_service.sh" --root "$ROOT" --user "${SUDO_USER:-root}"

say ''
say "done. systemctl status song-stack"
say "pool: ${SITE_URL}/api/song/pool"
