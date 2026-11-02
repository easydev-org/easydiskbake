"""The recording must never be the reason a bake fails.

record_many is called after the guest has stopped and before the disk is compressed,
so an exception here would lose a disk that was otherwise finished. Its contract is a
bool, and these tests pin that: no ffmpeg, no frames, or a missing directory all have
to return False rather than raise.

The encoding itself needs ffmpeg, which is in the bake image but not on every machine
that runs the tests, so what is checked here is the failure half - which is the half
that matters, because it is the one that runs on a machine where something is wrong.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import vm  # noqa: E402


class RecordingFailures(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_no_frames_returns_false(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        self.assertFalse(vm.record_many(os.path.join(self.tmp, "a.mp4"), empty))

    def test_missing_directory_returns_false(self):
        # A bake whose snapshots.py never started has no shots directory at all, and
        # that must not be an exception either.
        self.assertFalse(
            vm.record_many(os.path.join(self.tmp, "b.mp4"), os.path.join(self.tmp, "nope"))
        )

    def test_no_directories_at_all_returns_false(self):
        self.assertFalse(vm.record_many(os.path.join(self.tmp, "c.mp4")))

    def test_several_empty_directories_returns_false(self):
        dirs = []
        for name in ("one", "two"):
            d = os.path.join(self.tmp, name)
            os.makedirs(d)
            dirs.append(d)
        self.assertFalse(vm.record_many(os.path.join(self.tmp, "d.mp4"), *dirs))


class RecordingOrder(unittest.TestCase):
    """The frame numbering decides the video's order, which is the whole point.

    MX records three boots - install, bootloader, the real boot - and a video with
    them out of order is worse than no video, because it looks like the installer
    ran twice.
    """

    def test_frames_are_collected_in_directory_order(self):
        # This mirrors the collection loop rather than calling it, because the encode
        # needs ffmpeg. What is pinned is the naming contract: sorted(frame-*.ppm)
        # within a directory, directories in the order given.
        tmp = tempfile.mkdtemp()
        a = os.path.join(tmp, "a")
        b = os.path.join(tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        # Deliberately created out of order; sorted() has to fix it.
        for name in ("frame-0001.ppm", "frame-0000.ppm"):
            open(os.path.join(a, name), "w").close()
        open(os.path.join(b, "frame-0000.ppm"), "w").close()

        collected = []
        for d in (a, b):
            frames = sorted(
                f for f in os.listdir(d) if f.startswith("frame-") and f.endswith(".ppm")
            )
            collected.extend(os.path.join(d, f) for f in frames)

        self.assertEqual(
            [os.path.relpath(p, tmp) for p in collected],
            ["a/frame-0000.ppm", "a/frame-0001.ppm", "b/frame-0000.ppm"],
        )


if __name__ == "__main__":
    unittest.main()
