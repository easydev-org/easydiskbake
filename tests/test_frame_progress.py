"""Telling a slow install from a frozen one, from the frames.

Windows setup writes nothing to the serial console, so `console N bytes` sits at a
constant value through a healthy install and through a hang. The frames are the only
signal the host has, and this is the function that reads them.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "windows_bake", os.path.join(REPO, "windows", "11", "bake.py"))
bake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bake)


class FrameProgress(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _frame(self, name, payload):
        with open(os.path.join(self.tmp, name), "wb") as fh:
            fh.write(payload)

    def test_no_directory_is_zero(self):
        self.assertEqual(bake.frame_progress("/nonexistent"), (0, False))

    def test_empty_directory_is_zero(self):
        self.assertEqual(bake.frame_progress(self.tmp), (0, False))

    def test_one_frame_cannot_show_change(self):
        # One frame says the capture works, not that the screen moves.
        self._frame("frame-0000.ppm", b"aaaa")
        self.assertEqual(bake.frame_progress(self.tmp), (1, False))

    def test_identical_frames_are_static(self):
        # A frozen guest, or a dialog nobody clicks.
        for i in range(4):
            self._frame(f"frame-{i:04d}.ppm", b"same")
        count, changed = bake.frame_progress(self.tmp)
        self.assertEqual(count, 4)
        self.assertFalse(changed)

    def test_different_frames_are_changing(self):
        # An installer drawing, a progress bar moving.
        for i in range(4):
            self._frame(f"frame-{i:04d}.ppm", f"frame{i}".encode())
        count, changed = bake.frame_progress(self.tmp)
        self.assertEqual(count, 4)
        self.assertTrue(changed)

    def test_it_compares_against_an_older_frame_not_just_the_previous(self):
        # snapshots.py writes every N seconds, so two adjacent frames can be identical
        # while the screen is changing on a longer period. The window looks back.
        self._frame("frame-0000.ppm", b"first")
        self._frame("frame-0001.ppm", b"middle")
        self._frame("frame-0002.ppm", b"first")
        count, changed = bake.frame_progress(self.tmp, window=3)
        self.assertEqual(count, 3)
        self.assertFalse(changed)   # frame-0002 == frame-0000 at this window

    def test_a_partial_frame_does_not_crash(self):
        # snapshots.py writes asynchronously; a frame can be truncated mid-write.
        self._frame("frame-0000.ppm", b"a" * 100)
        self._frame("frame-0001.ppm", b"")
        count, changed = bake.frame_progress(self.tmp)
        self.assertEqual(count, 2)
        self.assertTrue(changed)


if __name__ == "__main__":
    unittest.main()
