#!/usr/bin/env python3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import socket, json, urllib.request, urllib.parse, re, os, threading, logging, queue, time, ssl, sys

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
# Radio artwork from the iTunes Search API (free, no key): on unless set to 0
ITUNES_ENABLED = os.environ.get('ITUNES_ARTWORK', '1').strip().lower() not in ('0', 'false', 'no', 'off')

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

_itunes_cache = {}

def get_itunes_art(artist, title):
    # Artwork for radio tracks: radios send no album, so Last.fm's
    # album.getinfo can't help, but iTunes finds the song from artist + title
    key = f'{artist}|{title}'
    if key in _itunes_cache:
        return _itunes_cache[key]
    if len(_itunes_cache) > 500:
        _itunes_cache.clear()  # simple cap to avoid unbounded growth
    art = ''
    try:
        q = urllib.parse.urlencode({'term': f'{artist} {title}', 'media': 'music', 'entity': 'song', 'limit': 1})
        with urllib.request.urlopen(f'https://itunes.apple.com/search?{q}', timeout=5) as r:
            results = json.loads(r.read()).get('results', [])
        if results and results[0].get('artworkUrl100'):
            # 100x100 thumbnails; the same URL serves larger sizes
            art = results[0]['artworkUrl100'].replace('/100x100bb.', '/600x600bb.')
    except Exception as e:
        log.warning('iTunes artwork lookup failed for %s - %s: %s', artist, title, e)
    _itunes_cache[key] = art
    return art

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
    # Artwork: mpd itself first (embedded tags or cover file, no API key
    # needed), then Last.fm (artist + album), then iTunes for radios
    # (artist + title, as radios send no album)
    art_url = ''
    if file_url and not stream:
        data, _ = get_mpd_art(file_url)
        if data:
            art_url = '/art?file=' + urllib.parse.quote(file_url, safe='')
    if not art_url and artist and album and LASTFM_ENABLED:
        art_url = get_art_url(artist, album)
    if not art_url and stream and shown_artist and shown_artist != name and ITUNES_ENABLED:
        art_url = get_itunes_art(shown_artist, title)
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

class Broadcaster:
    # Fans each status change out to the pages listening on /events
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()

    def subscribe(self):
        q = queue.Queue(maxsize=20)
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

def publish_status():
    # The current status (or mpd's error) to the pages listening, if any.
    # Never raises: the watcher thread must outlive any bug in here, or the
    # pages would keep a live but silent event stream and freeze
    if events.has_clients():
        try:
            events.publish(status_or_error()[0])
        except Exception:
            log.exception('cannot build the status for /events')

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

PAGE = ('index.html', 'text/html; charset=utf-8')
STATIC_FILES = {
    '/': PAGE,
    '/index.html': PAGE,
    # Same page: it switches to French from this path (or ?lang=fr)
    '/index.fr.html': PAGE,
    '/apple-touch-icon.png': ('assets/apple-touch-icon.png', 'image/png'),
    '/touch-icon-v2.png': ('assets/touch-icon-v2.png', 'image/png'),
    '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
    '/icon-192.png': ('assets/icon-192.png', 'image/png'),
    '/icon-512.png': ('assets/logo.png', 'image/png'),
}

class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def stream_events(self):
        # Server-sent events: the current status, then every change
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')  # nginx: pass events through unbuffered
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        q = events.subscribe()
        try:
            self.wfile.write(b'retry: 2000\n\n')  # reconnect delay after a bridge restart
            self.send_event(status_or_error()[0])
            while True:
                try:
                    self.send_event(q.get(timeout=20))
                except queue.Empty:
                    # Keeps proxies from closing a quiet stream, and finds out
                    # about pages that went away
                    self.wfile.write(b': ping\n\n')
        except OSError:
            pass  # the page went away
        finally:
            events.unsubscribe(q)

    def send_event(self, payload):
        self.wfile.write(f'data: {json.dumps(payload)}\n\n'.encode())

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == '/events':
            self.stream_events()
        elif url.path == '/now':
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

    print('nowplaying setup check\n')
    report('ok' if sys.version_info >= (3, 7) else 'FAIL', f'Python {sys.version.split()[0]}',
           '' if sys.version_info >= (3, 7) else 'Python 3.7 or later is needed')
    env_file = os.path.join(SCRIPT_DIR, '.env')
    report('ok' if os.path.exists(env_file) else '--',
           f'Settings from {env_file}' if os.path.exists(env_file) else 'No .env file: defaults and environment variables only')

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
            report('ok' if found else 'warn', 'iTunes Search API: reachable (radio artwork)' if found
                   else 'iTunes Search API: no answer to a test search')
        except Exception as e:
            report('warn', f'iTunes Search API unreachable: {e}', 'Radios will show no artwork (ITUNES_ARTWORK=0 silences this)')
    else:
        report('--', 'iTunes radio artwork: off (ITUNES_ARTWORK=0)')
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
    if '://' in file_url:
        report('--', 'A stream: artwork comes from iTunes (see below)')
        return
    for cmd, where_from in (('readpicture', 'embedded in the file'), ('albumart', 'cover file in its folder')):
        try:
            data, mime = _fetch_mpd_binary(cmd, file_url)
        except (OSError, MPDError, ValueError):
            data = None
        if data:
            size = f'{len(data) // 1024} KB' if len(data) >= 1024 else f'{len(data)} bytes'
            report('ok', f'Artwork: {where_from} ({mime or _sniff_mime(data)}, {size})')
            return
    report('--', 'No artwork in mpd for this track (embedded picture or cover file)',
           'Last.fm can fill in: set LASTFM_API_KEY' if not LASTFM_ENABLED else '')

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

def main():
    if '--check' in sys.argv[1:]:
        raise SystemExit(check())
    server = Server(('0.0.0.0', PORT), Handler)
    if TLS_CERT or TLS_KEY:
        server.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            server.tls.load_cert_chain(TLS_CERT, TLS_KEY or None)
        except (OSError, ssl.SSLError) as e:
            log.error('cannot load TLS_CERT=%s / TLS_KEY=%s: %s', TLS_CERT, TLS_KEY, e)
            raise SystemExit(1)
    log.info('nowplaying bridge on %s port %s, mpd at %s:%s%s, Last.fm artwork %s, iTunes radio artwork %s',
             'HTTPS' if server.tls else 'HTTP', PORT, MPD_HOST, MPD_PORT,
             ' (with password)' if MPD_PASSWORD else '',
             'on' if LASTFM_ENABLED else 'off', 'on' if ITUNES_ENABLED else 'off')
    threading.Thread(target=watch_mpd, name='mpd-idle', daemon=True).start()
    server.serve_forever()

if __name__ == '__main__':
    main()
