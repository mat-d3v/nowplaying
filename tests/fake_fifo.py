#!/usr/bin/env python3
"""Fake mpd "fifo" output for the tests: a sine wave written to a named pipe.

    python3 tests/fake_fifo.py --pipe /tmp/mpd.fifo --seconds 3 --left 1 --right 0.5

16-bit stereo samples at 44.1 kHz, like mpd's fifo output with format
"44100:16:2", written at the pace of the music. Writing waits (up to 10 s)
for a reader, the bridge, to have the pipe open.
"""
import argparse
import array
import math
import os
import sys
import time

RATE = 44100


def sine(seconds, left, right, freq=440):
    # 16-bit stereo samples; left and right are amplitudes, 1 is full scale
    frames = int(RATE * seconds)
    samples = array.array('h', bytes(frames * 4))
    for i in range(frames):
        v = math.sin(2 * math.pi * freq * i / RATE)
        samples[2 * i], samples[2 * i + 1] = int(32767 * left * v), int(32767 * right * v)
    if sys.byteorder == 'big':
        samples.byteswap()
    return samples.tobytes()


def play(pipe, seconds, left=1.0, right=1.0, timeout=10):
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
    block = sine(0.1, left, right)  # 44 whole periods of 440 Hz: it loops seamlessly
    start, written = time.time(), 0.0
    try:
        while written < seconds:
            os.write(fd, block)
            written += 0.1
            time.sleep(max(0.0, start + written - time.time()))
    except BrokenPipeError:
        pass  # the reader went away
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pipe', default='/tmp/mpd.fifo')
    parser.add_argument('--seconds', type=float, default=3)
    parser.add_argument('--left', type=float, default=1.0)
    parser.add_argument('--right', type=float, default=1.0)
    args = parser.parse_args()
    if not os.path.exists(args.pipe):
        os.mkfifo(args.pipe)
    play(args.pipe, args.seconds, args.left, args.right)
