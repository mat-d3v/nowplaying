#!/usr/bin/env python3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import socket, json, urllib.request, urllib.parse, re, os, threading, logging, queue, time, ssl, sys, stat
import ipaddress, unicodedata

VERSION = '1.1.0'  # with a matching section in CHANGELOG.md
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_dotenv(path):
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

_load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

MPD_HOST = os.environ.get('MPD_HOST', '127.0.0.1')
MPD_PORT = int(os.environ.get('MPD_PORT', '6600'))
MPD_PASSWORD = os.environ.get('MPD_PASSWORD', '')
PORT = int(os.environ.get('PORT', '8766'))
# HTTPS: certificate and private key files (PEM), e.g. made with mkcert.
# Relative paths start from this folder (systemd runs the bridge from /)
TLS_CERT = os.environ.get('TLS_CERT', '') and os.path.join(SCRIPT_DIR, os.environ['TLS_CERT'])
TLS_KEY = os.environ.get('TLS_KEY', '') and os.path.join(SCRIPT_DIR, os.environ['TLS_KEY'])
LASTFM_KEY = os.environ.get('LASTFM_API_KEY', '')
# Empty, or still the placeholder of older setups: no Last.fm lookups
LASTFM_ENABLED = LASTFM_KEY not in ('', 'your_lastfm_api_key_here')
# Artwork from the iTunes Search API (free, no key) for radios, and tracks
# mpd has none for: on unless set to 0
ITUNES_ENABLED = os.environ.get('ITUNES_ARTWORK', '1').strip().lower() not in ('0', 'false', 'no', 'off')
# Demo mode: made-up tracks (demo.py) instead of mpd
DEMO = os.environ.get('DEMO', '').strip().lower() in ('1', 'true', 'yes', 'on')
# AirPlay: shairport-sync's metadata pipe (shairport.py); empty turns it off
SHAIRPORT_PIPE = os.environ.get('SHAIRPORT_PIPE', '/tmp/shairport-sync-metadata').strip()
# Spotify Connect: librespot's events, posted by spotify-event.py; 0 turns it off
SPOTIFY_ENABLED = os.environ.get('SPOTIFY', '1').strip().lower() not in ('0', 'false', 'no', 'off')
# VU meters: mpd's fifo output (levels.py); empty turns them off
MPD_FIFO = os.environ.get('MPD_FIFO', '/tmp/mpd.fifo').strip()
# Where the bridge keeps what it saves (display settings); relative paths
# start from this folder
DATA_DIR = os.path.join(SCRIPT_DIR, os.environ.get('DATA_DIR', '').strip() or '.')
# The settings page (/settings); 0 turns it off, saved settings still apply
SETTINGS_PAGE = os.environ.get('SETTINGS_PAGE', '1').strip().lower() not in ('0', 'false', 'no', 'off')
# Listening history (history.py), kept in DATA_DIR; 0 turns it off
HISTORY_ENABLED = os.environ.get('HISTORY', '1').strip().lower() not in ('0', 'false', 'no', 'off')
# Scrobbling: to ListenBrainz with a user token; to Last.fm with the API key
# above, its secret, and a session key from --lastfm-login
LISTENBRAINZ_TOKEN = os.environ.get('LISTENBRAINZ_TOKEN', '').strip()
LASTFM_SECRET = os.environ.get('LASTFM_API_SECRET', '').strip()
LASTFM_SESSION = os.environ.get('LASTFM_SESSION_KEY', '').strip()
SCROBBLE_SOURCES = [s.strip() for s in os.environ.get('SCROBBLE_SOURCES', 'mpd,airplay').split(',') if s.strip()]

# systemd's journal already timestamps each line
logging.basicConfig(level=logging.INFO, format=('%(levelname)s %(message)s' if os.environ.get('JOURNAL_STREAM')
                                                else '%(asctime)s %(levelname)s %(message)s'))
log = logging.getLogger('nowplaying')

sys.path.insert(0, SCRIPT_DIR)  # demo.py, history.py, levels.py, shairport.py and spotify.py
import history, levels, shairport, spotify
if DEMO:
    import demo
AIRPLAY = shairport.AirPlay() if SHAIRPORT_PIPE and not DEMO else None
SPOTIFY = spotify.Spotify() if SPOTIFY_ENABLED and not DEMO else None
LEVELS = (levels.LevelMeter(levels.demo_levels) if DEMO
          else levels.LevelMeter(levels.fifo_levels(MPD_FIFO, log)) if MPD_FIFO else None)
HISTORY_FILE = os.path.join(DATA_DIR, 'history.jsonl')
LISTENING = None  # history.Listening, once the bridge runs (see main)

def scrobble_services():
    services = []
    if LISTENBRAINZ_TOKEN:
        services.append(history.ListenBrainz(LISTENBRAINZ_TOKEN, VERSION))
    if LASTFM_ENABLED and LASTFM_SECRET and LASTFM_SESSION:
        services.append(history.LastFm(LASTFM_KEY, LASTFM_SECRET, LASTFM_SESSION))
    return services
OTHER_PLAYERS = [player for player in (AIRPLAY, SPOTIFY) if player]  # besides mpd

_mpd_sock = None
_mpd_lock = threading.Lock()  # the HTTP server is multi-threaded, the mpd socket is shared

class MPDError(Exception):
    # An "ACK [code@index] {command} message" answer from mpd
    def __init__(self, line):
        super().__init__(line)
        m = re.match(r'ACK \[(\d+)@', line)
        self.code = int(m.group(1)) if m else 0

ACK_PASSWORD, ACK_PERMISSION = 3, 4

def _quote(arg):
    return arg.replace('\\', '\\\\').replace('"', '\\"')

def mpd_command(cmd):
    with _mpd_lock:
        response = _mpd_command_unlocked(cmd)
    if response.startswith('ACK '):
        raise MPDError(response.strip())
    return response

def _mpd_connect():
    s = socket.create_connection((MPD_HOST, MPD_PORT), timeout=3)  # a frozen mpd can't hang requests
    try:
        s.recv(1024)  # "OK MPD x.y.z" banner
        if MPD_PASSWORD:
            s.sendall(f'password "{_quote(MPD_PASSWORD)}"\n'.encode())
            response = _mpd_recv(s)
            if response.startswith('ACK '):
                raise MPDError(response.strip())
    except BaseException:
        s.close()
        raise
    return s

def _mpd_recv(sock):
    # Read a full mpd protocol response: it ends with a lone "OK" line,
    # or an "ACK ..." error line. TCP may fragment, so loop until complete.
    buf = b''
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError('mpd connection closed')
        buf += chunk
        if buf == b'OK\n' or buf.endswith(b'\nOK\n'):
            return buf.decode()
        if buf.startswith(b'ACK ') and buf.endswith(b'\n'):
            return buf.decode()

