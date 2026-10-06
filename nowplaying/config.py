"""Settings, from the environment and the .env file next to mpd-bridge.py.

Read once, when the bridge starts. Variables set in the environment win
over .env, so systemd and Docker settings still apply.
"""
import os

# The folder of mpd-bridge.py: the pages, their assets, .env
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path):
    # Minimal .env support (KEY=value lines, # comments). Variables already
    # set in the environment win, so systemd/Docker settings still apply.
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                if key.startswith('export '):
                    key = key[7:].strip()
                if key:
                    os.environ.setdefault(key, value.strip().strip('"\''))
    except OSError:
        pass


load_dotenv(os.path.join(SCRIPT_DIR, '.env'))


def _on(name, default='1'):
    # On unless set to 0, false, no or off
    return os.environ.get(name, default).strip().lower() not in ('0', 'false', 'no', 'off')


def _path(name, default=''):
    # A path; relative ones start from this folder (systemd runs the bridge from /)
    value = os.environ.get(name, default).strip()
    return value and os.path.join(SCRIPT_DIR, value)


MPD_HOST = os.environ.get('MPD_HOST', '127.0.0.1')
MPD_PORT = int(os.environ.get('MPD_PORT', '6600'))
MPD_PASSWORD = os.environ.get('MPD_PASSWORD', '')
PORT = int(os.environ.get('PORT', '8766'))
# HTTPS: certificate and private key files (PEM), e.g. made with mkcert
TLS_CERT = _path('TLS_CERT')
TLS_KEY = _path('TLS_KEY')
# ALSA card read for the audio format when mpd gives none
ALSA_CARD = os.environ.get('ALSA_CARD', '0')

LASTFM_KEY = os.environ.get('LASTFM_API_KEY', '')
# Empty, or still the placeholder of older setups: no Last.fm lookups
LASTFM_ENABLED = LASTFM_KEY not in ('', 'your_lastfm_api_key_here')
# Artwork from the iTunes Search API (free, no key) for radios, and tracks
# mpd has none for
ITUNES_ENABLED = _on('ITUNES_ARTWORK')

# Demo mode: made-up tracks (demo.py) instead of mpd
DEMO = os.environ.get('DEMO', '').strip().lower() in ('1', 'true', 'yes', 'on')
# AirPlay: shairport-sync's metadata pipe (shairport.py); empty turns it off
SHAIRPORT_PIPE = os.environ.get('SHAIRPORT_PIPE', '/tmp/shairport-sync-metadata').strip()
# Spotify Connect: librespot's events, posted by spotify-event.py
SPOTIFY_ENABLED = _on('SPOTIFY')
# Bluetooth: what a phone streaming here plays, from BlueZ (bluetooth.py)
BLUETOOTH = _on('BLUETOOTH')
# VU meters: mpd's fifo output (levels.py); empty turns them off
MPD_FIFO = os.environ.get('MPD_FIFO', '/tmp/mpd.fifo').strip()

# Where the bridge keeps what it saves (display settings, history)
DATA_DIR = os.path.join(SCRIPT_DIR, os.environ.get('DATA_DIR', '').strip() or '.')
# The settings page (/settings); saved settings apply even without it
SETTINGS_PAGE = _on('SETTINGS_PAGE')
# Listening history (history.py), kept in DATA_DIR
HISTORY_ENABLED = _on('HISTORY')
# Scrobbling: to ListenBrainz with a user token; to Last.fm with the API key
# above, its secret, and a session key from --lastfm-login
LISTENBRAINZ_TOKEN = os.environ.get('LISTENBRAINZ_TOKEN', '').strip()
LASTFM_SECRET = os.environ.get('LASTFM_API_SECRET', '').strip()
LASTFM_SESSION = os.environ.get('LASTFM_SESSION_KEY', '').strip()
SCROBBLE_SOURCES = [s.strip() for s in os.environ.get('SCROBBLE_SOURCES', 'mpd,airplay,bluetooth').split(',')
                    if s.strip()]

# Once a day, ask GitHub whether a newer version is out (updates.py)
UPDATE_CHECK = _on('UPDATE_CHECK')

# Home Assistant, through an MQTT broker (mqtt.py); no host: off
MQTT_HOST = os.environ.get('MQTT_HOST', '').strip()
MQTT_TLS = os.environ.get('MQTT_TLS', '').strip().lower() in ('1', 'true', 'yes', 'on')
MQTT_PORT = int(os.environ.get('MQTT_PORT', '').strip() or (8883 if MQTT_TLS else 1883))
MQTT_USER = os.environ.get('MQTT_USER', '').strip()
MQTT_PASSWORD = os.environ.get('MQTT_PASSWORD', '')
MQTT_TOPIC = os.environ.get('MQTT_TOPIC', '').strip().strip('/') or 'nowplaying'
# Home Assistant's discovery prefix; 0 turns discovery off
MQTT_DISCOVERY = os.environ.get('MQTT_DISCOVERY', 'homeassistant').strip().strip('/')
if MQTT_DISCOVERY.lower() in ('0', 'false', 'no', 'off'):
    MQTT_DISCOVERY = ''
# The bridge's address as others reach it (artwork for Home Assistant);
# empty: guessed
PUBLIC_URL = os.environ.get('PUBLIC_URL', '').strip().rstrip('/')
