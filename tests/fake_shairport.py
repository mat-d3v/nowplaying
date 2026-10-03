#!/usr/bin/env python3
"""Fake shairport-sync for the tests: writes AirPlay metadata to a pipe.

    python3 tests/fake_shairport.py --pipe /tmp/shairport-sync-metadata play
    python3 tests/fake_shairport.py --pipe /tmp/shairport-sync-metadata pause

The items have the format shairport-sync writes to its metadata pipe.
Writing waits (up to 10 s) for a reader, the bridge, to have the pipe open.
"""
import argparse
import base64
import os
import struct
import time
import zlib


def item(kind, code, data=b''):
    # One metadata item, as shairport-sync writes it
    if isinstance(data, str):
        data = data.encode()
    head = f'<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code><length>{len(data)}</length>'
    if data:
        head += '\n<data encoding="base64">\n' + base64.b64encode(data).decode() + '</data>'
    return (head + '</item>\n').encode()


def _png(rgb, size=64):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    rows = (b'\x00' + bytes(rgb) * size) * size
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


COVER = _png((200, 80, 40))


def track(title, artist, album, seconds=200, elapsed=30, rate=44100):
    # What shairport-sync sends when a track starts: the metadata bundle,
    # the cover, then the progress (RTP timestamps start/current/end)
    start = 1_000_000
    return (item('ssnc', 'mdst')
            + item('core', 'minm', title) + item('core', 'asar', artist) + item('core', 'asal', album)
            + item('core', 'astm', (seconds * 1000).to_bytes(4, 'big'))
            + item('ssnc', 'mden')
            + item('ssnc', 'pcst') + item('ssnc', 'PICT', COVER) + item('ssnc', 'pcen')
            + item('ssnc', 'prgr', f'{start}/{start + elapsed * rate}/{start + seconds * rate}'))


SCENARIOS = {
    # An iPhone starts playing
    'play': (item('ssnc', 'snam', "Mat's iPhone") + item('ssnc', 'pbeg')
             + track('Harbor Lights', 'June Avenue', 'Night Ferries')),
    'next': track('Second Wind', 'June Avenue', 'Night Ferries'),
    'pause': item('ssnc', 'pfls'),
    'resume': item('ssnc', 'prsm'),
    'stop': item('ssnc', 'pend'),
}


def send(pipe, data, timeout=10):
    # Writes to the pipe once a reader (the bridge) has it open; gives up
    # after timeout seconds rather than waiting forever
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError:  # nobody reading yet
            if time.time() > deadline:
                raise TimeoutError(f'nobody reads {pipe}')
            time.sleep(0.05)
    os.set_blocking(fd, True)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pipe', default='/tmp/shairport-sync-metadata')
    parser.add_argument('scenario', choices=sorted(SCENARIOS))
    args = parser.parse_args()
    if not os.path.exists(args.pipe):
        os.mkfifo(args.pipe)
    send(args.pipe, SCENARIOS[args.scenario])