def _mpd_command_unlocked(cmd):
    global _mpd_sock
    try:
        if _mpd_sock is None:
            _mpd_sock = _mpd_connect()
        _mpd_sock.sendall((cmd + '\n').encode())
        return _mpd_recv(_mpd_sock)
    except Exception:
        # mpd closes idle connections after a while: reconnect once
        if _mpd_sock is not None:
            _mpd_sock.close()
        _mpd_sock = None
        s = _mpd_connect()
        try:
            s.sendall((cmd + '\n').encode())
            result = _mpd_recv(s)
        except BaseException:
            s.close()
            raise
        _mpd_sock = s
        return result

_mpd_art_cache = {}  # file uri -> (bytes|None, mime)

def _fetch_mpd_binary(cmd, uri):
    # Fetch a binary object (album art) over its own mpd connection:
    # keeping binary chunks off the shared text socket avoids any desync.
    s = _mpd_connect()
    try:
        buf = bytearray()

        def read_line():
            while True:
                i = buf.find(b'\n')
                if i >= 0:
                    line = bytes(buf[:i]).decode('utf-8', 'replace')
                    del buf[:i + 1]
                    return line
                chunk = s.recv(65536)
                if not chunk:
                    raise ConnectionError('mpd connection closed')
                buf.extend(chunk)

        def read_exact(n):
            while len(buf) < n:
                chunk = s.recv(65536)
                if not chunk:
                    raise ConnectionError('mpd connection closed')
                buf.extend(chunk)
            data = bytes(buf[:n])
            del buf[:n]
            return data

        quoted = _quote(uri)
        data = bytearray()
        mime = ''
        total = None
        while total is None or len(data) < total:
            s.sendall(f'{cmd} "{quoted}" {len(data)}\n'.encode())
            size = None
            chunk_len = None
            while True:
                line = read_line()
                if line.startswith('ACK '):
                    return None, ''
                if line == 'OK':
                    break
                if line.startswith('size: '):
                    size = int(line[6:])
                elif line.startswith('type: '):
                    mime = line[6:]
                elif line.startswith('binary: '):
                    chunk_len = int(line[8:])
                    data += read_exact(chunk_len)
            if size is None or not chunk_len:
                break  # no picture, or empty chunk
            total = size
        return (bytes(data), mime) if data else (None, '')
    finally:
        s.close()

def _sniff_mime(data):
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'

def get_mpd_art(uri):
    # readpicture: embedded tag art; albumart: cover file next to the track
    if uri in _mpd_art_cache:
        return _mpd_art_cache[uri]
    result = (None, '')
    for cmd in ('readpicture', 'albumart'):
        try:
            data, mime = _fetch_mpd_binary(cmd, uri)
            if data:
                result = (data, mime or _sniff_mime(data))
                break
        except Exception as e:
            log.debug('%s failed for %s: %s', cmd, uri, e)
    if len(_mpd_art_cache) > 20:
        _mpd_art_cache.clear()  # art blobs can be large, keep this cache small
    _mpd_art_cache[uri] = result
    return result

def parse_mpd(raw):
    d = {}
    for line in raw.splitlines():
        if ': ' in line:
            k, v = line.split(': ', 1)
            d[k.lower()] = v
    return d

ALSA_CARD = os.environ.get('ALSA_CARD', '0')

def get_alsa_format():
    try:
        with open(f'/proc/asound/card{ALSA_CARD}/pcm0p/sub0/hw_params') as f:
            content = f.read()
        rate = ''
        bits = ''
        for line in content.splitlines():
            if line.startswith('format:'):
                m = re.search(r'S(\d+)', line)
                if m:
                    bits = m.group(1)
            if line.startswith('rate:'):
                rate = line.split()[1]
        if rate and bits:
            return f"{rate}:{bits}:2"
        return ''
    except OSError:
        return ''  # no such card, or nothing playing on it

def lastfm_art(artist, album):
    # Last.fm's artwork for an album (needs LASTFM_API_KEY), '' if it has none
    q = urllib.parse.urlencode({'method': 'album.getinfo', 'api_key': LASTFM_KEY, 'artist': artist,
                                'album': album, 'format': 'json'})
    with urllib.request.urlopen(f'https://ws.audioscrobbler.com/2.0/?{q}', timeout=5) as r:
        data = json.loads(r.read())
    for img in reversed(data.get('album', {}).get('image', [])):
        if img.get('#text'):
            return re.sub(r'/\d+x\d+/', '/', img['#text'])
    return ''

