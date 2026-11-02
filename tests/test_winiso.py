"""winiso stages exactly one file, and that is the point.

The answer file is the whole guest-side contract for Windows. Everything that used
to need a script inside the guest is now checked from the host:

    which edition   lib/wim.py reads install.wim before the guest exists
    did it arrive   port 3389, which only opens once OOBE has finished

These tests pin that: if a $OEM$ tree ever reappears, the reason has to be argued
here rather than added quietly.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import winiso  # noqa: E402


class Staging(unittest.TestCase):

    def setUp(self):
        self.answer = tempfile.mkdtemp()
        self.overlay = None

    def tearDown(self):
        shutil.rmtree(self.answer, ignore_errors=True)
        if self.overlay:
            shutil.rmtree(self.overlay, ignore_errors=True)

    def _write(self, name, body="x"):
        with open(os.path.join(self.answer, name), "w") as fh:
            fh.write(body)

    def test_only_the_answer_file_is_staged(self):
        self._write("Autounattend.xml")

        self.overlay = winiso._stage(self.answer)

        staged = []
        for root, _dirs, files in os.walk(self.overlay):
            for name in files:
                staged.append(os.path.relpath(os.path.join(root, name), self.overlay))
        self.assertEqual(sorted(staged), ["Autounattend.xml"])

    def test_no_oem_tree_is_created(self):
        # The $OEM$ convention is documented in winiso.OEM_SCRIPTS but must not
        # appear in an overlay: staging a SetupComplete.cmd would put a script back
        # inside the guest, and that is the coupling this bake removed.
        self._write("Autounattend.xml")
        self.overlay = winiso._stage(self.answer)
        self.assertFalse(os.path.exists(os.path.join(self.overlay, "$OEM$")))

    def test_script_files_in_the_answer_dir_are_ignored(self):
        # Someone adding a .cmd next to the answer file should not have it shipped
        # silently: the host-side checks make it unnecessary, and if it WERE needed
        # the reason belongs in the answer file or in a new stage.
        self._write("Autounattend.xml")
        self._write("SetupComplete.cmd")
        self._write("extra.ps1")

        self.overlay = winiso._stage(self.answer)
        staged = []
        for root, _dirs, files in os.walk(self.overlay):
            for name in files:
                staged.append(os.path.relpath(os.path.join(root, name), self.overlay))
        self.assertEqual(sorted(staged), ["Autounattend.xml"])

    def test_missing_answer_file_is_an_error(self):
        # An ISO rebuilt without the answer file boots to the first setup pane and
        # hangs, which reads as a VM problem.
        with self.assertRaises(winiso.IsoError):
            winiso._stage(self.answer)


class OemPathDocumentation(unittest.TestCase):

    def test_constant_still_records_the_literal_dollars(self):
        # It is unused, but it is the thing a future reader will grep for, and the
        # dollars are the part that used to require shell escaping.
        self.assertEqual(winiso.OEM_SCRIPTS, "$OEM$/$$/Setup/Scripts")
        self.assertEqual(winiso.OEM_SCRIPTS.split("/"), ["$OEM$", "$$", "Setup", "Scripts"])


if __name__ == "__main__":
    unittest.main()
