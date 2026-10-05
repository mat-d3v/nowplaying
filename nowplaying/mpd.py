"""mpd's protocol: commands over a shared connection, and embedded artwork.

The bridge's web server is multi-threaded: commands share one connection,
one at a time. Artwork comes in binary chunks, over a connection of its
own, so they never mix with the text answers.
"""
import logging
import re
import socket
import threading

from . import config

log = logging.getLogger('nowplaying')

ACK_PASSWORD, ACK_PERMISSION = 3, 4


class MPDError(Exception):
    # An "ACK [code@index] {command} message" answer from mpd
    def __init__(self, line):
        super().__init__(line)
        m = re.match(r'ACK \[(\d+)@', line)
        self.code = int(m.group(1)) if m else 0


def quote(arg):
    return arg.replace('\\', '\\\\').replace('"', '\\"')


def connect():
    s = socket.create_connection((config.MPD_HOST, config.MPD_PORT), timeout=3)  # a frozen mpd can't hang requests
    try:
        s.recv(1024)  # "OK MPD x.y.z" banner
        if config.MPD_PASSWORD:
            s.sendall(f'password "{quote(config.MPD_PASSWORD)}"\n'.encode())
            response = recv(s)
            if response.startswith('ACK '):
                raise MPDError(response.strip())
    except BaseException:
        s.close()
        raise
    return s


def recv(sock):
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


_sock = None
_lock = threading.Lock()  # the HTTP server is multi-threaded, the mpd socket is shared


def command(cmd):
    with _lock:
        response = _command_unlocked(cmd)
    if response.startswith('ACK '):
        raise MPDError(response.strip())
    return response


def _command_unlocked(cmd):
    global _sock
    try:
        if _sock is None:
            _sock = connect()
        _sock.sendall((cmd + '\n').encode())
        return recv(_sock)
    except Exception:
        # mpd closes idle connections after a while: reconnect once
        if _sock is not None:
            _sock.close()
        _sock = None
        s = connect()
        try:
            s.sendall((cmd + '\n').encode())
            result = recv(s)
        except BaseException:
            s.close()
            raise
        _sock = s
        return result


def parse(raw):
    d = {}
    for line in raw.splitlines():
        if ': ' in line:
            k, v = line.split(': ', 1)
            d[k.lower()] = v
    return d


def fetch_binary(cmd, uri):
    # Fetch a binary object (album art) over its own mpd connection:
    # keeping binary chunks off the shared text socket avoids any desync.
    s = connect()
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

        quoted = quote(uri)
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


def sniff_mime(data):
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'


_art_cache = {}  # file uri -> (bytes|None, mime)


def get_art(uri):
    # readpicture: embedded tag art; albumart: cover file next to the track
    if uri in _art_cache:
        return _art_cache[uri]
    result = (None, '')
    for cmd in ('readpicture', 'albumart'):
        try:
            data, mime = fetch_binary(cmd, uri)
            if data:
                result = (data, mime or sniff_mime(data))
                break
        except Exception as e:
            log.debug('%s failed for %s: %s', cmd, uri, e)
    if len(_art_cache) > 20:
        _art_cache.clear()  # art blobs can be large, keep this cache small
    _art_cache[uri] = result
    return result
