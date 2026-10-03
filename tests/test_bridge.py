"""Tests for mpd-bridge.py: its helpers, and the real bridge against a fake mpd.

    python3 -m unittest discover -s tests -v
"""
import http.client
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from fake_mpd import GRAY_PNG, FakeMPD  # noqa: E402


def load_bridge():
    spec = importlib.util.spec_from_file_location('bridge', os.path.join(ROOT, 'mpd-bridge.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # main() only runs as a script
    return module


bridge = load_bridge()


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
            ('http://radio.example/stream.mp3', '44100:f:2'): ('', False),
            ('', ''): ('', False),
        }
        for (uri, audio), expected in cases.items():
            with self.subTest(uri=uri, audio=audio):
                self.assertEqual(bridge.get_codec(uri, audio), expected)


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
                self.assertEqual(bridge.display_title(song), expected)


class AudioFormatTest(unittest.TestCase):
    def test_mpd_source_format_first(self):
        with mock.patch.object(bridge, 'get_alsa_format', return_value='44100:32:2'):
            for audio in ('44100:16:2', '96000:24:2', '44100:f:2', 'dsd64:2'):
                with self.subTest(audio=audio):
                    self.assertEqual(bridge.get_audio_format({'audio': audio}), audio)

    def test_alsa_fallback(self):
        with mock.patch.object(bridge, 'get_alsa_format', return_value='48000:32:2'):
            self.assertEqual(bridge.get_audio_format({}), '48000:32:2')
            self.assertEqual(bridge.get_audio_format({'audio': 'garbage'}), '48000:32:2')


class BadgesTest(unittest.TestCase):
    def test_same_rules_as_the_page(self):
        cases = [
            (('96000:24:2', 'FLAC', True), ['FLAC', '24bit / 96.0 kHz', 'Hi-Res']),
            (('44100:16:2', 'FLAC', True), ['FLAC', '16bit / 44.1 kHz']),
            (('44100:24:2', 'MP3', False), ['MP3', '44.1 kHz']),   # no bit depth for lossy files
            (('dsd64:2', 'DSF', True), ['DSF', 'DSD64', 'Hi-Res']),
            (('44100:f:2', '', False), ['44.1 kHz']),              # a radio
            (('', 'FLAC', True), ['FLAC']),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(bridge.describe_badges(*args), expected)


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
        bridge._load_dotenv(f.name)
        self.assertEqual(os.environ['NP_TEST_A'], '1')  # still read after the bad line
        self.assertEqual(os.environ['NP_TEST_B'], 'two words')
        self.assertEqual(os.environ['NP_TEST_C'], 'x')
        self.assertEqual(os.environ['NP_TEST_SET'], 'from-env')  # the environment wins

    def test_missing_file(self):
        bridge._load_dotenv('/nonexistent/.env')  # no error


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Bridge:
    # mpd-bridge.py running in its own process; every setting is passed
    # explicitly, so a local .env can't change what the tests see
    def __init__(self, mpd_port, **env):
        self.port = free_port()
        settings = dict(MPD_HOST='127.0.0.1', MPD_PORT=str(mpd_port), PORT=str(self.port), MPD_PASSWORD='',
                        LASTFM_API_KEY='', ITUNES_ARTWORK='0', TLS_CERT='', TLS_KEY='', ALSA_CARD='99')
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
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.log.seek(0)
        output = self.log.read()
        self.log.close()
        return output

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
        for path in ('/', '/index.html', '/index.fr.html', '/?lang=fr', '/manifest.webmanifest',
                     '/icon-192.png', '/icon-512.png', '/touch-icon-v2.png', '/apple-touch-icon.png'):
            with self.subTest(path=path):
                self.assertEqual(b.get(path)[0], 200)
        self.assertEqual(b.get('/nope')[0], 404)

    def test_mpd_unreachable(self):
        b = Bridge(free_port())
        self.addCleanup(b.stop)
        status, data = b.now()
        self.assertEqual((status, data['error']), (503, 'mpd_unreachable'))


def run_check(mpd_port, **env):
    settings = dict(MPD_HOST='127.0.0.1', MPD_PORT=str(mpd_port), PORT=str(free_port()), MPD_PASSWORD='',
                    LASTFM_API_KEY='', ITUNES_ARTWORK='0', TLS_CERT='', TLS_KEY='', ALSA_CARD='99')
    settings.update(env)
    result = subprocess.run([sys.executable, os.path.join(ROOT, 'mpd-bridge.py'), '--check'],
                            env=dict(os.environ, **settings), capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout


class CheckTest(BridgeTestCase):
    def test_all_good(self):
        code, out = run_check(self.mpd.port)
        self.assertEqual(code, 0, out)
        for line in ('mpd 0.23.5 at', "Instant updates: mpd's idle command works", 'Playing: Song',
                     'badges: FLAC, 24bit / 96.0 kHz, Hi-Res', 'Artwork: embedded in the file', 'No problem found.'):
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


class EventsTest(BridgeTestCase):
    def read_event(self, response):
        while True:
            line = response.readline().decode()
            if line.startswith('data: '):
                return json.loads(line[6:])

    def test_changes_are_pushed(self):
        b = self.start_bridge()
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/events')
        response = conn.getresponse()
        self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
        self.assertEqual(self.read_event(response)['title'], 'Song')  # current status first
        time.sleep(0.3)  # let the bridge enter mpd's idle mode
        start = time.time()
        self.mpd.set_scenario('mp3_mad')
        self.assertEqual(self.read_event(response)['title'], 'Song MP3')
        self.assertLess(time.time() - start, 1)

    def test_watcher_survives_errors(self):
        b = self.start_bridge()
        conn = http.client.HTTPConnection('127.0.0.1', b.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/events')
        response = conn.getresponse()
        self.assertEqual(self.read_event(response)['title'], 'Song')
        time.sleep(0.3)
        self.mpd.set_scenario('bad_status')  # building the status fails
        time.sleep(0.3)
        status, data = b.now()
        self.assertEqual((status, data['error']), (500, 'internal'))
        self.mpd.set_scenario('mp3_mad')  # the watcher must still be there
        self.assertEqual(self.read_event(response)['title'], 'Song MP3')


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
