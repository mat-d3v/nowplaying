"""Tests for levels.py: the VU meters' levels, from mpd's fifo output.

    python3 -m unittest discover -s tests -v
"""
import logging
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import levels  # noqa: E402
from fake_fifo import play, sine  # noqa: E402


class MeasureTest(unittest.TestCase):
    def test_sine_levels(self):
        # A full-scale sine: -3 dB RMS, 0 dB peak; at half the amplitude, 6 dB less
        left, right, left_peak, right_peak = levels.measure(sine(0.1, 1.0, 0.5))
        self.assertAlmostEqual(left, -3.0, delta=0.15)
        self.assertAlmostEqual(right, -9.0, delta=0.15)
        self.assertAlmostEqual(left_peak, 0.0, delta=0.15)
        self.assertAlmostEqual(right_peak, -6.0, delta=0.15)

    def test_silence(self):
        self.assertEqual(levels.measure(bytes(levels.CHUNK)), levels.SILENCE)
        self.assertEqual(levels.measure(b''), levels.SILENCE)


class LevelMeterTest(unittest.TestCase):
    def test_runs_while_someone_listens(self):
        stopped = threading.Event()

        def source(wanted):
            while wanted():
                yield [-10.0, -12.0, -1.5, -2.5]
                time.sleep(0.01)
            stopped.set()
        meter = levels.LevelMeter(source)
        q = queue.Queue(maxsize=40)
        meter.listen(q)
        self.assertEqual(q.get(timeout=2), ('levels', [-10.0, -12.0, -1.5, -2.5]))
        time.sleep(0.2)
        self.assertLessEqual(q.qsize(), 5)  # a page that doesn't keep up skips levels
        meter.unlisten(q)
        self.assertTrue(stopped.wait(2))  # nobody listens: the source stops


@unittest.skipUnless(hasattr(os, 'mkfifo'), 'needs named pipes')
class FifoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.pipe = os.path.join(tmp, 'mpd.fifo')
        os.mkfifo(self.pipe)

    def test_reads_what_mpd_plays(self):
        listening, found = threading.Event(), []
        read = levels.fifo_levels(self.pipe, logging.getLogger('test'))

        def collect():
            for value in read(lambda: not listening.is_set()):
                found.append(value)
        reader = threading.Thread(target=collect)
        reader.start()
        play(self.pipe, 0.6, left=1.0, right=0.5)
        time.sleep(0.5)  # mpd stops writing: back to silence
        listening.set()
        reader.join(2)
        self.assertFalse(reader.is_alive())  # stops once nobody listens
        playing = [value for value in found if value != levels.SILENCE]
        self.assertGreaterEqual(len(playing), 5)
        self.assertAlmostEqual(playing[-1][0], -3.0, delta=0.2)
        self.assertAlmostEqual(playing[-1][1], -9.0, delta=0.2)
        self.assertEqual(found[-1], levels.SILENCE)


if __name__ == '__main__':
    unittest.main()
