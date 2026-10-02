#!/bin/bash
set -e

echo "==> nowplaying installer"

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "ERROR: Python 3 is required"
  exit 1
fi

# Install dependencies
echo "==> Installing Python dependencies..."
pip3 install python-mpd2 requests --quiet

# Last.fm API key
if [ -z "$LASTFM_API_KEY" ]; then
  read -p "Enter your Last.fm API key: " LASTFM_API_KEY
  echo "export LASTFM_API_KEY=$LASTFM_API_KEY" >> ~/.bashrc
  export LASTFM_API_KEY
fi

# Systemd service
echo "==> Installing systemd service..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

sudo tee /etc/systemd/system/mpd-bridge.service > /dev/null << EOF
[Unit]
Description=MPD Bridge for nowplaying
After=network.target

[Service]
ExecStart=/usr/bin/python3 $SCRIPT_DIR/mpd-bridge.py
Restart=always
Environment=LASTFM_API_KEY=$LASTFM_API_KEY

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mpd-bridge
sudo systemctl start mpd-bridge

echo ""
echo "==> Done! Bridge running on port 8766"
echo "==> Now configure your web server to serve index.html"
echo "==> and proxy /mpd/ to http://localhost:8766/"