def simplify(name):
    # Names compared loosely: case, accents, punctuation, and notes such as
    # "(Remastered 2011)", "[Deluxe Edition]" or " - Single" don't count
    bare = re.sub(r'\s*(\([^)]*\)|\[[^\]]*\])', '', name)
    bare = re.sub(r'\s+-\s+(single|ep)\s*$', '', bare, flags=re.I).strip() or name
    bare = ''.join(c for c in unicodedata.normalize('NFKD', bare) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[^\W_]+', bare.lower()))

def names_match(a, b, exact=False):
    # "Daft Punk" matches "Daft Punk feat. Pharrell Williams" (whole words),
    # unless exact
    a, b = simplify(a), simplify(b)
    if not a or not b:
        return False
    return a == b if exact else f' {a} ' in f' {b} ' or f' {b} ' in f' {a} '

def itunes_search(term, entity):
    q = urllib.parse.urlencode({'term': term, 'media': 'music', 'entity': entity, 'limit': 10})
    with urllib.request.urlopen(f'https://itunes.apple.com/search?{q}', timeout=5) as r:
        return json.loads(r.read()).get('results', [])

def itunes_art(artist, title, album='', album_artist=''):
    # Artwork from the iTunes Search API (free, no key): the album when the
    # tags name one, else the song (radios send no album). Only a result
    # whose names match counts: no artwork rather than someone else's
    def pick(results, field, name, loose, by):
        # The same name first ("Discovery" before "Discovery (Live)"), then
        # a looser match, always by the same artist
        results = [r for r in results if names_match(r.get('artistName', ''), by)]
        same = [r for r in results if r.get(field, '').casefold() == name.casefold()]
        close = [r for r in results if names_match(r.get(field, ''), name, exact=not loose)]
        return ((same or close or [{}])[0]).get('artworkUrl100', '')

    found = ''
    if album:
        by = album_artist or artist
        found = pick(itunes_search(f'{by} {album}', 'album'), 'collectionName', album, False, by)
    if not found and title:
        found = pick(itunes_search(f'{artist} {title}', 'song'), 'trackName', title, True, artist)
    # 100x100 thumbnails; the same address serves bigger sizes
    return found.replace('/100x100bb.', '/600x600bb.')

RETRY_AFTER = 300  # seconds before an online lookup that failed (no network yet?) runs again
_online_art = {}   # lookup -> (artwork address or '', when); None as address: the lookup failed
_online_art_running = set()
_online_art_lock = threading.Lock()

def online_art(key, lookup):
    # Online artwork, without holding up the page: the address once known
    # ('' when the service has none), None while lookup() runs in the
    # background. When it's done the status is pushed again, and the page
    # loads artwork that arrives after the title
    with _online_art_lock:
        known = _online_art.get(key)
        if known and (known[0] is not None or time.monotonic() - known[1] < RETRY_AFTER):
            return known[0] or ''
        if key in _online_art_running:
            return None
        _online_art_running.add(key)
    threading.Thread(target=_look_up_art, args=(key, lookup), daemon=True).start()
    return None

def _look_up_art(key, lookup):
    try:
        found = lookup()
    except Exception as e:
        log.warning('%s artwork lookup failed for %s: %s', key[0], ' / '.join(filter(None, key[1:])), e)
        found = None
    with _online_art_lock:
        _online_art_running.discard(key)
        if len(_online_art) > 500:
            _online_art.clear()  # simple cap to avoid unbounded growth
        _online_art[key] = (found, time.monotonic())
    publish_status()  # with the artwork, or on to the next place to look

def get_audio_format(status):
    # mpd's own "audio" field is the decoded source format ("44100:16:2",
    # "44100:f:2" for float decoders, "dsd64:2"): that's what the badges
    # describe. The ALSA hardware format is only a fallback, since a DAC
    # that takes 32-bit samples would turn every CD rip into "32bit".
    audio = status.get('audio', '')
    if re.fullmatch(r'(\d+:(\d+|f)|dsd\d+):\d+', audio):
        return audio
    return get_alsa_format()

# File extension -> (badge label, lossless)
CODECS = {
    'flac': ('FLAC', True), 'wav': ('WAV', True), 'aif': ('AIFF', True), 'aiff': ('AIFF', True),
    'ape': ('APE', True), 'wv': ('WavPack', True), 'dsf': ('DSF', True), 'dff': ('DFF', True),
    'mp3': ('MP3', False), 'aac': ('AAC', False), 'ogg': ('OGG', False), 'oga': ('OGG', False),
    'opus': ('OPUS', False), 'wma': ('WMA', False), 'mpc': ('MPC', False),
}

# Qobuz's own stream addresses give their format as a number
QOBUZ_FORMATS = {'5': 'mp3', '6': 'flac', '7': 'flac', '27': 'flac'}

def _known(ext):
    return ext in CODECS or ext in ('m4a', 'mp4')

def _extension(file_url):
    # The file's extension. An address (UPnP/DLNA servers and controllers:
    # upmpdcli, BubbleUPnP, MinimServer... give mpd http addresses) usually
    # ends like the file ("/01%20Song.flac?..."); else its parameters may
    # say ("?file=01.flac", "?format=flac", Qobuz's "fmt=27")
    def ext_of(text):
        name = text.rsplit('/', 1)[-1]
        return name.rsplit('.', 1)[1].lower() if '.' in name else ''
    if '://' not in file_url:
        return ext_of(file_url)
    url = urllib.parse.urlparse(file_url)
    params = urllib.parse.parse_qsl(url.query)
    for text in [url.path] + [value for _, value in params]:
        if _known(ext_of(text)):
            return ext_of(text)
    for name, value in params:
        if name.lower() in ('format', 'fmt', 'ext', 'codec') and _known(value.lower().lstrip('.')):
            return value.lower().lstrip('.')
    if 'qobuz' in (url.hostname or ''):
        return QOBUZ_FORMATS.get(dict(params).get('fmt', ''), '')
    return ''

def get_codec(file_url, audio):
    # (badge, lossless), from the file's extension
    if not file_url:
        return '', False
    url = '://' in file_url
    ext = _extension(file_url)
    if ext in ('m4a', 'mp4'):
        # Same container for AAC and ALAC: mpd decodes AAC to float samples
        # ("44100:f:2") and ALAC to integer ones ("44100:16:2")
        if not audio:
            return 'M4A', False
        return ('AAC', False) if audio.split(':')[1:2] == ['f'] else ('ALAC', True)
    if ext in CODECS or (ext and not url):
        return CODECS.get(ext, (ext.upper(), False))
    # An address that doesn't say what it carries (a radio, a playlist.m3u8,
    # a stream from a service): no codec badge rather than a wrong one. Its
    # samples can still tell: integers at 88.2 kHz or more only come from
    # lossless files (lossy decoders give floating point, or 48 kHz at most)
    parts = audio.split(':')
    lossless = url and len(parts) == 3 and parts[0].isdigit() and int(parts[0]) >= 88200 and parts[1].isdigit()
    return '', lossless

def display_title(song):
    # Untagged files and radios without a title still get a name instead of
    # "Nothing playing": station name, then file name, then stream host
    title = song.get('title') or song.get('name')
    if title:
        return title
    uri = song.get('file', '')
    if '://' in uri:
        return urllib.parse.urlparse(uri).netloc or uri
    return os.path.splitext(uri.rsplit('/', 1)[-1])[0]

def is_fifo(path):
    try:
        return stat.S_ISFIFO(os.stat(path).st_mode)
    except OSError:
        return False

def get_status():
    if DEMO:
        return dict(demo.status(), levels=True)
    status = parse_mpd(mpd_command('status'))
    currentsong = parse_mpd(mpd_command('currentsong'))
    state = status.get('state', 'stop')
    elapsed = float(status.get('elapsed', 0))
    duration = float(status.get('duration', 0))
    file_url = currentsong.get('file', '')
    stream = '://' in file_url
    artist = currentsong.get('artist', '')
    album = currentsong.get('album', '')
    title = display_title(currentsong)
    name = currentsong.get('name', '')  # station name, for streams
    shown_artist, shown_album = artist, album
    stream_title = currentsong.get('title', '')
    if stream and not artist and ' - ' in stream_title:
        # Radios send "Artist - Title" as the title: split it, and put the
        # station on the album line
        left, right = (part.strip() for part in stream_title.split(' - ', 1))
        if left and right:
            shown_artist, title = left, right
            shown_album = album or name
    if not shown_artist and name != title:
        shown_artist = name  # a radio with no artist: the station instead
    fmt = get_audio_format(status) if state != 'stop' else ''
    codec, lossless = get_codec(file_url, status.get('audio', ''))
    # Artwork: mpd itself first (embedded tags or cover file), then online:
    # Last.fm (with an API key), then iTunes (the album, or the song for
    # radios). Online lookups run in the background: the title shows at
    # once, the artwork follows
    art_url = ''
    if file_url and not stream:
        data, _ = get_mpd_art(file_url)
        if data:
            art_url = '/art?file=' + urllib.parse.quote(file_url, safe='')
    looking = False
    if not art_url and artist and album and LASTFM_ENABLED:
        found = online_art(('Last.fm', artist, album), lambda: lastfm_art(artist, album))
        looking, art_url = found is None, found or ''
    if not art_url and not looking and shown_artist and shown_artist != name and ITUNES_ENABLED:
        # A compilation is under its album artist ("Various Artists")
        key = ('iTunes', shown_artist, title, album, currentsong.get('albumartist', ''))
        art_url = online_art(key, lambda: itunes_art(*key[1:])) or ''
    # Next track in the queue (mpd exposes its position via 'nextsong')
    next_title = ''
    next_artist = ''
    ns = status.get('nextsong')
    if ns is not None and state != 'stop':
        try:
            nxt = parse_mpd(mpd_command(f'playlistinfo {ns}'))
        except MPDError:
            nxt = {}  # the queue changed between the two commands
        next_title = display_title(nxt)
        next_artist = nxt.get('artist', '')
    return {
        'state': state,
        'title': title,
        'artist': shown_artist or '\u2014',
        'album': shown_album,
        'elapsed': elapsed,
        'duration': duration,
        'format': fmt,
        'codec': codec,
        'lossless': lossless,
        'art_url': art_url,
        'file': file_url,
        'stream': stream,
        'source': 'mpd',
        'next_title': next_title,
        'next_artist': next_artist,
        'levels': bool(MPD_FIFO) and is_fifo(MPD_FIFO),  # VU meters possible
        'station': name if stream else '',
    }

_mpd_problem = None  # last mpd error logged, so each change is logged once
_mpd_problem_lock = threading.Lock()  # pages ask at the same time: logged once all the same
PROBLEMS = {'mpd_unreachable': 'unreachable', 'mpd_password': 'refused access (check MPD_PASSWORD)',
            'mpd_error': 'answered with an error'}

def other_players():
    # What AirPlay and Spotify are up to: (playing, paused) payloads, the
    # one that started playing last first
    found = sorted(((player.started, data) for player in OTHER_PLAYERS for data in [player.status()] if data),
                   key=lambda found: found[0], reverse=True)
    return ([data for _, data in found if data['state'] == 'play'],
            [data for _, data in found if data['state'] != 'play'])

def status_or_error():
    # (payload, HTTP status): what's playing, or why mpd can't say. AirPlay
    # and Spotify come first while they play (the last one started, if
    # both do), and while paused if mpd isn't playing
    global _mpd_problem
    playing, paused = other_players()
    if playing:
        return playing[0], 200
    try:
        data = get_status()
    except MPDError as e:
        kind = 'mpd_password' if e.code in (ACK_PASSWORD, ACK_PERMISSION) else 'mpd_error'
        problem = (kind, str(e))
    except OSError as e:  # refused, timed out, closed...
        problem = ('mpd_unreachable', str(e) or e.__class__.__name__)
    else:
        with _mpd_problem_lock:
            if _mpd_problem:
                log.info('mpd at %s:%s is back', MPD_HOST, MPD_PORT)
                _mpd_problem = None
        if paused and data['state'] != 'play':
            return paused[0], 200
        return data, 200
    with _mpd_problem_lock:
        if problem[0] != (_mpd_problem or ('',))[0]:  # a new kind of problem, not each new wording
            log.warning('mpd at %s:%s %s: %s', MPD_HOST, MPD_PORT, PROBLEMS[problem[0]], problem[1])
        _mpd_problem = problem
    if paused:
        return paused[0], 200  # paused AirPlay or Spotify beats an mpd error
    return {'error': problem[0], 'detail': problem[1]}, 503

# Display settings saved from the settings page, for every screen; the
# page's address can still set each of them. The same values as there
DISPLAY_FILE = os.path.join(DATA_DIR, 'settings.json')
DISPLAY_CHOICES = {'lang': ('', 'en', 'fr'), 'clock': ('24', '12', '0'), 'bg': ('glow', 'blur'),
                   'next': ('1', '0'), 'vu': ('0', '1'), 'shift': ('1', '0')}

def valid_display(settings):
    # The display settings, checked (known names, allowed values), or None
    if not isinstance(settings, dict):
        return None
    clean = {}
    for key, value in settings.items():
        if not isinstance(value, str):
            return None
        if key == 'scale':  # '' fits the screen, else 0.5 to 3
            try:
                scale = float(value) if value else None
            except ValueError:
                return None
            if scale is not None and not 0.5 <= scale <= 3:
                return None
            clean[key] = f'{scale:.1f}' if scale else ''
        elif value in DISPLAY_CHOICES.get(key, ()):
            clean[key] = value
        else:
            return None
    return clean

def load_display():
    try:
        with open(DISPLAY_FILE) as f:
            return valid_display(json.load(f)) or {}
    except (OSError, ValueError):
        return {}

def save_display(settings):
    temporary = DISPLAY_FILE + '.tmp'
    with open(temporary, 'w') as f:
        json.dump(settings, f, indent=2)
    os.replace(temporary, DISPLAY_FILE)  # never half a file

class Broadcaster:
    # Fans each status change out to the pages listening on /events
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()

    def subscribe(self):
        q = queue.Queue(maxsize=40)  # status updates, and ('levels', [...]) for the VU meters
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def has_clients(self):
        with self.lock:
            return bool(self.clients)

    def publish(self, payload):
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # a stalled page: it gets the next change

events = Broadcaster()
IDLE_REFRESH = 55  # seconds without news before checking the idle connection

_publish_lock = threading.Lock()  # one at a time: the history sees them in order

def publish_status():
    # The current status (or mpd's error) to the listening history, and to
    # the pages listening. Never raises: the watcher thread must outlive any
    # bug in here, or the pages would keep a live but silent event stream
    # and freeze
    if not (LISTENING or events.has_clients()):
        return
    with _publish_lock:
        try:
            status = status_or_error()[0]
        except Exception:
            log.exception('cannot build the status for /events')
            return
        if LISTENING:
            try:
                LISTENING.observe(status)
            except Exception:
                log.exception('listening history failed')
        events.publish(status)

def watch_mpd():
    # mpd's "idle" command blocks until something changes (track, play/pause,
    # seek, queue, options): each change is pushed to the pages right away
    failing = False
    while True:
        try:
            s = _mpd_connect()
            failing = False
            try:
                publish_status()  # catch up after a reconnect
                s.settimeout(IDLE_REFRESH)
                while True:
                    s.sendall(b'idle player playlist options\n')
                    try:
                        response = _mpd_recv(s)
                    except socket.timeout:
                        # Quiet for a while: leave idle, which also checks the
                        # connection is still alive, then wait again
                        s.sendall(b'noidle\n')
                        response = _mpd_recv(s)
                    if response.startswith('ACK '):
                        raise MPDError(response.strip())
                    if 'changed: ' in response:
                        publish_status()
            finally:
                s.close()
        except (OSError, MPDError) as e:
            log.debug('mpd idle connection lost: %s', e)
            if not failing:
                publish_status()  # tells the pages what's wrong
            failing = True
            time.sleep(2)
        except Exception:
            log.exception('mpd watcher failed, restarting it')
            time.sleep(2)

def watch_demo():
    # Demo mode: a change each time the next made-up track starts
    threading.Thread(target=demo.draw_all, daemon=True).start()
    while True:
        time.sleep(demo.until_next_track() + 0.05)
        publish_status()

PAGE = ('index.html', 'text/html; charset=utf-8')
SETTINGS_FILE = ('settings.html', 'text/html; charset=utf-8')
STATIC_FILES = {
    '/': PAGE,
    '/index.html': PAGE,
    # Same page: it switches to French from this path (or ?lang=fr)
    '/index.fr.html': PAGE,
    '/hires.svg': ('hires.svg', 'image/svg+xml'),
    '/apple-touch-icon.png': ('assets/apple-touch-icon.png', 'image/png'),
    '/touch-icon-v2.png': ('assets/touch-icon-v2.png', 'image/png'),
    '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
    '/icon-192.png': ('assets/icon-192.png', 'image/png'),
    '/icon-512.png': ('assets/logo.png', 'image/png'),
    '/settings': SETTINGS_FILE,
    '/history': ('history.html', 'text/html; charset=utf-8'),
}

class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def stream_events(self, with_levels):
        # Server-sent events: the current status, then every change; with
        # ?vu=1, the levels for the VU meters too ("levels" events)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')  # nginx: pass events through unbuffered
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        q = events.subscribe()
        if with_levels and LEVELS:
            LEVELS.listen(q)
        try:
            self.wfile.write(b'retry: 2000\n\n')  # reconnect delay after a bridge restart
            self.send_event(status_or_error()[0])
            while True:
                try:
                    item = q.get(timeout=20)
                except queue.Empty:
                    # Keeps proxies from closing a quiet stream, and finds out
                    # about pages that went away
                    self.wfile.write(b': ping\n\n')
                    continue
                if isinstance(item, tuple):  # ('levels', [...]) or ('settings', {...})
                    self.wfile.write(f'event: {item[0]}\ndata: {json.dumps(item[1])}\n\n'.encode())
                else:
                    self.send_event(item)
        except OSError:
            pass  # the page went away
        finally:
            if LEVELS:
                LEVELS.unlisten(q)
            events.unsubscribe(q)

    def send_event(self, payload):
        self.wfile.write(f'data: {json.dumps(payload)}\n\n'.encode())

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == '/events':
            self.stream_events(urllib.parse.parse_qs(url.query).get('vu') == ['1'])
        elif url.path == '/now':
            try:
                self.send_json(*status_or_error())
            except Exception as e:
                log.exception('/now failed')
                self.send_json({'error': 'internal', 'detail': str(e)}, 500)
        elif url.path == '/art':
            qs = urllib.parse.parse_qs(url.query)
            uri = qs.get('file', [''])[0]
            if DEMO and qs.get('demo', [''])[0].isdigit():
                data, mime = demo.artwork(qs['demo'][0]), 'image/png'
            elif AIRPLAY and 'airplay' in qs:
                data = AIRPLAY.cover()
                mime = _sniff_mime(data) if data else ''
            else:
                data, mime = get_mpd_art(uri) if uri else (None, '')
            if data:
                self.send_response(200)
                self.send_header('Content-Type', mime or 'image/jpeg')
                self.send_header('Cache-Control', 'max-age=3600')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()
        elif url.path == '/display':
            self.send_json(load_display())
        elif url.path == '/history.json':
            on = bool(LISTENING) and HISTORY_ENABLED
            self.send_json({'enabled': on, 'tracks': LISTENING.recent() if on else []})
        elif url.path in STATIC_FILES and (STATIC_FILES[url.path] != SETTINGS_FILE or SETTINGS_PAGE):
            filename, content_type = STATIC_FILES[url.path]
            filepath = os.path.join(SCRIPT_DIR, filename)
            try:
                with open(filepath, 'rb') as f:
                    data = f.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            if filename == 'index.html':
                # The saved display settings, in the page itself: they apply
                # from the start (<, written \u003c, can't end the script)
                saved = json.dumps(load_display()).replace('<', '\\u003c')
                data = data.replace(b'/*display-settings*/null', saved.encode(), 1)
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            if content_type.startswith('text/html'):
                self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == '/spotify' and SPOTIFY:
            self.spotify_event()
        elif url.path == '/display' and SETTINGS_PAGE:
            self.save_display()
        else:
            self.send_response(404)
            self.end_headers()

    def read_json(self):
        # The JSON body of a request, None if there's none or it's not JSON
        try:
            length = int(self.headers.get('Content-Length', 0))
            return json.loads(self.rfile.read(length)) if 0 < length <= 65536 else None
        except ValueError:
            return None

    def save_display(self):
        # From the settings page. Another site's page can't post here: its
        # browser sends its Origin, and JSON needs the bridge's leave first.
        # Host names only: a proxy in front may leave the port out of Host
        origin = self.headers.get('Origin')
        host = urllib.parse.urlparse('//' + self.headers.get('Host', '')).hostname
        if origin and urllib.parse.urlparse(origin).hostname != host:
            self.send_json({'error': 'not from this page'}, 403)
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            self.send_json({'error': 'expected JSON'}, 415)
            return
        settings = valid_display(self.read_json())
        if settings is None:
            self.send_json({'error': 'unknown setting or value'}, 400)
            return
        try:
            save_display(settings)
        except OSError as e:
            log.error('cannot save the display settings in %s: %s', DISPLAY_FILE, e)
            self.send_json({'error': f'cannot write {DISPLAY_FILE} ({e.strerror})'}, 500)
            return
        log.info('display settings saved: %s', settings)
        events.publish(('settings', settings))  # the screens reload with them
        self.send_response(204)
        self.end_headers()

    def spotify_event(self):
        # An event from librespot, posted by spotify-event.py. Only from this
        # machine: elsewhere on the network, nobody can make the page show
        # what they like
        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
            self.send_json({'error': 'only from this machine'}, 403)
            return
        event = self.read_json()
        if not isinstance(event, dict) or not all(isinstance(v, str) for v in event.values()):
            self.send_json({'error': 'expected a JSON object of strings'}, 400)
            return
        global _spotify_seen
        if not _spotify_seen:
            log.info('Spotify Connect: librespot tells what it plays')
            _spotify_seen = True
        if SPOTIFY.handle(event):
            publish_status()
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args): pass

