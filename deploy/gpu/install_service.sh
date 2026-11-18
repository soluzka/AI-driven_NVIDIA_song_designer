#!/usr/bin/env bash
# Makes a Linux GPU machine run the whole stack at boot, with no terminal and no
# one logged in. Run once, as root, after install.sh has filled the environment:
#
#   sudo ./deploy/gpu/install_service.sh
#   sudo ./deploy/gpu/install_service.sh --user someone --root /srv/internet
#
# It writes /etc/systemd/system/song-stack.service from the template beside this
# script, enables it, and starts it now. The stack then comes back after a reboot
# on its own, which is what "click the site and it just works" needs from the GPU
# side: the machine publishes its address to the Worker on every start.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
USER_NAME="${SUDO_USER:-$(id -un)}"
ENV_FILE="/etc/song-stack.env"
UNIT="/etc/systemd/system/song-stack.service"

while [ $# -gt 0 ]; do
    case "$1" in
        --user) USER_NAME="$2"; shift 2 ;;
        --root) ROOT="$(cd "$2" && pwd)"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" != "0" ]; then
    echo "run this with sudo: it writes $UNIT and enables a system unit" >&2
    exit 1
fi

if [ ! -x "$ROOT/deploy/gpu/entrypoint.sh" ]; then
    chmod +x "$ROOT/deploy/gpu/entrypoint.sh" "$ROOT/deploy/gpu/install.sh" || true
fi

# The template is plain text with two placeholders, so a path with slashes cannot
# break a sed under any locale.
sed -e "s|__ROOT__|$ROOT|g" -e "s|__USER__|$USER_NAME|g" \
    "$HERE/song-stack.service" > "$UNIT"

if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'CONF'
# Read by song-stack.service. Fill these in and run: systemctl restart song-stack
#
# A named tunnel keeps one hostname for good (cloudflared tunnel token song-ai).
TUNNEL_TOKEN=
# The hostname that token is bound to, so the machine publishes its own address.
TUNNEL_HOSTNAME=
# Without a token, 1 opens a throwaway trycloudflare.com address instead.
QUICK_TUNNEL=0
# The key the Worker holds (build\song_endpoint.key on the PC that deploys it).
SONG_ENDPOINT_KEY=
SITE_URL=https://browser.bpiersing.workers.dev
CONF
    chmod 600 "$ENV_FILE"
    echo "wrote $ENV_FILE - fill in TUNNEL_TOKEN and SONG_ENDPOINT_KEY"
fi

systemctl daemon-reload
systemctl enable song-stack.service
systemctl restart song-stack.service

echo "song-stack is enabled and running from $ROOT as $USER_NAME"
echo "  systemctl status song-stack"
echo "  journalctl -u song-stack -f"
