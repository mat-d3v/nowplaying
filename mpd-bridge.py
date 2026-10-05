#!/usr/bin/env python3
"""nowplaying's bridge: serves the page and tells it what mpd, AirPlay and
Spotify Connect play. Settings in .env (see .env.example); the code is in
the nowplaying folder next to this file.

    python3 mpd-bridge.py                  runs the bridge
    python3 mpd-bridge.py --check          tests the setup
    python3 mpd-bridge.py --lastfm-login   lets it scrobble to Last.fm
    python3 mpd-bridge.py --version
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nowplaying.main import main  # noqa: E402

if __name__ == '__main__':
    main()
