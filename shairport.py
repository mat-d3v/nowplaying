"""AirPlay: what shairport-sync is playing, read from its metadata pipe.

shairport-sync, an AirPlay receiver, writes what it plays to a named pipe
when the "metadata" section of its configuration is enabled (see the
README). Each piece of metadata is an item like:

    <item><type>636f7265</type><code>6d696e6d</code><length>9</length>
    <data encoding="base64">
    U29tZSBzb25n</data></item>

type and code are four characters in hexadecimal ("core" / "minm": the
title), the data is base64.
"""
import base64
import os
import re
import stat
import threading
import time

ITEM = re.compile(rb'<item><type>([0-9a-fA-F]{1,8})</type><code>([0-9a-fA-F]{1,8})</code><length>(\d+)</length>'
                  rb'(?:\s*<data encoding="base64">([A-Za-z0-9+/=\s]*)</data>)?\s*</item>')
MAX_BUFFER = 8 * 1024 * 1024  # a big cover is about 1 MB of base64


def _four_chars(hex_text):
    return bytes.fromhex(hex_text.decode().rjust(8, '0')).decode('latin-1')


def parse_items(buffer):
    # The complete items at the start of buffer, and what's left after them
    # (an item still being written)
    items, end = [], 0
    for m in ITEM.finditer(buffer):
        data = base64.b64decode(re.sub(rb'\s', b'', m.group(4) or b''))
        items.append((_four_chars(m.group(1)), _four_chars(m.group(2)), data))
        end = m.end()
    rest = buffer[end:].lstrip()  # the newline after the last item
    if len(rest) > MAX_BUFFER:  # garbage that never makes an item: drop it
        start = rest.rfind(b'<item>')
        rest = rest[start:] if start > 0 else b''
    return items, rest


TRACK_FIELDS = {'minm': 'title', 'asar': 'artist', 'asal': 'album'}


class AirPlay:
    # What shairport-sync is playing, kept up to date from its metadata items
    def __init__(self):
        self.lock = threading.Lock()
        self.session = False   # between the start and the end of a play session
        self.playing = False
        self.sender = ''       # e.g. "Mat's iPhone"
        self.picture, self.picture_id = None, 0
        self._pending = None   # track fields of a metadata bundle being received
        self._clear_track()

    def _clear_track(self):
        self.title = self.artist = self.album = ''
        self.duration_ms = 0
        self.picture = None
        self.elapsed, self.duration, self.since = 0.0, 0.0, None

    def _now_elapsed(self):
        elapsed = self.elapsed
        if self.playing and self.since is not None:
            elapsed += time.monotonic() - self.since
        return min(elapsed, self.duration) if self.duration else elapsed

    def _start(self):
        # Metadata only flows while something plays: a session is on even if
        # its start went by before the bridge was there
        if not self.session:
            self.session = self.playing = True
            self.since = time.monotonic()

    def handle(self, kind, code, data):
        # Applies one metadata item; True when what the page shows changed
        with self.lock:
            return self._handle(kind, code, data)

    def _handle(self, kind, code, data):
        if kind == 'core':
            if code in TRACK_FIELDS:
                value = data.decode('utf-8', 'replace')
                if self._pending is not None:
                    self._pending[TRACK_FIELDS[code]] = value
                    return False
                setattr(self, TRACK_FIELDS[code], value)
                self._start()
                return True
            if code == 'astm' and len(data) == 4:  # duration, in milliseconds
                self.duration_ms = int.from_bytes(data, 'big')
            return False
        if kind != 'ssnc':
            return False
        if code == 'mdst':      # a bundle of track metadata starts
            self._pending = {}
            return False
        if code == 'mden':      # ...and ends: apply it all at once
            if self._pending:
                for field, value in self._pending.items():
                    setattr(self, field, value)
            self._pending = None
            self._start()
            return True
        if code == 'PICT':      # the cover (JPEG or PNG), or none
            self.picture = data or None
            self.picture_id += 1
            self._start()
            return True
        if code == 'prgr':      # progress: RTP timestamps "start/current/end"
            try:
                start, current, end = (int(v) for v in data.decode().split('/'))
            except ValueError:
                return False
            total = (end - start) % 2 ** 32
            rate = 44100        # AirPlay's frame rate, unless the duration says 48 kHz
            if self.duration_ms and total:
                guess = total / (self.duration_ms / 1000)
                rate = 48000 if abs(guess - 48000) < abs(guess - 44100) else 44100
            self.elapsed = ((current - start) % 2 ** 32) / rate
            self.duration = total / rate
            self._start()
            self.since = time.monotonic()
            return True
        if code == 'pbeg':      # play session begins
            self.session = self.playing = True
            self.since = time.monotonic()
            return True
        if code == 'prsm':      # resume after a pause
            self.session = self.playing = True
            self.since = time.monotonic()
            return True
        if code == 'pfls':      # flush: pause, or a seek about to happen
            self.elapsed = self._now_elapsed()
            self.playing, self.since = False, None
            return True
        if code in ('pend', 'aend', 'disc'):  # the session is over
            changed = self.session
            self.session = self.playing = False
            self._clear_track()
            return changed
        if code == 'snam':      # who's playing: "X-Apple-Client-Name"
            self.sender = data.decode('utf-8', 'replace')
            return self.session
        return False

    def status(self):
        # The page's payload while an AirPlay session is on, None otherwise
        with self.lock:
            if not self.session:
                return None
            return {
                'state': 'play' if self.playing else 'pause',
                'title': self.title or 'AirPlay',
                'artist': self.artist or self.sender or '—',
                'album': self.album,
                'elapsed': self._now_elapsed(),
                'duration': self.duration,
                'format': '',
                'codec': 'AirPlay',
                'lossless': False,
                'art_url': f'/art?airplay={self.picture_id}' if self.picture else '',
                'file': '',
                'stream': False,
                'source': 'airplay',
                'sender': self.sender,
                'next_title': '',
                'next_artist': '',
            }

    def cover(self):
        with self.lock:
            return self.picture


def follow(path, airplay, on_change, log):
    # Reads shairport-sync's metadata pipe for good: waits for it to exist,
    # reopens it when shairport-sync restarts, calls on_change() when what
    # the page shows changed
    found, last_error = False, None
    while True:
        try:
            if not stat.S_ISFIFO(os.stat(path).st_mode):
                raise OSError(f'{path} is not a named pipe')
            if not found:
                log.info('AirPlay: reading shairport-sync metadata from %s', path)
                found = True
            with open(path, 'rb', buffering=0) as pipe:  # waits for shairport-sync to open it
                buffer = b''
                while True:
                    chunk = pipe.read(65536)
                    if not chunk:
                        break  # shairport-sync closed it: open it again
                    items, buffer = parse_items(buffer + chunk)
                    if any([airplay.handle(*item) for item in items]):
                        on_change()
        except FileNotFoundError:
            time.sleep(5)  # shairport-sync not started (yet), or metadata off
        except Exception as e:
            if str(e) != last_error:  # once per problem, not every 30 s
                log.warning('AirPlay: cannot read %s: %s', path, e)
                last_error = str(e)
            time.sleep(30)
