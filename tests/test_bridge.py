"""Tests for the bridge: its modules, and mpd-bridge.py against a fake mpd.

    python3 -m unittest discover -s tests -v
"""
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT]

from fake_broker import FakeBroker  # noqa: E402
from fake_mpd import GRAY_PNG, FakeMPD  # noqa: E402
import fake_bluez  # noqa: E402
import fake_fifo  # noqa: E402
import fake_shairport  # noqa: E402
import nowplaying  # noqa: E402
from nowplaying import (artwork, audio, bluetooth, check, config, demo, display, mpd, mqtt,  # noqa: E402
                        shairport, status, updates)


class CodecTest(unittest.TestCase):
    def test_codec_from_extension(self):
        cases = {
            ('Music/a.flac', '96000:24:2'): ('FLAC', True),
            ('Music/a.MP3', '44100:24:2'): ('MP3', False),
            ('Music/a.m4a', '44100:f:2'): ('AAC', False),    # mpd decodes AAC to float
            ('Music/a.m4a', '96000:24:2'): ('ALAC', True),   # and ALAC to integers
            ('Music/a.m4a', ''): ('M4A', False),
            ('Music/a.dsf', 'dsd64:2'): ('DSF', True),
            ('Music/a.tak', '44100:16:2'): ('TAK', False),   # unknown: no bit depth / Hi-Res
            ('Music/Vol.1/track', '44100:16:2'): ('', False),
            # Addresses: UPnP/DLNA servers keep the file's extension
            ('http://192.168.1.10:9790/minimserver/*/Music/01%20Song.flac', '192000:24:2'): ('FLAC', True),
            ('http://192.168.1.10:8200/MediaItems/23.flac?quality=hi', '96000:24:2'): ('FLAC', True),
            ('https://cloud.example/a/b.m4a', '44100:f:2'): ('AAC', False),
            ('http://radio.example/stream.mp3', '44100:f:2'): ('MP3', False),
            ('http://radio.example/stream', '44100:f:2'): ('', False),
            ('http://radio.example/live.m3u8', '48000:f:2'): ('', False),
            ('http://radio.example:8000/live.v2', '48000:f:2'): ('', False),
            # ...or in their parameters, Qobuz's own addresses with a number
            ('http://192.168.1.20:49149/qobuz/track?trackId=12&ext=.flac', '96000:24:2'): ('FLAC', True),
            ('http://192.168.1.20:8080/stream/12?format=flac', '44100:16:2'): ('FLAC', True),
            ('http://192.168.1.20:9090/qobuz/track/12/flac', '96000:24:2'): ('FLAC', True),
            ('https://streaming-qobuz-std.akamaized.net/file?uid=1&fmt=27', '192000:24:2'): ('FLAC', True),
            ('https://streaming-qobuz-std.akamaized.net/file?uid=1&fmt=5', '44100:f:2'): ('MP3', False),
            # An address that says nothing: integer samples at 88.2 kHz or
            # more only come from lossless files; anything else, no claim
            ('http://192.168.1.20:49149/qobuz/track/version/1/trackId/12', '96000:24:2'): ('', True),
            ('http://radio.example/stream', '96000:f:2'): ('', False),
            ('http://radio.example/stream', '44100:16:2'): ('', False),
            ('http://radio.example/stream', 'dsd64:2'): ('', False),
            ('', ''): ('', False),
        }
        for (uri, fmt), expected in cases.items():
            with self.subTest(uri=uri, audio=fmt):
                self.assertEqual(audio.get_codec(uri, fmt), expected)


class DisplayTitleTest(unittest.TestCase):
    def test_fallbacks(self):
        cases = [
            ({'title': 'T', 'name': 'Radio X', 'file': 'a.flac'}, 'T'),
            ({'name': 'Radio X', 'file': 'http://radio.example/s'}, 'Radio X'),
            ({'file': 'http://radio.example:8000/s'}, 'radio.example:8000'),
            ({'file': 'Music/rip/track01.flac'}, 'track01'),
            ({}, ''),
        ]
        for song, expected in cases:
            with self.subTest(song=song):
                self.assertEqual(audio.display_title(song), expected)


class AudioFormatTest(unittest.TestCase):
    def test_mpd_source_format_first(self):
        with mock.patch.object(audio, 'get_alsa_format', return_value='44100:32:2'):
            for fmt in ('44100:16:2', '96000:24:2', '44100:f:2', 'dsd64:2'):
                with self.subTest(audio=fmt):
                    self.assertEqual(audio.get_audio_format({'audio': fmt}), fmt)

    def test_alsa_fallback(self):
        with mock.patch.object(audio, 'get_alsa_format', return_value='48000:32:2'):
            self.assertEqual(audio.get_audio_format({}), '48000:32:2')
            self.assertEqual(audio.get_audio_format({'audio': 'garbage'}), '48000:32:2')


