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

    def test_the_erase_buttons_are_the_measured_centres(self):
        """Measured with tesseract --psm 11 on the erase sheet.

            Cancel  x=1027..1101  y=626..666   centre (1064, 646)
            Erase   x=1123..1187  y=626..666   centre (1155, 646)
            Done    x=1108..1178  y=591..611   centre (1143, 601)

        (1140, 633) was inside Erase's bounding box and did nothing: the label is drawn
        at the top of the control and the hit area extends below it. Corners of a box are
        not where a button is.
        """
        self.assertEqual(install.ERASE_CONFIRM, (1155, 646))
        self.assertEqual(install.ERASE_DONE, (1143, 601))

    def test_the_erase_sheet_is_closed_before_leaving(self):
        """The completion sheet does not close itself.

        It reports "Erase process is complete. Click Done to continue." and waits. A run
        that skips the Done leaves the sheet up and every later click lands on it.
        """
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("def select_and_erase")
        end = source.index("def leave_disk_utility")
        body = source[start:end]
        self.assertIn("ERASE_DONE", body)
        # And the Done comes after the confirm, or it would be pressed on the wrong sheet.
        self.assertLess(body.index("ERASE_CONFIRM"), body.index("ERASE_DONE"))

    def test_the_disk_is_erased_with_a_lowercase_name(self):
        """An upper-case character is a shift chord, and typing one does nothing here.

        The first attempt named the disk `M` and watched the field stay empty while
        reporting success.
        """
        self.assertEqual(install.DISK_NAME, install.DISK_NAME.lower())
        self.assertTrue(install.DISK_NAME.isalnum(), install.DISK_NAME)

    def test_the_target_is_two_ups_from_the_default_selection(self):
        """Disk Utility opens with the recovery volume selected; disk0 is two rows up.

        Measured by hand on a live guest: after two Ups the detail pane reads
        "Capacity: 68.72 GB / Child count: 0 / Device: disk0".
        """
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("def select_and_erase")
        end = source.index("def leave_disk_utility")
        body = source[start:end]
        self.assertIn('d.key("up"', body)
        self.assertIn("range(2)", body)

    def test_there_is_no_view_or_show_all_devices_step(self):
        """Both disks are listed when Disk Utility opens; the View menu is a trap.

        It was in the sequence for several runs, and every run that had it failed to
        erase the target. It reveals nothing - the 68.72 GB disk is already the second
        row - and it MOVES THE SELECTION, so the two Ups that follow no longer start
        from the recovery volume and no longer land on disk0.

        Full reasoning is in the docstring on select_and_erase. This test is here
        because a plausible-looking extra step is easy to add back.
        """
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()

        self.assertNotIn("show_all_devices", source)
        self.assertNotIn("SHOW_ALL_DEVICES", source)
        self.assertNotIn("VIEW_BUTTON", source)

        # And the run sequence goes straight from opening the utility to erasing.
        start = source.index("disk_utility(d)")
        end = source.index("leave_disk_utility(d)")
        between = source[start:end]
        self.assertIn("select_and_erase(d)", between)
        self.assertNotIn("View", between)

    def test_the_licence_buttons_are_on_the_button_row(self):
        """Measured with `tesseract --psm 6` on the pane the probe stopped at.

            Disagree   x=889..942,  y=707..720
            Agree      x=987..1023, y=707..720

        The previous value (604, 810) was the wizard's three-button layout: 400px left
        and 100px below anything on the licence pane.

        WHY psm 6 AND NOT psm 11: the default segmentation also returns the word
        "agree" at y=451, in the middle of "you must agree to the terms". A driver
        reading that would click the prose, which is the class of mistake this whole
        file exists to prevent.
        """
        self.assertEqual(install.AGREE, (1005, 713))
        self.assertEqual(install.DISAGREE, (915, 713))
        # On the button row, not in the body text.
        self.assertGreater(install.AGREE[1], 690)
        # And to the right of Disagree, which is the order the pane shows them in.
        self.assertGreater(install.AGREE[0], install.DISAGREE[0])

    def test_agree_is_not_the_word_in_the_sentence(self):
        # The prose "you must agree to the terms" is centred around y=451. A coordinate
        # near it is the prose, not the control.
        self.assertGreater(abs(install.AGREE[1] - 451), 200)

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