_spotify_seen = False  # logged once, on librespot's first event

def describe_badges(fmt, codec, lossless):
    # The badges the page shows for this format (same rules as index.html)
    parts = fmt.split(':')
    badges = [codec] if codec else []
    dsd = re.fullmatch(r'dsd(\d+)', parts[0]) if fmt else None
    if dsd:
        return badges + [f'DSD{dsd.group(1)}', 'Hi-Res']
    if fmt:
        rate = int(parts[0]) if parts[0].isdigit() else 0
        bits = int(parts[1]) if lossless and parts[1:2] and parts[1].isdigit() else 0
        khz = f'{rate / 1000:.1f} kHz' if rate >= 1000 else ''
        badges += [f'{bits}bit / {khz}' if bits and khz else khz] if khz else []
        if lossless and (rate >= 88200 or bits >= 24):
            badges.append('Hi-Res')
    return badges

def check():
    # python3 mpd-bridge.py --check: tests the setup, says what's wrong and
    # how to fix it. Exit status 1 when something needs fixing.
    problems = 0

    def report(status, text, hint=''):
        nonlocal problems
        problems += status == 'FAIL'
        print(f'  {status:<4}  {text}')
        if hint:
            print(f'        {hint}')

    def fetch_json(url):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if '127.0.0.1' in url \
            else urllib.request.build_opener()
        try:
            with opener.open(url, timeout=8) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b'{}')

    print(f'nowplaying {VERSION} - setup check\n')
    report('ok' if sys.version_info >= (3, 7) else 'FAIL', f'Python {sys.version.split()[0]}',
           '' if sys.version_info >= (3, 7) else 'Python 3.7 or later is needed')
    env_file = os.path.join(SCRIPT_DIR, '.env')
    report('ok' if os.path.exists(env_file) else '--',
           f'Settings from {env_file}' if os.path.exists(env_file) else 'No .env file: defaults and environment variables only')

    if DEMO:
        report('--', 'Demo mode (DEMO=1): made-up tracks, mpd is not used')
    else:
        _check_mpd(report)

    # ALSA: only a fallback for the audio format
    if not os.path.isdir('/proc/asound'):
        report('--', 'ALSA: not visible here (another OS, or a container): mpd\'s format is used')
    elif not os.path.isdir(f'/proc/asound/card{ALSA_CARD}'):
        try:
            with open('/proc/asound/cards') as f:
                cards = ', '.join(line.split(']:')[0].strip().replace(' [', ' ').strip()
                                  for line in f if ']:' in line)
        except OSError:
            cards = ''
        report('warn', f'ALSA card {ALSA_CARD} not found ({cards or "no card"})',
               'Only used when mpd gives no format; set ALSA_CARD to the right number')
    else:
        try:
            with open(f'/proc/asound/card{ALSA_CARD}/id') as f:
                card_id = f.read().strip()
        except OSError:
            card_id = '?'
        fmt = get_alsa_format()
        report('--', f'ALSA card {ALSA_CARD} ({card_id}): ' + (f'playing {fmt}' if fmt else 'idle')
               + ' - only used when mpd gives no format')

    # HTTPS
    if TLS_CERT or TLS_KEY:
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(TLS_CERT, TLS_KEY or None)
        except (OSError, ssl.SSLError) as e:
            report('FAIL', f'HTTPS: cannot load TLS_CERT / TLS_KEY: {e}', 'Check both paths (relative to this folder)')
        else:
            try:
                cert = ssl._ssl._test_decode_cert(TLS_CERT)  # names and expiry; CPython only
            except Exception:
                cert = None
            if not cert:
                report('ok', 'HTTPS: certificate and key load')
            else:
                names = ', '.join(v for _, v in cert.get('subjectAltName', ())) or '?'
                days = (ssl.cert_time_to_seconds(cert['notAfter']) - time.time()) / 86400
                text = f'HTTPS: certificate for {names}, ' + (f'valid {int(days)} more days' if days >= 1
                                                                       else 'expires within a day')
                if days < 0:
                    report('FAIL', f'HTTPS: certificate for {names} expired', 'Make a new one (e.g. with mkcert)')
                else:
                    report('warn' if days < 30 else 'ok', text, 'Renew it soon' if days < 30 else '')
    else:
        report('--', 'HTTPS: off, so phones and tablets get no Wake Lock', 'See "HTTPS" in the README')

    # AirPlay
    if DEMO or not SHAIRPORT_PIPE:
        report('--', 'AirPlay: off' + (' (demo mode)' if DEMO else ' (SHAIRPORT_PIPE is empty)'))
    elif not os.path.exists(SHAIRPORT_PIPE):
        report('--', f'AirPlay: no shairport-sync metadata pipe at {SHAIRPORT_PIPE}',
               'To show AirPlay too, enable "metadata" in shairport-sync.conf (see the README)')
    elif not stat.S_ISFIFO(os.stat(SHAIRPORT_PIPE).st_mode):
        report('FAIL', f'AirPlay: {SHAIRPORT_PIPE} is not a named pipe', 'Check pipe_name in shairport-sync.conf')
    elif not os.access(SHAIRPORT_PIPE, os.R_OK):
        report('FAIL', f'AirPlay: no permission to read {SHAIRPORT_PIPE}', 'The bridge must be able to read the pipe')
    else:
        report('ok', f'AirPlay: shairport-sync metadata pipe at {SHAIRPORT_PIPE}')

    _check_spotify(report)

    # Settings page
    if not SETTINGS_PAGE:
        report('--', 'Settings page: off (SETTINGS_PAGE=0)')
    elif os.access(DATA_DIR, os.W_OK):
        report('ok', f'Settings page at /settings, saved in {DATA_DIR}')
    else:
        report('FAIL', f'Settings page: cannot save in {DATA_DIR}', 'Set DATA_DIR to a folder the bridge can write to')

    # Listening history, scrobbling
    if not HISTORY_ENABLED:
        report('--', 'Listening history: off (HISTORY=0)')
    else:
        kept = len(history.Listening(HISTORY_FILE).entries)
        report('ok', f'Listening history at /history: {kept} track(s) in {HISTORY_FILE}')
    for service in scrobble_services():
        try:
            report('ok', f'{service.name}: scrobbling as {service.user()} ({", ".join(SCROBBLE_SOURCES) or "nothing"})')
        except history.ScrobbleError as e:
            report('FAIL', f'{service.name}: {e}', 'Check its settings in .env (see "Listening history" in the README)')
        except Exception as e:
            report('warn', f'{service.name} unreachable: {e}')
    if LASTFM_SECRET and not LASTFM_SESSION:
        report('warn', 'Last.fm: an API secret but no session key, so no scrobbling',
               'Run python3 mpd-bridge.py --lastfm-login')
    elif not scrobble_services():
        report('--', 'Scrobbling: off (ListenBrainz or Last.fm, see "Listening history" in the README)')

    # VU meters
    if DEMO:
        report('--', 'VU meters (?vu=1): made-up levels (demo mode)')
    elif not MPD_FIFO:
        report('--', 'VU meters: off (MPD_FIFO is empty)')
    elif not os.path.exists(MPD_FIFO):
        report('--', f'VU meters (?vu=1): no mpd fifo output at {MPD_FIFO}',
               'To use them, add a "fifo" audio_output to mpd.conf (see the README)')
    elif not is_fifo(MPD_FIFO):
        report('FAIL', f'VU meters: {MPD_FIFO} is not a named pipe', 'Check the path of the fifo output in mpd.conf')
    elif not os.access(MPD_FIFO, os.R_OK):
        report('FAIL', f'VU meters: no permission to read {MPD_FIFO}', 'The bridge must be able to read the pipe')
    else:
        report('ok', f'VU meters (?vu=1): mpd\'s fifo output at {MPD_FIFO}')

    # Port
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('0.0.0.0', PORT))
        report('ok', f'Port {PORT} is free')
    except OSError:
        if TLS_CERT or TLS_KEY:
            report('--', f'Port {PORT} is in use, probably by the bridge itself')
        else:
            try:
                answer = fetch_json(f'http://127.0.0.1:{PORT}/now')
                ours = 'state' in answer or 'error' in answer
            except Exception:
                ours = False
            if ours:
                report('--', f'Port {PORT}: the bridge is already running there')
            else:
                report('FAIL', f'Port {PORT} is taken by another program', 'Choose another PORT in .env')

    # Online artwork
    if ITUNES_ENABLED:
        try:
            found = fetch_json('https://itunes.apple.com/search?term=Daft+Punk+One+More+Time'
                               '&media=music&entity=song&limit=1').get('resultCount', 0)
            report('ok' if found else 'warn', 'iTunes Search API: reachable (artwork for radios, and tracks '
                   'mpd has none for)' if found else 'iTunes Search API: no answer to a test search')
        except Exception as e:
            report('warn', f'iTunes Search API unreachable: {e}',
                   'Radios and tracks without a cover will show no artwork (ITUNES_ARTWORK=0 silences this)')
    else:
        report('--', 'iTunes artwork: off (ITUNES_ARTWORK=0)')
    if LASTFM_ENABLED:
        try:
            q = urllib.parse.urlencode({'method': 'album.getinfo', 'api_key': LASTFM_KEY, 'artist': 'Daft Punk',
                                        'album': 'Discovery', 'format': 'json'})
            answer = fetch_json(f'https://ws.audioscrobbler.com/2.0/?{q}')
            if 'error' in answer:
                report('FAIL', f'Last.fm refused the key: {answer.get("message", answer["error"])}',
                       'Check LASTFM_API_KEY (https://www.last.fm/api/accounts)')
            else:
                report('ok', 'Last.fm: key accepted (artwork fallback)')
        except Exception as e:
            report('warn', f'Last.fm unreachable: {e}')
    else:
        report('--', 'Last.fm artwork fallback: off (no LASTFM_API_KEY)')

    print('\n' + ('No problem found.' if not problems else f'{problems} problem(s) to fix.'))
    return 1 if problems else 0

