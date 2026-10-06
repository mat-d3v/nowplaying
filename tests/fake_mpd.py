#!/usr/bin/env python3
"""Fake mpd server for the tests: answers the commands the bridge uses.

Each scenario describes what mpd reports (status, current song, next song,
embedded artwork). Switch scenarios with FakeMPD.set_scenario(), or from
another process by sending "fake-scenario <name>" on a connection, which
also wakes up clients waiting in "idle" ("fake-quiet <name>" doesn't).

    python3 tests/fake_mpd.py --port 6600 [--password secret] [--scenario flac_hires]
"""
import argparse
import select
import socketserver
import struct
import threading
import zlib


def _png(width, height, rgb):
    # Solid-color PNG, built by hand to avoid any dependency
    def chunk(kind, data):
        body = kind + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body))
    row = b'\x00' + bytes(rgb) * width
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(row * height))
            + chunk(b'IEND', b''))


GRAY_PNG = _png(64, 64, (100, 100, 100))


def _playing(audio, duration='200.0', **extra):
    status = {'state': 'play', 'elapsed': '12.0', 'audio': audio}
    if duration:
        status['duration'] = duration
    status.update(extra)
    return status


SCENARIOS = {
    'stopped': {'status': {'state': 'stop'}, 'song': {}},
    'flac_hires': {
        'status': _playing('96000:24:2'),
        'song': {'file': 'Music/Album/01 Song.flac', 'Title': 'Song', 'Artist': 'Art', 'Album': 'Alb'},
        'art': GRAY_PNG,
    },
    'flac_paused': {
        'status': dict(_playing('44100:16:2'), state='pause'),
        'song': {'file': 'Music/Album/02 Other.flac', 'Title': 'Other', 'Artist': 'Art', 'Album': 'Alb'},
    },
    # libmad decodes MP3 to 24-bit samples
    'mp3_mad': {
        'status': _playing('44100:24:2'),
        'song': {'file': 'Music/a.mp3', 'Title': 'Song MP3', 'Artist': 'Art', 'Album': 'Alb'},
    },
    'untagged': {
        'status': _playing('44100:16:2', nextsong='1'),
        'song': {'file': 'Music/rip/track01.flac'},
        'next': {'file': 'Music/rip/track02.flac'},
    },
    'm4a_aac': {
        'status': _playing('44100:f:2'),
        'song': {'file': 'Music/b.m4a', 'Title': 'Song AAC', 'Artist': 'Art', 'Album': 'Alb2'},
    },
    'm4a_alac': {
        'status': _playing('96000:24:2'),
        'song': {'file': 'Music/c.m4a', 'Title': 'Song ALAC', 'Artist': 'Art', 'Album': 'Alb3'},
    },
    'dsf': {
        'status': _playing('dsd64:2'),
        'song': {'file': 'Music/d.dsf', 'Title': 'Song DSD', 'Artist': 'Art', 'Album': 'Alb4'},
    },
    'long_title': {
        'status': _playing('44100:16:2'),
        'song': {'file': 'Music/e.flac', 'Title': 'A medium sized track title, again',
                 'Artist': 'Art', 'Album': 'Alb5'},
    },
    # Radios: no duration, title from the stream ("Artist - Title"), station name
    'radio_name_only': {
        'status': _playing('44100:f:2', duration=None),
        'song': {'file': 'http://radio.example/stream', 'Name': 'Radio X'},
    },
    'radio_artist_title': {
        'status': _playing('44100:f:2', duration=None),
        'song': {'file': 'http://radio.example/stream', 'Name': 'Radio X',
                 'Title': 'Daft Punk - One More Time'},
    },
    # Played from a UPnP/DLNA server (upmpdcli, BubbleUPnP...): an http address
    # with the tags, and a duration
    'upnp_flac': {
        'status': _playing('192000:24:2', duration='301.5'),
        'song': {'file': 'http://192.168.1.10:9790/minimserver/*/Music/Night%20Ferries/01%20Pier.flac',
                 'Title': 'Pier', 'Artist': 'June Avenue', 'Album': 'Night Ferries'},
    },
    # A streaming service through a UPnP controller: an address that doesn't
    # say what it carries, 24-bit samples at 96 kHz
    'stream_hires': {
        'status': _playing('96000:24:2', duration='254.0'),
        'song': {'file': 'http://192.168.1.20:49149/qobuz/track/version/1/trackId/123456',
                 'Title': 'Quay', 'Artist': 'June Avenue', 'Album': 'Night Ferries'},
    },
    # Not something mpd sends: makes the bridge hit an unexpected error
    'bad_status': {
        'status': {'state': 'play', 'elapsed': 'oops', 'audio': '44100:16:2'},
        'song': {'file': 'Music/f.flac', 'Title': 'Bad'},
    },
    'radio_plain_title': {
        'status': _playing('44100:f:2', duration=None),
        'song': {'file': 'http://radio.example/stream', 'Name': 'Radio X', 'Title': 'Morning show'},
    },
}


