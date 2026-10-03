"""VU meters: the sound levels of what mpd plays, from its "fifo" output.

mpd can write what it plays to a named pipe, as raw samples (an
audio_output of type "fifo", format "44100:16:2", in mpd.conf: see the
README). While pages show the meters (?vu=1), the bridge reads that pipe
and sends them the level of each channel about 30 times a second: the
average (RMS) and the peak, in dB below full scale.
"""
import array
import math
import operator
import os
import random
import select
import sys
import threading
import time

RATE = 44100
CHUNK = RATE // 30 * 4  # 1/30 s of 16-bit stereo frames
FLOOR = -60.0           # dB: silence
SILENCE = [FLOOR] * 4

if hasattr(math, 'sumprod'):  # Python 3.12
    def _sum_of_squares(samples):
        return math.sumprod(samples, samples)
else:
    def _sum_of_squares(samples):
        return sum(map(operator.mul, samples, samples))


def _db(value):
    return round(max(FLOOR, 20 * math.log10(value)), 1) if value > 0 else FLOOR


def measure(chunk):
    # [left RMS, right RMS, left peak, right peak] of 16-bit stereo samples,
    # in dB below full scale
    samples = array.array('h', chunk[:len(chunk) // 4 * 4])
    if sys.byteorder == 'big':
        samples.byteswap()  # mpd writes them in the machine's order: little-endian here
    channels = samples[0::2], samples[1::2]
    if not channels[0]:
        return list(SILENCE)
    rms = [math.sqrt(_sum_of_squares(c) / len(c)) / 32768 for c in channels]
    peak = [max(max(c), -min(c)) / 32768 for c in channels]
    return [_db(v) for v in rms + peak]


def fifo_levels(path, log):
    # Levels from mpd's fifo output while wanted() says so: one about every
    # 1/30 s while mpd plays, SILENCE once when it stops writing
    def read(wanted):
        fd, buffer, quiet, problem = None, b'', False, None
        try:
            while wanted():
                if fd is None:
                    try:
                        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    except OSError as e:
                        if str(e) != problem:  # once per problem
                            log.warning('VU meters: cannot read %s: %s', path, e)
                            problem = str(e)
                        yield list(SILENCE)
                        time.sleep(2)
                        continue
                    problem = None
                ready, _, _ = select.select([fd], [], [], 0.2)
                data = b''
                if ready:
                    try:
                        data = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    if not data:  # mpd closed its end: open again, to wait for it
                        os.close(fd)
                        fd, buffer = None, b''
                        time.sleep(0.1)
                if not data:
                    if not quiet:
                        yield list(SILENCE)
                        quiet = True
                    continue
                quiet = False
                buffer += data
                if len(buffer) >= CHUNK:
                    # Only the latest: the meters show now, not a backlog
                    end = len(buffer) // CHUNK * CHUNK
                    yield measure(buffer[end - CHUNK:end])
                    buffer = buffer[end:]
        finally:
            if fd is not None:
                os.close(fd)
    return read


def demo_levels(wanted):
    # Demo mode: levels that move like music would
    start = time.monotonic()
    while wanted():
        t = time.monotonic() - start
        beat = max(0.0, math.sin(t * math.pi * 4)) ** 4  # 120 beats a minute
        levels = []
        for phase in (0.0, 1.7):
            rms = -15 + 4 * math.sin(t * 0.4 + phase) + 6 * beat + random.uniform(-1.2, 1.2)
            levels.append(round(rms, 1))
        levels += [round(min(0.0, rms + 8 + random.uniform(0, 2)), 1) for rms in levels]
        yield levels
        time.sleep(1 / 30)


class LevelMeter:
    # Sends levels to the queues listening, from a thread that runs while
    # any listens: nobody watching the meters, mpd's pipe isn't read
    def __init__(self, source):
        self.source = source  # source(wanted): yields levels while wanted()
        self.lock = threading.Lock()
        self.listeners = set()
        self.running = False

    def listen(self, q):
        with self.lock:
            self.listeners.add(q)
            if self.running:
                return
            self.running = True
        threading.Thread(target=self._run, name='levels', daemon=True).start()

    def unlisten(self, q):
        with self.lock:
            self.listeners.discard(q)

    def wanted(self):
        with self.lock:
            return bool(self.listeners)

    def _run(self):
        while True:
            for levels in self.source(self.wanted):
                with self.lock:
                    listeners = list(self.listeners)
                for q in listeners:
                    if q.qsize() < 5:  # a slow page skips levels, never status updates
                        q.put_nowait(('levels', levels))
            with self.lock:
                if not self.listeners:  # else: someone came back just now
                    self.running = False
                    return
