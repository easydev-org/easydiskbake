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


class TheSequenceStops(unittest.TestCase):
    """The driver stops three minutes after its last click, and says so.

    It used to watch for the whole installation - 40-70 minutes - behind a
    DISK_STALL_SECONDS/INSTALL_PATIENCE pair that no run had ever been measured against.
    What this bake now asks is "did every click land on the pane it was aimed at", which
    is answered within seconds of the last one: the installer accepts the destination and
    writes gigabytes, or it does not.

    A cancelled run is the other half of that: if nothing is published from this path,
    the recording is the result, and three minutes is enough to see whether it worked.
    """

    def test_the_watch_after_the_last_click_is_two_to_five_minutes(self):
        self.assertGreaterEqual(install.AFTER_LAST_ACTION_SECONDS, 120)
        self.assertLessEqual(install.AFTER_LAST_ACTION_SECONDS, 300)

    def test_it_keeps_watching_the_disk_after_the_sequence(self):
        """A disk that stops growing after the clicks is the failure this catches."""
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("def installer_panes")
        end = source.index("def run_installation")
        body = source[start:end]
        self.assertIn("AFTER_LAST_ACTION_SECONDS", body)
        self.assertIn("disk_allocated", body)

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


class ABlankScreenIsNotADrawnPane(unittest.TestCase):
    """Stability alone passes a black screen, and did.

    MEASURED ON RUN 41501519376. The recording from t=45 to t=59 has a mean grey of 1 -
    the screen was black - and the driver said

        18:37:28  the screen settled after 10s - Disk Utility has to replace the
                  recovery menu
        18:37:59  the screen settled after 10s - Disk Utility has to finish drawing
                  its sidebar

    then clicked Continue, Up, Erase and a disk name at that black screen.

    A blank frame does not change, so "two samples agree" is true of it from the first
    second, forever. wait_until_drawn() now also requires that something was drawn.
    """

    def test_the_threshold_is_a_floor_not_a_brightness_test(self):
        self.assertTrue(hasattr(install, "NOTHING_DRAWN"))
        # Above the boot screen (0-1) and below every real pane (20+).
        self.assertGreater(install.NOTHING_DRAWN, 2)
        self.assertLess(install.NOTHING_DRAWN, 20)

    def test_the_wait_checks_brightness_as_well_as_stability(self):
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("def wait_until_drawn")
        end = source.index("def wait_for_change")
        body = source[start:end]
        self.assertIn("NOTHING_DRAWN", body)
        self.assertIn("brightness()", body)

    def test_a_blank_frame_does_not_count_towards_stability(self):
        """The level check has to RESET the stable counter, not merely log.

        If it only skipped the sample, the next comparison would still match the blank
        frame it was compared against and the wait would pass on the following poll.
        """
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("def wait_until_drawn")
        end = source.index("def wait_for_change")
        body = source[start:end]
        blank = body[body.index("NOTHING_DRAWN"):]
        self.assertIn("stable = 0", blank[:400])
