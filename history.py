"""Listening history, and scrobbling to ListenBrainz or Last.fm.

The bridge hands every status it builds to Listening.observe(), whatever
plays it (mpd, AirPlay, Spotify). When a track ends (the next one starts,
or playback stops), it goes in the history if it played long enough, and
gets scrobbled. The history is saved in DATA_DIR/history.jsonl, a track
per line, the latest last.
"""
import collections
import hashlib
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

KEEP = 1000      # tracks in the history
MIN_PLAYED = 30  # seconds: a track skipped sooner doesn't count


def counts(played, duration):
    # Last.fm's rule: half the track, or 4 minutes; tracks of 30 s or less
    # never count. Without a duration (radios): 30 s
    if duration:
        return duration > 30 and played >= min(duration / 2, 240)
    return played >= MIN_PLAYED


class Listening:
    # Follows what plays, keeps the history, hands plays to the scrobbler
    def __init__(self, path=None, scrobbler=None, scrobble_sources=(), log=None):
        self.path = path  # None: in memory only
        self.scrobbler = scrobbler
        self.scrobble_sources = set(scrobble_sources)
        self.log = log
        self.lock = threading.Lock()
        self.entries = collections.deque(self._load(), maxlen=KEEP)
        self.lines = len(self.entries)  # in the file, to compact it now and then
        self.current = None              # the track playing (or paused), and how long it played
        self.playing_since = None

    def _load(self):
        entries = []
        try:
            with open(self.path) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue  # a line cut short by a power cut
                    if isinstance(entry, dict) and entry.get('title'):
                        entries.append(entry)
        except (OSError, TypeError):
            pass
        return entries[-KEEP:]

    def _save(self, entry):
        if not self.path:
            return
        try:
            if self.lines >= 2 * KEEP:  # rewritten now and then, so it doesn't grow forever
                temporary = self.path + '.tmp'
                with open(temporary, 'w') as f:
                    f.writelines(json.dumps(e) + '\n' for e in self.entries)
                os.replace(temporary, self.path)
                self.lines = len(self.entries)
            else:
                with open(self.path, 'a') as f:
                    f.write(json.dumps(entry) + '\n')
                self.lines += 1
        except OSError as e:
            if self.log:
                self.log.warning('cannot save the listening history in %s: %s', self.path, e)

    def recent(self, count=200):
        # The latest tracks, the latest first
        with self.lock:
            return list(self.entries)[-count:][::-1]

    def observe(self, status):
        # A status built by the bridge (any player): follows the current
        # track, and finishes the previous one when it changes
        with self.lock:
            now = time.monotonic()
            if self.current and self.playing_since is not None:
                self.current['played'] += now - self.playing_since
                self.playing_since = None
            state = status.get('state')
            key = None
            if state in ('play', 'pause') and status.get('title') and not status.get('error'):
                key = (status.get('source'), status['title'], status.get('artist', ''), status.get('album', ''))
            current = self.current
            elapsed = status.get('elapsed') or 0
            # The same track again from the start (repeat): a new play
            again = (current and key == current['key'] and state == 'play'
                     and elapsed < current['elapsed'] - 10 and current['played'] > 10)
            if not current or key != current['key'] or again:
                self._finish()
                if key:
                    self._start(key, status, elapsed)
            if self.current:
                self.current['elapsed'] = elapsed
                self.current['duration'] = status.get('duration') or self.current['duration']
                if status.get('art_url'):
                    self.current['art'] = status['art_url']
                if state == 'play':
                    self.playing_since = now
                    if not self.current['announced']:
                        self.current['announced'] = True
                        if self._scrobbles(self.current):
                            self.scrobbler.now_playing(self._entry(self.current))

    def _start(self, key, status, elapsed):
        self.current = {
            'key': key, 'source': key[0], 'title': key[1], 'artist': key[2], 'album': key[3],
            'station': status.get('station', ''), 'art': status.get('art_url', ''),
            'duration': status.get('duration') or 0, 'elapsed': elapsed,
            # When it started: it may have been playing before the bridge saw it
            'at': int(time.time() - elapsed), 'played': 0.0, 'announced': False,
        }

    def _entry(self, track):
        entry = {name: track[name] for name in ('at', 'title', 'artist', 'album', 'source', 'station')}
        entry['duration'] = round(track['duration'])
        art = track['art']
        # AirPlay's cover address only ever shows the current one
        entry['art'] = '' if art.startswith('/art?airplay=') else art
        return entry

    def _scrobbles(self, track):
        # Only tracks with a real artist: not a radio's own name, nor "—"
        return (self.scrobbler is not None and track['source'] in self.scrobble_sources
                and track['artist'] not in ('', '—', track['station']))

    def _finish(self):
        track, self.current = self.current, None
        if not track or not counts(track['played'], track['duration']):
            return
        entry = self._entry(track)
        self.entries.append(entry)
        self._save(entry)
        if self._scrobbles(track):
            self.scrobbler.scrobble(entry)


class ScrobbleError(Exception):
    # permanent: no use sending it again (refused key, or the listen itself)
    def __init__(self, message, permanent=False, disable=False):
        super().__init__(message)
        self.permanent = permanent
        self.disable = disable  # the service itself refuses us: stop using it


def _open(request, timeout=10):
    with urllib.request.urlopen(request, timeout=timeout) as r:
        return json.loads(r.read() or b'{}')