class BadgesTest(unittest.TestCase):
    def test_same_rules_as_the_page(self):
        cases = [
            (('96000:24:2', 'FLAC', True), ['FLAC', '24bit / 96.0 kHz', 'Hi-Res']),
            (('44100:16:2', 'FLAC', True), ['FLAC', '16bit / 44.1 kHz']),
            (('44100:24:2', 'MP3', False), ['MP3', '44.1 kHz']),   # no bit depth for lossy files
            (('dsd64:2', 'DSF', True), ['DSF', 'DSD64', 'Hi-Res']),
            (('44100:f:2', '', False), ['44.1 kHz']),              # a radio
            (('96000:24:2', '', True), ['24bit / 96.0 kHz', 'Hi-Res']),  # a stream that doesn't say
            (('', 'FLAC', True), ['FLAC']),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(audio.describe_badges(*args), expected)


def album(name, artist, art):
    return dict(collectionName=name, artistName=artist, artworkUrl100=f'https://is1.example/{art}/100x100bb.jpg')


def song(name, artist, art):
    return dict(trackName=name, artistName=artist, artworkUrl100=f'https://is1.example/{art}/100x100bb.jpg')


def wait_until(check, timeout=5):
    deadline = time.time() + timeout
    while not check():
        if time.time() > deadline:
            raise AssertionError('timed out')
        time.sleep(0.02)


class OnlineArtTest(unittest.TestCase):
    def test_names_compared_loosely(self):
        for name, simple in [('Random Access Memories (10th Anniversary Edition)', 'random access memories'),
                             ('Get Lucky - Single', 'get lucky'), ('Beyoncé', 'beyonce'), ('AC/DC', 'ac dc'),
                             ('坂本龍一', '坂本龍一'), ('(Live)', 'live')]:
            with self.subTest(name=name):
                self.assertEqual(artwork.simplify(name), simple)
        self.assertTrue(artwork.names_match('Daft Punk', 'Daft Punk feat. Pharrell Williams'))
        self.assertFalse(artwork.names_match('Art', 'The Smart Band'))  # whole words only
        self.assertFalse(artwork.names_match('Greatest Hits', 'Greatest Hits, Vol. 2', exact=True))
        self.assertFalse(artwork.names_match('', 'Anything'))

    def test_itunes_album_then_song(self):
        searches = {
            ('Daft Punk Discovery', 'album'): [album('Discovery', 'Some Cover Band', 'other'),
                                               album('Discovery (Live)', 'Daft Punk', 'live'),
                                               album('Discovery', 'Daft Punk', 'discovery')],
            ('Various Artists Now 99', 'album'): [],
            ('Daft Punk One More Time', 'song'): [song('One More Time (Radio Edit)', 'Daft Punk', 'omt')],
        }
        with mock.patch.object(artwork, 'itunes_search', side_effect=lambda *a: searches.get(a, [])):
            # The album itself, rather than its live version or a namesake
            self.assertEqual(artwork.itunes_art('Daft Punk', 'Aerodynamic', 'Discovery'),
                             'https://is1.example/discovery/600x600bb.jpg')
            # A compilation: its album artist, then the song by its artist
            self.assertEqual(artwork.itunes_art('Daft Punk', 'One More Time', 'Now 99', 'Various Artists'),
                             'https://is1.example/omt/600x600bb.jpg')
            # Radios: the song
            self.assertEqual(artwork.itunes_art('Daft Punk', 'One More Time'), 'https://is1.example/omt/600x600bb.jpg')
            # Nobody of that name: no artwork rather than someone else's
            self.assertEqual(artwork.itunes_art('Nobody', 'One More Time'), '')

    def test_lookups_run_in_the_background(self):
        release, calls = threading.Event(), []

        def lookup():
            calls.append(1)
            release.wait(5)
            return 'https://art.example/a.jpg'
        key = ('Test', 'background', str(time.time()))
        with mock.patch.object(artwork, 'changed') as published:
            self.assertIsNone(artwork.online_art(key, lookup))  # started: not known yet
            self.assertIsNone(artwork.online_art(key, lookup))  # still running: not started twice
            release.set()
            wait_until(lambda: published.called)  # the page gets the artwork
            self.assertEqual(artwork.online_art(key, lookup), 'https://art.example/a.jpg')
        self.assertEqual(len(calls), 1)

    def test_failed_lookups_run_again_later(self):
        calls = []

        def failing():
            calls.append(1)
            raise OSError('no network yet')
        key = ('Test', 'failing', str(time.time()))
        with mock.patch.object(artwork, 'changed') as published, self.assertLogs('nowplaying', 'WARNING'):
            artwork.online_art(key, failing)
            wait_until(lambda: published.call_count == 1)
            self.assertEqual(artwork.online_art(key, failing), '')  # not again right away
            with mock.patch.object(artwork, 'RETRY_AFTER', 0):
                self.assertIsNone(artwork.online_art(key, failing))
                wait_until(lambda: published.call_count == 2)
        self.assertEqual(len(calls), 2)


class VersionTest(unittest.TestCase):
    def test_version_has_its_changelog_section(self):
        # The release workflow takes the notes from it
        with open(os.path.join(ROOT, 'CHANGELOG.md')) as f:
            latest = next(line.split()[1] for line in f
                          if line.startswith('## ') and not line.startswith('## Unreleased'))
        self.assertEqual(nowplaying.VERSION, latest)


class UpdatesTest(unittest.TestCase):
    def release(self, tag):
        # GitHub's answer for the latest release
        answer = mock.MagicMock()
        answer.__enter__.return_value.read.return_value = json.dumps(
            {'tag_name': tag, 'html_url': f'https://github.com/mat-d3v/nowplaying/releases/tag/{tag}'}).encode()
        return mock.patch('urllib.request.urlopen', return_value=answer)

    def setUp(self):
        patcher = mock.patch.object(updates, '_latest', None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_versions_compared(self):
        self.assertEqual(updates.parse('v1.10.0'), (1, 10, 0))
        self.assertIsNone(updates.parse('1.2'))
        self.assertTrue(updates.newer('1.10.0', '1.9.3'))  # numbers, not text
        self.assertFalse(updates.newer('1.1.1', '1.1.1'))
        self.assertFalse(updates.newer('1.0.9', '1.1.0'))
        self.assertFalse(updates.newer('nightly', '1.1.0'))

    def test_newer_release(self):
        self.assertEqual(updates.about()['update'], None)  # nobody asked yet
        with self.release('v99.0.0'):
            new = updates.refresh()
        self.assertEqual(new, {'version': '99.0.0', 'url': 'https://github.com/mat-d3v/nowplaying/releases/tag/v99.0.0'})
        self.assertEqual(updates.about()['update'], new)
        self.assertEqual(updates.about()['version'], nowplaying.VERSION)

    def test_this_is_the_latest(self):
        with self.release('v' + nowplaying.VERSION):
            self.assertIsNone(updates.refresh())
        self.assertIsNone(updates.about()['update'])

    def test_logged_once(self):
        # The background check: once a day, a new version said once
        calls = []

        def sleep(seconds):
            calls.append(seconds)
            if len(calls) > 3:
                raise StopIteration  # out of the endless loop
        with self.release('v99.0.0'), mock.patch.object(updates, 'time', mock.Mock(sleep=sleep)), \
                self.assertLogs('nowplaying') as logs:
            with self.assertRaises(StopIteration):
                updates.watch()
        self.assertEqual(calls, [updates.FIRST_CHECK, updates.EVERY, updates.EVERY, updates.EVERY])
        self.assertEqual(len(logs.output), 1)
        self.assertIn('nowplaying 99.0.0 is out', logs.output[0])


class DotenvTest(unittest.TestCase):
    KEYS = ('NP_TEST_A', 'NP_TEST_B', 'NP_TEST_C', 'NP_TEST_SET')

    def tearDown(self):
        for key in self.KEYS:
            os.environ.pop(key, None)

    def test_parse(self):
        with tempfile.NamedTemporaryFile('w', suffix='.env', delete=False) as f:
            f.write('# comment\n\n=oops\nNP_TEST_A=1\nexport NP_TEST_B="two words"\n'
                    "NP_TEST_C='x'\nNP_TEST_SET=from-file\n")
        self.addCleanup(os.unlink, f.name)
        os.environ['NP_TEST_SET'] = 'from-env'
        config.load_dotenv(f.name)
        self.assertEqual(os.environ['NP_TEST_A'], '1')  # still read after the bad line
        self.assertEqual(os.environ['NP_TEST_B'], 'two words')
        self.assertEqual(os.environ['NP_TEST_C'], 'x')
        self.assertEqual(os.environ['NP_TEST_SET'], 'from-env')  # the environment wins

    def test_missing_file(self):
        config.load_dotenv('/nonexistent/.env')  # no error


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Bridge:
    # mpd-bridge.py running in its own process; every setting is passed
    # explicitly, so a local .env can't change what the tests see
    def __init__(self, mpd_port, **env):
        self.port = free_port()
        self.data = tempfile.mkdtemp()  # what it saves
        settings = dict(MPD_HOST='127.0.0.1', MPD_PORT=str(mpd_port), PORT=str(self.port), MPD_PASSWORD='',
                        LASTFM_API_KEY='', ITUNES_ARTWORK='0', TLS_CERT='', TLS_KEY='', ALSA_CARD='99',
                        SHAIRPORT_PIPE='', MPD_FIFO='', DATA_DIR=self.data, UPDATE_CHECK='0', BLUETOOTH='0',
                        MQTT_HOST='')
        settings.update(env)
        self.https = bool(settings['TLS_CERT'])
        self.log = tempfile.TemporaryFile('w+')
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, 'mpd-bridge.py')],
                                     env=dict(os.environ, **settings), stderr=self.log)
        deadline = time.time() + 10
        while True:
            try:
                socket.create_connection(('127.0.0.1', self.port), timeout=0.2).close()
                return
            except OSError:
                if time.time() > deadline or self.proc.poll() is not None:
                    raise RuntimeError('the bridge did not start:\n' + self.stop())
                time.sleep(0.05)

    def stop(self):
        # What it logged; stopping twice is fine
        if not self.log.closed:
            self.proc.terminate()
            self.proc.wait(timeout=5)
            shutil.rmtree(self.data, ignore_errors=True)
            self.log.seek(0)
            self.output = self.log.read()
            self.log.close()
        return self.output

    def get(self, path, cafile=None):
        handlers = [urllib.request.ProxyHandler({})]  # never through an HTTP proxy
        if cafile:
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))
        url = f"{'https' if self.https else 'http'}://localhost:{self.port}{path}"
        try:
            with urllib.request.build_opener(*handlers).open(url, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def now(self, **kw):
        status, _, body = self.get('/now', **kw)
        return status, json.loads(body)


class BridgeTestCase(unittest.TestCase):
    password = None

    def setUp(self):
        self.mpd = FakeMPD(password=self.password, scenario='flac_hires').start()
        self.addCleanup(self.mpd.server_close)
        self.addCleanup(self.mpd.shutdown)

    def start_bridge(self, **env):
        b = Bridge(self.mpd.port, **env)
        self.addCleanup(b.stop)
        return b

    def wait_for_idle(self):
        # The bridge waits for mpd's changes: it told the pages what plays
        # when it connected, and the next status it pushes is a change
        wait_until(lambda: self.mpd.idling > 0)


class StatusTest(BridgeTestCase):
    def test_payloads(self):
        b = self.start_bridge()
        expected = {
            'flac_hires': dict(title='Song', artist='Art', album='Alb', codec='FLAC', lossless=True,
                               format='96000:24:2', stream=False),
            'mp3_mad': dict(title='Song MP3', codec='MP3', lossless=False, format='44100:24:2'),
            'untagged': dict(title='track01', artist='—', codec='FLAC', next_title='track02'),
            'm4a_aac': dict(codec='AAC', lossless=False),
            'm4a_alac': dict(codec='ALAC', lossless=True),
            'dsf': dict(codec='DSF', format='dsd64:2'),
            'radio_artist_title': dict(title='One More Time', artist='Daft Punk', album='Radio X',
                                       codec='', stream=True, duration=0.0),
            'upnp_flac': dict(title='Pier', artist='June Avenue', album='Night Ferries', codec='FLAC',
                              lossless=True, format='192000:24:2', stream=True, duration=301.5),
            'radio_plain_title': dict(title='Morning show', artist='Radio X', album=''),
            'radio_name_only': dict(title='Radio X', artist='—'),
            'stopped': dict(state='stop', title='', format=''),
        }
        for scenario, fields in expected.items():
            with self.subTest(scenario=scenario):
                self.mpd.set_scenario(scenario)
                status, data = b.now()
                self.assertEqual(status, 200)
                self.assertEqual({k: data[k] for k in fields}, fields)

    def test_artwork(self):
        b = self.start_bridge()
        _, data = b.now()
        self.assertTrue(data['art_url'].startswith('/art?file='))
        status, headers, body = b.get(data['art_url'])
        self.assertEqual((status, headers['Content-Type'], body), (200, 'image/png', GRAY_PNG))
        self.assertEqual(b.get('/art?file=nope')[0], 404)

    def test_static_routes(self):
        b = self.start_bridge()
        for path in ('/', '/index.html', '/index.fr.html', '/?lang=fr', '/hires.svg', '/manifest.webmanifest',
                     '/icon-192.png', '/icon-512.png', '/touch-icon-v2.png', '/apple-touch-icon.png'):
            with self.subTest(path=path):
                self.assertEqual(b.get(path)[0], 200)
        self.assertEqual(b.get('/nope')[0], 404)

    def test_version(self):
        b = self.start_bridge()
        status, _, body = b.get('/version')
        self.assertEqual((status, json.loads(body)), (200, {'version': nowplaying.VERSION, 'update': None,
                                                            'checked': False}))

    def test_mpd_unreachable(self):
        b = Bridge(free_port())
        self.addCleanup(b.stop)
        status, data = b.now()
        self.assertEqual((status, data['error']), (503, 'mpd_unreachable'))


class OnlineArtStatusTest(BridgeTestCase):
    # The bridge's own get_status(), in this process, against the fake mpd
    def setUp(self):
        super().setUp()
        patcher = mock.patch.multiple(config, MPD_HOST='127.0.0.1', MPD_PORT=self.mpd.port, MPD_PASSWORD='',
                                      ITUNES_ENABLED=True, LASTFM_ENABLED=False, DEMO=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(status, 'publish_status')
        self.published = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.close_mpd)

    def close_mpd(self):
        if mpd._sock:
            mpd._sock.close()
            mpd._sock = None

    def test_title_first_then_artwork(self):
        self.mpd.set_scenario('mp3_mad')  # no artwork in mpd
        search = mock.Mock(return_value=[album('Alb', 'Art', 'alb')])
        with mock.patch.object(artwork, 'itunes_search', search):
            first = status.get_status()
            self.assertEqual((first['title'], first['art_url']), ('Song MP3', ''))  # not held up
            wait_until(lambda: self.published.called)
            self.assertEqual(status.get_status()['art_url'], 'https://is1.example/alb/600x600bb.jpg')
        search.assert_called_once_with('Art Alb', 'album')

    def test_mpd_artwork_first(self):
        search = mock.Mock(return_value=[])
        with mock.patch.object(artwork, 'itunes_search', search):
            self.assertTrue(status.get_status()['art_url'].startswith('/art?file='))
        search.assert_not_called()


def run_check(mpd_port, **env):
    settings = dict(MPD_HOST='127.0.0.1', MPD_PORT=str(mpd_port), PORT=str(free_port()), MPD_PASSWORD='',
                    LASTFM_API_KEY='', ITUNES_ARTWORK='0', TLS_CERT='', TLS_KEY='', ALSA_CARD='99',
                    SHAIRPORT_PIPE='', MPD_FIFO='', DATA_DIR=tempfile.gettempdir(), UPDATE_CHECK='0',
                    BLUETOOTH='0', MQTT_HOST='')
    settings.update(env)
    result = subprocess.run([sys.executable, os.path.join(ROOT, 'mpd-bridge.py'), '--check'],
                            env=dict(os.environ, **settings), capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout


class CheckTest(BridgeTestCase):
    def test_all_good(self):
        code, out = run_check(self.mpd.port)
        self.assertEqual(code, 0, out)
        for line in ('mpd 0.23.5 at', "Instant updates: mpd's idle command works", 'Playing: Song',
                     'badges: FLAC, 24bit / 96.0 kHz, Hi-Res', 'Artwork: embedded in the file',
                     f'Version {nowplaying.VERSION} (update check off', 'No problem found.'):
            self.assertIn(line, out)

    def test_mpd_down(self):
        code, out = run_check(free_port())
        self.assertEqual(code, 1, out)
        self.assertIn('mpd: nothing answers at', out)

    def test_port_taken(self):
        with socket.socket() as busy:
            busy.bind(('0.0.0.0', 0))
            busy.listen()
            code, out = run_check(self.mpd.port, PORT=str(busy.getsockname()[1]))
        self.assertEqual(code, 1, out)
        self.assertIn('is taken by another program', out)


class PasswordTest(BridgeTestCase):
    password = 'secret'

    def test_password(self):
        for password, expected in (('', 'mpd_password'), ('wrong', 'mpd_password'), ('secret', None)):
            with self.subTest(password=password):
                b = self.start_bridge(MPD_PASSWORD=password)
                status, data = b.now()
                if expected:
                    self.assertEqual((status, data['error']), (503, expected))
                else:
                    self.assertEqual((status, data['title']), (200, 'Song'))
                    self.assertEqual(b.get(data['art_url'])[0], 200)  # its own connection, same password

    def test_check_explains(self):
        code, out = run_check(self.mpd.port)
        self.assertEqual(code, 1, out)
        self.assertIn('mpd needs a password', out)
        code, out = run_check(self.mpd.port, MPD_PASSWORD='wrong')
        self.assertIn('mpd refused MPD_PASSWORD', out)
        code, out = run_check(self.mpd.port, MPD_PASSWORD='secret')
        self.assertEqual(code, 0, out)


def read_sse(response):
    # (type, payload) of the next server-sent event
    kind = 'message'
    while True:
        line = response.readline().decode()
        if line.startswith('event: '):
            kind = line[7:].strip()
        elif line.startswith('data: '):
            return kind, json.loads(line[6:])


def read_event(response, wanted='message'):
    # The payload of the next event of that type: a status update, unless
    # asked for another
    while True:
        kind, data = read_sse(response)
        if kind == wanted:
            return data


class EventsTest(BridgeTestCase):
    def test_changes_are_pushed(self):
        b = self.start_bridge()
        self.wait_for_idle()
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/events')
        response = conn.getresponse()
        self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
        self.assertEqual(read_event(response)['title'], 'Song')  # current status first
        start = time.time()
        self.mpd.set_scenario('mp3_mad')
        self.assertEqual(read_event(response)['title'], 'Song MP3')
        self.assertLess(time.time() - start, 1)

    def test_watcher_survives_errors(self):
        b = self.start_bridge()
        self.wait_for_idle()
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/events')
        response = conn.getresponse()
        self.assertEqual(read_event(response)['title'], 'Song')
        self.mpd.set_scenario('bad_status')  # building the status fails
        time.sleep(0.3)
        status, data = b.now()
        self.assertEqual((status, data['error']), (500, 'internal'))
        self.mpd.set_scenario('mp3_mad')  # the watcher must still be there
        self.assertEqual(read_event(response)['title'], 'Song MP3')


class DemoTest(unittest.TestCase):
    def test_tracks_cycle(self):
        step, count = demo.STEP, len(demo.TRACKS)
        self.assertEqual(demo.status(demo.START)['title'], demo.TRACKS[0]['title'])
        self.assertEqual(demo.status(demo.START + step)['title'], demo.TRACKS[1]['title'])
        self.assertEqual(demo.status(demo.START + step * count)['title'], demo.TRACKS[0]['title'])
        self.assertAlmostEqual(demo.until_next_track(demo.START + step / 4), step * 3 / 4)
        radio = [demo.status(demo.START + step * i) for i in range(count) if demo.TRACKS[i].get('stream')][0]
        self.assertEqual((radio['stream'], radio['duration'], radio['codec']), (True, 0.0, ''))

    def test_bridge_in_demo_mode(self):
        b = Bridge(free_port(), DEMO='1', DEMO_STEP='1')  # no mpd at all
        self.addCleanup(b.stop)
        titles = [t['title'] for t in demo.TRACKS]
        status, data = b.now()
        self.assertEqual(status, 200)
        self.assertIn(data['title'], titles)
        status, headers, body = b.get(data['art_url'])
        self.assertEqual((status, headers['Content-Type'], body[:8]), (200, 'image/png', b'\x89PNG\r\n\x1a\n'))
        # A made-up history too, the latest first
        played = json.loads(b.get('/history.json')[2])['tracks']
        self.assertEqual([t['title'] for t in played], [demo.TRACKS[i]['title'] for _, i, _ in demo.PLAYED[::-1]])
        self.assertEqual({t['source'] for t in played}, {'mpd', 'airplay', 'spotify', 'bluetooth'})
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/events')
        response = conn.getresponse()
        # The next track gets pushed (the current one may come twice: an
        # update can land in the queue just as the page subscribes)
        first = read_event(response)['title']
        titles = [read_event(response)['title'] for _ in range(3)]
        self.assertTrue(any(title != first for title in titles), (first, titles))


@unittest.skipUnless(hasattr(os, 'mkfifo'), 'needs named pipes')
class AirPlayBridgeTest(BridgeTestCase):
    def setUp(self):
        super().setUp()  # mpd plays a FLAC ("Song")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.pipe = os.path.join(tmp, 'shairport-sync-metadata')
        os.mkfifo(self.pipe)

    def send(self, scenario):
        fake_shairport.send(self.pipe, fake_shairport.SCENARIOS[scenario])

    def wait_for(self, b, check, timeout=5):
        deadline = time.time() + timeout
        while True:
            status, data = b.now()
            if check(data) or time.time() > deadline:
                return status, data
            time.sleep(0.05)

    def test_airplay_and_mpd_take_turns(self):
        b = self.start_bridge(SHAIRPORT_PIPE=self.pipe)
        self.send('play')  # AirPlay plays: it wins over mpd
        status, data = self.wait_for(b, lambda d: d.get('source') == 'airplay')
        self.assertEqual((status, data['state'], data['title'], data['artist'], data['codec'], data['sender']),
                         (200, 'play', 'Harbor Lights', 'June Avenue', 'AirPlay', "Mat's iPhone"))
        art_status, _, cover = b.get(data['art_url'])
        self.assertEqual((art_status, cover), (200, fake_shairport.COVER))

        self.send('pause')  # paused while mpd plays: mpd comes back
        status, data = self.wait_for(b, lambda d: d.get('source') == 'mpd')
        self.assertEqual(data['title'], 'Song')
        self.mpd.set_scenario('stopped')  # nothing else plays: the paused AirPlay shows
        status, data = self.wait_for(b, lambda d: d.get('source') == 'airplay')
        self.assertEqual(data['state'], 'pause')

        self.send('stop')  # session over
        status, data = self.wait_for(b, lambda d: d.get('source') == 'mpd')
        self.assertEqual(data['source'], 'mpd')

    def test_changes_are_pushed(self):
        b = self.start_bridge(SHAIRPORT_PIPE=self.pipe)
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/events')
        response = conn.getresponse()
        self.assertEqual(read_event(response)['source'], 'mpd')
        self.send('play')
        titles = [read_event(response)['title']]
        while 'Harbor Lights' not in titles:
            titles.append(read_event(response)['title'])
        self.send('next')
        while titles[-1] != 'Second Wind':
            titles.append(read_event(response)['title'])

    def test_airplay_while_mpd_is_down(self):
        b = Bridge(free_port(), SHAIRPORT_PIPE=self.pipe)
        self.addCleanup(b.stop)
        self.send('play')
        status, data = self.wait_for(b, lambda d: d.get('source') == 'airplay')
        self.assertEqual((status, data['title']), (200, 'Harbor Lights'))

    def test_the_last_one_started_wins(self):
        b = self.start_bridge(SHAIRPORT_PIPE=self.pipe)
        self.send('play')
        self.wait_for(b, lambda d: d.get('source') == 'airplay')
        spotify_event(b.port, **SPOTIFY_TRACK)
        spotify_event(b.port, PLAYER_EVENT='playing')
        self.assertEqual(b.now()[1]['source'], 'spotify')  # started after AirPlay
        self.send('pause')
        self.send('resume')  # AirPlay starts again: it's the last one now
        status, data = self.wait_for(b, lambda d: d.get('source') == 'airplay')
        self.assertEqual(data['source'], 'airplay')

    def test_check_finds_the_pipe(self):
        code, out = run_check(self.mpd.port, SHAIRPORT_PIPE=self.pipe)
        self.assertIn('AirPlay: shairport-sync metadata pipe at', out)
        code, out = run_check(self.mpd.port, SHAIRPORT_PIPE=self.pipe + '-missing')
        self.assertIn('AirPlay: no shairport-sync metadata pipe at', out)


class AirPlayArtworkTest(unittest.TestCase):
    # What's played over AirPlay without a cover: artwork from iTunes, once
    # the cover had time to come
    def setUp(self):
        self.airplay = shairport.AirPlay()
        self.title = f'Pier {time.time()}'  # a lookup of its own
        search = mock.Mock(return_value=[album('Night Ferries', 'June Avenue', 'ferries')])
        for patcher in (mock.patch.multiple(status, AIRPLAY=self.airplay, OTHER_PLAYERS=[self.airplay]),
                        mock.patch.multiple(config, ITUNES_ENABLED=True, LASTFM_ENABLED=False),
                        mock.patch.object(artwork, 'itunes_search', search)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def send(self, data):
        for it in shairport.parse_items(data)[0]:
            self.airplay.handle(*it)

    def art(self):
        return status.status_or_error()[0]['art_url']

    def test_sender_says_none(self):
        self.send(fake_shairport.item('ssnc', 'pbeg')
                  + fake_shairport.track(self.title, 'June Avenue', 'Night Ferries', cover=b''))
        self.assertEqual(self.art(), '')  # looking
        wait_until(lambda: self.art() == 'https://is1.example/ferries/600x600bb.jpg')

    def test_sender_says_nothing(self):
        with mock.patch.object(shairport, 'COVER_WAIT', 0.3), mock.patch.object(status, 'publish_status') as published:
            self.send(fake_shairport.item('ssnc', 'pbeg')
                      + fake_shairport.track(self.title, 'June Avenue', 'Night Ferries', cover=None))
            self.assertEqual(self.art(), '')  # its cover may still come
            artwork.itunes_search.assert_not_called()
            wait_until(lambda: published.called)  # the wait is over: the page asks again
        wait_until(lambda: self.art() == 'https://is1.example/ferries/600x600bb.jpg')

    def test_cover_from_the_sender(self):
        self.send(fake_shairport.SCENARIOS['play'])
        self.assertTrue(self.art().startswith('/art?airplay='))
        artwork.itunes_search.assert_not_called()


# busctl's answer for a phone playing, as it writes it
BUSCTL_JSON = json.dumps({'type': 'a{oa{sa{sv}}}', 'data': [{
    fake_bluez.PHONE: {'org.bluez.Device1': {'Alias': {'type': 's', 'data': "Mat's iPhone"}}},
    fake_bluez.PHONE + '/player0': {'org.bluez.MediaPlayer1': {
        'Status': {'type': 's', 'data': 'playing'}, 'Position': {'type': 'u', 'data': 42000},
        'Device': {'type': 'o', 'data': fake_bluez.PHONE},
        'Track': {'type': 'a{sv}', 'data': {'Title': {'type': 's', 'data': 'Harbor Lights'},
                                            'Duration': {'type': 'u', 'data': 200000}}}}},
}]})


class BluetoothTest(unittest.TestCase):
    def test_busctl_answer(self):
        objects = bluetooth.plain(json.loads(BUSCTL_JSON)['data'][0])
        self.assertEqual(objects[fake_bluez.PHONE + '/player0']['org.bluez.MediaPlayer1']['Track'],
                         {'Title': 'Harbor Lights', 'Duration': 200000})
        self.assertEqual(bluetooth.playing(objects)['sender'], "Mat's iPhone")

    def test_what_plays(self):
        playing = bluetooth.playing
        self.assertEqual(playing(fake_bluez.objects(fake_bluez.phone())), {
            'device': fake_bluez.PHONE, 'sender': "Mat's iPhone", 'state': 'play', 'title': 'Harbor Lights',
            'artist': 'June Avenue', 'album': 'Night Ferries', 'duration': 200.0, 'position': 42.0})
        self.assertEqual(playing(fake_bluez.objects(fake_bluez.phone('paused')))['state'], 'pause')
        self.assertIsNone(playing(fake_bluez.objects(fake_bluez.phone('stopped', transport='idle'))))
        self.assertIsNone(playing(fake_bluez.objects()))
        # Headphones this machine plays to: what they play is mpd's
        self.assertIsNone(playing(fake_bluez.objects(fake_bluez.headphones())))
        self.assertEqual(playing(fake_bluez.objects(fake_bluez.headphones(), fake_bluez.phone('paused')))['state'],
                         'pause')
        # Sound from a device that doesn't say what it plays
        quiet = fake_bluez.device(fake_bluez.PHONE, 'Office Mac', fake_bluez.SINK)
        self.assertEqual({k: playing(fake_bluez.objects(quiet))[k] for k in ('state', 'title', 'sender')},
                         {'state': 'play', 'title': '', 'sender': 'Office Mac'})
        # AVRCP's "unknown" duration
        self.assertEqual(playing(fake_bluez.objects(fake_bluez.phone(duration=0xFFFFFFFF)))['duration'], 0)

    def test_changes(self):
        phone = bluetooth.Bluetooth()
        now = [1000.0]

        def update(**track):
            with mock.patch('time.monotonic', return_value=now[0]):
                return phone.update(fake_bluez.objects(fake_bluez.phone(**track)) if track.get('status', 1) else None)
        self.assertTrue(update(position=42000))
        self.assertEqual(phone.status()['source'], 'bluetooth')
        self.assertFalse(update(position=42000))           # nothing new
        now[0] += 10
        self.assertFalse(update(position=52000))           # it plays on: no news for the page
        with mock.patch('time.monotonic', return_value=now[0] + 2):
            self.assertAlmostEqual(phone.status()['elapsed'], 54)
        self.assertTrue(update(position=150000))           # a seek
        self.assertTrue(update(status='paused', position=150000))
        self.assertTrue(update(title='Second Wind', position=0))
        self.assertTrue(update(status=None))               # gone
        self.assertIsNone(phone.status())

    def test_artwork_online(self):
        phone = bluetooth.Bluetooth()
        phone.update(fake_bluez.objects(fake_bluez.phone()))
        self.assertEqual(phone.cover_wanted(), (0, 'June Avenue', 'Harbor Lights', 'Night Ferries'))
        phone.update(fake_bluez.objects(fake_bluez.phone(artist='')))
        self.assertIsNone(phone.cover_wanted())

    def test_problems(self):
        for text, kind in [('Call failed: The name org.bluez was not provided by any .service files', 'missing'),
                           ('Failed to connect to bus: No such file or directory', 'missing'),
                           ("busctl not found (systemd's D-Bus tool)", 'missing'),
                           ('Call failed: Access denied', 'denied'),
                           ('Call failed: Timeout was reached', 'error')]:
            with self.subTest(text=text):
                self.assertEqual(bluetooth.problem(bluetooth.BusError(text)), kind)

    def test_through_busctl(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        state = os.path.join(tmp, 'bluez.json')
        command = [fake_bluez.install(tmp)] + bluetooth.COMMAND[1:]
        with mock.patch.object(bluetooth, 'COMMAND', command), mock.patch.dict(os.environ, FAKE_BLUEZ=state):
            fake_bluez.write(state, fake_bluez.objects(fake_bluez.phone()))
            self.assertEqual(bluetooth.playing(bluetooth.managed_objects())['title'], 'Harbor Lights')
            fake_bluez.write(state, error='Call failed: Access denied')
            with self.assertRaisesRegex(bluetooth.BusError, 'Access denied'):
                bluetooth.managed_objects()
        with mock.patch.object(bluetooth, 'COMMAND', [os.path.join(tmp, 'nothing-here')]):
            with self.assertRaisesRegex(bluetooth.BusError, 'busctl not found'):
                bluetooth.managed_objects()


class BluetoothBridgeTest(BridgeTestCase):
    # The bridge, its fake busctl first in PATH
    def setUp(self):
        super().setUp()  # mpd plays a FLAC ("Song")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.state = os.path.join(tmp, 'bluez.json')
        fake_bluez.write(self.state)
        fake_bluez.install(tmp)
        self.env = dict(BLUETOOTH='1', FAKE_BLUEZ=self.state, PATH=tmp + os.pathsep + os.environ.get('PATH', ''))

    def wait_for(self, b, check, timeout=8):
        deadline = time.time() + timeout
        while True:
            status, data = b.now()
            if check(data) or time.time() > deadline:
                return status, data
            time.sleep(0.1)

    def test_bluetooth_and_mpd_take_turns(self):
        b = self.start_bridge(**self.env)
        self.assertEqual(b.now()[1]['source'], 'mpd')
        fake_bluez.write(self.state, fake_bluez.objects(fake_bluez.phone()))
        _, data = self.wait_for(b, lambda d: d['source'] == 'bluetooth')
        self.assertEqual({k: data[k] for k in ('state', 'title', 'artist', 'codec', 'sender', 'duration')},
                         {'state': 'play', 'title': 'Harbor Lights', 'artist': 'June Avenue', 'codec': 'Bluetooth',
                          'sender': "Mat's iPhone", 'duration': 200.0})
        self.assertGreaterEqual(data['elapsed'], 42)
        # Paused while mpd plays: mpd shows
        fake_bluez.write(self.state, fake_bluez.objects(fake_bluez.phone('paused')))
        _, data = self.wait_for(b, lambda d: d['source'] == 'mpd')
        self.assertEqual(data['title'], 'Song')
        # Playing again, then gone
        fake_bluez.write(self.state, fake_bluez.objects(fake_bluez.phone()))
        self.wait_for(b, lambda d: d['source'] == 'bluetooth')
        fake_bluez.write(self.state, fake_bluez.objects())
        self.assertEqual(self.wait_for(b, lambda d: d['source'] == 'mpd')[1]['source'], 'mpd')

    def test_changes_are_pushed(self):
        b = self.start_bridge(**self.env)
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request('GET', '/events')
        response = conn.getresponse()
        self.assertEqual(read_event(response)['source'], 'mpd')
        fake_bluez.write(self.state, fake_bluez.objects(fake_bluez.phone()))
        sources = [read_event(response)['source']]
        while sources[-1] != 'bluetooth' and len(sources) < 5:
            sources.append(read_event(response)['source'])
        self.assertEqual(sources[-1], 'bluetooth')

    def test_check(self):
        for state, line in [(dict(found=fake_bluez.objects(fake_bluez.phone())),
                             "ok    Bluetooth: Mat's iPhone plays June Avenue - Harbor Lights"),
                            (dict(found=fake_bluez.objects()), 'ok    Bluetooth: BlueZ answers, no device connected'),
                            (dict(error='Call failed: Access denied'), 'FAIL  Bluetooth: BlueZ refused to answer'),
                            (dict(error='Call failed: The name org.bluez was not provided by any .service files'),
                             '--    Bluetooth: cannot ask BlueZ')]:
            with self.subTest(line=line):
                fake_bluez.write(self.state, **state)
                code, out = run_check(self.mpd.port, **self.env)
                self.assertIn(line, out)
        code, out = run_check(self.mpd.port, BLUETOOTH='0')
        self.assertIn('Bluetooth: off (BLUETOOTH=0)', out)


class MqttTest(unittest.TestCase):
    def test_lengths(self):
        # MQTT's "remaining length": 7 bits a byte
        for n, encoded in [(0, b'\x00'), (127, b'\x7f'), (128, b'\x80\x01'), (16383, b'\xff\x7f'),
                           (16384, b'\x80\x80\x01'), (2097151, b'\xff\xff\x7f')]:
            with self.subTest(n=n):
                self.assertEqual(mqtt._length(n), encoded)

    def test_state_for_home_assistant(self):
        state = mqtt.state_payload({
            'state': 'play', 'title': 'Song', 'artist': 'Art', 'album': 'Alb', 'source': 'mpd', 'codec': 'FLAC',
            'format': '96000:24:2', 'lossless': True, 'duration': 200.04, 'elapsed': 12.345,
            'art_url': '/art?file=a.flac'}, 'http://192.168.1.20:8766')
        self.assertEqual({k: state[k] for k in ('state', 'title', 'badges', 'quality', 'hires', 'art_url', 'elapsed')}, {
            'state': 'play', 'title': 'Song', 'badges': ['FLAC', '24bit / 96.0 kHz', 'Hi-Res'],
            'quality': 'FLAC · 24bit / 96.0 kHz · Hi-Res', 'hires': True,
            'art_url': 'http://192.168.1.20:8766/art?file=a.flac', 'elapsed': 12.3})
        radio = mqtt.state_payload({'state': 'play', 'title': 'Radio X', 'artist': '—', 'art_url': 'https://a/b.jpg'},
                                   'http://x')
        self.assertEqual((radio['artist'], radio['art_url']), ('', 'https://a/b.jpg'))
        error = mqtt.state_payload({'error': 'mpd_unreachable', 'detail': 'refused'}, 'http://x')
        self.assertEqual((error['state'], error['error'], error['badges']), ('error', 'mpd_unreachable', []))

    def test_discovery(self):
        configs = dict(mqtt.discovery('nowplaying', 'homeassistant', 'http://192.168.1.20:8766'))
        self.assertEqual(sorted(configs), [
            'homeassistant/binary_sensor/nowplaying/playing/config', 'homeassistant/image/nowplaying/artwork/config',
            'homeassistant/sensor/nowplaying/album/config', 'homeassistant/sensor/nowplaying/artist/config',
            'homeassistant/sensor/nowplaying/quality/config', 'homeassistant/sensor/nowplaying/source/config',
            'homeassistant/sensor/nowplaying/state/config', 'homeassistant/sensor/nowplaying/title/config'])
        title = configs['homeassistant/sensor/nowplaying/title/config']
        self.assertEqual({k: title[k] for k in ('name', 'unique_id', 'state_topic', 'value_template',
                                                'json_attributes_topic', 'availability_topic')}, {
            'name': 'Title', 'unique_id': 'nowplaying_title', 'state_topic': 'nowplaying/state',
            'value_template': '{{ value_json.title }}', 'json_attributes_topic': 'nowplaying/state',
            'availability_topic': 'nowplaying/availability'})
        self.assertEqual(title['device']['name'], 'Now Playing')
        self.assertEqual(title['device']['configuration_url'], 'http://192.168.1.20:8766/settings')
        self.assertEqual(configs['homeassistant/image/nowplaying/artwork/config']['url_topic'], 'nowplaying/artwork')
        # Another bridge, its own topic: its own device
        other = dict(mqtt.discovery('nowplaying/kitchen', 'homeassistant', 'http://x'))
        self.assertIn('homeassistant/sensor/nowplaying_kitchen/title/config', other)
        self.assertEqual(other['homeassistant/sensor/nowplaying_kitchen/title/config']['device']['name'],
                         'Now Playing nowplaying_kitchen')


class MqttBridgeTest(BridgeTestCase):
    # The bridge publishing to a fake broker; mpd plays a FLAC ("Song")
    def start_broker(self, refuse=0):
        broker = FakeBroker(refuse).start()
        self.addCleanup(broker.server_close)
        self.addCleanup(broker.shutdown)
        return broker

    def mqtt_env(self, broker, **env):
        return dict(MQTT_HOST='127.0.0.1', MQTT_PORT=str(broker.port), PUBLIC_URL='http://nowplaying.local:8766',
                    **env)

    def test_published(self):
        broker = self.start_broker()
        b = self.start_bridge(**self.mqtt_env(broker, MQTT_USER='ha', MQTT_PASSWORD='secret'))
        state = lambda: json.loads(broker.retained.get('nowplaying/state', '{}'))  # noqa: E731
        broker.wait_for(lambda: state().get('title') == 'Song')
        client = broker.clients[0]
        self.assertEqual({k: client[k] for k in ('protocol', 'clean', 'user', 'password', 'will', 'will_retain')}, {
            'protocol': ('MQTT', 4), 'clean': True, 'user': 'ha', 'password': 'secret',
            'will': ('nowplaying/availability', 'offline'), 'will_retain': True})
        self.assertEqual(broker.retained['nowplaying/availability'], 'online')
        self.assertEqual(state()['quality'], 'FLAC · 24bit / 96.0 kHz · Hi-Res')
        # The artwork's address comes right after the state
        artwork = lambda: broker.retained.get('nowplaying/artwork')  # noqa: E731
        broker.wait_for(lambda: artwork() == 'http://nowplaying.local:8766/art?file=Music%2FAlbum%2F01%20Song.flac')
        title = json.loads(broker.retained['homeassistant/sensor/nowplaying/title/config'])
        self.assertEqual(title['device']['configuration_url'], 'http://nowplaying.local:8766/settings')
        # A change, pushed at once
        self.mpd.set_scenario('mp3_mad')
        broker.wait_for(lambda: state().get('title') == 'Song MP3')
        broker.wait_for(lambda: artwork() == 'http://nowplaying.local:8766/icon-512.png')  # none: the app's icon
        # The bridge stops: the broker says it's gone
        b.stop()
        broker.wait_for(lambda: broker.retained['nowplaying/availability'] == 'offline')

    def test_without_discovery(self):
        broker = self.start_broker()
        self.start_bridge(**self.mqtt_env(broker, MQTT_DISCOVERY='0', MQTT_TOPIC='salon/nowplaying'))
        broker.wait_for(lambda: 'salon/nowplaying/state' in broker.retained)
        self.assertFalse([topic for topic in broker.retained if topic.startswith('homeassistant/')])

    def test_refused(self):
        broker = self.start_broker(refuse=5)
        b = self.start_bridge(**self.mqtt_env(broker))
        broker.wait_for(lambda: broker.clients)
        time.sleep(0.3)
        self.assertIn('MQTT: cannot connect to 127.0.0.1', b.stop())
        code, out = run_check(self.mpd.port, **self.mqtt_env(broker))
        self.assertEqual(code, 1, out)
        self.assertIn('FAIL  MQTT broker at 127.0.0.1', out)
        self.assertIn('not authorized', out)

    def test_check(self):
        broker = self.start_broker()
        code, out = run_check(self.mpd.port, **self.mqtt_env(broker))
        self.assertIn(f'ok    MQTT broker at 127.0.0.1:{broker.port}: publishing to nowplaying/, Home Assistant '
                      'finds it (homeassistant/...)', out)
        self.assertTrue(broker.clients[0]['client_id'].startswith('nowplaying-check-'))  # not the bridge's own
        code, out = run_check(self.mpd.port)
        self.assertIn('Home Assistant (MQTT): off (no MQTT_HOST)', out)


@unittest.skipUnless(hasattr(os, 'mkfifo'), 'needs named pipes')
class VuMetersTest(BridgeTestCase):
    def setUp(self):
        super().setUp()  # mpd plays a FLAC ("Song")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.fifo = os.path.join(tmp, 'mpd.fifo')
        os.mkfifo(self.fifo)

    def test_levels_with_the_updates(self):
        b = self.start_bridge(MPD_FIFO=self.fifo)
        self.assertTrue(b.now()[1]['levels'])  # the page can show meters
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/events?vu=1')
        response = conn.getresponse()
        self.assertEqual(read_event(response)['title'], 'Song')
        mpd = threading.Thread(target=fake_fifo.play, args=(self.fifo, 1.5, 1.0, 0.5))
        mpd.start()
        self.addCleanup(mpd.join)
        found = []
        while len(found) < 10:
            kind, data = read_sse(response)
            if kind == 'levels' and data[0] > -60:
                found.append(data)
        left, right, left_peak, right_peak = found[-1]
        self.assertAlmostEqual(left, -3.0, delta=0.2)
        self.assertAlmostEqual(right, -9.0, delta=0.2)
        self.assertAlmostEqual(right_peak, -6.0, delta=0.2)

    def test_no_fifo_no_meters(self):
        b = self.start_bridge(MPD_FIFO=self.fifo + '-missing')
        self.assertFalse(b.now()[1]['levels'])
        code, out = run_check(self.mpd.port, MPD_FIFO=self.fifo + '-missing')
        self.assertIn('VU meters (?vu=1): no mpd fifo output at', out)
        code, out = run_check(self.mpd.port, MPD_FIFO=self.fifo)
        self.assertIn("VU meters (?vu=1): mpd's fifo output at", out)


class DisplaySettingsTest(BridgeTestCase):
    SETTINGS = {'lang': 'fr', 'clock': '12', 'bg': 'blur', 'next': '0', 'vu': '1', 'shift': '0', 'scale': '1.5'}

    def post(self, b, body, content_type='application/json', origin=None):
        headers = {'Content-Type': content_type}
        if origin:
            headers['Origin'] = origin
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=5)
        try:
            conn.request('POST', '/display', body if isinstance(body, bytes) else json.dumps(body).encode(), headers)
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    def test_values(self):
        self.assertEqual(display.valid(self.SETTINGS), self.SETTINGS)
        self.assertEqual(display.valid({'scale': '3'}), {'scale': '3.0'})
        self.assertEqual(display.valid({'scale': ''}), {'scale': ''})  # fits the screen
        for wrong in ({'clock': '13'}, {'scale': '9'}, {'scale': 'nan'}, {'color': 'red'}, {'next': 0}, ['clock']):
            with self.subTest(wrong=wrong):
                self.assertIsNone(display.valid(wrong))

    def test_saved_then_pushed_to_the_screens(self):
        b = self.start_bridge()
        self.assertEqual(b.get('/settings')[0], 200)
        self.assertEqual(json.loads(b.get('/display')[2]), {})
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/events')
        response = conn.getresponse()
        read_event(response)
        self.assertEqual(self.post(b, self.SETTINGS), 204)
        self.assertEqual(read_event(response, 'settings'), self.SETTINGS)  # the screens reload with them
        self.assertEqual(json.loads(b.get('/display')[2]), self.SETTINGS)
        with open(os.path.join(b.data, 'settings.json')) as f:
            self.assertEqual(json.load(f), self.SETTINGS)
        # In the page itself, so they apply from the start
        self.assertIn(f'const SERVED = {json.dumps(self.SETTINGS)};', b.get('/')[2].decode())

    def test_refused(self):
        b = self.start_bridge()
        self.assertEqual(self.post(b, {'clock': '13'}), 400)
        self.assertEqual(self.post(b, b'{"clock": '), 400)
        self.assertEqual(self.post(b, b'clock=12', 'application/x-www-form-urlencoded'), 415)
        # Another site's page, in a browser on the network
        self.assertEqual(self.post(b, {'clock': '12'}, origin='http://elsewhere.example'), 403)
        self.assertEqual(self.post(b, {'clock': '12'}, origin=f'http://127.0.0.1:{b.port}'), 204)
        self.assertEqual(self.post(b, {'clock': '12'}, origin='https://127.0.0.1'), 204)  # through a proxy

    def test_cannot_save(self):
        b = self.start_bridge(DATA_DIR='/nonexistent/folder')
        self.assertEqual(self.post(b, {'clock': '12'}), 500)
        self.assertIn('cannot save the display settings', b.stop())
        code, out = run_check(self.mpd.port, DATA_DIR='/nonexistent/folder')
        self.assertIn('Settings page: cannot save in /nonexistent/folder', out)

    def test_page_off(self):
        b = self.start_bridge(SETTINGS_PAGE='0')
        self.assertEqual(b.get('/settings')[0], 404)
        self.assertEqual(self.post(b, {'clock': '12'}), 404)


class HistoryBridgeTest(BridgeTestCase):
    def test_history_page(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        with open(os.path.join(data, 'history.jsonl'), 'w') as f:
            f.write(json.dumps({'at': 1700000000, 'title': 'Earlier', 'artist': 'Art', 'album': 'Alb',
                                'source': 'mpd', 'station': '', 'duration': 200, 'art': ''}) + '\n')
        b = self.start_bridge(DATA_DIR=data)
        self.assertEqual(b.get('/history')[0], 200)
        answer = json.loads(b.get('/history.json')[2])
        self.assertEqual((answer['enabled'], [t['title'] for t in answer['tracks']]), (True, ['Earlier']))
        code, out = run_check(self.mpd.port, DATA_DIR=data)
        self.assertIn('Listening history at /history: 1 track(s)', out)
        self.assertIn('Scrobbling: off', out)

    def test_history_off(self):
        b = self.start_bridge(HISTORY='0')
        self.assertEqual(json.loads(b.get('/history.json')[2]), {'enabled': False, 'tracks': []})

    def test_radio_station_in_the_status(self):
        b = self.start_bridge()
        self.mpd.set_scenario('radio_artist_title')
        self.assertEqual(b.now()[1]['station'], 'Radio X')
        self.mpd.set_scenario('flac_hires')
        self.assertEqual(b.now()[1]['station'], '')


def spotify_event(port, **event):
    # librespot running spotify-event.py, with the event in its environment
    subprocess.run([sys.executable, os.path.join(ROOT, 'spotify-event.py'), f'http://127.0.0.1:{port}'],
                   env=dict(os.environ, **event), check=True, timeout=10, capture_output=True)


SPOTIFY_TRACK = dict(PLAYER_EVENT='track_changed', NAME='Blue Hour', ARTISTS='Vela Nova', ALBUM='City After Hours',
                     COVERS='https://i.scdn.co/image/ab67616d0000b273abcd', DURATION_MS='240000', ITEM_TYPE='Track')


def post(port, path, body, address='127.0.0.1'):
    conn = http.client.HTTPConnection(address, port, timeout=5)
    try:
        conn.request('POST', path, body=body, headers={'Content-Type': 'application/json'})
        return conn.getresponse().status
    finally:
        conn.close()


def outward_address():
    # This machine's address on its network (no packet is sent), if any
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('10.255.255.255', 1))
            address = s.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith('127.') or address == '0.0.0.0' else address


class SpotifyBridgeTest(BridgeTestCase):
    def test_spotify_and_mpd_take_turns(self):
        b = self.start_bridge()  # mpd plays a FLAC ("Song")
        spotify_event(b.port, PLAYER_EVENT='session_client_changed', CLIENT_NAME="Mat's iPhone")
        spotify_event(b.port, **SPOTIFY_TRACK)
        self.assertEqual(b.now()[1]['source'], 'mpd')  # not playing yet
        spotify_event(b.port, PLAYER_EVENT='playing', POSITION_MS='15000')
        status, data = b.now()
        self.assertEqual((status, data['source'], data['title'], data['artist'], data['codec'], data['sender']),
                         (200, 'spotify', 'Blue Hour', 'Vela Nova', 'Spotify', "Mat's iPhone"))
        self.assertEqual(data['art_url'], 'https://i.scdn.co/image/ab67616d0000b273abcd')

        spotify_event(b.port, PLAYER_EVENT='paused', POSITION_MS='16000')
        self.assertEqual(b.now()[1]['source'], 'mpd')  # paused while mpd plays: mpd
        self.mpd.set_scenario('stopped')
        self.assertEqual(b.now()[1]['state'], 'pause')  # nothing else plays: the paused Spotify
        spotify_event(b.port, PLAYER_EVENT='stopped')
        self.assertEqual(b.now()[1]['source'], 'mpd')

    def test_changes_are_pushed(self):
        b = self.start_bridge()
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/events')
        response = conn.getresponse()
        self.assertEqual(read_event(response)['source'], 'mpd')
        spotify_event(b.port, **SPOTIFY_TRACK)
        spotify_event(b.port, PLAYER_EVENT='playing')
        titles = [read_event(response)['title']]
        while titles[-1] != 'Blue Hour' and len(titles) < 5:
            titles.append(read_event(response)['title'])
        self.assertEqual(titles[-1], 'Blue Hour')

    def test_only_events_from_this_machine(self):
        b = self.start_bridge()
        self.assertEqual(post(b.port, '/spotify', b'not json'), 400)
        self.assertEqual(post(b.port, '/spotify', b'["a list"]'), 400)
        self.assertEqual(post(b.port, '/spotify', json.dumps({'PLAYER_EVENT': 'playing'}).encode()), 204)
        address = outward_address()
        if address:  # the same request from the network: refused
            self.assertEqual(post(b.port, '/spotify', b'{"PLAYER_EVENT": "stopped"}', address=address), 403)
            self.assertEqual(b.now()[1]['source'], 'spotify')

    def test_spotify_off(self):
        b = self.start_bridge(SPOTIFY='0')
        self.assertEqual(post(b.port, '/spotify', b'{"PLAYER_EVENT": "playing"}'), 404)

    def test_hook_without_bridge(self):
        # The bridge isn't running: the hook says so, and librespot carries on
        result = subprocess.run([sys.executable, os.path.join(ROOT, 'spotify-event.py'),
                                 f'http://127.0.0.1:{free_port()}'], env=dict(os.environ, PLAYER_EVENT='playing'),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn('nowplaying-spotify-event', result.stderr)


class CheckSpotifyTest(unittest.TestCase):
    def check(self, conf=None):
        reports = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'conf')
            if conf is not None:
                with open(path, 'w') as f:
                    f.write(conf)
            with mock.patch.object(check, 'RASPOTIFY_CONF', path), \
                    mock.patch.multiple(config, DEMO=False, SPOTIFY_ENABLED=True):
                check.check_spotify(lambda state, text, hint='': reports.append(state))
        return reports

    def test_raspotify_settings(self):
        self.assertEqual(self.check(), ['--'])  # raspotify not installed
        self.assertEqual(self.check('#LIBRESPOT_ONEVENT=\nLIBRESPOT_NAME="Salon"\n'), ['warn'])
        self.assertEqual(self.check('LIBRESPOT_ONEVENT=/nowhere/nowplaying-spotify-event\n'), ['FAIL'])
        self.assertEqual(self.check(f'LIBRESPOT_ONEVENT="{sys.executable} http://127.0.0.1:8766"\n'), ['ok'])


@unittest.skipUnless(shutil.which('openssl'), 'needs openssl to make a test certificate')
class HttpsTest(BridgeTestCase):
    def test_https(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        cert, key = os.path.join(tmp, 'cert.pem'), os.path.join(tmp, 'key.pem')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', cert,
                        '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                       check=True, capture_output=True)
        b = self.start_bridge(TLS_CERT=cert, TLS_KEY=key)
        status, data = b.now(cafile=cert)
        self.assertEqual((status, data['title']), (200, 'Song'))


if __name__ == '__main__':
    unittest.main()
