"""Reading a screendump's brightness, which is the only screen question this bake asks.

macos/15/install.py drives Recovery by coordinates and never looks at what is drawn.
The one exception is whether the screen has gone dark, and it exists because of a
recorded failure: run 41435208961 produced 93 identical black frames over 7.75 minutes
while the driver clicked coordinates at nothing, and bake.py had already handed the
screen to the Setup Assistant driver because the installer script had exited 0.

The probe is deliberately narrow. It answers "is this screen black" and nothing else -
no OCR, no template, no pane recognition - because every screen-reading question this
project has added has become a way to be wrong, and this one replaces a failure rather
than adding a feature.
"""

import os
import struct
import subprocess
import sys
import tempfile
import unittest

MACOS15 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "macos", "15")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
sys.path.insert(0, MACOS15)

import importlib.util  # noqa: E402

# Loaded by path: the file is macos/15/install.py, and "install" is also the name of a
# directory the standard library's distutils used to ship. Importing by module name
# finds whichever is first on sys.path.
_spec = importlib.util.spec_from_file_location(
    "macos_install", os.path.join(MACOS15, "install.py"))
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


def write_ppm(path, width, height, pixel):
    """A P6 image filled by `pixel(x, y)` returning (r, g, b)."""
    body = bytearray()
    for y in range(height):
        for x in range(width):
            body += bytes(pixel(x, y))
    with open(path, "wb") as fh:
        fh.write(b"P6\n%d %d\n255\n" % (width, height))
        fh.write(body)


class FakeQmp:
    """Stands in for qmp.QMP: a screendump that copies a prepared image."""

    def __init__(self, source):
        self.source = source

    def execute(self, command, **arguments):
        assert command == "screendump"
        with open(self.source, "rb") as src, open(arguments["filename"], "wb") as dst:
            dst.write(src.read())

    def close(self):
        pass


class Brightness(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="install-brightness-")
        self.frames = os.path.join(self.tmp, "frames")
        os.makedirs(self.frames)

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def probe(self, pixel, width=320, height=200):
        source = os.path.join(self.tmp, "screen.ppm")
        write_ppm(source, width, height, pixel)
        recovery = install.Recovery.__new__(install.Recovery)
        recovery.q = FakeQmp(source)
        recovery.frames = self.frames
        return recovery.brightness()

    def test_a_black_screen_reads_dark(self):
        # The exact case: OVMF stopped at a text error and then the console cleared.
        self.assertEqual(self.probe(lambda x, y: (0, 0, 0)), 0)

    def test_a_white_pane_reads_lit(self):
        self.assertEqual(self.probe(lambda x, y: (255, 255, 255)), 255)

    def test_a_mid_grey_pane_reads_above_the_dark_threshold(self):
        level = self.probe(lambda x, y: (144, 144, 144))
        self.assertGreater(level, install.DARK_LEVEL)

    def test_the_threshold_is_between_dark_and_lit(self):
        # Not a tautology: DARK_LEVEL has to sit in the gap, or a dark wallpaper fails
        # the check and a dim pane passes it.
        self.assertLess(install.DARK_LEVEL, 40)
        self.assertGreater(install.DARK_LEVEL, 8)

    def test_a_mostly_black_screen_with_text_still_reads_dark(self):
        """An OVMF error console: black with a few lines of white text.

        This is what the frames of the failed run actually contained, and it must not
        be mistaken for a working screen. Text covers well under one percent of the
        pixels, so the mean stays near zero.
        """
        def pixel(x, y):
            return (255, 255, 255) if 40 <= y < 52 and 20 <= x < 300 else (0, 0, 0)
        self.assertLess(self.probe(pixel), install.DARK_LEVEL)

    def test_no_frames_directory_means_no_answer(self):
        recovery = install.Recovery.__new__(install.Recovery)
        recovery.q = FakeQmp(os.path.join(self.tmp, "screen.ppm"))
        recovery.frames = None
        self.assertIsNone(recovery.brightness())

    def test_an_unreadable_screendump_means_no_answer(self):
        recovery = install.Recovery.__new__(install.Recovery)
        recovery.q = FakeQmp(os.path.join(self.tmp, "does-not-exist.ppm"))
        recovery.frames = self.frames
        self.assertIsNone(recovery.brightness())

    def test_a_non_ppm_file_means_no_answer(self):
        source = os.path.join(self.tmp, "screen.ppm")
        with open(source, "wb") as fh:
            fh.write(b"not an image at all")
        recovery = install.Recovery.__new__(install.Recovery)
        recovery.q = FakeQmp(source)
        recovery.frames = self.frames
        self.assertIsNone(recovery.brightness())


class InstallerPatience(unittest.TestCase):
    """The constants that decide how long a dark screen is tolerable."""

    def test_dark_patience_covers_a_real_installation(self):
        # dockur-macos's runs spend 40-70 minutes copying and rebooting. Anything
        # shorter than that would call a working install a failure.
        self.assertGreaterEqual(install.DARK_PATIENCE, 60 * 60)

    def test_dark_patience_fits_inside_the_job_timeout(self):
        # The bake's own --timeout defaults to 5400 and the workflow allows 240 minutes;
        # a patience longer than the job would only ever be reported by the job being
        # cancelled, which says nothing about the guest.
        self.assertLess(install.DARK_PATIENCE, 240 * 60)

    def test_the_forward_control_is_not_pressed_faster_than_a_pane_draws(self):
        self.assertGreaterEqual(install.CLICK_INTERVAL, 8)


if __name__ == "__main__":
    unittest.main()
