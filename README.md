<p align="center">
  <img src="assets/logo.png" width="96" alt="nowplaying logo">
</p>

# nowplaying

![Preview](screenshot.png)

Fullscreen Now Playing page for MPD. Shows the current track with a dynamic background gradient pulled from the album art, audio quality badges, and Hi-Res detection.

No nginx required - the bridge serves everything on port 8766.

## What it looks like

- Background gradient extracted from the album art colors
- Album art straight from mpd (embedded tags or cover file in the folder), Last.fm as an optional fallback for streams and radios
- FLAC / bit depth / sample rate badges
- Hi-Res Audio logo for anything >= 88.2 kHz or >= 24 bit
- Progress bar, elapsed and total time
- Scrolling marquee for long titles
- Dimmed overlay when paused
- Clock in the top right corner
- "Up next" line showing the next track in the queue
- Smooth progress interpolation between polls (no 2s jumps)
- Offline indicator when the bridge stops responding
- Responsive portrait layout for phones (iOS and Android): artwork centered on top, clock hidden
- Screen Wake Lock and full-screen support via add-to-home-screen, on both iOS and Android

## Requirements

- MPD
- Python 3
- Optional: a Last.fm API key for artwork on streams and radios - free at https://www.last.fm/api

## Getting started
```bash
git clone https://github.com/mat-d3v/nowplaying.git
cd nowplaying
pip install python-mpd2 requests
export LASTFM_API_KEY=your_key_here   # optional, artwork fallback for streams
python mpd-bridge.py
```

Then open http://localhost:8766 in a browser.

For the French version: http://localhost:8766/index.fr.html or http://localhost:8766?lang=fr

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| LASTFM_API_KEY | optional | Artwork fallback for streams and radios |
| ALSA_CARD | 0 | ALSA card number for audio format detection |
| MPD_HOST | 127.0.0.1 | MPD host |
| MPD_PORT | 6600 | MPD port |
| PORT | 8766 | HTTP port of the bridge |

Copy `.env.example` to `.env` and fill in your values.

## Docker

A `docker-compose.yml` is included. Optionally set `LASTFM_API_KEY` in the environment section and run:
docker compose up -d

## License

MIT
