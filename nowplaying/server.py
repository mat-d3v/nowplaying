"""The web server: the pages, their updates (/events), artwork, settings.

    /                   the page (index.html), with the saved display settings
    /now, /events       what's playing: once, or each change as it happens
    /art                artwork: from mpd, AirPlay or the demo mode
    /settings, /display the settings page, and its settings (GET, POST)
    /history(.json)     the listening history
    /spotify            librespot's events (POST, from this machine only)
"""
import ipaddress
import json
import logging
import os
import queue
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, demo, display, mpd, status

log = logging.getLogger('nowplaying')

PAGE = ('index.html', 'text/html; charset=utf-8')
SETTINGS_PAGE = ('settings.html', 'text/html; charset=utf-8')
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
    '/settings': SETTINGS_PAGE,
    '/history': ('history.html', 'text/html; charset=utf-8'),
}

_spotify_seen = False  # logged once, on librespot's first event


class Handler(BaseHTTPRequestHandler):
    def send(self, code, data=b'', content_type=None, **headers):
        # A whole answer, with its length: the client knows where it ends
        # without waiting for the connection to close (over HTTPS, Python
        # 3.7 clients took a close without TLS's goodbye for a cut-off)
        self.send_response(code)
        if content_type:
            self.send_header('Content-Type', content_type)
        for name, value in headers.items():
            self.send_header(name.replace('_', '-'), value)
        if code != 204:  # "No Content" has no length at all
            self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data, code=200):
        self.send(code, json.dumps(data).encode(), 'application/json', Access_Control_Allow_Origin='*')

    def stream_events(self, with_levels):
        # Server-sent events: the current status, then every change; with
        # ?vu=1, the levels for the VU meters too ("levels" events)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')  # nginx: pass events through unbuffered
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        q = status.events.subscribe()
        levels = status.LEVELS
        if with_levels and levels:
            levels.listen(q)
        try:
            self.wfile.write(b'retry: 2000\n\n')  # reconnect delay after a bridge restart
            self.send_event(status.status_or_error()[0])
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
            if levels:
                levels.unlisten(q)
            status.events.unsubscribe(q)

    def send_event(self, payload):
        self.wfile.write(f'data: {json.dumps(payload)}\n\n'.encode())

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == '/events':
            self.stream_events(urllib.parse.parse_qs(url.query).get('vu') == ['1'])
        elif url.path == '/now':
            try:
                self.send_json(*status.status_or_error())
            except Exception as e:
                log.exception('/now failed')
                self.send_json({'error': 'internal', 'detail': str(e)}, 500)
        elif url.path == '/art':
            self.send_art(urllib.parse.parse_qs(url.query))
        elif url.path == '/display':
            self.send_json(display.load())
        elif url.path == '/history.json':
            on = bool(status.LISTENING) and config.HISTORY_ENABLED
            self.send_json({'enabled': on, 'tracks': status.LISTENING.recent() if on else []})
        elif url.path in STATIC_FILES and (STATIC_FILES[url.path] != SETTINGS_PAGE or config.SETTINGS_PAGE):
            self.send_file(*STATIC_FILES[url.path])
        else:
            self.send(404)

    def send_art(self, qs):
        uri = qs.get('file', [''])[0]
        if config.DEMO and qs.get('demo', [''])[0].isdigit():
            data, mime = demo.artwork(qs['demo'][0]), 'image/png'
        elif status.AIRPLAY and 'airplay' in qs:
            data = status.AIRPLAY.cover()
            mime = mpd.sniff_mime(data) if data else ''
        else:
            data, mime = mpd.get_art(uri) if uri else (None, '')
        if data:
            self.send(200, data, mime or 'image/jpeg', Cache_Control='max-age=3600', Access_Control_Allow_Origin='*')
        else:
            self.send(404)

    def send_file(self, filename, content_type):
        try:
            with open(os.path.join(config.SCRIPT_DIR, filename), 'rb') as f:
                data = f.read()
        except OSError:
            self.send(404)
            return
        if filename == 'index.html':
            # The saved display settings, in the page itself: they apply
            # from the start (<, written <, can't end the script)
            saved = json.dumps(display.load()).replace('<', '\\u003c')
            data = data.replace(b'/*display-settings*/null', saved.encode(), 1)
        if content_type.startswith('text/html'):
            self.send(200, data, content_type, Cache_Control='no-cache')
        else:
            self.send(200, data, content_type)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == '/spotify' and status.SPOTIFY:
            self.spotify_event()
        elif url.path == '/display' and config.SETTINGS_PAGE:
            self.save_display()
        else:
            self.send(404)

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
        settings = display.valid(self.read_json())
        if settings is None:
            self.send_json({'error': 'unknown setting or value'}, 400)
            return
        try:
            display.save(settings)
        except OSError as e:
            log.error('cannot save the display settings in %s: %s', display.path(), e)
            self.send_json({'error': f'cannot write {display.path()} ({e.strerror})'}, 500)
            return
        log.info('display settings saved: %s', settings)
        status.events.publish(('settings', settings))  # the screens reload with them
        self.send(204)

    def spotify_event(self):
        # An event from librespot, posted by spotify-event.py. Only from this
        # machine: elsewhere on the network, nobody can make the page show
        # what they like
        global _spotify_seen
        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
            self.send_json({'error': 'only from this machine'}, 403)
            return
        event = self.read_json()
        if not isinstance(event, dict) or not all(isinstance(v, str) for v in event.values()):
            self.send_json({'error': 'expected a JSON object of strings'}, 400)
            return
        if not _spotify_seen:
            log.info('Spotify Connect: librespot tells what it plays')
            _spotify_seen = True
        if status.SPOTIFY.handle(event):
            status.publish_status()
        self.send(204)

    def log_message(self, *args):
        pass


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
