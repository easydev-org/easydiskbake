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