RASPOTIFY_CONF = '/etc/raspotify/conf'  # raspotify's settings for librespot

def _check_spotify(report):
    # Does raspotify run the event program? (librespot started another way
    # is given --onevent itself: not checked)
    conf = RASPOTIFY_CONF
    if DEMO or not SPOTIFY_ENABLED:
        report('--', 'Spotify Connect: off' + (' (demo mode)' if DEMO else ' (SPOTIFY=0)'))
        return
    try:
        with open(conf) as f:
            text = f.read()
    except FileNotFoundError:
        report('--', 'Spotify Connect: raspotify not found', 'To show Spotify too, see "Spotify Connect" in the README')
        return
    except OSError as e:
        report('--', f'Spotify Connect: cannot read {conf} ({e.strerror})')
        return
    m = re.search(r'^[ \t]*LIBRESPOT_ONEVENT[ \t]*=[ \t]*["\']?([^"\'\n]*)', text, re.M)
    program = m.group(1).split()[0] if m and m.group(1).split() else ''
    if not program:
        report('warn', 'Spotify Connect: raspotify does not tell the bridge what it plays',
               f'Set LIBRESPOT_ONEVENT in {conf} (see "Spotify Connect" in the README)')
    elif not os.access(program, os.X_OK):
        report('FAIL', f'Spotify Connect: LIBRESPOT_ONEVENT runs {program}, which is not an executable file',
               'Install it as the README says (sudo install -m 755 ...)')
    else:
        report('ok', f'Spotify Connect: raspotify tells the bridge what it plays ({program})')

