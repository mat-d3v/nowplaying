"""Bluetooth: what a phone plays through this machine, from BlueZ.

When a phone streams its sound here over Bluetooth (A2DP, received by
bluez-alsa, PulseAudio or PipeWire), BlueZ also gets what it plays over
AVRCP: title, artist, album, duration, position, playing or paused. The
bridge asks BlueZ for it on the system D-Bus with busctl (systemd's tool)
every few seconds, every second while something is there.
"""
import json
import subprocess
import threading
import time

COMMAND = ['busctl', '--system', '--json=short', 'call', 'org.bluez', '/',
           'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects']
SINK = '0000110b-0000-1000-8000-00805f9b34fb'    # A2DP sink: sound coming in, from a phone
SOURCE = '0000110a-0000-1000-8000-00805f9b34fb'  # A2DP source: sound going out, to headphones
EVERY, EVERY_ACTIVE = 3, 1  # seconds between looks: nothing there / a device playing or paused
STATES = {'playing': 'play', 'forward-seek': 'play', 'reverse-seek': 'play', 'paused': 'pause'}
UNKNOWN = 0xFFFFFFFF  # AVRCP's "no idea", for a duration or a position


class BusError(Exception):
    pass


def plain(value):
    # busctl's JSON without its types: each variant, {"type": ..., "data":
    # ...}, becomes its data (BlueZ's own names are capitalized)
    if isinstance(value, dict):
        if set(value) == {'type', 'data'}:
            return plain(value['data'])
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def managed_objects(timeout=5):
    # BlueZ's objects: {path: {interface: {property: value}}}
    try:
        result = subprocess.run(COMMAND, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=timeout)
    except FileNotFoundError:
        raise BusError("busctl not found (systemd's D-Bus tool)")
    except subprocess.TimeoutExpired:
        raise BusError('BlueZ did not answer')
    if result.returncode:
        raise BusError(result.stderr.strip() or f'busctl failed (status {result.returncode})')
    try:
        return plain(json.loads(result.stdout)['data'][0])
    except (ValueError, KeyError, IndexError, TypeError):
        raise BusError('unexpected answer from busctl')


def problem(error):
    # What a BusError means: 'missing' (BlueZ, busctl or the bus aren't
    # there), 'denied', or 'error'
    text = str(error)
    if 'not provided by any .service' in text or 'not found' in text or 'Failed to connect to bus' in text:
        return 'missing'
    if 'Access denied' in text or 'Rejected send message' in text:
        return 'denied'
    return 'error'


def _seconds(ms):
    return ms / 1000 if isinstance(ms, int) and 0 <= ms < UNKNOWN else 0.0


def playing(objects):
    # What the devices streaming here play: the one playing, else the one
    # paused; None when there's neither. A device we stream to (headphones)
    # doesn't count: what it plays is what this machine sends it
    devices, sending, receiving = {}, set(), {}
    players = []
    for path, interfaces in sorted(objects.items()):
        if 'org.bluez.Device1' in interfaces:
            device = interfaces['org.bluez.Device1']
            devices[path] = device.get('Alias') or device.get('Name') or device.get('Address') or ''
        transport = interfaces.get('org.bluez.MediaTransport1')
        if transport:
            if transport.get('UUID') == SOURCE:
                sending.add(transport.get('Device'))
            elif transport.get('UUID') == SINK:
                receiving[transport.get('Device')] = transport.get('State') == 'active'
        if 'org.bluez.MediaPlayer1' in interfaces:
            players.append(interfaces['org.bluez.MediaPlayer1'])
    found = []
    for player in players:
        device = player.get('Device', '')
        state = STATES.get(player.get('Status'))
        if not state or device in sending:
            continue
        track = player.get('Track') or {}
        found.append({
            'device': device, 'sender': devices.get(device, ''), 'state': state,
            'title': track.get('Title', ''), 'artist': track.get('Artist', ''), 'album': track.get('Album', ''),
            'duration': _seconds(track.get('Duration')), 'position': _seconds(player.get('Position')),
        })
    # Sound coming in, from a device that doesn't say what it plays
    named = {track['device'] for track in found}
    for device, active in receiving.items():
        if active and device not in named:
            found.append({'device': device, 'sender': devices.get(device, ''), 'state': 'play',
                          'title': '', 'artist': '', 'album': '', 'duration': 0.0, 'position': 0.0})
    found.sort(key=lambda track: track['state'] != 'play')
    return found[0] if found else None