class _Handler(socketserver.StreamRequestHandler):
    rbufsize = 0  # unbuffered: a "noidle" sent right after "idle" stays visible to select()

    def write(self, text):
        data = text if isinstance(text, bytes) else text.encode()
        self.wfile.write(data)

    def handle(self):
        server = self.server
        authed = server.password is None
        self.write('OK MPD 0.23.5\n')
        while True:
            line = self.rfile.readline()
            if not line:
                return
            line = line.decode().strip()
            cmd, _, arg = line.partition(' ')
            if cmd == 'close':
                return
            if cmd in ('fake-scenario', 'fake-quiet'):
                server.set_scenario(arg, notify=cmd == 'fake-scenario')
                self.write('OK\n')
                continue
            if cmd == 'password':
                if arg.strip('"') == server.password:
                    authed = True
                    self.write('OK\n')
                else:
                    self.write('ACK [3@0] {password} incorrect password\n')
                continue
            if not authed:
                self.write(f'ACK [4@0] {{{cmd}}} you don\'t have permission for "{cmd}"\n')
                continue
            server.commands.append(cmd)
            scenario = SCENARIOS[server.scenario]
            if cmd == 'status':
                self.write(''.join(f'{k}: {v}\n' for k, v in scenario['status'].items()) + 'OK\n')
            elif cmd == 'currentsong':
                self.write(_song_lines(scenario['song']) + 'OK\n')
            elif cmd == 'playlistinfo':
                nxt = scenario.get('next')
                if nxt and arg == scenario['status'].get('nextsong'):
                    self.write(_song_lines(nxt) + 'OK\n')
                else:
                    self.write('ACK [2@0] {playlistinfo} Bad song index\n')
            elif cmd in ('readpicture', 'albumart'):
                self._picture(cmd, arg, scenario)
            elif cmd == 'idle':
                if not self._idle():
                    return
            elif cmd == 'noidle':
                self.write('OK\n')
            else:
                self.write(f'ACK [5@0] {{{cmd}}} unknown command "{cmd}"\n')

    def _picture(self, cmd, arg, scenario):
        uri, _, offset = arg.rpartition(' ')
        uri = uri.strip('"').replace('\\"', '"').replace('\\\\', '\\')
        art = scenario.get('art')
        if cmd != 'readpicture' or not art or uri != scenario['song'].get('file'):
            self.write('OK\n')
            return
        offset = int(offset)
        chunk = art[offset:offset + 8192]
        self.write(f'size: {len(art)}\ntype: image/png\nbinary: {len(chunk)}\n'.encode()
                   + chunk + b'\nOK\n')

    def _idle(self):
        server = self.server
        with server.changed:
            server.idling += 1
        try:
            return self._wait_for_change()
        finally:
            with server.changed:
                server.idling -= 1

    def _wait_for_change(self):
        # Block until the scenario changes (mpd's "changed: player"), or
        # until the client sends "noidle"
        server = self.server
        with server.changed:
            version = server.version
        while True:
            with server.changed:
                server.changed.wait(0.1)
                if server.version != version:
                    self.write('changed: player\nOK\n')
                    return True
            readable, _, _ = select.select([self.connection], [], [], 0)
            if readable:
                line = self.rfile.readline()
                if not line:
                    return False
                self.write('OK\n')  # noidle
                return True


def _song_lines(song):
    lines = ''
    if song.get('file'):
        lines += f"file: {song['file']}\n"
    for key, value in song.items():
        if key != 'file':
            lines += f'{key}: {value}\n'
    return lines


class FakeMPD(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port=0, password=None, scenario='stopped'):
        super().__init__(('127.0.0.1', port), _Handler)
        self.password = password
        self.scenario = scenario
        self.version = 0
        self.changed = threading.Condition()
        self.idling = 0  # clients waiting in "idle"
        self.commands = []

    @property
    def port(self):
        return self.server_address[1]

    def set_scenario(self, name, notify=True):
        if name not in SCENARIOS:
            raise KeyError(name)
        with self.changed:
            self.scenario = name
            if notify:  # what clients waiting in "idle" get told about
                self.version += 1
                self.changed.notify_all()

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--port', type=int, default=6600)
    parser.add_argument('--password')
    parser.add_argument('--scenario', default='flac_hires', choices=sorted(SCENARIOS))
    args = parser.parse_args()
    FakeMPD(args.port, args.password, args.scenario).serve_forever()
