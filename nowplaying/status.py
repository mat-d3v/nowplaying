"""What's playing, whoever plays it, and the updates for the pages.

mpd (or the demo mode) is always asked; AirPlay and Spotify Connect, when
on, come first while they play (main.py sets them up). Each change is
pushed to the pages listening on /events, and to the listening history.
"""
import logging
import os
import queue
import socket
import stat
import threading
import time
import urllib.parse

from . import VERSION, artwork, config, demo, history, mpd
from .audio import display_title, get_audio_format, get_codec

log = logging.getLogger('nowplaying')

# Set up by main.py, when the bridge runs
AIRPLAY = SPOTIFY = None   # shairport.AirPlay, spotify.Spotify
OTHER_PLAYERS = []         # the players besides mpd
LEVELS = None              # levels.LevelMeter, for the VU meters
LISTENING = None           # history.Listening


def is_fifo(path):
    try:
        return stat.S_ISFIFO(os.stat(path).st_mode)
    except OSError:
        return False


def get_status():
    if config.DEMO:
        return dict(demo.status(), levels=True)
    status = mpd.parse(mpd.command('status'))
    currentsong = mpd.parse(mpd.command('currentsong'))
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
    # Artwork: mpd itself first (embedded tags or cover file), then online
    # (in the background: the title shows at once, the artwork follows)
    art_url = ''
    if file_url and not stream:
        data, _ = mpd.get_art(file_url)
        if data:
            art_url = '/art?file=' + urllib.parse.quote(file_url, safe='')
    if not art_url:
        art_url = artwork.for_track(shown_artist, title, album, currentsong.get('albumartist', ''), exclude=name)
    # Next track in the queue (mpd exposes its position via 'nextsong')
    next_title = ''
    next_artist = ''
    ns = status.get('nextsong')
    if ns is not None and state != 'stop':
        try:
            nxt = mpd.parse(mpd.command(f'playlistinfo {ns}'))
        except mpd.MPDError:
            nxt = {}  # the queue changed between the two commands
        next_title = display_title(nxt)
        next_artist = nxt.get('artist', '')
    return {
        'state': state,
        'title': title,
        'artist': shown_artist or '—',
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
        'levels': bool(config.MPD_FIFO) and is_fifo(config.MPD_FIFO),  # VU meters possible
        'station': name if stream else '',
    }


_mpd_problem = None  # last mpd error logged, so each change is logged once
_mpd_problem_lock = threading.Lock()  # pages ask at the same time: logged once all the same
PROBLEMS = {'mpd_unreachable': 'unreachable', 'mpd_password': 'refused access (check MPD_PASSWORD)',
            'mpd_error': 'answered with an error'}


def player_status(player):
    data = player.status()
    if data and player is AIRPLAY and not data['art_url']:
        data['art_url'] = airplay_artwork()
    return data


_cover_timer = None  # pushes the status again once AirPlay's cover had time to come
_cover_lock = threading.Lock()


def airplay_artwork():
    # Online artwork for what's played over AirPlay without a cover, once
    # the cover had time to come: no other artwork flashing by first
    global _cover_timer
    wanted = AIRPLAY.cover_wanted()
    if not wanted:
        return ''
    wait, artist, title, album = wanted
    if wait:
        with _cover_lock:
            if _cover_timer is None:
                _cover_timer = threading.Timer(wait + 0.05, _cover_wait_over)
                _cover_timer.daemon = True
                _cover_timer.start()
        return ''
    return artwork.for_track(artist, title, album)


def _cover_wait_over():
    global _cover_timer
    with _cover_lock:
        _cover_timer = None
    publish_status()


def other_players():
    # What AirPlay and Spotify are up to: (playing, paused) payloads, the
    # one that started playing last first
    found = sorted(((player.started, data) for player in OTHER_PLAYERS for data in [player_status(player)] if data),
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
    except mpd.MPDError as e:
        kind = 'mpd_password' if e.code in (mpd.ACK_PASSWORD, mpd.ACK_PERMISSION) else 'mpd_error'
        problem = (kind, str(e))
    except OSError as e:  # refused, timed out, closed...
        problem = ('mpd_unreachable', str(e) or e.__class__.__name__)
    else:
        with _mpd_problem_lock:
            if _mpd_problem:
                log.info('mpd at %s:%s is back', config.MPD_HOST, config.MPD_PORT)
                _mpd_problem = None
        if paused and data['state'] != 'play':
            return paused[0], 200
        return data, 200
    with _mpd_problem_lock:
        if problem[0] != (_mpd_problem or ('',))[0]:  # a new kind of problem, not each new wording
            log.warning('mpd at %s:%s %s: %s', config.MPD_HOST, config.MPD_PORT, PROBLEMS[problem[0]], problem[1])
        _mpd_problem = problem
    if paused:
        return paused[0], 200  # paused AirPlay or Spotify beats an mpd error
    return {'error': problem[0], 'detail': problem[1]}, 503


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


# An online lookup is done: the pages get its artwork
artwork.changed = lambda: publish_status()

IDLE_REFRESH = 55  # seconds without news before checking the idle connection


def watch_mpd():
    # mpd's "idle" command blocks until something changes (track, play/pause,
    # seek, queue, options): each change is pushed to the pages right away
    failing = False
    while True:
        try:
            s = mpd.connect()
            failing = False
            try:
                publish_status()  # catch up after a reconnect
                s.settimeout(IDLE_REFRESH)
                while True:
                    s.sendall(b'idle player playlist options\n')
                    try:
                        response = mpd.recv(s)
                    except socket.timeout:
                        # Quiet for a while: leave idle, which also checks the
                        # connection is still alive, then wait again
                        s.sendall(b'noidle\n')
                        response = mpd.recv(s)
                    if response.startswith('ACK '):
                        raise mpd.MPDError(response.strip())
                    if 'changed: ' in response:
                        publish_status()
            finally:
                s.close()
        except (OSError, mpd.MPDError) as e:
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


def history_file():
    return os.path.join(config.DATA_DIR, 'history.jsonl')


def scrobble_services():
    # The scrobbling services the settings ask for
    services = []
    if config.LISTENBRAINZ_TOKEN:
        services.append(history.ListenBrainz(config.LISTENBRAINZ_TOKEN, VERSION))
    if config.LASTFM_ENABLED and config.LASTFM_SECRET and config.LASTFM_SESSION:
        services.append(history.LastFm(config.LASTFM_KEY, config.LASTFM_SECRET, config.LASTFM_SESSION))
    return services
