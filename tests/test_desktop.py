"""Deciding "is this a Windows desktop" from a frame.

The RDP check it replaces depended on the guest's NIC having a driver in the installed
system, which nothing installs - so it could wait forever on a good install. A desktop
is on screen the moment OOBE finishes, and the screen is captured anyway.

The numbers below are from a real desktop screenshot at 1280x750:

    top half    avg (143,183,212)  sat 0.33  var  1409
    bottom      avg ( 67,116,195)  sat 0.66  var 13812

A gradient with a busy dark bottom. The tests here check that shape and, just as
importantly, that the shapes it is NOT are rejected.
"""

import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import desktop  # noqa: E402


W, H = 320, 200   # small, so the tests are fast


def write_ppm(path: str, rows) -> str:
    """rows: a list of (r,g,b) per row, or a single colour to repeat."""
    if isinstance(rows, tuple):
        rows = [rows] * H
    with open(path, "wb") as fh:
        fh.write(f"P6\n{W} {H}\n255\n".encode())
        for colour in rows:
            fh.write(bytes(colour) * W)
    return path


def gradient(top, bottom):
    """A vertical gradient, which is what a desktop wallpaper looks like."""
    rows = []
    for y in range(H):
        t = y / (H - 1)
        rows.append(tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return rows


def with_noise(rows, amount=70):
    """Add variation so the bottom band has variance like a taskbar and icons."""
    out = []
    for y, colour in enumerate(rows):
        if y > H * 0.8:
            # Alternate dark and light within the row: variance without changing the
            # mean, which is exactly what a taskbar looks like at this resolution.
            out.append(tuple(max(0, min(255, c + (amount if (y % 2) else -amount)))
                             for c in colour))
        else:
            out.append(colour)
    return out


class TheShape(unittest.TestCase):
    """A desktop: light top, dark busy bottom, saturation rising with darkness."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_reference_shape_passes(self):
        # Measured values: drop 53, ratio 9.8x, saturation +0.33.
        rows = with_noise(gradient((143, 183, 212), (67, 116, 195)))
        path = write_ppm(os.path.join(self.tmp, "f.ppm"), rows)
        stats = desktop.analyse(path)
        self.assertIsNotNone(stats)
        self.assertGreater(stats["brightness_drop"], 15)
        self.assertGreater(stats["variance_ratio"], 2.0)
        self.assertGreater(stats["saturation_rise"], 0.10)
        self.assertTrue(desktop.is_desktop(path))

    def test_a_uniform_blue_screen_does_not_pass(self):
        # The Windows PE installer and the classic blue screen are both one flat blue.
        # A colour-matching check could not tell them from a desktop; this can, because
        # there is no gradient and no busy bottom.
        path = write_ppm(os.path.join(self.tmp, "f.ppm"), (26, 58, 92))
        self.assertFalse(desktop.is_desktop(path))

    def test_black_does_not_pass(self):
        path = write_ppm(os.path.join(self.tmp, "f.ppm"), (0, 0, 0))
        self.assertFalse(desktop.is_desktop(path))

    def test_an_inverted_gradient_does_not_pass(self):
        # A wallpaper that is dark at the top and light at the bottom fails the
        # brightness test. That is a limitation, not an accident: nothing has been
        # observed with that shape, and loosening the test would let the PE screen in.
        rows = with_noise(gradient((40, 60, 110), (200, 220, 240)))
        path = write_ppm(os.path.join(self.tmp, "f.ppm"), rows)
        self.assertFalse(desktop.is_desktop(path))

    def test_a_flat_light_screen_does_not_pass(self):
        # An Explorer window filling the screen is light everywhere.
        path = write_ppm(os.path.join(self.tmp, "f.ppm"), (240, 240, 240))
        self.assertFalse(desktop.is_desktop(path))


class ReadPpm(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_missing_file_is_none(self):
        # Frames land asynchronously; a file that is not there yet is not an error.
        self.assertIsNone(desktop.read_ppm("/nonexistent/frame-0000.ppm"))

    def test_a_truncated_frame_is_none(self):
        # qemu writes the file as it screendumps; the newest frame can be half written.
        # Reading it must not raise and must not decide anything.
        path = os.path.join(self.tmp, "f.ppm")
        with open(path, "wb") as fh:
            fh.write(f"P6\n{W} {H}\n255\n".encode())
            fh.write(b"\x10" * 100)   # far short of W*H*3
        self.assertIsNone(desktop.read_ppm(path))

    def test_a_comment_in_the_header_is_skipped(self):
        # PPM allows comments, and a parser that trips on one reads garbage offsets.
        path = os.path.join(self.tmp, "f.ppm")
        with open(path, "wb") as fh:
            fh.write(f"P6\n# written by qemu\n{W} {H}\n255\n".encode())
            fh.write(bytes((10, 20, 30)) * (W * H))
        parsed = desktop.read_ppm(path)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], W)
        self.assertEqual(parsed[1], H)

    def test_a_png_is_rejected(self):
        # Only qemu's PPM is expected; anything else is a caller mistake.
        path = os.path.join(self.tmp, "f.png")
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
        self.assertIsNone(desktop.read_ppm(path))


class Saturation(unittest.TestCase):

    def test_grey_has_none(self):
        self.assertAlmostEqual(desktop.saturation((128, 128, 128)), 0.0)

    def test_a_pure_colour_has_one(self):
        self.assertAlmostEqual(desktop.saturation((255, 0, 0)), 1.0)

    def test_black_does_not_divide_by_zero(self):
        self.assertEqual(desktop.saturation((0, 0, 0)), 0.0)


class ReachedDesktop(unittest.TestCase):
    """It looks at several frames, because the newest can be mid-repaint."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_empty_directory_is_false(self):
        self.assertFalse(desktop.reached_desktop(self.tmp))

    def test_it_finds_a_desktop_among_recent_frames(self):
        write_ppm(os.path.join(self.tmp, "frame-0000.ppm"), (0, 0, 0))
        write_ppm(os.path.join(self.tmp, "frame-0001.ppm"),
                  with_noise(gradient((143, 183, 212), (67, 116, 195))))
        write_ppm(os.path.join(self.tmp, "frame-0002.ppm"), (0, 0, 0))
        self.assertTrue(desktop.reached_desktop(self.tmp))

    def test_all_installer_frames_are_false(self):
        for i in range(3):
            write_ppm(os.path.join(self.tmp, f"frame-{i:04d}.ppm"), (26, 58, 92))
        self.assertFalse(desktop.reached_desktop(self.tmp))

    def test_newest_frame_is_reported(self):
        for i in range(3):
            write_ppm(os.path.join(self.tmp, f"frame-{i:04d}.ppm"), (0, 0, 0))
        self.assertTrue(desktop.newest_frame(self.tmp).endswith("frame-0002.ppm"))


if __name__ == "__main__":
    unittest.main()
