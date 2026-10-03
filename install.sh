#!/bin/bash
set -e

echo "==> nowplaying installer"

# Check Python and systemd
if ! command -v python3 &>/dev/null; then
  echo "ERROR: Python 3 is required"
  exit 1
fi
if ! command -v systemctl &>/dev/null; then
  echo "ERROR: systemd is required to install the service"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
PYTHON="$(command -v python3)"
# The service runs as the user who launched the installer, not as root
RUN_USER="${SUDO_USER:-$(id -un)}"

# No Python packages to install: the bridge only uses the standard library.
# Settings go to .env, read by the bridge at startup.
if [ ! -f "$ENV_FILE" ]; then
  cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
  if [ -z "$LASTFM_API_KEY" ]; then
    read -r -p "Last.fm API key (optional, more artwork besides iTunes - press Enter to skip): " LASTFM_API_KEY || true
  fi
  if [ -n "$LASTFM_API_KEY" ]; then
    sed -i "s|^LASTFM_API_KEY=.*|LASTFM_API_KEY=$LASTFM_API_KEY|" "$ENV_FILE"
  fi
  if [ -z "$MPD_PASSWORD" ]; then
    read -r -s -p "mpd password (optional - press Enter if mpd has none): " MPD_PASSWORD || true
    echo
  fi
  if [ -n "$MPD_PASSWORD" ]; then
    # Appended rather than substituted: a password may contain any character
    printf 'MPD_PASSWORD=%s\n' "$MPD_PASSWORD" >> "$ENV_FILE"
  fi
  if [ "$(id -u)" -eq 0 ] && [ -n "$SUDO_USER" ]; then
    chown "$SUDO_USER" "$ENV_FILE"
  fi
  echo "==> Settings written to $ENV_FILE"
else
  echo "==> Keeping existing $ENV_FILE"
fi

# Systemd service
echo "==> Installing systemd service..."

sudo tee /etc/systemd/system/mpd-bridge.service > /dev/null << UNIT
[Unit]
Description=MPD Bridge for nowplaying
After=network.target mpd.service

[Service]
User=$RUN_USER
ExecStart=$PYTHON "$SCRIPT_DIR/mpd-bridge.py"
Restart=always

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable mpd-bridge
# restart rather than start: running the installer again (after a git pull)
# picks up the new version
sudo systemctl restart mpd-bridge

PORT="$(grep -E '^PORT=' "$ENV_FILE" | cut -d= -f2)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
SCHEME=http
if grep -qE '^TLS_CERT=.+' "$ENV_FILE"; then
  SCHEME=https
fi
echo ""
echo "==> Done! Bridge running as $RUN_USER"
echo "==> Open $SCHEME://${IP:-localhost}:${PORT:-8766} in a browser"
echo ""
echo "==> Checking the setup (run it again any time: python3 mpd-bridge.py --check)"
"$PYTHON" "$SCRIPT_DIR/mpd-bridge.py" --check || true