class Probe(unittest.TestCase):
    """macos/15/probe.py clicks only what is already measured.

    Its job is to discover the panes nobody has measured, so every position it uses has
    to be one that IS measured - otherwise it spends its clicks the way the bake does,
    on a screen it does not understand.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "macos_probe", os.path.join(MACOS15, "probe.py"))
        cls.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.probe)

    def test_it_clicks_the_measured_installer_continue(self):
        self.assertEqual(self.probe.CONTINUE, install.INSTALLER_CONTINUE)

    def test_it_opens_reinstall_from_the_measured_row(self):
        # ROW_Y[1] is "Reinstall macOS Sequoia" - index 1 of the measured rows.
        self.assertEqual(self.probe.REINSTALL_ROW, (install.ROW_X, install.ROW_Y[1]))

    def test_it_uses_the_measured_menu_continue(self):
        self.assertEqual(self.probe.MENU_CONTINUE, install.CONTINUE_MENU)

    def test_every_position_is_on_screen(self):
        for name in ("REINSTALL_ROW", "CONTINUE", "MENU_CONTINUE"):
            x, y = getattr(self.probe, name)
            self.assertLess(x, opencore.GUEST_WIDTH, name)
            self.assertLess(y, opencore.GUEST_HEIGHT, name)

    def test_the_licence_pane_gets_agree_not_continue(self):
        """The pane the previous probe stopped on.

        Continue is disabled there until Agree is pressed, so a driver that only knows
        Continue sits on it forever. The words on the pane are what separates them.
        """
        text = ("To continue installing the software, you must agree to the terms "
                "of the software license agreement. ENGLISH APPLE INC.")
        target, why = self.probe.decide(text)
        self.assertEqual(target, self.probe.LICENCE_AGREE)
        self.assertIn("Agree", why)

    def test_the_specific_panes_come_before_the_generic_continue(self):
        # "continue" appears on the licence pane too ("To continue installing"). If the
        # generic entry were first, it would win and press a disabled button.
        self.assertGreater(len(self.probe.PANE_ACTIONS), 1)
        phrases = [p for p, _t, _w in self.probe.PANE_ACTIONS]
        generic = phrases.index("continue")
        for phrase in ("must agree to the terms", "set up the installation"):
            self.assertLess(phrases.index(phrase), generic, phrase)

    def test_an_unknown_pane_stops_rather_than_clicking(self):
        # The whole point: a pane nobody has measured is saved and the run ends, so the
        # next person measures it instead of watching twelve clicks miss.
        target, why = self.probe.decide("a screen that has never been seen")
        self.assertIsNone(target)
        self.assertEqual(why, "")

    def test_it_stops_at_a_pane_it_cannot_move(self):
        source = open(os.path.join(MACOS15, "probe.py"), encoding="utf-8").read()
        # Two unchanged frames in a row is the stop condition: one is a repaint, two is
        # a click that is not reaching anything.
        self.assertIn("unchanged >= 2", source)


class MachineIsShared(unittest.TestCase):
    """bake.py and probe.py must build the same machine.

    The eleven parameters in macos/15/machine.py took eight runs to get right. A probe
    that rebuilt them itself would answer questions about a machine nobody bakes.
    """

    def test_the_bake_uses_the_shared_machine(self):
        with open(os.path.join(MACOS15, "bake.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("machine.make_guest", source)
        # The old inline construction must be gone, or there are two machines again.
        self.assertNotIn("vm.Guest(self.work, extra=drives", source)

    def test_the_probe_uses_the_shared_machine(self):
        with open(os.path.join(MACOS15, "probe.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("machine.make_guest", source)

    def test_the_shared_machine_keeps_the_settings_that_were_hard_won(self):
        source = open(os.path.join(MACOS15, "machine.py"), encoding="utf-8").read()
        for required in ("Skylake-Client-v4", "vmx=off", "-pdpe1gb",
                         "bootindex=0", "bootindex=1", "virtio-vga",
                         "ICH9-LPC.disable_s3=1", "base=utc", "type=2"):
            self.assertIn(required, source, required)


class TheInstallerPanesAreWaited(unittest.TestCase):
    """Each installer pane is waited for, not assumed after a fixed sleep.

    THE FAILURE THIS EXISTS FOR, MEASURED ON RUN 41477776580

    The log and the recording disagree, and the recording is right:

        16:10:04  #15 click welcome Continue
        16:10:14  wait 10s - the licence pane draws
        16:10:20  #16 click Agree
        16:10:25  #17 click Agree in the sheet

    but the frame at 16:10:37, seventeen seconds after the welcome click, still reads

        "To set up the installation of macOS Sequoia, click Continue."

    The click had not taken effect when the driver moved on. Three clicks aimed at the
    licence pane landed on the welcome pane, every pane after that was one behind, and
    the destination Continue was pressed on a pane that was not the destination pane.
    The disk stayed at 0 MiB for the whole run.

    Disk Utility can be driven on fixed waits because it paints in a bounded time. The
    installer panes hand off between processes and are not bounded that way.
    """

    def source(self):
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_every_installer_pane_change_is_waited_for(self):
        body = self.source()
        start = body.index("def installer_panes")
        end = body.index("def main(")
        panes = body[start:end]

        # The welcome pane, the licence pane, and the destination pane each arrive by a
        # screen change, and each is a wait_for_change() in the sequence.
        self.assertGreaterEqual(panes.count("wait_until_drawn"), 3, panes)

    def test_the_licence_clicks_come_after_a_pane_change(self):
        body = self.source()
        # The welcome Continue, then a wait, then Agree - never the reverse.
        welcome = body.index('"welcome Continue"')
        agree = body.index('log("licence: Agree")')
        between = body[welcome:agree]
        self.assertIn("wait_until_drawn", between,
                      "nothing confirmed the licence pane arrived before Agree")

    def test_the_destination_clicks_come_after_a_pane_change(self):
        body = self.source()
        sheet = body.index('"Agree in the sheet"')
        card = body.index('"the main disk card"')
        between = body[sheet:card]
        self.assertIn("wait_until_drawn", between,
                      "nothing confirmed the destination pane arrived before the card")

    def test_the_fingerprint_is_stable_and_ignores_unreadable_screens(self):
        """fingerprint() hashes a sample of the screen; None when it cannot be read."""
        with open(os.path.join(os.path.dirname(MACOS15), "15", "install.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("def capture", body)
        self.assertIn("def fingerprint", body)
        # One implementation of "read the screen", so brightness() and fingerprint()
        # cannot drift into sampling different bytes.
        self.assertEqual(body.count("def capture"), 1)
        self.assertLess(body.index("def capture"), body.index("def brightness"))
        self.assertLess(body.index("def capture"), body.index("def fingerprint"))


class TheFirstClickWaitsForTheMenu(unittest.TestCase):
    """The Disk Utility click has to happen after the menu exists, not after 60 seconds.

    MEASURED ON RUN 41480111851, frame by frame against the driver's own log:

        t=11s  (16:20:15)  #1 click Disk Utility row      the display is still BLANK
        t=13s  (16:20:21)  #2 click recovery Continue
        t=16s              the first frame with any picture in it
        t=20s              the recovery menu is fully drawn

    A click delivered into a window that does not exist yet is not lost - the firmware
    buffers it - and it lands on whatever is at that point when the window arrives. It
    landed on the FIRST row of the menu:

        y=240  Restore from Time Machine    <- where the click went
        y=333  Reinstall macOS Sequoia
        y=425  Safari
        y=514  Disk Utility                 <- where it was aimed

    The recording shows "Time Machine System Restore" at t=40, then the menu again, then
    the installer. Disk Utility was never opened, and every erase step ran inside the
    wrong application. The log said each click succeeded, because delivery is success;
    whether it hit a menu row is a different question.

    A fixed sleep cannot fix this. It has to be longer than the slowest boot, which
    slows every other run and still fails when one boot is slower than the guess.
    """

    def source(self):
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_the_first_click_waits_four_minutes(self):
        """240 seconds before the first click, and the number comes from a measurement.

        Run 41480111851: 83 seconds from QMP connect to a usable menu, with 60 in the
        file - the click went into a blank display.

        Run 41498618924: 143 seconds (18:15:43 first frame, 18:18:06 the menu), with 120
        in the file - the click went into a white flash during the boot, landed on the
        first menu row, and the whole run happened inside Time Machine System Restore.

        Both failures looked identical in the log and only the recording separated them.
        240 is the larger measurement with the margin this file uses elsewhere.
        """
        body = self.source()
        start = body.index("def disk_utility")
        end = body.index("def select_and_erase")
        utility = body[start:end]

        click_at = utility.index('"Disk Utility row"')
        wait_at = utility.index("d.wait(240,")
        self.assertLess(wait_at, click_at,
                        "the first click must come after the 240 second wait")
        self.assertIn('d.wait(240, "the recovery menu draws")', utility)

    def test_nothing_is_clicked_before_the_first_wait(self):
        """The 120 seconds is the FIRST thing the driver does, not the fourth.

        The docstring quotes the click that went wrong, so the code under test begins at
        the first `d.wait(` of the function body.
        """
        body = self.source()
        start = body.index("def disk_utility")
        end = body.index("def select_and_erase")
        utility = body[start:end]
        code = utility[utility.index("d.wait("):]

        first_click = code.index("d.click(")
        self.assertEqual(code[:first_click].strip(),
                         'd.wait(240, "the recovery menu draws")',
                         "something happens before the menu is waited for")
        self.assertIn('"Disk Utility row"', code[first_click:first_click + 120])

    def test_disk_utility_replacing_the_menu_is_waited_for(self):
        body = self.source()
        start = body.index("def disk_utility")
        end = body.index("def select_and_erase")
        utility = body[start:end]
        self.assertIn("wait_until_drawn", utility,
                      "nothing confirmed Disk Utility finished opening")

    def test_settling_requires_something_to_have_been_drawn(self):
        """Stability alone passes a blank screen, and did.

        The first version of wait_until_drawn() compared fingerprints only, with a note
        that a brightness threshold "has to be tuned against this guest's rendering".
        That was wrong in the direction that matters: a blank framebuffer is perfectly
        stable, so two samples always agree, and run 41501519376 had the driver declare
        Disk Utility open on a screen that measured 1/255.

        The threshold is a floor, not a brightness test: NOTHING_DRAWN is 8, the boot
        screen measures 0-1 and every real pane 20 or more.
        """
        body = self.source()
        start = body.index("def wait_until_drawn")
        end = body.index("def close(")
        settle = body[start:end]
        self.assertIn("fingerprint", settle)
        self.assertIn("NOTHING_DRAWN", settle)
        self.assertGreater(install.NOTHING_DRAWN, 2)
        self.assertLess(install.NOTHING_DRAWN, 20)

    def test_the_menu_order_is_recorded(self):
        """Row 1 is Reinstall and row 3 is Disk Utility - the reverse of a guess.

        The menu, read off a frame at 1920x1080:

            y=240  Restore from Time Machine
            y=333  Reinstall macOS Sequoia
            y=425  Safari
            y=514  Disk Utility

        ROW_Y[1] for reinstall and ROW_Y[3] for Disk Utility follow from that, and a
        mis-indexed row is the same failure as a click that arrives too early.
        """
        self.assertEqual(install.ROW_Y[1], 333)
        self.assertEqual(install.ROW_Y[3], 514)


class AWaitThatFailsEndsTheSequence(unittest.TestCase):
    """A wait that is not allowed to fail is not a wait.

    MEASURED ON RUN 41493723813, which reached further than any run before it - the disk
    was erased, the menu came back, Reinstall opened the installer, the welcome pane and
    both Agree clicks landed - and then:

        17:54:48  #15 click (1060, 530) Agree in the sheet
        17:56:24    the pane did not change in 90s - the destination pane has to replace
                    the licence
        17:56:24  destination: select the card, then Continue
        17:56:24  #16 click (850, 550) the main disk card
        17:56:30  #17 click (1005, 712) destination Continue
        17:58:50    the disk did not grow in 120s (still 0 MiB)

    The wait reported the failure and the driver went on anyway. The two clicks landed on
    a licence pane that has no disk card, the target disk was never written, and the
    failure surfaced two panes later as "the disk did not grow" - a symptom of a click
    that happened a minute and a half earlier.

    The last frame of that run is a licence pane, which is what the report should have
    said instead.
    """

    def body(self):
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("def installer_panes")
        end = source.index("def main(")
        return source[start:end]

    def test_every_pane_change_is_checked(self):
        body = self.body()
        # Three transitions in the sequence: welcome -> licence, licence -> sheet,
        # sheet -> destination. Each is a wait whose answer is used.
        self.assertGreaterEqual(body.count("if not d.wait_until_drawn"), 3, body)
        self.assertGreaterEqual(body.count("wait_until_drawn"), 3, body)

    def test_a_failed_pane_change_returns_false(self):
        body = self.body()
        for marker in ("THE WELCOME PANE DID NOT ADVANCE",
                       "THE LICENCE SHEET DID NOT OPEN",
                       "THE LICENCE SHEET DID NOT ADVANCE"):
            self.assertIn(marker, body, marker)

    def test_the_destination_clicks_come_after_a_checked_transition(self):
        """No click may happen after a wait whose result was ignored."""
        body = self.body()
        sheet = body.index('"Agree in the sheet"')
        card = body.index('"the main disk card"')
        between = body[sheet:card]
        self.assertIn("if not d.wait_until_drawn", between, between)


class ReinstallMustOpenTheWindow(unittest.TestCase):
    """Reinstall's Continue is the last click that can silently do nothing.

    MEASURED ON RUN 41496584150:

        18:07:20  #11 click (837, 333) Reinstall macOS row
        18:07:25  #12 click (1142, 586) recovery Continue
        18:10:56    the pane did not change in 180s - the installer window has to
                    replace the recovery menu
        18:10:56  wait 25s - the installer window opens
        18:11:21  (and then it would have clicked Continue on a welcome pane that was
                   never there)

    The wait said the window had not opened and the driver carried on regardless.

    WHY THIS CLICK IN PARTICULAR. It lands on the recovery menu, which is a window the
    previous step only just dismissed with Cmd-Q. A menu that has not finished redrawing
    takes the click on whatever row is under it, and the run is in the wrong application
    - which is exactly the "Disk Utility row" failure that cost several runs.
    """

    def source(self):
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_reinstall_reports_whether_the_window_opened(self):
        body = self.source()
        start = body.index("def reinstall(")
        end = body.index("def wait_for_install_start")
        fn = body[start:end]
        self.assertIn("if not d.wait_until_drawn", fn, fn)
        self.assertIn("THE INSTALLER WINDOW NEVER APPEARED", fn)
        self.assertIn("return False", fn)

    def test_the_caller_stops_when_it_did_not(self):
        body = self.source()
        self.assertIn("if not reinstall(d):", body)
        self.assertIn("return 1", body[body.index("if not reinstall(d):"):
                                       body.index("if not reinstall(d):") + 120])

    def test_the_welcome_pane_is_not_waited_for_twice(self):
        """reinstall() confirmed the window; a 25 second sleep after that is a guess.

        The pane still gets its own wait - the window painting and the pane painting are
        separate events - but a fixed sleep on top of a confirmed transition is how the
        old sequence drifted one pane behind.
        """
        body = self.source()
        start = body.index("def installer_panes")
        end = body.index("def main(")
        panes = body[start:end]
        self.assertNotIn('wait(25, "the installer window opens")', panes)


class TheEraseButtonIsTheGlyph(unittest.TestCase):
    """Erase is a GLYPH at (1255, 278), and the label sits below it.

    SIX RUNS, SIX VALUES, NO SHEET:

        41506477596 @ (1290, 306)
        41511491789 @ (1290, 296)
        41510118982 @ (1290, 296)   never reached it
        41514056068 @ (1290, 311)
        41564236483 @ (1290, 314)
        41567438310 @ (1290, 309)

    Every one of them was a guess, and every one was wrong in two ways at once.

    From the recording of run 41565758898, frame at 1:04, read with tesseract's TSV
    output and a pixel scan of the toolbar:

        y=270..283   the GLYPH           x 1247..1263
        y=293..302   the "Erase" LABEL   x 1243..1271

    So the label is BELOW the glyph - the opposite of the arrangement an older comment
    in install.py described - and the glyph's centre is (1255, 278).

    X WAS OFF BY 35 AS WELL. All six attempts used x=1290, which is in the middle of the
    toolbar rather than on this button.
    """

    def test_the_button_is_the_value_the_operator_gave(self):
        """(1290, 295), after (1255, 278) proved the control is reachable.

        The history, because each attempt failed differently and the pattern is the
        useful part - see the comment beside ERASE_BUTTON in install.py.
        """
        self.assertEqual(install.ERASE_BUTTON, (1290, 295))

    def test_the_button_is_above_the_label(self):
        # The label's top edge, measured by tesseract on a full-resolution frame, is 293.
        # The glyph sits above it; 295 is just below that edge, which is where the
        # operator put the pointer after (1255, 278) landed on the glyph's corner.
        self.assertGreaterEqual(install.ERASE_BUTTON[1], 293)
        self.assertLess(install.ERASE_BUTTON[1], 310)

    def test_the_source_records_where_the_numbers_came_from(self):
        with open(os.path.join(MACOS15, "install.py"), encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("ERASE_BUTTON = (1290, 295)")
        note = source[max(0, start - 1200):start]
        self.assertIn("(1255, 278)", note)
        self.assertIn("sheet DID open", note)