def _shown(track):
    # What the page shows, besides the progress
    return tuple(track[name] for name in ('state', 'title', 'artist', 'album', 'duration', 'sender'))


class Bluetooth:
    # What plays over Bluetooth, kept up to date from BlueZ
    def __init__(self):
        self.lock = threading.Lock()
        self.started = 0.0  # when it last started playing (time.monotonic)
        self.current = None
        self.position, self.since = 0.0, None  # position, as of `since`

    def _elapsed(self, now=None):
        elapsed = self.position
        if self.current['state'] == 'play' and self.since is not None:
            elapsed += (now or time.monotonic()) - self.since
        duration = self.current['duration']
        return min(elapsed, duration) if duration else elapsed

    def update(self, objects):
        # BlueZ's objects, looked at again (None: BlueZ can't say); True
        # when what the page shows changed
        found = playing(objects) if objects else None
        with self.lock:
            return self._update(found, time.monotonic())

    def _update(self, found, now):
        before = self.current
        if found is None:
            self.current = None
            return before is not None
        if found['state'] == 'play' and (before is None or before['state'] != 'play'):
            self.started = now
        jumped = False
        if before is None or (found['position'], found['state'], _shown(found)) != \
                (before['position'], before['state'], _shown(before)):
            # The phone says where it is: from now on, the progress counts
            # from there. Far from where it should be: a seek
            expected = self._elapsed(now) if before else found['position']
            jumped = abs(found['position'] - expected) > 3
            self.position, self.since = found['position'], now
        self.current = found
        return before is None or _shown(found) != _shown(before) or jumped

    def active(self):
        with self.lock:
            return self.current is not None

    def status(self):
        # The page's payload while a device plays or is paused, None otherwise
        with self.lock:
            track = self.current
            if not track:
                return None
            return {
                'state': track['state'],
                'title': track['title'] or 'Bluetooth',
                'artist': track['artist'] or track['sender'] or '—',
                'album': track['album'],
                'elapsed': self._elapsed(),
                'duration': track['duration'],
                'format': '',
                'codec': 'Bluetooth',
                'lossless': False,
                'art_url': '',
                'file': '',
                'stream': False,
                'source': 'bluetooth',
                'sender': track['sender'],
                'next_title': '',
                'next_artist': '',
            }

    def cover_wanted(self):
        # Phones send no cover over AVRCP: online artwork for what has a
        # title and an artist (no wait, as for AirPlay)
        with self.lock:
            track = self.current
            if not track or not (track['title'] and track['artist']):
                return None
            return 0.0, track['artist'], track['title'], track['album']


def follow(bluetooth, on_change, log):
    # Looks at BlueZ for good: every few seconds, every second while a
    # device plays or is paused. on_change() when what the page shows changed
    last_problem = None
    while True:
        try:
            objects = managed_objects()
        except BusError as e:
            kind = problem(e)
            if kind != last_problem:  # once per kind of problem
                if kind == 'missing':
                    log.info('Bluetooth: BlueZ is not there (%s); BLUETOOTH=0 stops looking', e)
                elif kind == 'denied':
                    log.warning('Bluetooth: BlueZ refused to answer (%s): add the user running the bridge to '
                                'the "bluetooth" group, then restart the bridge', e)
                else:
                    log.warning('Bluetooth: cannot ask BlueZ: %s', e)
                last_problem = kind
            if bluetooth.update(None):
                on_change()
            time.sleep(30)
            continue
        if last_problem:
            log.info('Bluetooth: BlueZ answers')
            last_problem = None
        try:
            if bluetooth.update(objects):
                on_change()
        except Exception:
            log.exception('Bluetooth: cannot follow what BlueZ says')
        time.sleep(EVERY_ACTIVE if bluetooth.active() else EVERY)
