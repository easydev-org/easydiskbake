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
    """What decides that a dark screen means a stopped guest.

    NOT a duration. The first version used a flat 90 minutes with a comment about how
    long a copy takes - a number with no measurement behind it, since no run of this
    bake had ever reached an installation. A duration cannot tell slow from stopped
    anyway, which is why the check moved to the disk.
    """

    def test_a_disk_stall_is_measured_not_guessed(self):
        self.assertTrue(hasattr(install, "DISK_STALL_SECONDS"))

    def test_the_stall_window_is_longer_than_a_slow_write(self):
        # An installer flushing a large buffer can pause for a minute. Five minutes of
        # literally no allocation is not a slow write.
        self.assertGreaterEqual(install.DISK_STALL_SECONDS, 3 * 60)

    def test_the_stall_window_is_short_enough_to_be_useful(self):
        # A run that hangs should fail while somebody is still watching, not after the
        # job timeout has already cancelled it.
        self.assertLess(install.DISK_STALL_SECONDS, 20 * 60)

    def test_the_backstop_covers_a_real_installation(self):
        # For the paths where the disk is not available, or the screen never goes dark.
        self.assertGreaterEqual(install.INSTALL_PATIENCE, 60 * 60)

    def test_the_backstop_fits_inside_the_job_timeout(self):
        self.assertLess(install.INSTALL_PATIENCE, 240 * 60)

    def test_the_forward_control_is_not_pressed_faster_than_a_pane_draws(self):
        self.assertGreaterEqual(install.CLICK_INTERVAL, 8)


class DiskGrowth(unittest.TestCase):
    """disk_allocated() reads st_blocks, which is what grows on a sparse qcow2."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="install-disk-")
        self.path = os.path.join(self.tmp, "disk.qcow2")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def recovery(self, disk_path):
        recovery = install.Recovery.__new__(install.Recovery)
        recovery.disk_path = disk_path
        return recovery

    def test_a_sparse_file_reports_what_it_allocated(self):
        # The apparent size is the virtual disk size from creation and never changes;
        # st_blocks is what an installation moves.
        with open(self.path, "wb") as fh:
            fh.truncate(64 * 1024 * 1024)
        before = self.recovery(self.path).disk_allocated()
        with open(self.path, "r+b") as fh:
            fh.seek(2 * 1024 * 1024)
            fh.write(b"\xAA" * (8 * 1024 * 1024))
        after = self.recovery(self.path).disk_allocated()
        self.assertIsNotNone(before)
        self.assertGreater(after, before)

    def test_a_missing_disk_reports_nothing(self):
        self.assertIsNone(self.recovery(os.path.join(self.tmp, "nope.qcow2"))
                          .disk_allocated())

    def test_no_disk_path_reports_nothing(self):
        # The driver still works without one; it just cannot answer this question, and
        # says so rather than inventing an answer.
        self.assertIsNone(self.recovery(None).disk_allocated())


if __name__ == "__main__":
    unittest.main()


class TheLastFrameIsWritten(unittest.TestCase):
    """lastframe.convert() has to exist, because the driver calls it after every click.

    MEASURED ON RUN 41493723813:

        could not refresh last-frame.png: module 'lastframe' has no attribute 'convert'

    on every click. The call is wrapped so a missing picture cannot stop a run - which is
    why it showed up as a warning rather than a failure, and also why nobody noticed the
    picture was never written.

    A cancelled run uploads out/last-frame.png as its only evidence of where it stopped,
    so a converter that does not exist turns every cancelled run back into 1086 bytes of
    frames.txt.
    """

    def test_convert_exists_and_takes_a_file(self):
        import lastframe
        self.assertTrue(hasattr(lastframe, "convert"))
        import inspect
        params = list(inspect.signature(lastframe.convert).parameters)
        self.assertEqual(params, ["source", "dest"])

    def test_the_driver_calls_convert_and_not_something_else(self):
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("lastframe.convert(", source)

    def test_a_missing_source_is_false_rather_than_raising(self):
        """Called from inside click(); an exception here would stop the driver."""
        import tempfile
        import lastframe
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(lastframe.convert(os.path.join(tmp, "nope.ppm"),
                                               os.path.join(tmp, "nope.png")))

    def test_the_inst_prefix_is_recognised(self):
        """shot() writes inst-NNN-<label>.ppm, which newest_frame() has to see.

        The names carry the action - "inst-003-after-02-recovery-Continue.ppm" says more
        than "frame-0003" - so the number is a prefix rather than the whole stem.
        """
        import lastframe
        self.assertIn("inst-", lastframe.PREFIXES)