class ListenBrainz:
    name = 'ListenBrainz'
    URL = 'https://api.listenbrainz.org/1/'

    def __init__(self, token, version):
        self.token, self.version = token, version

    def _metadata(self, entry):
        metadata = {'artist_name': entry['artist'], 'track_name': entry['title']}
        if entry.get('album') and entry['album'] != entry.get('station'):
            metadata['release_name'] = entry['album']
        info = {'media_player': 'nowplaying', 'submission_client': 'nowplaying',
                'submission_client_version': self.version}
        if entry.get('duration'):
            info['duration_ms'] = entry['duration'] * 1000
        metadata['additional_info'] = info
        return metadata

    def call(self, path, body=None):
        request = urllib.request.Request(self.URL + path, data=json.dumps(body).encode() if body else None,
                                         headers={'Authorization': f'Token {self.token}',
                                                  'Content-Type': 'application/json'})
        try:
            return _open(request)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ScrobbleError('the token was refused (LISTENBRAINZ_TOKEN)', permanent=True, disable=True)
            if e.code == 400:
                raise ScrobbleError(f'listen refused: {e.read()[:200]!r}', permanent=True)
            raise ScrobbleError(f'HTTP {e.code}')

    def scrobble(self, entry):
        self.call('submit-listens', {'listen_type': 'single',
                                     'payload': [{'listened_at': entry['at'], 'track_metadata': self._metadata(entry)}]})

    def now_playing(self, entry):
        self.call('submit-listens', {'listen_type': 'playing_now',
                                     'payload': [{'track_metadata': self._metadata(entry)}]})

    def user(self):
        answer = self.call('validate-token')
        if not answer.get('valid'):
            raise ScrobbleError('the token was refused (LISTENBRAINZ_TOKEN)', permanent=True, disable=True)
        return answer.get('user_name', '?')


class LastFm:
    name = 'Last.fm'
    URL = 'https://ws.audioscrobbler.com/2.0/'
    REFUSED = {4, 9, 10, 13, 14, 26}  # authentication or key problems: stop trying

    def __init__(self, key, secret, session=''):
        self.key, self.secret, self.session = key, secret, session

    def call(self, method, **params):
        # A signed call: the parameters, sorted, then the secret, in MD5
        params = {name: str(value) for name, value in params.items() if value not in (None, '')}
        params.update(method=method, api_key=self.key)
        if self.session:
            params['sk'] = self.session
        signature = ''.join(name + params[name] for name in sorted(params)) + self.secret
        params['api_sig'] = hashlib.md5(signature.encode('utf-8')).hexdigest()
        params['format'] = 'json'
        request = urllib.request.Request(self.URL, data=urllib.parse.urlencode(params).encode())
        try:
            answer = _open(request)
        except urllib.error.HTTPError as e:
            try:
                answer = json.loads(e.read())
            except ValueError:
                raise ScrobbleError(f'HTTP {e.code}')
        if 'error' in answer:
            code = answer['error']
            refused = code in self.REFUSED
            raise ScrobbleError(f'{answer.get("message", "error")} (error {code})', permanent=refused, disable=refused)
        return answer

    def _track(self, entry):
        album = entry.get('album') if entry.get('album') != entry.get('station') else ''
        return dict(artist=entry['artist'], track=entry['title'], album=album, duration=entry.get('duration') or None)

    def scrobble(self, entry):
        self.call('track.scrobble', timestamp=entry['at'], **self._track(entry))

    def now_playing(self, entry):
        self.call('track.updateNowPlaying', **self._track(entry))

    def user(self):
        return self.call('user.getInfo').get('user', {}).get('name', '?')


class Scrobbler:
    # Sends plays to the services in the background. What fails for want of
    # network is sent again later (up to 500 plays waiting, until a restart)
    RETRY = 300

    def __init__(self, services, log):
        self.services = list(services)
        self.log = log
        self.queue = queue.Queue()
        self.waiting = {service.name: [] for service in self.services}
        self.problems = {}  # the last problem logged, per service: each is logged once
        threading.Thread(target=self._run, name='scrobbler', daemon=True).start()

    def scrobble(self, entry):
        self.queue.put(('scrobble', entry))

    def now_playing(self, entry):
        self.queue.put(('now', entry))

    def _run(self):
        while True:
            try:
                kind, entry = self.queue.get(timeout=self.RETRY if any(self.waiting.values()) else None)
            except queue.Empty:
                kind = entry = None
            for service in list(self.services):
                waiting = self.waiting[service.name]
                if kind == 'now':
                    try:
                        service.now_playing(entry)
                    except ScrobbleError as e:
                        if e.disable:
                            self._stop(service, e)
                            continue
                    except Exception as e:  # no network: it doesn't matter much
                        self.log.debug('%s: now playing not sent: %s', service.name, e)
                elif kind == 'scrobble':
                    waiting.append(entry)
                    del waiting[:-500]
                self._send(service, waiting)

    def _send(self, service, waiting):
        while waiting:
            try:
                service.scrobble(waiting[0])
            except ScrobbleError as e:
                if e.disable:
                    self._stop(service, e)
                    return
                if not e.permanent:
                    self._problem(service, str(e))
                    return
                self.log.warning('%s: %s - %s not scrobbled: %s', service.name, waiting[0]['artist'],
                                 waiting[0]['title'], e)
            except Exception as e:  # no network, timeout...
                self._problem(service, str(e))
                return
            else:
                if self.problems.get(service.name) != 'ok':
                    self.log.info('%s: scrobbling', service.name)
                    self.problems[service.name] = 'ok'
            waiting.pop(0)

    def _stop(self, service, error):
        self.log.error('%s: %s; no more scrobbling there until the bridge restarts', service.name, error)
        self.services.remove(service)
        self.waiting[service.name].clear()

    def _problem(self, service, text):
        if self.problems.get(service.name) != text:
            self.log.warning('%s: scrobble not sent (%s), sent again later', service.name, text)
            self.problems[service.name] = text
