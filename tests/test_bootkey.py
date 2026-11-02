"""The boot keypress, and why a Windows ISO needs one.

A Windows install ISO does not boot by itself. The El Torito boot image prints

    Press any key to boot from CD or DVD...

and waits about five seconds. Nothing presses it in a bake, so the guest falls through
to PXE - which is what the first full run recorded, for fifteen minutes, while the
arrival check waited for a Windows that had never started.

No answer-file setting avoids it: the prompt is in the boot sector, before setup.exe
runs.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import bootkey  # noqa: E402


REAL_CONSOLE = """\
BdsDxe: loading Boot0001 "UEFI QEMU DVD-ROM QM00001" from PciRoot(0x0)/Pci(0x1F,0x2)
BdsDxe: starting Boot0001 "UEFI QEMU DVD-ROM QM00001" from PciRoot(0x0)/Pci(0x1F,0x2)
Press any key to boot from CD or DVD...
"""


class Prompt(unittest.TestCase):

    def test_the_marker_is_the_one_the_boot_image_prints(self):
        # A substring match against what the guest writes; the trailing dots and the
        # locale variants vary, the leading words do not.
        self.assertIn(bootkey.PROMPT, REAL_CONSOLE)

    def test_it_presses_on_the_marker(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        console = os.path.join(tmp, "console.log")
        with open(console, "w") as fh:
            fh.write(REAL_CONSOLE)

        with mock.patch.object(bootkey, "press", return_value=True) as press:
            ok = bootkey.wait_for_boot_prompt(console, "/nonexistent.sock", timeout=5)

        self.assertTrue(ok)
        press.assert_called_once()

    def test_it_does_not_press_before_the_marker(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        console = os.path.join(tmp, "console.log")
        with open(console, "w") as fh:
            fh.write("BdsDxe: loading Boot0001 ...\nstarting Boot0001 ...\n")

        with mock.patch.object(bootkey, "press", return_value=True) as press:
            # timeout below blind_after: no prompt, no press.
            ok = bootkey.wait_for_boot_prompt(console, "/x.sock", timeout=3, blind_after=60)

        self.assertFalse(ok)
        press.assert_not_called()

    def test_it_presses_blind_when_the_prompt_never_arrives(self):
        # Some Windows versions do not emit the prompt to the serial console until much
        # later. dockur waits for the marker and falls back to blind timing for exactly
        # this reason: pressing Return at a guest that is not listening is harmless.
        import tempfile
        tmp = tempfile.mkdtemp()
        console = os.path.join(tmp, "console.log")
        with open(console, "w") as fh:
            fh.write("BdsDxe: loading Boot0001 ...\n")

        with mock.patch.object(bootkey, "press", return_value=True) as press:
            ok = bootkey.wait_for_boot_prompt(console, "/x.sock", timeout=5, blind_after=0)

        self.assertTrue(ok)
        press.assert_called_once()

    def test_a_missing_console_does_not_crash(self):
        with mock.patch.object(bootkey, "press", return_value=True):
            ok = bootkey.wait_for_boot_prompt("/nonexistent/console.log", "/x.sock",
                                              timeout=2, blind_after=0)
        self.assertTrue(ok)


class Press(unittest.TestCase):

    def test_no_socket_is_a_quiet_failure(self):
        # Before qemu has created the socket there is nothing to connect to; that is
        # expected, not an error.
        self.assertFalse(bootkey.press("/nonexistent/qmp.sock"))

    def test_it_sends_the_configured_number_of_keys(self):
        sent = []

        class FakeQmp:
            def __init__(self, _path): pass
            def close(self): pass

        def fake_key(_q, code):
            sent.append(code)

        with mock.patch.object(bootkey.os.path, "exists", return_value=True), \
             mock.patch.object(bootkey.qmpmod, "QMP", FakeQmp), \
             mock.patch.object(bootkey.qmpmod, "key", fake_key), \
             mock.patch.object(bootkey.time, "sleep"):
            self.assertTrue(bootkey.press("/x.sock", repeats=4, interval=0))

        self.assertEqual(len(sent), 4)
        self.assertTrue(all(k == bootkey.BOOT_KEY for k in sent))

    def test_the_key_is_return(self):
        # dockur sends `ret`; the boot image accepts any key, and Return is the one
        # that is safe if a boot menu happens to be up.
        self.assertEqual(bootkey.BOOT_KEY, "ret")


if __name__ == "__main__":
    unittest.main()