def _check_mpd(report):
    where = f'{MPD_HOST}:{MPD_PORT}'
    try:
        with socket.create_connection((MPD_HOST, MPD_PORT), timeout=3) as s:
            banner = s.recv(1024).decode('utf-8', 'replace').strip()
    except ConnectionRefusedError:
        report('FAIL', f'mpd: nothing answers at {where}',
               'Is mpd running? Check MPD_HOST / MPD_PORT, and port / bind_to_address in mpd.conf')
        return
    except OSError as e:
        report('FAIL', f'mpd: cannot reach {where}: {e}', 'Check MPD_HOST / MPD_PORT, and any firewall')
        return
    if not banner.startswith('OK MPD'):
        report('FAIL', f'Something else than mpd answers at {where}: {banner[:60]!r}', 'Check MPD_PORT')
        return
    report('ok', f'mpd {banner[7:]} at {where}')
    try:
        status = parse_mpd(mpd_command('status'))
        song = parse_mpd(mpd_command('currentsong'))
    except MPDError as e:
        if e.code == ACK_PASSWORD:
            report('FAIL', 'mpd refused MPD_PASSWORD', 'Compare it with the password line of mpd.conf')
        elif e.code == ACK_PERMISSION:
            report('FAIL', 'mpd needs a password', 'Set MPD_PASSWORD in .env')
        else:
            report('FAIL', f'mpd answered with an error: {e}')
        return
    except OSError as e:
        report('FAIL', f'mpd stopped answering: {e}')
        return
    report('ok', 'mpd accepts the password' if MPD_PASSWORD else 'mpd answers without a password')

    # Instant updates rely on mpd's "idle" command
    try:
        s = _mpd_connect()
        try:
            s.sendall(b'idle player\nnoidle\n')
            idle_ok = not _mpd_recv(s).startswith('ACK ')
        finally:
            s.close()
        report('ok' if idle_ok else 'warn', 'Instant updates: mpd\'s idle command works' if idle_ok
               else 'mpd refused "idle": the page will poll every 2 s instead')
    except (OSError, MPDError) as e:
        report('warn', f'Instant updates: idle failed ({e})', 'The page will poll every 2 s instead')

    file_url = song.get('file', '')
    state = status.get('state', 'stop')
    if state == 'stop' or not file_url:
        report('--', 'Nothing playing: start a track, then check again to see its format and artwork')
        return
    report('ok', f'{"Playing" if state == "play" else "Paused"}: {display_title(song)} ({file_url})')
    audio = status.get('audio', '')
    fmt = get_audio_format(status)
    codec, lossless = get_codec(file_url, audio)
    source = 'from mpd' if fmt and fmt == audio else f'from ALSA card {ALSA_CARD}' if fmt else ''
    badges = ', '.join(describe_badges(fmt, codec, lossless))
    report('ok' if fmt else 'warn', f'Audio format {fmt} {source} -> badges: {badges or "none"}' if fmt
           else 'No audio format from mpd nor ALSA: no quality badge')
    if '://' not in file_url:  # streams: no artwork in mpd
        for cmd, where_from in (('readpicture', 'embedded in the file'), ('albumart', 'cover file in its folder')):
            try:
                data, mime = _fetch_mpd_binary(cmd, file_url)
            except (OSError, MPDError, ValueError):
                data = None
            if data:
                size = f'{len(data) // 1024} KB' if len(data) >= 1024 else f'{len(data)} bytes'
                report('ok', f'Artwork: {where_from} ({mime or _sniff_mime(data)}, {size})')
                return
        report('--', 'No artwork in mpd for this track (embedded picture or cover file)')
    if not (ITUNES_ENABLED or LASTFM_ENABLED):
        report('--', 'Online artwork: off', 'ITUNES_ARTWORK=1 (or a LASTFM_API_KEY) looks for it online')
        return
    # What the page gets online: the same lookups, waited for
    try:
        deadline = time.time() + 15
        art = get_status()['art_url']
        while not art and _online_art_running and time.time() < deadline:
            time.sleep(0.2)
            art = get_status()['art_url']
    except (OSError, MPDError) as e:
        report('warn', f'mpd stopped answering: {e}')
        return
    if art:
        report('ok', f'Artwork: found online ({urllib.parse.urlparse(art).netloc})')
    else:
        report('--', 'No artwork online either for this track')

