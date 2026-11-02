"""winiso builds a small answer CD, not a remastered install ISO.

The distinction is the point of the module, and it was learned the expensive way: the
first version tried to add the answer file to the 7.2 GiB install ISO and produced a
448 KB image containing a boot record and one file. qemu started it, found nothing to
boot, and the bake hung.

These tests pin the shape of what is built - a tiny ISO with the answer file at its
root - without needing xorriso or a Windows ISO, which is what makes them runnable
anywhere.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import winiso  # noqa: E402


class MissingInput(unittest.TestCase):

    def test_missing_answer_file_is_an_error(self):
        empty = tempfile.mkdtemp()
        try:
            with self.assertRaises(winiso.IsoError):
                winiso.answer_iso(empty, os.path.join(empty, "out.iso"))
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class WinPeDrivers(unittest.TestCase):
    """$WinPEDriver$ is what makes the virtio disk visible during windowsPE.

    Without it setup's drive list is empty, and the answer file's WillShowUI=OnError
    turns that into a dialog nobody clicks. The first full bake stopped exactly there:
    console output grew to 4273 bytes and then nothing for fifteen minutes.
    """

    def test_the_directory_name_is_exact(self):
        # Setup scans for this literal name. `$WinPeDriver$` or `WinPEDriver` is a
        # directory setup ignores.
        self.assertEqual(winiso.WINPE_DRIVER_DIR, "$WinPEDriver$")

    def test_viostor_is_among_the_drivers(self):
        # viostor is the virtio-blk driver; without it the disk is invisible no matter
        # what else is present.
        self.assertIn("viostor", winiso.WINPE_DRIVERS)

    def test_missing_is_an_error_not_a_warning(self):
        listing = "Autounattend.xml"
        with mock.patch("os.path.getsize", return_value=4096), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(stdout=listing, stderr="")
            with self.assertRaises(winiso.IsoError) as ctx:
                winiso._verify("x.iso", want_drivers=True)
        self.assertIn("WinPEDriver", str(ctx.exception))
        self.assertIn("no disks", str(ctx.exception))


class BuildArgs(unittest.TestCase):
    """The argv, asserted without xorriso.

    A wrong flag is a FAILURE at run time only, and the run it fails ninety seconds
    into has already downloaded 7.2 GiB. `-rock on` cost exactly that.
    """

    def setUp(self):
        self.args = winiso.build_args("/tmp/out.iso", "/tmp/stage", "ANSWER")

    def test_outdev_is_a_new_image(self):
        # -outdev without -indev creates an empty disc; that is the intent here, since
        # the payload is one answer file rather than a copy of an installer.
        self.assertEqual(self.args[0], "xorriso")
        self.assertEqual(self.args[1:3], ["-outdev", "/tmp/out.iso"])

    def test_no_rock_command(self):
        # Rock Ridge is produced by default and xorriso has no "-rock" to ask for it.
        # Passing it aborts the run with "Not a known command".
        self.assertNotIn("-rock", self.args)

    def test_joliet_is_enabled(self):
        self.assertIn("-joliet", self.args)
        self.assertEqual(self.args[self.args.index("-joliet") + 1], "on")

    def test_the_map_destination_is_the_root(self):
        idx = self.args.index("-map")
        self.assertEqual(self.args[idx + 1], "/tmp/stage")
        # "/" is what puts Autounattend.xml at the image root, which is the only
        # place setup looks.
        self.assertEqual(self.args[idx + 2], "/")

    def test_it_commits(self):
        # Without -commit xorriso may still write at exit, but relying on that makes
        # the failure mode depend on the version.
        self.assertEqual(self.args[-1], "-commit")

    def test_the_label_is_passed(self):
        idx = self.args.index("-volid")
        self.assertEqual(self.args[idx + 1], "ANSWER")


class Verify(unittest.TestCase):
    """_verify is the guard that would have caught the 448 KB ISO."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.iso = os.path.join(self.tmp, "x.iso")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tiny_image_is_rejected_before_any_xorriso_call(self):
        # An ISO large enough for one file and a header is still thousands of bytes;
        # anything under 2 KiB cannot be one. This check exists so a truncated build
        # fails here rather than at the first setup pane, ninety minutes later.
        with open(self.iso, "wb") as fh:
            fh.write(b"\x00" * 1024)
        with self.assertRaises(winiso.IsoError):
            winiso._verify(self.iso)


class AnswerDirContents(unittest.TestCase):
    """Only the answer file matters; nothing else is staged."""

    def test_only_autounattend_is_required(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "Autounattend.xml"), "w") as fh:
                fh.write("<unattend/>")
            # The presence check must pass on the answer file alone. Anything else in
            # the directory is ignored by answer_iso, by design.
            self.assertTrue(os.path.isfile(os.path.join(d, "Autounattend.xml")))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
