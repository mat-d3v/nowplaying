"""Tests for shairport.py: reading shairport-sync's AirPlay metadata.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import shairport  # noqa: E402
from fake_shairport import COVER, SCENARIOS, item, track  # noqa: E402


def feed(airplay, data, chunk=None):
    # Feeds raw pipe data like the reader does, in chunks of `chunk` bytes
    changed, buffer = False, b''
    for i in range(0, len(data), chunk or len(data)):
        items, buffer = shairport.parse_items(buffer + data[i:i + (chunk or len(data))])
        changed |= any([airplay.handle(*it) for it in items])
    return changed, buffer


class ParseTest(unittest.TestCase):
    def test_items(self):
        data = item('core', 'minm', 'Café') + item('ssnc', 'pbeg') + item('ssnc', 'PICT', COVER)
        items, rest = shairport.parse_items(data)
        self.assertEqual(items, [('core', 'minm', 'Café'.encode()), ('ssnc', 'pbeg', b''), ('ssnc', 'PICT', COVER)])
        self.assertEqual(rest, b'')

    def test_split_anywhere(self):
        data = SCENARIOS['play']
        whole = shairport.parse_items(data)[0]
        items, buffer = [], b''
        for i in range(len(data)):  # one byte at a time
            got, buffer = shairport.parse_items(buffer + data[i:i + 1])
            items += got
        self.assertEqual((items, buffer), (whole, b''))

    def test_garbage_is_dropped(self):
        with mock.patch.object(shairport, 'MAX_BUFFER', 100):
            items, rest = shairport.parse_items(b'x' * 500)
        self.assertEqual((items, rest), ([], b''))


class AirPlayTest(unittest.TestCase):
    def setUp(self):
        self.airplay = shairport.AirPlay()

    def test_nothing_before_a_session(self):
        self.assertIsNone(self.airplay.status())

    def test_play_pause_resume_stop(self):
        changed, rest = feed(self.airplay, SCENARIOS['play'], chunk=7)
        self.assertTrue(changed)
        self.assertEqual(rest, b'')
        status = self.airplay.status()
        self.assertEqual({k: status[k] for k in ('state', 'title', 'artist', 'album', 'codec', 'sender', 'source')},
                         {'state': 'play', 'title': 'Harbor Lights', 'artist': 'June Avenue', 'album': 'Night Ferries',
                          'codec': 'AirPlay', 'sender': "Mat's iPhone", 'source': 'airplay'})
        self.assertAlmostEqual(status['duration'], 200, places=3)
        self.assertGreaterEqual(status['elapsed'], 30)
        self.assertTrue(status['art_url'].startswith('/art?airplay='))
        self.assertEqual(self.airplay.cover(), COVER)

        feed(self.airplay, SCENARIOS['pause'])
        paused = self.airplay.status()
        self.assertEqual(paused['state'], 'pause')
        self.assertEqual(self.airplay.status()['elapsed'], paused['elapsed'])  # frozen

        feed(self.airplay, SCENARIOS['resume'])
        self.assertEqual(self.airplay.status()['state'], 'play')

        feed(self.airplay, SCENARIOS['stop'])
        self.assertIsNone(self.airplay.status())

    def test_next_track_replaces_the_whole_bundle(self):
        feed(self.airplay, SCENARIOS['play'])
        first_cover = self.airplay.status()['art_url']
        feed(self.airplay, SCENARIOS['next'])
        status = self.airplay.status()
        self.assertEqual(status['title'], 'Second Wind')
        self.assertNotEqual(status['art_url'], first_cover)  # a new URL, so the page reloads it

    def test_bundle_applies_at_its_end(self):
        feed(self.airplay, item('ssnc', 'pbeg') + item('ssnc', 'mdst') + item('core', 'minm', 'Half'))
        self.assertEqual(self.airplay.status()['title'], 'AirPlay')  # not yet
        feed(self.airplay, item('ssnc', 'mden'))
        self.assertEqual(self.airplay.status()['title'], 'Half')

    def test_48_khz_progress(self):
        feed(self.airplay, item('ssnc', 'pbeg') + track('T', 'A', 'B', seconds=180, elapsed=60, rate=48000))
        status = self.airplay.status()
        self.assertAlmostEqual(status['duration'], 180, places=3)
        self.assertLess(status['elapsed'] - 60, 1)

    def test_metadata_alone_means_playing(self):
        # The bridge started in the middle of a session: no "pbeg" seen
        feed(self.airplay, track('T', 'A', 'B'))
        self.assertEqual(self.airplay.status()['state'], 'play')

    def test_without_metadata(self):
        # e.g. a Mac sending its sound: the sender stands in for the track
        feed(self.airplay, item('ssnc', 'snam', 'Office Mac') + item('ssnc', 'pbeg'))
        status = self.airplay.status()
        self.assertEqual((status['title'], status['artist'], status['art_url']), ('AirPlay', 'Office Mac', ''))


if __name__ == '__main__':
    unittest.main()