class Server(ThreadingHTTPServer):
    tls = None  # ssl.SSLContext when serving HTTPS

    def finish_request(self, request, client_address):
        if not self.tls:
            return super().finish_request(request, client_address)
        # TLS handshake in the request's own thread, with a deadline: a slow
        # or broken client can't hold up the others
        try:
            request.settimeout(10)
            request = self.tls.wrap_socket(request, server_side=True)
            request.settimeout(None)
        except OSError:  # includes ssl.SSLError, e.g. plain HTTP on the HTTPS port
            return
        try:
            super().finish_request(request, client_address)
        finally:
            self.shutdown_request(request)

def lastfm_login():
    # python3 mpd-bridge.py --lastfm-login: lets the bridge scrobble to your
    # Last.fm account (Last.fm's desktop authentication: a token you allow
    # on its site, exchanged for a session key that doesn't expire)
    if not (LASTFM_ENABLED and LASTFM_SECRET):
        print('First set LASTFM_API_KEY and LASTFM_API_SECRET in .env: create them (free) at\n'
              'https://www.last.fm/api/account/create')
        return 1
    service = history.LastFm(LASTFM_KEY, LASTFM_SECRET)
    try:
        token = service.call('auth.getToken')['token']
        print('Open this address, log in to Last.fm if needed, and allow nowplaying:\n\n'
              f'  https://www.last.fm/api/auth/?api_key={LASTFM_KEY}&token={token}\n')
        input('Then press Enter here... ')
        session = service.call('auth.getSession', token=token)['session']
    except (history.ScrobbleError, OSError, KeyError) as e:
        print(f'\nLast.fm said no: {e}')
        return 1
    line = f'LASTFM_SESSION_KEY={session["key"]}'
    print(f'\nAllowed for {session["name"]}. The session key, for .env:\n\n  {line}\n')
    env_file = os.path.join(SCRIPT_DIR, '.env')
    if input(f'Add it to {env_file} now? [Y/n] ').strip().lower() in ('', 'y', 'yes'):
        try:
            with open(env_file, 'a') as f:
                f.write(f'\n{line}\n')
        except OSError as e:  # e.g. read-only, in a container
            print(f'Cannot write to {env_file} ({e.strerror}): add the line yourself.')
            return 1
        print('Added: restart the bridge to start scrobbling.')
    return 0

