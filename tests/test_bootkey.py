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

    def test_it_presses_when_the_firmware_lists_the_dvd(self):
        # THE BUG THIS FIXES. The prompt is on screen for about five seconds, and
        # waiting to see it on the serial console meant pressing at 60s - long after
        # the window had closed. The firmware's own DVD line appears seconds earlier
        # and in every UEFI boot, so it is the trigger to rely on.
        import tempfile
        tmp = tempfile.mkdtemp()
        console = os.path.join(tmp, "console.log")
        with open(console, "w") as fh:
            fh.write(
                'BdsDxe: loading Boot0001 "UEFI QEMU DVD-ROM QM00001" from PciRoot(0x0)\n'
            )

        with mock.patch.object(bootkey, "press", return_value=True) as press:
            # blind_after far in the future: only the firmware marker can trigger this.
            ok = bootkey.wait_for_boot_prompt(console, "/x.sock", timeout=5, blind_after=300)

        self.assertTrue(ok)
        press.assert_called_once()

    def test_the_poll_interval_matches_dockur(self):
        # Its loop ends with `sleep 0.25`. The window between the DVD marker and the
        # end of the prompt is a few seconds; a one-second poll can straddle it, which
        # is what the first attempt did - and it pressed at 60s, after the guest had
        # already fallen through to PXE.
        self.assertAlmostEqual(bootkey.POLL, 0.25, places=3)

    def test_it_presses_soon_after_the_marker_not_at_the_blind_deadline(self):
        """The timing that failed: marker at t=0, press at t=60.

        This drives the loop with a console that gains the marker immediately and
        asserts the press happens within a couple of polls, not at blind_after.
        """
        import tempfile
        import time as _time

        tmp = tempfile.mkdtemp()
        console = os.path.join(tmp, "console.log")
        with open(console, "w") as fh:
            fh.write('BdsDxe: loading Boot0001 "UEFI QEMU DVD-ROM QM00001"\n')

        pressed_at = {}

        def fake_press(_path):
            pressed_at["t"] = _time.time()
            return True

        started = _time.time()
        with mock.patch.object(bootkey, "press", side_effect=fake_press):
            ok = bootkey.wait_for_boot_prompt(
                console, "/x.sock", timeout=30, blind_after=300,
            )

        self.assertTrue(ok)
        elapsed = pressed_at["t"] - started
        # Two polls is 0.5s; allow generous slack for a loaded machine but stay far
        # below the old blind deadline.
        self.assertLess(elapsed, 5.0, f"pressed after {elapsed:.1f}s")

    def test_the_firmware_marker_is_checked(self):
        # Both halves: "UEFI QEMU" alone is a firmware version banner, and "DVD-ROM"
        # alone could be anything. Together they are the device listing.
        self.assertIn("UEFI QEMU", bootkey.FIRMWARE_DVD_MARKER)
        self.assertIn("DVD-ROM", bootkey.DVD_MARKER)

    def test_the_legacy_marker_is_recognised(self):
        # SeaBIOS prints "Booting from DVD/CD". A bake that only handled UEFI would
        # never press on a legacy guest.
        self.assertIn("DVD/CD", bootkey.LEGACY_DVD_MARKER)

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

    def test_the_key_is_escape_not_return(self):
        """Return is destructive when it arrives somewhere other than the prompt.

        The boot loader accepts any key, so the choice is about the case where the key
        lands on the desktop instead: Return opens the Start menu, which is what turned
        a finished bake into a screenshot full of menu. ESC closes things and opens
        nothing.
        """
        self.assertEqual(bootkey.BOOT_KEY, "esc")

    def test_the_sequence_is_short(self):
        # Six presses at 250ms all land inside the five-second prompt window; the ones
        # the loader does not consume stay queued. Three is enough to cover a loader
        # that is already listening.
        self.assertEqual(bootkey.REPEATS, 3)


if __name__ == "__main__":
    unittest.main()
