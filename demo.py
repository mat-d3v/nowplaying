"""Demo mode (DEMO=1): made-up tracks instead of mpd.

To try the page without a music setup, or to take screenshots. The bridge
plays these tracks in a loop, DEMO_STEP seconds each (20 by default), and
pushes each change to the page like it does for mpd. The artwork is drawn
here, so the demo shows nobody else's pictures.
"""
import math
import os
import struct
import time
import zlib

STEP = max(1.0, float(os.environ.get('DEMO_STEP', '20')))

TRACKS = [
    dict(title='Midnight Tides', artist='Lumen Harbor', album='Coastal Lights', duration=287.0,
         format='96000:24:2', codec='FLAC', lossless=True, colors=((10, 42, 84), (36, 168, 160)), sun=(0.66, 0.34)),
    dict(title='Paper Planes at Dawn', artist='The Quiet Orchard', album='Morning Sketches', duration=214.0,
         format='44100:16:2', codec='ALAC', lossless=True, colors=((232, 108, 64), (248, 204, 128)), sun=(0.34, 0.62)),
    dict(title='Neon Rain', artist='Vela Nova', album='City After Hours', duration=251.0,
         format='44100:24:2', codec='MP3', lossless=False, colors=((58, 18, 104), (214, 52, 146)), sun=(0.5, 0.42)),
    dict(title='Golden Hour', artist='Solene Marr', album='Demo FM', duration=0.0, stream=True,
         format='44100:f:2', codec='', lossless=False, colors=((196, 132, 24), (104, 36, 22)), sun=(0.6, 0.56)),
    dict(title='Velvet Hours', artist='Ines Calder', album='Slow Bloom', duration=332.0,
         format='dsd64:2', codec='DSF', lossless=True, colors=((18, 64, 40), (186, 166, 82)), sun=(0.38, 0.36)),
]

START = time.time()


def _position(now=None):
    # (index of the current track, seconds since it started)
    played = (time.time() if now is None else now) - START
    return int(played // STEP) % len(TRACKS), played % STEP


def status(now=None):
    # The same payload as the bridge's get_status()
    index, played = _position(now)
    track, after = TRACKS[index], TRACKS[(index + 1) % len(TRACKS)]
    duration = track['duration']
    return {
        'state': 'play',
        'title': track['title'],
        'artist': track['artist'],
        'album': track['album'],
        # A third of the way in: the progress bar shows, and moves
        'elapsed': duration / 3 + played if duration else played,
        'duration': duration,
        'format': track['format'],
        'codec': track['codec'],
        'lossless': track['lossless'],
        'art_url': f'/art?demo={index}',
        'file': 'http://demo.example/stream' if track.get('stream') else f'Demo/{track["album"]}/{track["title"]}.flac',
        'stream': bool(track.get('stream')),
        'next_title': after['title'],
        'next_artist': after['artist'],
    }


def until_next_track(now=None):
    return STEP - _position(now)[1]


_artwork = {}


def artwork(index):
    # PNG bytes for a track's cover, drawn once then kept. No lock: a page
    # asking for a cover never waits behind the others being drawn (at
    # worst, a cover gets drawn twice)
    index = int(index) % len(TRACKS)
    if index not in _artwork:
        track = TRACKS[index]
        _artwork.setdefault(index, _draw(*track['colors'], track['sun']))
    return _artwork[index]


def draw_all():
    # Each cover takes about a second to draw: done ahead, in the background,
    # the current track first, then in playing order
    current = _position()[0]
    for offset in range(len(TRACKS)):
        artwork((current + offset) % len(TRACKS))


def _draw(top, bottom, sun, size=600):
    # A diagonal gradient, a pale disc with a soft edge, and faint ripples
    # around it
    cx, cy, radius = sun[0] * size, sun[1] * size, size * 0.2
    rows = []
    for y in range(size):
        row = bytearray(b'\x00')  # PNG filter: none
        for x in range(size):
            t = (x + y) / (2 * size - 2)
            color = [a + (b - a) * t for a, b in zip(top, bottom)]
            d = math.hypot(x - cx, y - cy)
            if d < radius:
                light = min(1.0, (radius - d) / (size / 120)) * 0.6
                color = [c + (255 - c) * light for c in color]
            else:
                ripple = (math.sin((d - radius) / (size / 43)) + 1) / 2 * 0.08 * math.exp(-(d - radius) / (size * 0.5))
                color = [c * (1 - ripple) for c in color]
            row += bytes(max(0, min(255, int(c))) for c in color)
        rows.append(bytes(row))

    def chunk(kind, data):
        body = kind + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(rows), 9))
            + chunk(b'IEND', b''))
