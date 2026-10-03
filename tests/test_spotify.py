"""Tests for spotify.py: librespot's events, and what the page gets from them.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import spotify  # noqa: E402

COVERS = ('https://i.scdn.co/image/ab67616d00004851aaaa\n'
          'https://i.scdn.co/image/ab67616d0000b273bbbb\n'
          'https://i.scdn.co/image/ab67616d00001e02cccc')


def track(name='Harbor Lights', artists='June Avenue', album='Night Ferries', duration_ms='200000'):
    return dict(PLAYER_EVENT='track_changed', TRACK_ID='4uLU6hMCjMI75M1A2tKUQC', NAME=name, ARTISTS=artists,
                ALBUM=album, COVERS=COVERS, DURATION_MS=duration_ms, ITEM_TYPE='Track')


class SpotifyTest(unittest.TestCase):
    def setUp(self):
        self.clock = 1000.0
        patcher = mock.patch.object(spotify.time, 'monotonic', side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.player = spotify.Spotify()

    def test_nothing_until_it_plays(self):
        self.assertIsNone(self.player.status())
        self.assertFalse(self.player.handle(track()))  # metadata alone: not shown yet
        self.assertIsNone(self.player.status())

    def test_playing(self):
        self.player.handle({'PLAYER_EVENT': 'session_client_changed', 'CLIENT_NAME': "Mat's iPhone"})
        self.player.handle(track())
        self.assertTrue(self.player.handle({'PLAYER_EVENT': 'playing', 'POSITION_MS': '30000'}))
        self.clock += 10
        data = self.player.status()
        self.assertEqual((data['state'], data['title'], data['artist'], data['album'], data['codec'], data['sender'],
                          data['source']),
                         ('play', 'Harbor Lights', 'June Avenue', 'Night Ferries', 'Spotify', "Mat's iPhone", 'spotify'))
        self.assertEqual((data['elapsed'], data['duration']), (40.0, 200.0))
        self.assertEqual(data['art_url'], 'https://i.scdn.co/image/ab67616d0000b273bbbb')  # the biggest

    def test_pause_seek_and_stop(self):
        self.player.handle(track())
        self.player.handle({'PLAYER_EVENT': 'playing', 'POSITION_MS': '0'})
        self.clock += 5
        self.assertTrue(self.player.handle({'PLAYER_EVENT': 'paused', 'POSITION_MS': '5000'}))
        self.clock += 60  # time stands still while paused
        self.assertEqual((self.player.status()['state'], self.player.status()['elapsed']), ('pause', 5.0))
        self.player.handle({'PLAYER_EVENT': 'seeked', 'POSITION_MS': '120000'})
        self.assertEqual(self.player.status()['elapsed'], 120.0)
        self.assertTrue(self.player.handle({'PLAYER_EVENT': 'stopped'}))  # e.g. moved to another device
        self.assertIsNone(self.player.status())

    def test_next_track(self):
        self.player.handle(track())
        self.player.handle({'PLAYER_EVENT': 'playing', 'POSITION_MS': '0'})
        self.clock += 200
        self.player.handle({'PLAYER_EVENT': 'end_of_track'})
        self.assertTrue(self.player.handle(track('Second Wind', 'June Avenue\nGuest Singer')))
        self.player.handle({'PLAYER_EVENT': 'playing', 'POSITION_MS': '0'})
        data = self.player.status()
        self.assertEqual((data['title'], data['artist'], data['elapsed']), ('Second Wind', 'June Avenue, Guest Singer', 0.0))

    def test_podcast_shows_its_show(self):
        self.player.handle(dict(track(), ITEM_TYPE='Episode', NAME='Episode 12', SHOW_NAME='The Show'))
        self.player.handle({'PLAYER_EVENT': 'playing'})
        data = self.player.status()
        self.assertEqual((data['title'], data['artist'], data['album']), ('Episode 12', 'The Show', ''))

    def test_gone_without_a_word(self):
        # librespot killed mid-track: once long past the end, it's over
        self.player.handle(track(duration_ms='200000'))
        self.player.handle({'PLAYER_EVENT': 'playing', 'POSITION_MS': '0'})
        self.clock += 200 + spotify.Spotify.STALE - 1
        self.assertIsNotNone(self.player.status())
        self.clock += 2
        self.assertIsNone(self.player.status())

    def test_started_orders_the_players(self):
        self.player.handle({'PLAYER_EVENT': 'playing'})
        first = self.player.started
        self.clock += 1
        self.player.handle({'PLAYER_EVENT': 'playing'})  # already playing: same start
        self.assertEqual(self.player.started, first)
        self.player.handle({'PLAYER_EVENT': 'paused'})
        self.player.handle({'PLAYER_EVENT': 'playing'})  # resumed: started again
        self.assertEqual(self.player.started, 1001.0)

    def test_pick_cover(self):
        self.assertEqual(spotify.pick_cover(COVERS), 'https://i.scdn.co/image/ab67616d0000b273bbbb')
        self.assertEqual(spotify.pick_cover('https://i.scdn.co/image/other\n'), 'https://i.scdn.co/image/other')
        self.assertEqual(spotify.pick_cover(''), '')
        self.assertEqual(spotify.pick_cover('javascript:alert(1)'), '')  # addresses only


if __name__ == '__main__':
    unittest.main()
