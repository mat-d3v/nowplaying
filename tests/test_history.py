"""Tests for history.py: what counts as a play, the history, scrobbling.

    python3 -m unittest discover -s tests -v
"""
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import history  # noqa: E402

STOP = {'state': 'stop', 'title': ''}
ENTRY = {'at': 1700000000, 'title': 'Aerodynamic', 'artist': 'Daft Punk', 'album': 'Discovery', 'station': '',
         'source': 'mpd', 'duration': 212, 'art': ''}


def status(title='Song', artist='Art', album='Alb', state='play', elapsed=0.0, duration=200.0, source='mpd',
           art_url='', station=''):
    return dict(state=state, title=title, artist=artist, album=album, elapsed=elapsed, duration=duration,
                source=source, art_url=art_url, station=station)


def wait_until(check, timeout=5):
    deadline = time.time() + timeout
    while not check():
        if time.time() > deadline:
            raise AssertionError('timed out')
        time.sleep(0.02)


class FakeScrobbler:
    def __init__(self):
        self.scrobbled, self.announced = [], []

    def scrobble(self, entry):
        self.scrobbled.append(entry['title'])

    def now_playing(self, entry):
        self.announced.append(entry['title'])


class ListeningTest(unittest.TestCase):
    def setUp(self):
        self.clock = 1000.0
        patcher = mock.patch.object(history.time, 'monotonic', side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.scrobbler = FakeScrobbler()
        self.listening = history.Listening(scrobbler=self.scrobbler, scrobble_sources=('mpd', 'airplay'))

    def play(self, seconds, listening=None, **track):
        (listening or self.listening).observe(status(**track))
        self.clock += seconds

    def titles(self, listening=None):
        return [entry['title'] for entry in (listening or self.listening).recent()]

    def test_what_counts(self):
        self.assertTrue(history.counts(100, 200))   # half the track
        self.assertFalse(history.counts(99, 200))
        self.assertTrue(history.counts(240, 600))   # or 4 minutes
        self.assertFalse(history.counts(30, 30))    # 30 s tracks never count
        self.assertTrue(history.counts(30, 0))      # no duration (radio): 30 s
        self.assertFalse(history.counts(29, 0))

    def test_played_tracks_in_the_history(self):
        self.play(120, title='First')
        self.play(10, title='Skipped')
        self.play(150, title='Third')
        self.listening.observe(STOP)
        self.assertEqual(self.titles(), ['Third', 'First'])  # the latest first
        self.assertEqual(self.scrobbler.scrobbled, ['First', 'Third'])
        self.assertEqual(self.scrobbler.announced, ['First', 'Skipped', 'Third'])  # "now playing"
        entry = self.listening.recent()[0]
        self.assertEqual((entry['artist'], entry['album'], entry['source'], entry['duration']), ('Art', 'Alb', 'mpd', 200))

    def test_started_before_the_bridge_saw_it(self):
        start = time.time()
        self.play(120, title='Halfway', elapsed=90)
        self.listening.observe(STOP)
        self.assertAlmostEqual(self.listening.recent()[0]['at'], start - 90, delta=2)

    def test_pauses_dont_count(self):
        self.play(60, title='Long')
        self.play(600, title='Long', state='pause', elapsed=60)  # 10 minutes paused
        self.play(39, title='Long', elapsed=60)                 # 99 s played: not half of 200
        self.listening.observe(STOP)
        self.assertEqual(self.titles(), [])

    def test_repeat_counts_twice(self):
        self.play(150, title='Loop', elapsed=0)
        self.play(40, title='Loop', elapsed=150)
        self.play(150, title='Loop', elapsed=1)  # from the start again
        self.listening.observe(STOP)
        self.assertEqual(self.titles(), ['Loop', 'Loop'])

    def test_radios(self):
        # A song (the title gave its artist): in the history, and scrobbled
        self.play(60, title='One More Time', artist='Daft Punk', album='Radio X', station='Radio X', duration=0)
        # The station's own name as the artist, or no artist: never scrobbled
        self.play(60, title='Morning show', artist='Radio X', album='', station='Radio X', duration=0)
        self.play(60, title='Jingle', artist='—', album='', station='Radio X', duration=0)
        self.listening.observe(STOP)
        self.assertEqual(self.titles(), ['Jingle', 'Morning show', 'One More Time'])
        self.assertEqual(self.scrobbler.scrobbled, ['One More Time'])

    def test_every_player_but_only_some_scrobbled(self):
        self.play(120, title='From Spotify', source='spotify')
        self.play(120, title='From AirPlay', source='airplay', art_url='/art?airplay=3')
        self.listening.observe(STOP)
        self.assertEqual(self.titles(), ['From AirPlay', 'From Spotify'])
        self.assertEqual(self.scrobbler.scrobbled, ['From AirPlay'])
        self.assertEqual(self.listening.recent()[0]['art'], '')  # AirPlay's cover address doesn't last

    def test_mpd_errors_end_the_track(self):
        self.play(120, title='Before')
        self.listening.observe({'error': 'mpd_unreachable', 'detail': 'refused'})
        self.assertEqual(self.titles(), ['Before'])

    def test_saved_then_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'history.jsonl')
            listening = history.Listening(path)
            self.play(120, listening, title='Kept')
            listening.observe(STOP)
            with open(path, 'a') as f:
                f.write('{"cut short by a power cut\n')
            self.assertEqual(self.titles(history.Listening(path)), ['Kept'])

    def test_file_rewritten_now_and_then(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(history, 'KEEP', 3):
            path = os.path.join(tmp, 'history.jsonl')
            listening = history.Listening(path)
            for i in range(8):
                self.play(120, listening, title=f'T{i}')
            listening.observe(STOP)
            with open(path) as f:
                self.assertLessEqual(len(f.readlines()), 6)
            self.assertEqual(self.titles(history.Listening(path)), ['T7', 'T6', 'T5'])


class FakeService:
    def __init__(self, failures=()):
        self.name = 'Fake'
        self.failures = list(failures)
        self.sent, self.now = [], []

    def scrobble(self, entry):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(entry['title'])

    def now_playing(self, entry):
        self.now.append(entry['title'])


class ScrobblerTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(history.Scrobbler, 'RETRY', 0.1)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = logging.getLogger('test-scrobbler')

    def test_sent_again_after_a_failure(self):
        service = FakeService([OSError('no network')])
        with self.assertLogs(self.log, 'INFO') as logs:
            scrobbler = history.Scrobbler([service], self.log)
            scrobbler.now_playing(dict(ENTRY, title='A'))
            scrobbler.scrobble(dict(ENTRY, title='A'))
            wait_until(lambda: service.sent == ['A'])
        self.assertEqual(service.now, ['A'])
        self.assertIn('sent again later', logs.output[0])

    def test_a_refused_listen_is_dropped(self):
        service = FakeService([history.ScrobbleError('bad listen', permanent=True)])
        with self.assertLogs(self.log, 'WARNING'):
            scrobbler = history.Scrobbler([service], self.log)
            scrobbler.scrobble(dict(ENTRY, title='A'))
            scrobbler.scrobble(dict(ENTRY, title='B'))
            wait_until(lambda: service.sent == ['B'])

    def test_a_refused_key_stops_it(self):
        service = FakeService([history.ScrobbleError('key refused', permanent=True, disable=True)])
        with self.assertLogs(self.log, 'ERROR'):
            scrobbler = history.Scrobbler([service], self.log)
            scrobbler.scrobble(dict(ENTRY, title='A'))
            wait_until(lambda: not scrobbler.services)
        scrobbler.scrobble(dict(ENTRY, title='B'))
        time.sleep(0.3)
        self.assertEqual(service.sent, [])


class ServicesTest(unittest.TestCase):
    def test_lastfm_signs_its_calls(self):
        sent = {}

        def fake_open(request, timeout=10):
            sent.update(urllib.parse.parse_qsl(request.data.decode()))
            return {'scrobbles': {}}
        with mock.patch.object(history, '_open', fake_open):
            history.LastFm('KEY', 'SECRET', 'SESSION').scrobble(ENTRY)
        # Last.fm's recipe: the parameters sorted by name, then the secret, in MD5
        signed = ('albumDiscoveryapi_keyKEYartistDaft Punkduration212methodtrack.scrobble'
                  'skSESSIONtimestamp1700000000trackAerodynamicSECRET')
        self.assertEqual(sent['api_sig'], hashlib.md5(signed.encode()).hexdigest())
        self.assertEqual((sent['format'], sent['method']), ('json', 'track.scrobble'))

    def test_lastfm_errors(self):
        service = history.LastFm('KEY', 'SECRET', 'SESSION')
        with mock.patch.object(history, '_open', return_value={'error': 9, 'message': 'Invalid session key'}):
            with self.assertRaises(history.ScrobbleError) as refused:
                service.scrobble(ENTRY)
        self.assertTrue(refused.exception.disable)
        with mock.patch.object(history, '_open', return_value={'error': 16, 'message': 'Try again later'}):
            with self.assertRaises(history.ScrobbleError) as later:
                service.scrobble(ENTRY)
        self.assertFalse(later.exception.permanent)

    def test_listenbrainz_listen(self):
        sent = []

        def fake_open(request, timeout=10):
            sent.append((request.full_url, request.get_header('Authorization'), json.loads(request.data)))
            return {'status': 'ok'}
        with mock.patch.object(history, '_open', fake_open):
            history.ListenBrainz('TOKEN', '1.1.0').scrobble(ENTRY)
        url, authorization, body = sent[0]
        self.assertEqual((url, authorization, body['listen_type']),
                         ('https://api.listenbrainz.org/1/submit-listens', 'Token TOKEN', 'single'))
        listen = body['payload'][0]
        self.assertEqual(listen['listened_at'], 1700000000)
        self.assertEqual((listen['track_metadata']['artist_name'], listen['track_metadata']['release_name']),
                         ('Daft Punk', 'Discovery'))
        self.assertEqual(listen['track_metadata']['additional_info']['duration_ms'], 212000)


if __name__ == '__main__':
    unittest.main()
