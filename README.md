<p align="center">
  <img src="assets/logo.png" width="96" alt="nowplaying logo">
</p>

# nowplaying

<p align="center">
  <a href="https://cdn.jsdelivr.net/gh/mat-d3v/nowplaying@main/assets/nowplaying-presentation.mp4">
    <img src="assets/presentation-poster.jpg" width="800" alt="Watch the NowPlaying presentation video">
  </a>
  <br>
  <sub>▶ <a href="https://cdn.jsdelivr.net/gh/mat-d3v/nowplaying@main/assets/nowplaying-presentation.mp4">Watch the presentation video</a> (52 s, with sound)</sub>
</p>

![Preview](screenshot.png)

Fullscreen Now Playing page for MPD. Shows the current track with a dynamic background gradient pulled from the album art, audio quality badges, and Hi-Res detection.

No nginx and no Python packages required - the bridge only uses the standard library and serves everything on port 8766.

## What it looks like

- Background gradient extracted from the album art colors
- Album art straight from mpd (embedded tags or cover file in the folder), Last.fm as an optional fallback when mpd has none (needs artist and album tags)
- Codec badge (FLAC, ALAC, MP3, AAC...) with bit depth and sample rate
- Hi-Res Audio logo for lossless files >= 88.2 kHz or >= 24 bit, and for DSD
- Untagged files and radios show the file or station name
- Progress bar, elapsed and total time
- Scrolling marquee for long titles
- Dimmed overlay when paused
- Clock in the top right corner
- "Up next" line showing the next track in the queue
- Smooth progress interpolation between polls (no 2s jumps)
- Offline indicator when the bridge stops responding
- Responsive portrait layout for phones (iOS and Android): artwork centered on top, clock hidden
- Screen Wake Lock and full-screen support via add-to-home-screen, on both iOS and Android (Wake Lock needs HTTPS or localhost, see [below](#keeping-the-screen-on))

## Requirements

- MPD
- Python 3, nothing else to install
- Optional: a Last.fm API key for artwork on streams and radios - free at https://www.last.fm/api

## Getting started

```bash
git clone https://github.com/mat-d3v/nowplaying.git
cd nowplaying
cp .env.example .env   # optional: settings, e.g. your Last.fm API key
python3 mpd-bridge.py
```

Then open http://localhost:8766 in a browser, or `http://<machine-ip>:8766` from a phone or tablet.

For the French version: http://localhost:8766/index.fr.html or http://localhost:8766/?lang=fr

## Configuration

Settings come from environment variables or from a `.env` file next to `mpd-bridge.py` (copy `.env.example`). Environment variables take precedence over `.env`.

| Variable | Default | Description |
|----------|---------|-------------|
| LASTFM_API_KEY | empty (off) | Artwork fallback when mpd has none (streams, radios) |
| MPD_HOST | 127.0.0.1 | MPD host |
| MPD_PORT | 6600 | MPD port |
| PORT | 8766 | HTTP port of the bridge |
| ALSA_CARD | 0 | ALSA card read for the audio format when mpd doesn't report it |

## Run it as a service (systemd)

```bash
./install.sh
```

Creates `.env` (asks for an optional Last.fm API key) and installs an `mpd-bridge` service that runs as your user and starts at boot. Run it again after a `git pull` to restart the service on the new version.

## Docker

```bash
docker compose up -d
```

The container uses the host network: the bridge reaches mpd on `127.0.0.1` and listens on the host's port 8766. Settings come from `.env`, as with a local install. Host networking works out of the box on Linux; Docker Desktop (macOS, Windows) needs it enabled in its settings.

## Keeping the screen on

Browsers only grant the Screen Wake Lock to secure pages: HTTPS, or `localhost`. A phone or tablet opening `http://192.168.x.x:8766` gets the page but not the Wake Lock: turn off auto-lock on the device, or put the bridge behind an HTTPS reverse proxy (`nginx.conf` is a starting point).

## License

[MIT](LICENSE)
