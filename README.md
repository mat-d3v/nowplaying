<p align="center">
  <img src="assets/logo.png" width="96" alt="nowplaying logo">
</p>

# nowplaying

<p align="center">
  <a href="https://github.com/mat-d3v/nowplaying/actions/workflows/ci.yml"><img src="https://github.com/mat-d3v/nowplaying/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/mat-d3v/nowplaying/releases/latest"><img src="https://img.shields.io/github/v/release/mat-d3v/nowplaying" alt="Latest release"></a>
</p>

<p align="center">
  <a href="https://cdn.jsdelivr.net/gh/mat-d3v/nowplaying@main/assets/nowplaying-presentation.mp4">
    <img src="assets/presentation-poster.jpg" width="800" alt="Watch the NowPlaying presentation video">
  </a>
  <br>
  <sub>▶ <a href="https://cdn.jsdelivr.net/gh/mat-d3v/nowplaying@main/assets/nowplaying-presentation.mp4">Watch the presentation video</a> (52 s, with sound)</sub>
</p>

![Preview](screenshot.jpg)

Fullscreen Now Playing page for MPD. Shows the current track with a dynamic background gradient pulled from the album art, audio quality badges, and Hi-Res detection.

No nginx and no Python packages required - the bridge only uses the standard library and serves everything on port 8766.

## What it looks like

