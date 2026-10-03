#!/usr/bin/env python3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import socket, json, urllib.request, urllib.parse, re, os, threading, logging

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
LASTFM_KEY = os.environ.get('LASTFM_API_KEY', '')
# Empty, or still the placeholder of older setups: no Last.fm lookups
LASTFM_ENABLED = LASTFM_KEY not in ('', 'your_lastfm_api_key_here')

# systemd's journal already timestamps each line
logging.basicConfig(level=logging.INFO, format=('%(levelname)s %(message)s' if os.environ.get('JOURNAL_STREAM')
                                                else '%(asctime)s %(levelname)s %(message)s'))
log = logging.getLogger('nowplaying')

_art_cache = {}

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

def get_art_url(artist, album):
    key = f"{artist}|{album}"
    if key in _art_cache:
        return _art_cache[key]
    if len(_art_cache) > 500:
        _art_cache.clear()  # simple cap to avoid unbounded growth
    try:
        q = urllib.parse.urlencode({
            'method': 'album.getinfo',
            'api_key': LASTFM_KEY,
            'artist': artist,
            'album': album,
            'format': 'json'
        })
        url = f'https://ws.audioscrobbler.com/2.0/?{q}'
        data = json.loads(urllib.request.urlopen(url, timeout=5).read())
        images = data.get('album', {}).get('image', [])
        art = ''
        for img in reversed(images):
            if img.get('#text'):
                art = re.sub(r'/\d+x\d+/', '/', img['#text'])
                break
        _art_cache[key] = art
        return art
    except Exception as e:
        log.warning('Last.fm lookup failed for %s / %s: %s', artist, album, e)
    _art_cache[key] = ''
    return ''

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

def get_codec(file_url, audio):
    # Streams don't say what they carry: no badge rather than a wrong one
    if not file_url or '://' in file_url:
        return '', False
    name = file_url.rsplit('/', 1)[-1]
    if '.' not in name:
        return '', False
    ext = name.rsplit('.', 1)[1].lower()
    if ext in ('m4a', 'mp4'):
        # Same container for AAC and ALAC: mpd decodes AAC to float samples
        # ("44100:f:2") and ALAC to integer ones ("44100:16:2")
        if not audio:
            return 'M4A', False
        return ('AAC', False) if audio.split(':')[1:2] == ['f'] else ('ALAC', True)
    return CODECS.get(ext, (ext.upper(), False))

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

def get_status():
    status = parse_mpd(mpd_command('status'))
    currentsong = parse_mpd(mpd_command('currentsong'))
    state = status.get('state', 'stop')
    elapsed = float(status.get('elapsed', 0))
    duration = float(status.get('duration', 0))
    file_url = currentsong.get('file', '')
    artist = currentsong.get('artist', '')
    album = currentsong.get('album', '')
    title = display_title(currentsong)
    # Radios: the station name goes on the artist line when the stream sends
    # no artist (and the name isn't already used as the title)
    name = currentsong.get('name', '')
    shown_artist = artist or (name if name != title else '')
    fmt = get_audio_format(status) if state != 'stop' else ''
    codec, lossless = get_codec(file_url, status.get('audio', ''))
    # Artwork: prefer mpd itself (embedded tags or cover file, no API key
    # needed), fall back to Last.fm for streams or when mpd has nothing.
    art_url = ''
    if file_url and not file_url.startswith('http'):
        data, _ = get_mpd_art(file_url)
        if data:
            art_url = '/art?file=' + urllib.parse.quote(file_url, safe='')
    if not art_url and artist and album and LASTFM_ENABLED:
        art_url = get_art_url(artist, album)
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
        'album': album,
        'elapsed': elapsed,
        'duration': duration,
        'format': fmt,
        'codec': codec,
        'lossless': lossless,
        'art_url': art_url,
        'file': file_url,
        'next_title': next_title,
        'next_artist': next_artist
    }

_mpd_problem = None  # last mpd error logged, so each change is logged once
PROBLEMS = {'mpd_unreachable': 'unreachable', 'mpd_password': 'refused access (check MPD_PASSWORD)',
            'mpd_error': 'answered with an error'}

def status_or_error():
    # (payload, HTTP status): the player status, or why mpd can't give it
    global _mpd_problem
    try:
        data = get_status()
    except MPDError as e:
        kind = 'mpd_password' if e.code in (ACK_PASSWORD, ACK_PERMISSION) else 'mpd_error'
        problem = (kind, str(e))
    except OSError as e:  # refused, timed out, closed...
        problem = ('mpd_unreachable', str(e) or e.__class__.__name__)
    else:
        if _mpd_problem:
            log.info('mpd at %s:%s is back', MPD_HOST, MPD_PORT)
            _mpd_problem = None
        return data, 200
    if problem != _mpd_problem:
        log.warning('mpd at %s:%s %s: %s', MPD_HOST, MPD_PORT, PROBLEMS[problem[0]], problem[1])
        _mpd_problem = problem
    return {'error': problem[0], 'detail': problem[1]}, 503

PAGE = ('index.html', 'text/html; charset=utf-8')
STATIC_FILES = {
    '/': PAGE,
    '/index.html': PAGE,
    # Same page: it switches to French from this path (or ?lang=fr)
    '/index.fr.html': PAGE,
    '/hires.svg': ('hires.svg', 'image/svg+xml'),
    '/apple-touch-icon.png': ('assets/apple-touch-icon.png', 'image/png'),
    '/touch-icon-v2.png': ('assets/touch-icon-v2.png', 'image/png'),
}

class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == '/now':
            try:
                self.send_json(*status_or_error())
            except Exception as e:
                log.exception('/now failed')
                self.send_json({'error': 'internal', 'detail': str(e)}, 500)
        elif url.path == '/art':
            qs = urllib.parse.parse_qs(url.query)
            uri = qs.get('file', [''])[0]
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
        elif url.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[url.path]
            filepath = os.path.join(SCRIPT_DIR, filename)
            try:
                with open(filepath, 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *args): pass

log.info('nowplaying bridge on port %s, mpd at %s:%s%s, Last.fm artwork %s', PORT, MPD_HOST, MPD_PORT,
         ' (with password)' if MPD_PASSWORD else '', 'on' if LASTFM_ENABLED else 'off')
ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
