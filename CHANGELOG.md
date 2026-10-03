# Changelog

## Unreleased

### Added

- AirPlay: what shairport-sync plays shows up too (title, artist, album, artwork, progress, the device sending it), taking over from mpd while it plays.

### Changed

- The page fits any screen: upright screens (a TV on its side, the Raspberry Pi Touch Display 2) show the artwork on top and the text below, small screens (800×480) shrink the layout so the text keeps its room, big ones (1440p, 4K) grow it. `scale` still sets the size by hand.
- No progress bar stuck at 0:00 when the duration is unknown.

### Fixed

- Tracks played from a UPnP/DLNA server (upmpdcli, BubbleUPnP, MinimServer...) had no codec badge, bit depth nor Hi-Res logo: mpd gets them as http addresses, whose extension now counts like a file's.

## 1.0.0 - 2026-10-03

First release.

### Added

- Instant updates: the bridge follows mpd's own change notifications (idle) and pushes them to the page as they happen, with polling as a fallback.
- Codec badge from the file (FLAC, ALAC, MP3, AAC, DSF...), with bit depth and sample rate.
- Radios: "Live" badge, "Artist - Title" split in two lines, station name, artwork from the iTunes Search API.
- MPD password support (`MPD_PASSWORD`), and clear messages when mpd is unreachable or refuses access.
- `--check` tests the setup (mpd, password, formats, artwork, HTTPS, port, online artwork) and says how to fix what's wrong.
- Demo mode (`DEMO=1`): made-up tracks with generated artwork, no mpd needed.
- One page for English and French, following the browser's language.
- Display options in the URL: clock, "Up next" line, blurred artwork background, size (`scale`).
- Burn-in protection for OLED screens: what's on screen drifts by a few pixels every few minutes.
- Installable as a full-screen web app, and built-in HTTPS (`TLS_CERT`, `TLS_KEY`).
- Settings from a `.env` file, a systemd installer, and a Docker setup.
- Guides for HTTPS (mkcert, Tailscale, nginx) and Raspberry Pi kiosks.
- Tests (the bridge against a fake mpd, the page in Chromium), run by GitHub Actions.

### Fixed

- The format badge said FLAC for every file, and lossy files or CD rips played through 32-bit DACs got the Hi-Res logo.
- Untagged files and radios showed "Nothing playing".
- The bottom glow color came out twice too bright.
- The kiosk page could get stuck on a browser error page after its periodic reload.
- The installer failed on Debian 12 / Raspberry Pi OS, and the Docker setup served nothing.