- Background gradient extracted from the album art colors (or the blurred artwork, see [display options](#display-options))
- Album art straight from mpd (embedded tags or cover file in the folder). When mpd has none, and for radios, from the iTunes Search API (free, no key), or Last.fm with an API key. The title shows at once, the artwork follows
- Codec badge (FLAC, ALAC, MP3, AAC...) with bit depth and sample rate, also for tracks played from a UPnP/DLNA server or a streaming service through it (upmpdcli, BubbleUPnP, MinimServer, Qobuz...)
- Hi-Res Audio logo for lossless files >= 88.2 kHz or >= 24 bit, and for DSD
- VU meters, as an option: two needles that move with what mpd plays, with a peak lamp
- Radios: "Live" badge, "Artist - Title" split in two lines, station name below
- Untagged files show their file name
- AirPlay: what's played from an iPhone, iPad or Mac through shairport-sync shows up too
- Spotify Connect: what's played from the Spotify app through raspotify (librespot) shows up too
- Bluetooth: what a phone plays through the machine, as a Bluetooth speaker, shows up too
- Instant updates: the bridge pushes every change as it happens (track, pause, seek, queue)
- Progress bar, elapsed and total time, smoothly interpolated
- Scrolling marquee for long titles
- Dimmed overlay when paused
- Clock in the top right corner
- "Up next" line showing the next track in the queue
- Clear messages when mpd is unreachable or needs a password, and an offline indicator when the bridge stops responding
- English, French, German, Spanish, Italian and Dutch, following the browser's language
- Listening history: what played, by day, whatever the player; scrobbling to ListenBrainz or Last.fm, as an option
- Home Assistant: what's playing, its quality and its artwork, over MQTT; the entities show up by themselves
- A settings page for your phone: language, clock, background, size, VU meters... with a preview; saved, every screen shows them at once
- Fits any screen, from 800×480 to 4K: small screens shrink the layout so the text keeps its room, big ones grow it
- Upright screens (a TV on its side, the Raspberry Pi Touch Display 2, phones): artwork on top, text below
- Full-screen app from the home screen (on Android, this needs HTTPS or localhost), with Screen Wake Lock to keep the screen on (HTTPS or localhost too, see [below](#https-keeping-the-screen-on-and-installing-the-app))

## Requirements

- MPD
- Python 3.7 or later, nothing else to install
- Optional: a Last.fm API key - free at https://www.last.fm/api

## Getting started

```bash
git clone https://github.com/mat-d3v/nowplaying.git
cd nowplaying
cp .env.example .env   # optional: settings, e.g. your mpd password
python3 mpd-bridge.py
```

Then open http://localhost:8766 in a browser, or `http://<machine-ip>:8766` from a phone or tablet.

The page follows the browser's language: English, French, German, Spanish, Italian or Dutch. To force one: http://localhost:8766/?lang=fr (or `en`, `de`, `es`, `it`, `nl`; http://localhost:8766/index.fr.html still works too).

## Try it without mpd

```bash
DEMO=1 python3 mpd-bridge.py
```

Plays a few made-up tracks in a loop (Hi-Res FLAC, ALAC, MP3, a radio, DSD), one every 20 seconds (`DEMO_STEP` changes that), with generated artwork: handy to try the page, or to take screenshots like the one above.

## Check your setup

```bash
python3 mpd-bridge.py --check
```

Tests the connection to mpd and its password, the current track's format and artwork, instant updates, HTTPS, the port and online artwork, and says how to fix what's wrong. With Docker: `docker compose run --rm nowplaying python mpd-bridge.py --check`.

## Display options

From a phone or a computer, the settings page sets them for every screen, with a preview: http://localhost:8766/settings (or `http://<machine-ip>:8766/settings`). Saved, they show on the screens at once. At the bottom of the page: the version running, and a link when a newer one is out.

A screen can also have its own, in its address, combined with `&` - for example http://localhost:8766/?bg=blur&clock=12. They win over the saved settings.

| Option | Effect |
|--------|--------|
| `lang=fr`, `lang=en`... | Force the language: `en`, `fr`, `de`, `es`, `it` or `nl` |
| `clock=0` | Hide the clock |
| `clock=12` | 12-hour clock |
| `next=0` | Hide the "Up next" line |
| `bg=blur` | Blurred artwork as the background, instead of the color gradient |
| `scale=1.5` | Size set by hand (from `0.5` to `3`), e.g. bigger for a screen seen from afar. By default, the page fits itself to the screen |
| `vu=1` | Two VU meters under the badges, moving with what mpd plays (needs a "fifo" output in `mpd.conf`, see [VU meters](#vu-meters)) |
| `shift=0` | No burn-in protection. By default, what's on screen drifts by a few pixels every 3 minutes, too slowly to notice, so OLED screens don't keep a ghost of it |

## Configuration

Settings come from environment variables or from a `.env` file next to `mpd-bridge.py` (copy `.env.example`). Environment variables take precedence over `.env`.

| Variable | Default | Description |
|----------|---------|-------------|
| MPD_HOST | 127.0.0.1 | MPD host |
| MPD_PORT | 6600 | MPD port |
| MPD_PASSWORD | empty | MPD password, if `mpd.conf` sets one |
| PORT | 8766 | Port of the bridge |
| ITUNES_ARTWORK | 1 (on) | Artwork from the iTunes Search API, for radios and tracks mpd has no artwork for. Only the artist, title and album are sent; `0` turns it off |
| LASTFM_API_KEY | empty (off) | Last.fm artwork fallback when mpd has none (needs artist and album tags) |
| TLS_CERT, TLS_KEY | empty | Certificate and private key (PEM) to serve HTTPS, see [below](#https-keeping-the-screen-on-and-installing-the-app) |
| ALSA_CARD | 0 | ALSA card read for the audio format when mpd doesn't report it |
| SHAIRPORT_PIPE | /tmp/shairport-sync-metadata | shairport-sync's metadata pipe, for AirPlay; empty turns AirPlay off |
| MPD_FIFO | /tmp/mpd.fifo | mpd's fifo output, read for the VU meters; empty turns them off |
| SPOTIFY | 1 (on) | Spotify Connect: what librespot (raspotify) plays, from its events; `0` turns it off |
| BLUETOOTH | 1 (on) | Bluetooth: what a phone streaming here plays, from BlueZ, see [Bluetooth](#bluetooth); `0` turns it off |
| DATA_DIR | this folder | Where the bridge saves the display settings (`settings.json`) and the listening history (`history.jsonl`) |
| HISTORY | 1 (on) | The listening history; `0` turns it off |
| LISTENBRAINZ_TOKEN | empty (off) | Scrobbling to ListenBrainz: your user token, see [below](#listening-history) |
| LASTFM_API_SECRET, LASTFM_SESSION_KEY | empty (off) | Scrobbling to Last.fm, with `LASTFM_API_KEY`, see [below](#listening-history) |
| SCROBBLE_SOURCES | mpd,airplay,bluetooth | What gets scrobbled: `mpd`, `airplay`, `bluetooth`, `spotify` |
| SETTINGS_PAGE | 1 (on) | The settings page; `0` turns it off (saved settings still apply) |
| MQTT_HOST | empty (off) | MQTT broker: what's playing, for Home Assistant, see [Home Assistant](#home-assistant-mqtt) |
| MQTT_PORT | 1883 (8883 with TLS) | Port of the broker |
| MQTT_USER, MQTT_PASSWORD | empty | User name and password on the broker |
| MQTT_TLS | 0 | `1`: TLS to the broker, its certificate checked |
| MQTT_TOPIC | nowplaying | Where the bridge publishes; with several bridges, one each |
| MQTT_DISCOVERY | homeassistant | Home Assistant's discovery prefix; `0` turns discovery off |
| PUBLIC_URL | guessed | The bridge's address as Home Assistant reaches it, for the artwork, e.g. `http://192.168.1.20:8766` |
| UPDATE_CHECK | 1 (on) | Once a day, asks GitHub for the latest release: the settings page, the log and `--check` say when a newer version is out. Nothing else is sent; `0` turns it off |
| DEMO | empty | `1` for the demo mode: made-up tracks, no mpd needed |

## Run it as a service (systemd)

```bash
./install.sh
```

Creates `.env` (asks for an optional mpd password and Last.fm API key), installs an `mpd-bridge` service that runs as your user and starts at boot, then checks the setup. Run it again after a `git pull` to restart the service on the new version. Logs: `journalctl -u mpd-bridge -f`.

## Docker

Each release is published as an image, for PCs and Raspberry Pis (64 and 32-bit), with nothing to clone:

```bash
docker run -d --name nowplaying --network host --restart unless-stopped \
  -v nowplaying-data:/data ghcr.io/mat-d3v/nowplaying
```

Settings go as `-e NAME=value` (e.g. `-e MPD_PASSWORD=secret`), or all at once from a file with `--env-file .env`. For AirPlay and the VU meters, hand it their pipes: `-v /tmp/shairport-sync-metadata:/tmp/shairport-sync-metadata -v /tmp/mpd.fifo:/tmp/mpd.fifo`; for HTTPS, your certificate: `-v ~/certs:/certs:ro -e TLS_CERT=/certs/nowplaying.pem -e TLS_KEY=/certs/nowplaying-key.pem`. Check the setup with `docker exec nowplaying python3 mpd-bridge.py --check`, and see the logs with `docker logs -f nowplaying`. What it saves (display settings, listening history) stays in the `nowplaying-data` volume.

From a clone of this repository, `docker compose up -d` runs the bridge from the folder itself, your `.env`, certificates and changes included. The container uses the host network: the bridge reaches mpd on `127.0.0.1` and listens on the host's port 8766. Settings come from `.env`, as with a local install; the display settings saved from the settings page go to a volume of their own. Host networking works out of the box on Linux; Docker Desktop (macOS, Windows) needs it enabled in its settings. Logs: `docker compose logs -f`.

## AirPlay (shairport-sync)

When [shairport-sync](https://github.com/mikebrady/shairport-sync) runs on the same machine, the page also shows what's played over AirPlay: title, artist, album, artwork, progress, and the device sending it. AirPlay comes first while it plays; when it's paused and mpd plays, mpd shows; when AirPlay stops, mpd comes back. When the app playing gives no cover (some radio apps, some Android apps), the artwork is looked up online, as for radios.

In `/etc/shairport-sync.conf`, turn on the metadata (uncomment these lines in its `metadata` section), then restart it with `sudo systemctl restart shairport-sync`:

```
metadata =
{
	enabled = "yes";
	include_cover_art = "yes";
	pipe_name = "/tmp/shairport-sync-metadata";
	pipe_timeout = 5000;
};
```

The bridge reads that pipe by itself, and `--check` says whether it finds it. `SHAIRPORT_PIPE` sets another path, or turns AirPlay off when empty. Only one program can read the pipe. With Docker, uncomment its line in `docker-compose.yml`.

## Spotify Connect (raspotify)

With [raspotify](https://github.com/dtcooper/raspotify) on the same machine, it shows up as a speaker in the Spotify app, and the page shows what it plays: title, artist, album, artwork, progress, and the device in control. Like AirPlay, Spotify comes first while it plays; when AirPlay and Spotify both play, the last one started shows.

librespot, which raspotify runs, starts a program on each event (track changed, play, pause...): `spotify-event.py` passes them on to the bridge. Install it outside your home folder, where raspotify's service can run it, set it in raspotify's settings, and restart raspotify:

```bash
sudo install -m 755 spotify-event.py /usr/local/bin/nowplaying-spotify-event
echo 'LIBRESPOT_ONEVENT=/usr/local/bin/nowplaying-spotify-event' | sudo tee -a /etc/raspotify/conf
sudo systemctl restart raspotify
```

When the bridge uses another port, or HTTPS, add its address after the program: `LIBRESPOT_ONEVENT="/usr/local/bin/nowplaying-spotify-event https://127.0.0.1:8766"`. The bridge only takes these events from its own machine; `--check` says whether raspotify runs the program, and `SPOTIFY=0` turns Spotify off. Track details need librespot 0.5 or later; librespot started another way takes the same program as `--onevent`.

## Bluetooth

When a phone plays through this machine over Bluetooth (the machine set up as a Bluetooth speaker, for instance with [BlueALSA](https://github.com/arkq/bluez-alsa), PulseAudio or PipeWire), the page shows what it plays: title, artist, album, progress, and the phone's name. Phones send no artwork over Bluetooth: it comes from the Internet, as for radios. Like AirPlay and Spotify, Bluetooth comes first while it plays.

Nothing to install: every few seconds, the bridge asks BlueZ (the Linux Bluetooth service) with `busctl`, which comes with systemd. It only needs to be allowed to ask: on Raspberry Pi OS the first user is; otherwise, add the user running the bridge to the `bluetooth` group, then restart the bridge:

```bash
sudo usermod -aG bluetooth "$USER"
```

`--check` says what BlueZ answers, and `BLUETOOTH=0` turns Bluetooth off. Headphones or speakers this machine plays to don't count: what they play is mpd's. Not available with Docker, which has no access to the machine's Bluetooth.

## Home Assistant (MQTT)

With an MQTT broker (Home Assistant's Mosquitto add-on, or any other), the bridge publishes what's playing for Home Assistant. Set the broker in `.env`, then restart the bridge:

```
MQTT_HOST=192.168.1.10
MQTT_USER=nowplaying
MQTT_PASSWORD=...
```

Home Assistant finds it by itself (MQTT discovery): a "Now Playing" device with the title, artist, album, source (`mpd`, `airplay`, `spotify`, `bluetooth`), quality (`FLAC · 24bit / 96.0 kHz · Hi-Res`) and state, a "Playing" sensor for automations, and the artwork as an image. The title sensor carries the rest as attributes (duration, elapsed time, sender, station, badges...). On a dashboard, for instance:

```yaml
type: picture-entity
entity: image.now_playing_artwork
```

Home Assistant fetches the artwork from the bridge: the bridge gives its address on the network, or `PUBLIC_URL` when Home Assistant reaches it another way (a name, a proxy). Other home automation systems can use the same topics: `nowplaying/state` (JSON), `nowplaying/artwork` (an address) and `nowplaying/availability` (`online`, or `offline` once the bridge is gone). `--check` tries the broker; `MQTT_TLS=1` connects with TLS, `MQTT_DISCOVERY=0` leaves Home Assistant's discovery out.

## Listening history

http://localhost:8766/history lists what played, by day, from mpd, AirPlay or Spotify, with the track playing now on top. A track counts once it has played for half its length or 4 minutes, like on Last.fm (30 s for a radio's songs): skipped tracks don't. The history stays on the machine, in `history.jsonl` in `DATA_DIR` (the last 1000 tracks); `HISTORY=0` turns it off.

The same plays can go to your ListenBrainz or Last.fm profile (scrobbling):

- **ListenBrainz**: copy your user token from https://listenbrainz.org/settings/ into `.env` as `LISTENBRAINZ_TOKEN=...`
- **Last.fm**: create an API account at https://www.last.fm/api/account/create (any name will do), put its key and secret in `.env` as `LASTFM_API_KEY=...` and `LASTFM_API_SECRET=...`, then run `python3 mpd-bridge.py --lastfm-login`: it gives you an address where you allow the bridge, then adds the session key to `.env`

Restart the bridge; `--check` says as whom it scrobbles. Plays that can't be sent (no network) go again later. By default, mpd's, AirPlay's and Bluetooth's plays are scrobbled: Spotify scrobbles by itself once linked to Last.fm, so add `spotify` to `SCROBBLE_SOURCES` only if it isn't. If mpdscribble already scrobbles mpd, leave `mpd` out.

## VU meters

With `?vu=1`, two VU meters move with the music, like on a hi-fi amplifier: the needles follow the average level of each channel, and a lamp lights up when the sound gets close to full scale. They need a copy of what mpd plays: add this output to `/etc/mpd.conf`, then restart mpd with `sudo systemctl restart mpd`:

```
audio_output {
	type	"fifo"
	name	"nowplaying"
	path	"/tmp/mpd.fifo"
	format	"44100:16:2"
}
```

mpd keeps playing on your other outputs as before; this one only hands the bridge the sound, which it reads while a page shows the meters (nobody reading it costs nothing). `--check` says whether it finds the pipe, and `MPD_FIFO` sets another path. AirPlay and Spotify don't go through mpd: the meters hide while they play. With Docker, uncomment the pipe's line in `docker-compose.yml`. The demo mode has made-up levels: try http://localhost:8766/?vu=1.

## HTTPS: keeping the screen on and installing the app

Browsers only grant the Screen Wake Lock to secure pages: HTTPS, or `localhost` (and Android only installs a secure page as an app). A phone or tablet opening `http://192.168.x.x:8766` gets the page, but its screen may go to sleep. Two ways to get HTTPS:

**mkcert** makes a certificate for your local network. On the machine running the bridge, from the `nowplaying` folder (use its own name and IP address):

```bash
mkcert -install
mkcert -cert-file nowplaying.pem -key-file nowplaying-key.pem nowplaying.local 192.168.1.10
```

Then add to `.env` and restart the bridge, which now answers on https://192.168.1.10:8766 (`.pem` files are git-ignored):

```
TLS_CERT=nowplaying.pem
TLS_KEY=nowplaying-key.pem
```

Each phone or tablet must trust mkcert's root certificate, `rootCA.pem` in the folder shown by `mkcert -CAROOT`. On iOS, send it to the device (AirDrop, email), install the profile in Settings, then turn it on in Settings > General > About > Certificate Trust Settings. On Android: Settings > Security > Encryption & credentials > Install a certificate > CA certificate (menu names vary by brand).

**Tailscale**, if your devices are on your tailnet: `tailscale serve --bg 8766` publishes the bridge at `https://<machine>.<tailnet>.ts.net`, with a certificate every browser already trusts (HTTPS certificates must be enabled in the Tailscale admin console).

Already running nginx with certificates? `nginx.conf` is a reverse proxy example.

Once on HTTPS, Android offers to install the page ("Install app"); on iOS, Share > Add to Home Screen works either way. It then opens full screen, from its own icon.

## Raspberry Pi kiosk

On Raspberry Pi OS with desktop:

1. Install the bridge as a service: `./install.sh`
2. Turn off screen blanking: `sudo raspi-config` > Display Options > Screen Blanking > No
3. Start Chromium full screen when the desktop starts:

   ```bash
   mkdir -p ~/.config/autostart
   cat > ~/.config/autostart/nowplaying.desktop <<'DESKTOP'
   [Desktop Entry]
   Type=Application
   Name=Now Playing
   Exec=chromium --kiosk --noerrdialogs --disable-infobars --no-first-run --incognito http://localhost:8766
   DESKTOP
   ```

   (on older releases, the command is `chromium-browser`)
4. Rotate the screen if needed: Preferences > Screen Configuration. Upright, the page shows the artwork on top and the text below

The page comes from `localhost` there, so the Wake Lock works without HTTPS.

## Tests

```bash
python3 -m unittest discover -s tests -v    # the bridge, against a fake mpd
npm install --no-save playwright && npx playwright install chromium
node tests/ui_test.js                       # the page, in Chromium
```

GitHub Actions runs both on every push, with `shellcheck` on `install.sh`, and builds and tries the Docker image.

## License

[MIT](LICENSE)
