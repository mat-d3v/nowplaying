# Changelog

## Unreleased

### Changed

- The bridge's code is split into modules, in the `nowplaying` folder next to `mpd-bridge.py`, which still starts it: same commands, same settings. An install that copies files by hand needs that folder too (`git pull` brings it).

## 1.1.1 - 2026-10-05

### Fixed

- Qobuz and other streaming services through a UPnP controller: their FLAC badge and Hi-Res logo also show when the address gives the format in its parameters (`?format=flac`, `?file=01.flac`, Qobuz's own `fmt=27`) or as its last part (`.../flac`). And an address that doesn't say at all, but plays integer samples at 88.2 kHz or more (only lossless files give those), gets its bit depth and the Hi-Res logo, without a codec badge.
- Over HTTPS, answers could reach some clients cut short (Python 3.7's, as on Raspberry Pi OS Buster): every answer now gives its length. The bridge's tests also run on Python 3.7 and 3.9.

## 1.1.0 - 2026-10-03

### Added

- AirPlay: what shairport-sync plays shows up too (title, artist, album, artwork, progress, the device sending it), taking over from mpd while it plays.
- Spotify Connect: what raspotify (librespot) plays shows up too, the same way, from librespot's events passed on by `spotify-event.py`. When AirPlay and Spotify both play, the last one started shows.
- VU meters (`?vu=1`): two needles that move with what mpd plays, with a peak lamp, from mpd's "fifo" output, read only while a page shows them. The demo mode has made-up levels.
- Settings page (`/settings`), made for a phone: language, clock, background, size, VU meters, "Up next" line, burn-in protection, with a live preview. Saved (in `DATA_DIR`), they show on every screen at once; a screen's own address still wins.
- Listening history (`/history`): what played, by day, from mpd, AirPlay or Spotify, with the track playing now; a track counts once it played for half its length or 4 minutes. Kept in `DATA_DIR`. Scrobbling to ListenBrainz or Last.fm as an option (`--lastfm-login` sets Last.fm up); plays that can't be sent go again later.
- Artwork for tracks mpd has none for (no embedded picture, no cover file): looked up on the iTunes Search API, by album then by song, like radios. Only a result with the same names counts: no artwork rather than someone else's.
- A Docker image on ghcr.io, published with each release for PCs and Raspberry Pis (amd64, arm64, armv7): `docker run ... ghcr.io/mat-d3v/nowplaying`, nothing to clone. Small (Alpine, 84 MB), and not run as root.

### Changed

- The page fits any screen: upright screens (a TV on its side, the Raspberry Pi Touch Display 2) show the artwork on top and the text below, small screens (800×480) shrink the layout so the text keeps its room, big ones (1440p, 4K) grow it. `scale` still sets the size by hand.
- No progress bar stuck at 0:00 when the duration is unknown.
- Online artwork lookups (iTunes, Last.fm) no longer hold up the title: it shows at once, the artwork follows. A lookup that failed, with no network yet, runs again 5 minutes later instead of never.

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
