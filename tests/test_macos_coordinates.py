"""The installer coordinates, pinned to what a frame of this bake actually shows.

WHY A TEST FILE FOR NUMBERS

Every macOS run so far has failed on a position. The pattern is the same each time:
a coordinate is copied from a document, or from another driver, and it is plausible -
inside the screen, near where the thing usually is - so nothing catches it. The guest
sits on a pane with one button while thirty-seven clicks land elsewhere, and the log
says the clicks happened.

The numbers below came from tesseract on frames this repository captured, and they are
asserted so the next edit has to argue with a measurement rather than with a comment.

    run 41445286843, frame-0100      the installer's welcome pane
    run 41445286843, frame-0030      the Recovery menu

WHAT THESE TESTS CAN AND CANNOT SEE

They check that the constants are the measured ones and that they are inside the guest
screen. They cannot check that the thing is still there on a different macOS build -
only a run can. But a coordinate that has drifted back to the wizard's Continue, or
that is off the edge of the screen, is caught here in a tenth of a second.
"""

import importlib.util
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))

import opencore  # noqa: E402

MACOS15 = os.path.join(HERE, "macos", "15")
_spec = importlib.util.spec_from_file_location(
    "macos_install_coords", os.path.join(MACOS15, "install.py"))
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


class MeasuredPositions(unittest.TestCase):
    """The numbers from the frames, as constants."""

    def test_the_recovery_rows_are_title_and_description_centres(self):
        """Each entry is a title with a description 18px below it.

            Restore from Time Machine   title y=229  desc y=250
            Reinstall macOS Sequoia     title y=324  desc y=342
            Safari                      title y=416  desc y=434
            Disk Utility                title y=502  desc y=526

        The clickable centre is between the two, which is why the values are not the
        title's y.
        """
        self.assertEqual(install.ROW_Y, (240, 333, 425, 514))

    def test_the_labels_are_left_aligned_inside_the_window(self):
        # Every entry's text starts at x=837; clicking the far left of the row would
        # work too, but the measured alignment is what the frames show.
        self.assertEqual(install.ROW_X, 837)

    def test_the_recovery_continue_is_where_the_frame_has_it(self):
        # y=578..595, x=1115..1169.
        self.assertEqual(install.CONTINUE_MENU, (1142, 586))

    def test_the_installer_continue_is_not_the_wizards(self):
        """THE ONE THAT COST A RUN.

        (1306, 810) is macos/15/drive.py's Continue, measured on the Setup Assistant -
        a different window set entirely. The installer's welcome pane puts its button at
        x=919..1002, y=698..727, so the wizard's value lands 346px right and 98px below
        it and hits nothing.

        A pane with one control and thirty-seven clicks that miss it is the shape of
        every failed run so far.
        """
        self.assertEqual(install.INSTALLER_CONTINUE, (960, 712))

        # And it must not be the wizard's position, or this test has no subject.
        with open(os.path.join(MACOS15, "drive.py"), encoding="utf-8") as fh:
            driver = fh.read()
        match = re.search(r"^CONTINUE = \((\d+), (\d+)\)$", driver, re.M)
        self.assertIsNotNone(match, "drive.py no longer defines CONTINUE")
        wizard = (int(match.group(1)), int(match.group(2)))
        self.assertNotEqual(install.INSTALLER_CONTINUE, wizard)
        self.assertGreater(abs(install.INSTALLER_CONTINUE[0] - wizard[0]), 200)


class InsideTheScreen(unittest.TestCase):
    """A coordinate has to be on the screen the guest actually has."""

    def positions(self):
        """Every `NAME = (x, y)` constant in install.py."""
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()
        found = {}
        for name, x, y in re.findall(r"^([A-Z_]+) = \((\d+), (\d+)\)$", source, re.M):
            found[name] = (int(x), int(y))
        return found

    def test_every_position_is_on_screen(self):
        for name, (x, y) in self.positions().items():
            self.assertLess(x, opencore.GUEST_WIDTH, f"{name} x={x}")
            self.assertGreaterEqual(x, 0, name)
            self.assertLess(y, opencore.GUEST_HEIGHT, f"{name} y={y}")
            self.assertGreaterEqual(y, 0, name)

    def test_the_rows_are_listed(self):
        # A tuple of y values, not a single position - checked separately so a
        # reordering of the menu cannot pass silently.
        found = self.positions()
        self.assertNotIn("ROW_Y", found)
        self.assertTrue(hasattr(install, "ROW_Y"))
        for y in install.ROW_Y:
            self.assertLess(y, opencore.GUEST_HEIGHT)

    def test_the_rows_are_in_descending_screen_order(self):
        # If they are not ordered, "row 2 is Reinstall" stops being true and every step
        # in the sequence is one entry off.
        rows = install.ROW_Y
        self.assertEqual(list(rows), sorted(rows))
        self.assertEqual(len(set(rows)), len(rows))

    def test_the_sidebar_rows_fit(self):
        for y in install.SIDEBAR_ROWS_Y:
            self.assertLess(y, opencore.GUEST_HEIGHT)

    def test_at_least_one_position_was_actually_measured(self):
        """A guard against the file being rewritten from prose again.

        The comment blocks carry the frame coordinates; if none of them is left, the
        numbers above are no longer traceable to anything.
        """
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()
        measured = re.findall(r"y=(\d+)", source)
        self.assertGreater(len(measured), 8,
                           "no frame measurements left in install.py")


if __name__ == "__main__":
    unittest.main()