def main():
    global LISTENING
    if '--version' in sys.argv[1:]:
        print(f'nowplaying {VERSION}')
        return
    if '--check' in sys.argv[1:]:
        raise SystemExit(check())
    if '--lastfm-login' in sys.argv[1:]:
        raise SystemExit(lastfm_login())
    server = Server(('0.0.0.0', PORT), Handler)
    if TLS_CERT or TLS_KEY:
        server.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            server.tls.load_cert_chain(TLS_CERT, TLS_KEY or None)
        except (OSError, ssl.SSLError) as e:
            log.error('cannot load TLS_CERT=%s / TLS_KEY=%s: %s', TLS_CERT, TLS_KEY, e)
            raise SystemExit(1)
    if DEMO:
        log.info('nowplaying %s on %s port %s, demo mode: made-up tracks, mpd is not used',
                 VERSION, 'HTTPS' if server.tls else 'HTTP', PORT)
    else:
        log.info('nowplaying %s on %s port %s, mpd at %s:%s%s, Last.fm artwork %s, iTunes artwork %s',
                 VERSION, 'HTTPS' if server.tls else 'HTTP', PORT, MPD_HOST, MPD_PORT,
                 ' (with password)' if MPD_PASSWORD else '',
                 'on' if LASTFM_ENABLED else 'off', 'on' if ITUNES_ENABLED else 'off')
    services = [] if DEMO else scrobble_services()
    if HISTORY_ENABLED or services:
        LISTENING = history.Listening(HISTORY_FILE if HISTORY_ENABLED and not DEMO else None,
                                      history.Scrobbler(services, log) if services else None, SCROBBLE_SOURCES, log)
        log.info('listening history %s, scrobbling %s',
                 ('off' if not HISTORY_ENABLED else 'in memory (demo mode)' if DEMO else f'in {HISTORY_FILE}'),
                 f'to {" and ".join(s.name for s in services)} ({", ".join(SCROBBLE_SOURCES)})' if services else 'off')
    threading.Thread(target=watch_demo if DEMO else watch_mpd, name='updates', daemon=True).start()
    if AIRPLAY:
        threading.Thread(target=shairport.follow, args=(SHAIRPORT_PIPE, AIRPLAY, publish_status, log),
                         name='airplay', daemon=True).start()
    server.serve_forever()

if __name__ == '__main__':
    main()
