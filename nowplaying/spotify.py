"""Spotify Connect: what librespot (raspotify) is playing, from its events.

librespot runs a program on each event when started with --onevent
(LIBRESPOT_ONEVENT in raspotify's settings), with the details in its
environment: PLAYER_EVENT ("track_changed", "playing", "paused"...),
NAME, ARTISTS, ALBUM, COVERS, DURATION_MS, POSITION_MS, CLIENT_NAME...
spotify-event.py is that program: it posts them to the bridge, which
hands them to Spotify.handle(). Track details come with librespot 0.5 or
later: before, it only tells the progress.
"""
import threading
import time

# Spotify's cover addresses say their size: 640, 300 then 64 pixels
COVER_SIZES = ('ab67616d0000b273', 'ab67616d00001e02', 'ab67616d00004851')


def pick_cover(covers):
    # The biggest of the cover addresses (one per line)
    urls = [url.strip() for url in covers.splitlines() if url.strip().startswith('https://')]

    def rank(url):
        return next((i for i, size in enumerate(COVER_SIZES) if size in url), len(COVER_SIZES))
    return min(urls, key=rank) if urls else ''


def _seconds(ms):
    try:
        return max(0.0, int(ms) / 1000)
    except (TypeError, ValueError):
        return 0.0


class Spotify:
    # What librespot is playing, kept up to date from its events
    STALE = 60  # seconds past the end of a track without news: librespot is gone

    def __init__(self):
        self.lock = threading.Lock()
        self.state = 'stop'   # 'play', 'pause' or 'stop' (no session, or it ended)
        self.started = 0.0    # when it last started playing (time.monotonic)
        self.sender = ''      # the device in control, e.g. "Mat's iPhone"
        self.title = self.artist = self.album = self.cover = ''
        self.duration = 0.0
        self.position, self.since = 0.0, None  # position at `since`, while playing

    def _elapsed(self):
        elapsed = self.position
        if self.state == 'play' and self.since is not None:
            elapsed += time.monotonic() - self.since
        return min(elapsed, self.duration) if self.duration else elapsed

    def handle(self, event):
        # Applies one event (librespot's environment variables); True when
        # what the page shows changed
        with self.lock:
            return self._handle(event.get('PLAYER_EVENT', ''), event)

    def _handle(self, kind, event):
        now = time.monotonic()
        if kind == 'track_changed':
            self.title = event.get('NAME', '')
            if event.get('ITEM_TYPE') == 'Episode':  # a podcast: the show instead
                self.artist, self.album = event.get('SHOW_NAME', ''), ''
            else:
                self.artist = ', '.join(a for a in event.get('ARTISTS', '').splitlines() if a)
                self.album = event.get('ALBUM', '')
            self.cover = pick_cover(event.get('COVERS', ''))
            self.duration = _seconds(event.get('DURATION_MS'))
            self.position, self.since = 0.0, now if self.state == 'play' else None
            return self.state != 'stop'
        if kind in ('playing', 'paused'):
            if kind == 'playing' and self.state != 'play':
                self.started = now
            self.state = 'play' if kind == 'playing' else 'pause'
            self.position = _seconds(event.get('POSITION_MS'))
            self.since = now if kind == 'playing' else None
            if event.get('DURATION_MS'):  # librespot before 0.5 only says it here
                self.duration = _seconds(event['DURATION_MS'])
            return True
        if kind in ('seeked', 'position_correction'):
            self.position = _seconds(event.get('POSITION_MS'))
            self.since = now if self.state == 'play' else None
            return self.state != 'stop'
        if kind in ('stopped', 'session_disconnected'):  # playback moved away, or ended
            changed = self.state != 'stop'
            self.state = 'stop'
            return changed
        if kind == 'session_client_changed':
            self.sender = event.get('CLIENT_NAME', '')
            return self.state != 'stop'
        return False  # loading, preloading, volume_changed...

    def status(self):
        # The page's payload while Spotify plays or is paused, None otherwise
        with self.lock:
            if self.state == 'stop':
                return None
            if (self.state == 'play' and self.duration and self.since is not None
                    and self.position + time.monotonic() - self.since > self.duration + self.STALE):
                return None  # long past the end: librespot stopped without saying so
            return {
                'state': self.state,
                'title': self.title or 'Spotify',
                'artist': self.artist or self.sender or '—',
                'album': self.album,
                'elapsed': self._elapsed(),
                'duration': self.duration,
                'format': '',
                'codec': 'Spotify',
                'lossless': False,
                'art_url': self.cover,
                'file': '',
                'stream': False,
                'source': 'spotify',
                'sender': self.sender,
                'next_title': '',
                'next_artist': '',
            }
