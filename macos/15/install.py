"""Drive macOS Recovery to a finished installation, by coordinates only.

    python3 macos/15/install.py --qmp run/drive.sock.qmp --frames out/frames

WHAT THIS REPLACES, AND WHY

dockur-macos installs unattended by patching rc.cdrom.sh inside the recovery image: the
patched script mounts a 9p share and runs startosinstall with no interaction. That is
elegant and it does not work on 15. Its own patch.py records the failure:

    Expected exactly one rc.cdrom.sh bootstrap, found 0

The needle is macOS 14's text. Upstream ships the same needle, so the automated branch
has never been exercised on 15 or 26 - which is why upstream's own drive-installer.py
exists and why this follows it.

THE SEQUENCE, WHICH IS SHORT BECAUSE RECOVERY IS A FEW PANES

    recovery menu     Disk Utility, then Continue
    Disk Utility      View -> Show All Devices
                      select the unformatted disk (the only row with no capacity)
                      Erase -> APFS / GUID -> Erase -> confirm
                      Cmd-Q back to the menu
    recovery menu     Reinstall macOS, then Continue
    installer panes   Agree, then Continue until Setup Assistant appears

WHY THE ERASE IS THE DANGEROUS PART

A freshly created disk has no partition table, so Disk Utility's sidebar does not list
a volume for it - the target simply is not there. That is why Show All Devices comes
first: the switch puts physical disks in the list, and the list then reads

    Internal
      Apple Inc. VirtIO Block Media   Uninitialized  Capacity: Unknown   <- target
      Apple Inc. VirtIO Block Media   268.4 MB                          <- boot.img
      disk1s1                         2.88 GB                           <- recovery

The target does not report "64 GB". With no partition table there is nothing to total,
and Disk Utility says Unknown - so the test for it is the ABSENCE of a capacity and a
child count, not a size. Clicking row 3 instead erases the read-only volume the guest
is booting from, and the run dies at the pane it is currently on.

WHY FIXED POSITIONS AND NOT READING

Same argument as macos/15/drive.py, and the same source: every coordinate here was
measured from a captured frame of a real guest by upstream's drive-installer.py, and
carries that measurement in a comment. Frames are recorded, never read.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

# lib/assistant.py DECIDES WHEN THE INSTALL IS OVER, so it is imported here rather than in
# the caller. The wizard on screen is the end of the install - see the note in
# installer_panes() - and that answer has to be available while the disk is still being
# written.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))

import assistant  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))

import qmp as qmpmod  # noqa: E402


def log(msg: str) -> None:
    print(f"[macos-install {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- measured positions, all at 1920x1080 -------------------------------------------

# The recovery list, MEASURED with tesseract on a captured frame of this bake. Every
# entry's label is left-aligned at x=837 inside the window; each entry is a title line
# with an 18px-smaller description below it, so the clickable centre sits between them:
#
#   © Restore from Time Machine   title y=229  desc y=250   centre 240
#   Reinstall macOS Sequoia       title y=324  desc y=342   centre 333
#   Safari                       title y=416  desc y=434   centre 425
#   = Disk Utility               title y=502  desc y=526   centre 514
#   Continue                     y=585, x=1115..1169
#
# The previous values (232, 324, 416, 508) were prose rather than measurement; they are
# within 8px and would have worked, but the numbers here are the ones on the frame.
ROW_Y = (240, 333, 425, 514)
ROW_X = 837

# The recovery menu's own Continue. An entry is opened by selecting it and then pressing
# this; clicking the entry alone only highlights it. Measured: x=1115..1169, y=578..595,
# so the centre is (1142, 586).
CONTINUE_MENU = (1142, 586)

# SET BY THE OPERATOR, AND THE HISTORY IS THE USEFUL PART.
#
#     (1290, 306)  nothing
#     (1290, 296)  nothing
#     (1290, 311)  nothing
#     (1290, 314)  nothing
#     (1290, 309)  nothing
#     (1255, 278)  the sheet OPENED - backspace, "main" and Done all ran
#     (1290, 295)  the sheet OPENED again, and the whole 17-step sequence ran through
#                  to the destination pane
#
# The last two both worked, which is what makes this a refinement rather than a fix:
# the control's hit area is a few pixels wide in y, and 295 and 305 are both inside it.
#
# 305 -> 300 BY INSTRUCTION, five pixels up. 305 is the value that was in use when runs
# 41626151499 and 41632393923 clicked Erase and the sheet did not appear; 295 is the value
# that opened it. 300 is the midpoint of the two measurements that have worked, and it is
# a deliberate move toward the smaller of them rather than a new guess - the control's
# vertical extent is narrow, and the failures at 306, 309, 311 and 314 are all above it.
#
# A SWEEP, NOT A GUESS: y = 280..320 EVERY 2 PIXELS, TWO SECONDS APART.
#
# Every single-value attempt at this control has failed or succeeded without a pattern
# worth trusting. The history above records 296, 305, 306, 309, 311 and 314 producing
# nothing, and 278 and 295 opening the sheet; runs then repeated at 295, 300 and a 295/305
# pair with mixed results. The button's vertical hit area is a few pixels tall and it moves
# by a pixel or two as the toolbar is redrawn, so a fixed number is a coin toss per run.
#
# Sweeping the whole plausible band removes the coin toss. Twenty-one clicks at two-second
# intervals is forty seconds; the first one that lands raises the sheet, and every click
# after that lands on the sheet's own backdrop and does nothing.
#
# THE BAND IS CENTRED ON WHAT HAS BEEN OBSERVED. 278 opened the sheet and 314 did not, so
# the control lives somewhere inside 280..320; two pixels is finer than the hit area it is
# being swept for. X stays at 1290, which is inside the button horizontally in every frame
# that has been measured (its extent is x=1255..1310).
ERASE_BUTTON_Y_FIRST = 280
ERASE_BUTTON_Y_LAST = 320
ERASE_BUTTON_Y_STEP = 2
ERASE_BUTTON_X = 1290

# Sidebar rows under "Internal", 28px apart, x=540 inside the labels. Five rows because
# the count differs between Show Only Volumes (one) and Show All Devices (four), and a
# fixed triple can miss the disk.
SIDEBAR_ROWS_Y = (351, 379, 407, 435, 463)
SIDEBAR_CLICK_X = 540

# The erase sheet's buttons, measured on a frame of the sheet with tesseract --psm 11:
#   Cancel x=1027..1101  y=626..666   centre (1064, 646)
#   Erase  x=1123..1187  y=626..666   centre (1155, 646)
# The toolbar's Erase is at y=332 as well, so the y is what tells them apart.
#
# THE CENTRE, NOT A CORNER. (1140, 633) is inside the button's bounding box and did
# nothing when clicked: the label is drawn at the top of the control and the control's
# hit area extends below it. Measured centres are what get used everywhere in this file.
ERASE_CONFIRM = (1155, 646)

# After the erase finishes the sheet stays up with a progress report and one button:
#
#     Erasing "Apple Inc. VirtIO Block Media" and creating "main"
#     Erase process is complete. Click Done to continue.
#                                          [ Done ]  x=1108..1178 y=591..611
#
# WITHOUT THIS THE SHEET NEVER CLOSES and every later click lands on it. The text of the
# report contains "Donffto" - tesseract's reading of "Done to" in the sentence - so the
# button is found by its position on the button row, not by its word.
ERASE_DONE = (1143, 601)

# The volume name the erased disk is given. Lower case: an upper-case M is a shift chord,
# and the first attempt typed one and watched the field stay empty.
DISK_NAME = "main"

# HOW LONG THE INSTALLER MAY TAKE TO WRITE ITS FIRST BYTE after Continue is accepted.
#
# The progress pane appears within seconds and the copy starts immediately, so a disk that
# has not grown in two minutes is a pane that did not accept the click - not a slow start.
# This is the check the run before last did not make: it claimed to be watching the disk
# while the screen still showed the disk picker with nothing in it.
START_EVIDENCE_SECONDS = 120

# THE LICENCE BUTTONS, MEASURED ON A FRAME OF THIS BAKE.
#
# macos/15/probe.py walked the installer and stopped here, on the third pane:
#
#     To continue installing the software, you must agree to the terms of the
#     software license agreement.
#     ... ENGLISH | APPLE INC. | SOFTWARE LICENSE AGREEMENT FOR macOS Sequoia ...
#
# with these controls along the bottom, read with `tesseract --psm 6` (psm 11 returns
# "ree," for the Agree button, which is how a plausible-looking position gets missed):
#
#     Disagree   x=889..942,  y=707..720   centre (915, 713)
#     Agree      x=987..1023, y=707..720   centre (1005, 713)
#
# The previous value was (604, 810): the wizard's three-button layout, 400px left and
# 100px below anything on this pane.
#
# NOTE ON WHAT OCR FINDS FIRST. `--psm 11` also returns the word "agree" at y=451, which
# is the middle of a sentence: "you must agree to the terms". A driver that clicked the
# word would click the prose. The buttons are at the bottom, and only psm 6 puts them
# there.
AGREE = (1005, 713)

# Disagree, kept because a pane that offers both is telling you where the boundary is.
DISAGREE = (915, 713)

# THE FLOATING SHEET THAT FOLLOWS THE MAIN AGREE, measured on a live guest.
#
# Pressing Agree on the licence pane raises a second, smaller dialog:
#
#     To continue installing the software, you must agree to the terms of the
#     software license agreement.
#     I have read and agree to the terms of the software license agreement.
#
# Its buttons are unreadable to tesseract at any page-segmentation mode - grey on grey -
# so the position was found by having somebody read a coordinate grid drawn over the
# frame. (1060, 530) is in its lower right, and clicking it is what advances the installer.
#
# THE EARLIER VALUE WAS A GUESS. (1026, 634) was reasoned from the first sheet's layout
# and never worked; four runs clicked it while the installer sat still.
AGREE_SHEET = (1060, 530)

# THE INSTALLER'S CONTINUE IS NOT THE WIZARD'S CONTINUE, AND THAT COST A RUN.
#
# It was (1306, 810), copied from macos/15/drive.py - whose positions come from dockur's
# Setup Assistant driver, a different set of windows. Measured on a frame of the
# installer's own welcome pane:
#
#     macOS Sequoia                                   y=569..595, x=865..1056
#     To set up the installation of macOS Sequoia,
#     click Continue.                                 y=622
#     [ Continue ]                                    y=698..727, x=919..1002
#
# so the button is at (960, 712). Thirty-seven clicks at (1306, 810) landed 346px right
# and 98px below it, on a pane that had one control, and the screen never changed once.
INSTALLER_CONTINUE = (960, 712)

# THE DESTINATION PANE, measured on a live guest. It lists the erased disk as a card:
#
#     Select the disk where you want to install macOS.
#        main
#        68.51 GB total   68.30 GB available
#                                              [Back]  [Continue]
#
# THE CARD HAS TO BE SELECTED FIRST - Continue is grey until it is, and a click on a grey
# Continue does nothing and says nothing. (850, 550) is inside the card; (865, 622) is on
# its label, which is also inside it but was the first thing tried and worked only once
# the card's own area was used.
#
# After the card is selected the pane re-reads as
#     macOS Sequoia will be installed on the disk "main"
# and Continue becomes usable at (1005, 712).
DISK_CARD = (850, 550)
PANE_CONTINUE = (1005, 712)

# WHAT "NOTHING IS ON SCREEN" MEANS, in mean grey 0-255.
#
# Measured on the recordings, not chosen: a boot screen with its logo averages 0-1, a
# blank framebuffer 0, and every real pane here - the recovery menu, Disk Utility, the
# licence, the installer's progress sheet - measures 20 or more. 8 is far from both, and
# it is a floor rather than a brightness test on purpose: the question is whether
# anything was drawn at all, never whether it is bright enough.
NOTHING_DRAWN = 8

DISK_SAMPLE_SECONDS = 30


# HOW LONG THE COPY MAY PAUSE BEFORE IT COUNTS AS FINISHED.
#
# dockur-macos records the real numbers in its verify-golden.yml: "storage settled at
# 27306 MiB after 3120 s". 52 minutes. The rule here is the same shape - wait for growth
# to stop - with a pause long enough that a slow write is not mistaken for the end.
#
# TEN MINUTES, NOT THREE, AND RUN 41579186251 IS WHY. With 180s the disk was declared
# finished twice at 33,088 and 31,644 MiB, and both times it started growing again inside
# the window: macOS writes in stages, and the gap between the base system and the next
# volume is longer than three minutes. The run happened to reach its real end afterwards,
# but it had already said "the installation is complete" about a disk that was not. A
# wrong answer that is sometimes right afterwards is the shape of failure this bake can
# least afford, because the next stage boots the disk.
SETTLE_SECONDS = 600

# A FINISHED macOS 15 OCCUPIES ABOUT 26 GiB. Growth that stops below this is an
# installation that failed, not one that finished, and the two look identical on a disk
# that simply stopped changing.
MIN_FINISHED_MIB = 20000

# THE BACKSTOP. A copy that never settles must not hold a runner for three hours.
MAX_INSTALL_SECONDS = 7200

# The forward control is pressed no faster than a pane can redraw: the installer takes
# several seconds per pane and a click arriving during a redraw is lost.
CLICK_INTERVAL = 12


class Recovery:
    """The few actions this needs, each recording a frame afterwards.

    Frames are RECORDED and also READ - but only for one thing: whether the screen has
    gone dark. That is not OCR and not a screen-scraper; it is the difference between
    "the installer is copying 12 GB with a static progress bar" and "the machine
    stopped", and nothing else in this file can tell those apart.

    The distinction matters because the first version had no dark check at all. The run
    showed 93 identical black frames over 7.75 minutes and the driver cheerfully went on
    clicking coordinates at a screen that was not there.
    """

    def __init__(self, qmp_path: str, frames_dir: str | None = None,
                 disk_path: str | None = None):
        self.q = qmpmod.QMP(qmp_path)
        self.frames = frames_dir
        # The target qcow2, when the caller knows it. An installer writes gigabytes into
        # it; a stopped guest writes nothing. That is the difference between "copying"
        # and "hung", and a black screen cannot show it. Without a path, this class can
        # only report the screen.
        self.disk_path = disk_path
        self.n = 0
        self.shots = 0

    def disk_allocated(self) -> int | None:
        """How many bytes the qcow2 has actually allocated, or None.

        st_blocks, not st_size: a qcow2 is sparse, so its apparent size is the virtual
        disk size from the moment it is created and never changes. The allocated blocks
        are what grow as the guest writes.
        """
        if not self.disk_path:
            return None
        try:
            info = os.stat(self.disk_path)
        except OSError:
            return None
        return info.st_blocks * 512

    def wait(self, seconds: float, why: str = "") -> None:
        log(f"wait {seconds:g}s" + (f" - {why}" if why else ""))
        time.sleep(seconds)

    def shot(self, label: str) -> None:
        if not self.frames:
            return
        self.shots += 1
        safe = "".join(c if c.isalnum() else "-" for c in label[:40]).strip("-") or "step"
        try:
            self.q.execute("screendump",
                           filename=os.path.join(self.frames, f"inst-{self.shots:03d}-{safe}.ppm"))
        except (OSError, RuntimeError) as exc:
            log(f"screendump failed at {label}: {exc}")
            return

        # ONE PICTURE, KEPT UP TO DATE, WHICH IS WHAT A CANCELLED RUN CAN STILL UPLOAD.
        #
        # The last frame was written once, after the recording was encoded - and a run
        # that is cancelled (or times out) never reaches the encoding, so the fallback
        # artifact had nothing in it. Run 41483303829 was cancelled while sitting on a
        # pane and produced 1086 bytes of artifact: a frames.txt listing files that were
        # never uploaded.
        #
        # So every screenshot also becomes out/last-frame.png, overwritten in place.
        # Cancel at any moment and the artifact holds the pane the driver was looking at.
        # It is 300 KiB, and it is written at most once per click.
        #
        # WRAPPED, BECAUSE A PICTURE MUST NEVER STOP A CLICK. An earlier version called
        # this without a guard and the module never imported subprocess, so the FIRST
        # click of run 41485902304 died here:
        #
        #     NameError: name 'subprocess' is not defined
        #     the installer driver failed: exit status 1
        #
        # The import is fixed, but the guard is the real lesson: this is the diagnostic
        # path, and it sits inside click(), which is the thing it is diagnosing.
        try:
            self._write_last_frame(os.path.join(self.frames, "last-frame.png"))
        except Exception as exc:  # noqa: BLE001 - see above; a picture is not worth a run
            log(f"could not refresh last-frame.png: {exc}")

    def _write_last_frame(self, dest: str) -> None:
        """Convert the newest screenshot to a PNG, replacing `dest`.

        Not fatal: this runs beside the driver's real work, and a missing picture must
        not stop a run that is otherwise fine.
        """
        newest = None
        try:
            names = [n for n in os.listdir(self.frames)
                     if n.startswith("inst-") and n.endswith(".ppm")]
            if not names:
                return
            newest = os.path.join(self.frames, max(names, key=lambda n: int(n[5:8])
                                                   if n[5:8].isdigit() else 0))
        except OSError:
            return
        try:
            import lastframe
            lastframe.convert(newest, dest)
        except (ImportError, OSError, subprocess.SubprocessError):
            pass

    def capture(self) -> bytes | None:
        """One screendump, returned as its raw pixel bytes. None if it cannot be read.

        A screendump goes to a file the emulator wrote, so this is one capture and one
        pass over its pixels - no OCR, no template, no interpretation of what is drawn.
        Both brightness() and fingerprint() are built on it.
        """
        if not self.frames:
            return None
        path = os.path.join(self.frames, "probe.ppm")
        try:
            self.q.execute("screendump", filename=path)
        except (OSError, RuntimeError):
            return None
        try:
            with open(path, "rb") as fh:
                if fh.readline().strip() != b"P6":
                    return None
                line = fh.readline()
                while line.startswith(b"#"):
                    line = fh.readline()
                width, height = (int(v) for v in line.split())
                fh.readline()
                return fh.read(width * height * 3) or None
        except (OSError, ValueError):
            return None

    def screenshot(self, name: str = "ocr") -> str | None:
        """A screendump saved as a PPM, for OCR to read. The path, or None.

        DIFFERENT FROM capture() ON PURPOSE. capture() reads pixels for brightness and
        fingerprints; this hands a file to tesseract, which needs a path. Both go through
        one screendump and nothing is interpreted here.
        """
        if not self.frames:
            return None
        path = os.path.join(self.frames, f"{name}.ppm")
        try:
            self.q.execute("screendump", filename=path)
        except (OSError, RuntimeError) as exc:
            log(f"screendump failed for {name}: {exc}")
            return None
        return path if os.path.exists(path) else None

    def erase_sheet_is_open(self) -> bool:
        """Is the erase confirmation sheet on screen?

        THE SHEET IS THE PROOF THAT THE CLICK LANDED, and its words are unique to it: it
        asks for a Name and shows Format and Scheme pickers. The licence pane, the
        destination pane and Disk Utility itself show none of those together.

        Reading it means a click that did nothing can be retried deliberately instead of
        being followed by a backspace and a typed name into a field that is not there.
        """
        path = self.screenshot("probe-sheet")
        if not path:
            return False
        text = assistant.ocr_text(path)
        if not text:
            return False
        # "scheme" and "format" are the two picker labels; either alone is enough, since
        # Disk Utility's main window has neither on this screen.
        return "scheme" in text or "guid partition map" in text

    def selected_disk_gb(self) -> float | None:
        """The capacity Disk Utility is showing for the selected disk, in GB, or None.

        READ FROM THE DETAIL PANE, WHICH IS THE ONLY PLACE THE ANSWER IS A NUMBER.

        The sidebar shows rows, not sizes, and the row that is highlighted is whatever the
        last Up landed on - which is a runtime state, not a property of the disk. Two runs
        with identical clicks selected different disks, and one of them erased the 268 MB
        boot image and then spent fifty minutes installing macOS into it:

            macOS Base System   2.88 GB total
            main                268.4 MB total      <- what main turned out to be

        The detail pane, by contrast, prints the selected device's real capacity, and
        `Child count: 0` plus `Uninitialized` say it is a bare disk. So this reads the pane
        and returns the number; the caller decides whether it is the right order of
        magnitude.
        """
        path = self.screenshot("probe-ocr")
        if not path:
            return None
        # THE PANE IS CROPPED AND ENLARGED BEFORE OCR, BECAUSE THE CHARACTERS ARE SMALL.
        #
        # Read whole, the screendump gives "Apple Inc. VirtIO Block Media" and loses the
        # capacity line entirely - the numbers are about ten pixels tall at 1920x1080. The
        # detail pane occupies a fixed rectangle in this window, and enlarging it three
        # times is what makes "Capacity: 268.4 MB" readable at all.
        #
        # MEASURED ON A 1920x1080 SCREENDump, NOT GUESSED. The crop was found by locating
        # the table's own separator lines in the pixels - they are the bright rows between
        # y=545 and y=670 - and then read back to confirm:
        #
        #     ocr_text_cropped(path, (690, 540, 1490, 680), scale=3)
        #     -> 'location: internal capacity: 268.4 mb connection: unknown child ...'
        #
        # The crop is generous on purpose. A missed capacity is a retry; a MISREAD one
        # would erase the wrong disk, so the parsing below refuses anything ambiguous.
        text = assistant.ocr_text_cropped(path, (690, 540, 1490, 680), scale=3)
        if not text:
            return None

        # MB AND GB BOTH, THEN COMPARE IN GB.
        #
        # The first version matched only "NNN.N GB" and the disk it was pointed at reports
        # "Capacity: 268.4 MB" - so the boot image, the one disk this must never select,
        # produced no reading at all and the loop would have kept stepping. The unit is
        # part of the number, not decoration.
        best: float | None = None
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(gb|mb)\b", text):
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if match.group(2) == "mb":
                value /= 1024.0
            if best is None or value > best:
                best = value
        return best

    def brightness(self) -> int | None:
        """The mean grey level of a fresh screendump, or None if it cannot be read."""
        pixels = self.capture()
        if not pixels:
            return None
        # Every 97th pixel: a fixed stride larger than any text run, cheap, and it gives
        # the same answer as reading all of them for a question about the whole screen.
        total = 0
        count = 0
        for index in range(0, len(pixels), 97 * 3):
            total += pixels[index] + pixels[index + 1] + pixels[index + 2]
            count += 3
        return (total // count) if count else None

    def fingerprint(self) -> int | None:
        """A coarse hash of the screen, for "did this pane change at all".

        SAMPLED, NOT COMPLETE. Every 97th pixel, which is far coarser than the text on
        these panes and far finer than "the whole screen scrolled". The question it
        answers is only ever "is this a different picture than the one before", and for
        that a sample is sufficient and cheap.

        ALL THREE CHANNELS. The first version hashed only the red byte of every 401st
        pixel, and that is 5,172 samples for a 1920x1080 frame - enough to see a pane
        change, and too coarse to see a small one. Two macOS backgrounds that differ
        only in a grey-blue tint came out identical, which is what made run 41498618924
        report "the pane did not change" over a welcome page that had replaced the
        recovery menu.
        """
        pixels = self.capture()
        if not pixels:
            return None
        # A stable mixer; Python's own hash is randomised per process.
        digest = 0
        for index in range(0, len(pixels), 97 * 3):
            digest = (digest * 131 + pixels[index]) & 0xFFFFFFFF
            digest = (digest * 131 + pixels[index + 1]) & 0xFFFFFFFF
            digest = (digest * 131 + pixels[index + 2]) & 0xFFFFFFFF
        return digest

    def wait_until_drawn(self, timeout: float = 180.0, settle: float = 5.0,
                         why: str = "") -> bool:
        """Wait until the screen stops changing, i.e. a pane has finished painting.

        NO LONGER CALLED BY THE SEQUENCE - see wait_for_change() below, which is what
        every pane transition uses now. It is kept because the question it asks is a
        real one and the answer is not the same question:

            wait_until_drawn   "has this pane finished drawing"    - for the FIRST paint,
                               when there is nothing to compare against
            wait_for_change    "has a different pane replaced it"  - for every transition,
                               because a click that did nothing leaves a still screen

        The sequence had eight transitions asking the first question, and that is what
        run 41615881004 died of: the licence sheet never closed, the screen was perfectly
        still, the wait returned True, and the next two clicks landed on the sheet.

        THIS IS THE ONE THE FIRST CLICK NEEDS. Run 41480111851 clicked the Disk Utility
        row at 16:20:15, while the display was still blank, and the menu did not appear
        until about 16:20:40. A click delivered into a window that does not exist yet is
        not lost - the firmware buffers it - and it lands on whatever is at that point
        once the window arrives. It landed on the FIRST row, "Restore from Time Machine",
        and the whole run proceeded through the wrong application.

        STABILITY ALONE IS NOT ENOUGH - IT PASSES A BLANK SCREEN.

        Run 41501519376 proved that. The recording from t=45 to t=59 has a mean grey of
        1: the screen was black. The driver said

            18:37:28  the screen settled after 10s - Disk Utility has to replace the
                      recovery menu
            18:37:59  the screen settled after 10s - Disk Utility has to finish drawing
                      its sidebar

        and then clicked Continue, Up, Erase and a disk name at a black screen. A blank
        frame does not change, so "two samples agree" is true of it from the first
        second, forever.

        SO NOTHING IS DRAWN MEANS NOTHING IS DRAWN. A frame whose mean grey is at the
        floor is not a finished pane, whatever the fingerprints say; the wait keeps
        polling until it either sees a lit frame settle or runs out.

        The threshold is deliberately low - 8 out of 255. It is not "is this bright", it
        is "is there anything here at all". A legitimately dark pane (the licence sheet
        is dark grey) measures in the twenties, and the boot screen with its logo
        measures 1.

        Returns False on timeout so the caller can say what it was looking at.
        """
        started = time.time()
        deadline = started + timeout
        last = self.fingerprint()
        if last is None:
            # Nothing can be seen. Say so by returning True: the caller's fixed sleeps
            # are all the pacing there is, and a failure here would be a guess.
            log("  the screen cannot be read; falling back to the caller's waits")
            return True

        stable = 0
        while time.time() < deadline:
            time.sleep(settle)
            now = self.fingerprint()
            if now is None:
                continue
            # A BLACK SCREEN IS NOT A FINISHED PANE. See the docstring: this single test
            # is what run 41501519376 needed and did not have.
            level = self.brightness()
            if level is not None and level < NOTHING_DRAWN:
                stable = 0
                last = now
                continue
            if now == last:
                stable += 1
                # Two matches: the last two samples agree, so nothing is moving.
                if stable >= 2:
                    log(f"  the screen settled after {time.time() - started:.0f}s"
                        + (f" (mean grey {level})" if level is not None else "")
                        + (f" - {why}" if why else ""))
                    return True
            else:
                stable = 0
            last = now

        log(f"  the screen never settled in {timeout:.0f}s"
            + (f" - {why}" if why else ""))
        return False

    def wait_for_change(self, timeout: float = 120.0, settle: float = 5.0,
                        why: str = "") -> bool:
        """Wait until the screen stops looking like it did when this was called.

        THIS IS THE OTHER HALF OF wait_until_drawn, AND THE MISSING HALF IS WHY RUN
        41615881004 FAILED.

        wait_until_drawn asks "has the picture stopped moving", which is the right
        question for a pane that is PAINTING. It is the wrong question for a pane that
        is supposed to REPLACE another one: a sheet that never closed is perfectly
        still, so that function returned True while the licence sheet was still up,
        and the two clicks after it landed on the sheet instead of on the disk card
        and Continue.

        The evidence is in the run's last frame - the sheet with its highlighted Agree,
        and the greyed-out Agree behind it - and in the log:

            10:11:45  #15 click (1060, 530) Agree in the sheet
            10:12:00  the screen settled after 10s - destination pane has to be on screen
            10:12:00  #16 click (850, 550) the main disk card
            10:12:00  #17 click (1005, 712) destination Continue
            10:14:27  the disk did not grow in 120s (still 157 MiB)

        WHAT IT DOES: takes a fingerprint now, and returns True only once the picture
        has moved away from it and then stopped again - "something new is here and it
        has finished drawing". A screen that never changes times out, which is the
        honest answer for a click that did nothing.

        A DARK FRAME IS STILL NOT AN ANSWER. The change has to settle into something
        drawn: a transition through black is not the arrival of a pane.
        """
        started = time.time()
        deadline = started + timeout
        before = self.fingerprint()
        if before is None:
            log("  the screen cannot be read; nothing can be compared")
            return False

        changed = False
        stable = 0
        last = before
        while time.time() < deadline:
            time.sleep(settle)
            now = self.fingerprint()
            if now is None:
                continue
            level = self.brightness()
            if level is not None and level < NOTHING_DRAWN:
                # Black: a transition, not a pane. Start over from whatever comes next.
                changed = False
                stable = 0
                last = now
                continue
            if now == before:
                # Unchanged from the start: the click did nothing yet.
                stable = 0
                last = now
                continue
            changed = True
            if now == last:
                stable += 1
                if stable >= 2:
                    log(f"  the screen changed and settled after "
                        f"{time.time() - started:.0f}s"
                        + (f" (mean grey {level})" if level is not None else "")
                        + (f" - {why}" if why else ""))
                    return True
            else:
                stable = 0
            last = now

        if not changed:
            log(f"  the screen never changed in {timeout:.0f}s"
                + (f" - {why}" if why else ""))
            log("  the click that was supposed to move it landed on nothing")
        else:
            log(f"  the screen kept changing and never settled in {timeout:.0f}s"
                + (f" - {why}" if why else ""))
        return False

    def click(self, xy: tuple[int, int], why: str = "", settle: float = 5.0) -> None:
        """Click, then wait before anything else happens.

        FIVE SECONDS BETWEEN EVERY ACTION. This guest draws a pane in about three
        seconds on an unaccelerated qemu, and a click that arrives during the repaint is
        swallowed. Run 41472163272 sent seventeen actions in eighty seconds onto a
        display that had not painted yet.
        """
        self.n += 1
        log(f"#{self.n} click {xy} {why}")
        qmpmod.click(self.q, *xy)
        time.sleep(settle)
        self.shot(f"after-{self.n:02d}-{why}")

    def key(self, name: str, settle: float = 5.0) -> None:
        self.n += 1
        log(f"#{self.n} key {name}")
        qmpmod.key(self.q, name)
        time.sleep(settle)

    def type_text(self, text: str, settle: float = 0.3) -> None:
        """Type lower-case ASCII, one key at a time.

        The only thing typed here is the volume name, `main` - five lower-case letters.
        An upper-case character is a shift chord, and the first attempt at naming the
        disk typed `M`, watched the field stay empty, and reported success; so this
        refuses anything it cannot press in one key rather than appearing to work.
        """
        self.n += 1
        log(f"#{self.n} type {text!r}")
        for ch in text:
            if ch.isupper() or not (ch.islower() or ch.isdigit()):
                raise ValueError(
                    f"cannot type {ch!r}: only lower-case letters and digits, because "
                    f"an upper-case character is a shift chord and typing one silently "
                    f"does nothing on this guest"
                )
            qmpmod.key(self.q, ch)
            time.sleep(settle)

    def close(self) -> None:
        try:
            self.q.close()
        except OSError:
            pass


# --- the sequence -------------------------------------------------------------------


def disk_utility(d: Recovery) -> bool:
    """Open Disk Utility from the recovery menu, once the menu is really on screen.

    NO FIXED SLEEP CAN DO THIS, AND TWO RUNS MEASURED IT.

    Run 41480111851, 60 second wait:

        t=11s (16:20:15)  #1 click Disk Utility row     the display was still BLANK
        t=16s             the first frame with any picture in it
        t=20s             the recovery menu was fully drawn

    Run 41498618924, 120 second wait:

        t= 0..22   mean grey   0-1   the Apple logo and its progress bar
        t=23       mean grey    77   a full-screen white flash  <- #1 click Disk Utility
        t=24..25   mean grey     1   back to the logo
        t=26       mean grey    21   the menu starts to paint
        t=27+      mean grey 23-25   the menu

    Both clicks went out before the menu existed, both landed on the FIRST row, and both
    runs then spent everything inside "Time Machine System Restore" - where the Up keys,
    Erase, the name field and Done all mean nothing. Disk Utility was never opened, the
    disk was never erased, and the failure surfaced much later.

    240 SECONDS, and the number is the measurement plus room.

    The boot from QMP connect to a usable menu took 143 seconds on run 41498618924
    (18:15:43 first frame, 18:18:06 the menu at t=27). Sixty was too short, 120 was too
    short, and both failures looked identical: a click into the boot screen, landing on
    whichever menu row appears at that point, which is always the first one.

    240 is that 143 with the same margin the other waits in this file carry.
    """
    d.wait(240, "the recovery menu draws")

    d.click((ROW_X, ROW_Y[3]), "Disk Utility row", settle=5)
    d.wait(10, "Disk Utility has to replace the recovery menu")
    d.click(CONTINUE_MENU, "recovery Continue", settle=20)

    # THE SIDEBAR HAS TO BE FINISHED BEFORE IT IS TOUCHED, because the next thing the
    # sequence does is press Up twice and those presses move whatever the sidebar has
    # selected. This window paints its source list in stages, and an Up arriving early
    # moves a selection that is not the one the two-press count assumes.
    d.wait(10, "Disk Utility has to finish drawing its sidebar")
    # THE RETURN IS THE ANSWER TO "DID IT OPEN", and without it this function returned
    # None, which `if not disk_utility(d)` reads as failure. Every run since that check
    # was added exited 1 immediately after Disk Utility had opened correctly - run
    # 41504896288 stopped three lines past its own success message, with no traceback
    # because nothing was raised.
    return True


def select_and_erase(d: Recovery) -> None:
    """Select the target disk and erase it AS APFS, NAMED main.

    THE SEQUENCE, MEASURED BY HAND ON A LIVE GUEST (1920x1080)

    0. THERE IS NO VIEW / SHOW ALL DEVICES STEP. Disk Utility lists both disks the
       moment it opens: `disk2s1` (2.88 GB, the USB recovery volume, read-only) and
       `disk0` (68.72 GB, the target). Show All Devices only changes whether the
       sidebar's *volume* rows are collapsed into their parent devices - it reveals
       nothing here, because the target is already the second row.

       Worse, it moves the selection. The two Ups below are counted from the recovery
       volume being selected, which is the state Disk Utility opens in. Selecting the
       Show All Devices menu item leaves the selection somewhere else, and two Ups from
       there do not land on disk0 - so Erase would target the wrong row or nothing.

       This step existed for several runs and was wrong every time. It is gone.

    1. Disk Utility opens with the recovery volume selected - `disk2s1`, 2.88 GB,
       USB, read-only. The 64 GiB target is already in the list; it is the row ABOVE.

    2. TWO `up` PRESSES reach it. Not one: the first moves off the volume onto the
       device that holds it, the second reaches the physical disk above. After them the
       detail pane reads

           Apple Inc. VirtIO Block Media   68.72 GB
           Uninitialized
           Capacity: 68.72 GB   Child count: 0   Type: Solid state   Device: disk0

       `Child count: 0` is the test that it is the empty disk and not an image.

    3. Erase opens a sheet. Nothing in it needs changing except the name:
       Format is APFS and Scheme is GUID Partition Map already.

    4. THE NAME FIELD IS NOT EMPTY, and one Backspace is what makes typing work. The
       field carries a suggested name - the disk's - and it is selected, so without the
       Backspace the typed text appends to it, and the first version of this step typed
       `main` into a field that already said something else.

       That, and the case: `main` is lower case. An upper-case `M` needs shift, which is
       a two-key chord, and the earlier attempt typed `M` then found the field unchanged.

    5. Erase in the sheet starts the operation. It takes about a minute for 64 GiB and
       the guest paints a progress sheet; 60 seconds is the measured wait.

    6. Cmd-Q leaves Disk Utility. There is no close button in Recovery.

    WHY THE ERASE IS THE DANGEROUS PART. A wrong row here erases the installer the guest
    is booting from, and the run stops at the next pane with a screen that shows exactly
    what happened. The `Child count: 0` reading above is what the sequence is checking.
    """
    d.wait(5, "the sidebar settles")

    # THE TARGET IS ONE ROW ABOVE THE DEFAULT SELECTION. Measured, not assumed: with the
    # recovery volume selected, two Ups land on disk0. Five seconds apart: the detail
    # pane repaints between them and a second Up during that repaint is lost.
    #
    # THE UPS ARE FIVE SECONDS APART; THE WAIT GOES AFTER THE SECOND ONE.
    #
    # The rebuild this has to survive is triggered by the selection CHANGING, and the
    # selection that matters is disk0 - reached by the second Up. That is when Disk Utility
    # reads the newly selected disk's partition table and rebuilds the detail pane and the
    # toolbar's enabled states, and an Erase click arriving inside that rebuild lands on a
    # control that is not available yet. 41645873209 proved it: three attempts in a row at
    # the same point, none of which raised the sheet, is a disabled control rather than a
    # swallowed click.
    #
    # FIVE SECONDS BETWEEN THE UPS, THIRTY-FIVE BETWEEN THE SECOND UP AND ERASE - by
    # instruction. The 5 comes from key()'s own settle; the 30 is the wait below. One
    # click, at the coordinate that is known to have opened the sheet.
    # THE DISK IS CHOSEN BY READING ITS CAPACITY, NOT BY COUNTING KEYPRESSES.
    #
    # A fixed number of Ups was the earlier design and it was wrong, because the row it
    # starts from is a runtime state. Run 41685628982 ended with this on the destination
    # pane:
    #
    #     macOS Base System   2.88 GB total
    #     main                268.4 MB total
    #
    # `main` was the 268 MB boot image, not the 64 GB target. Two Ups from wherever the
    # sidebar happened to be selected landed one row short, and the install then spent
    # fifty minutes writing macOS into a volume smaller than the system.
    #
    # The detail pane prints the selected disk's real capacity, so this steps up one row at
    # a time and asks until the number is one a target disk could have. The window is wide
    # on purpose - 60 to 200 GB - because the disk size is an input to the bake
    # (`--disk-size`, 64G by default): pinning a literal would break the day somebody baked
    # a 128 GB disk. What it excludes is everything else this installer can select: the
    # 2.88 GB recovery volume and the 268 MB boot image.
    TARGET_MIN_GB = 60.0
    TARGET_MAX_GB = 200.0
    chosen: float | None = None
    for attempt in range(8):
        size = d.selected_disk_gb()
        log(f"  attempt {attempt + 1}: the detail pane reads "
            + ("nothing readable" if size is None else f"{size:.2f} GB"))
        if size is not None and TARGET_MIN_GB <= size <= TARGET_MAX_GB:
            chosen = size
            log(f"  the target is selected: {size:.2f} GB")
            break
        # ONE ROW UP, THEN LET THE PANE REBUILD BEFORE READING AGAIN.
        #
        # The pane repaints for several seconds after the selection moves - that is when
        # Disk Utility reads the newly selected device's partition table - and a read
        # taken too soon returns the PREVIOUS disk's number, which is worse than none.
        d.key("up", settle=3.0)
        d.wait(3, "the detail pane has to show the newly selected disk")

    if chosen is None:
        log("=== NO DISK IN THE TARGET RANGE WAS SELECTED ===")
        log(f"  every reading was outside {TARGET_MIN_GB:.0f}..{TARGET_MAX_GB:.0f} GB")
        log("  erasing the wrong one installs macOS into the recovery volume or the boot")
        log("  image, so the sequence stops here rather than guessing")
        raise SystemExit(1)

    d.wait(5, "the detail pane settles on the chosen disk")

    # THE SWEEP: y = 280..320 BY 2, TWO SECONDS APART.
    #
    # `settle` is the pause AFTER each click, so two seconds here is the interval between
    # one click and the next. The frame before each click is still taken by click()'s own
    # shot(), so the recording shows every position that was tried.
    #
    # It stops mattering the moment the sheet opens: the next click lands on the sheet's
    # backdrop. There is no early exit because detecting "the sheet is up" needs a
    # screen read this file deliberately does not do for control flow.
    sheet_open = False
    for y in range(ERASE_BUTTON_Y_FIRST, ERASE_BUTTON_Y_LAST + 1, ERASE_BUTTON_Y_STEP):
        d.click((ERASE_BUTTON_X, y), f"Erase (y={y})", settle=2)
        # EACH SWEEP STEP IS CHECKED, SO A WORKING CLICK ENDS THE SWEEP AND A DEAD ONE IS
        # KNOWN TO BE DEAD.
        #
        # The sweep exists because the control moves a pixel or two between runs; the
        # check exists because a sweep that never lands used to be followed by a backspace
        # and a typed name into a field that was not there, and the failure then surfaced
        # two minutes later as "the disk did not grow".
        if d.erase_sheet_is_open():
            log(f"  the erase sheet opened at y={y}")
            sheet_open = True
            break

    if not sheet_open:
        log("=== THE ERASE SHEET NEVER OPENED ===")
        log("  every y in the sweep was clicked and none raised it; the disk is unchanged")
        log("  and the sequence stops rather than typing a name into nothing")
        raise SystemExit(1)

    # ONE BACKSPACE, because the name field arrives holding the disk's own name with the
    # text selected. Without this the typed name appends instead of replacing.
    d.key("backspace", settle=5.0)
    d.type_text(DISK_NAME, settle=0.3)
    d.wait(5, "the name field settles")

    d.click(ERASE_CONFIRM, "confirm Erase", settle=60)

    # THE SHEET DOES NOT CLOSE ITSELF. It reports completion and waits for Done, and a
    # run that skips this leaves every subsequent click landing on the sheet.
    d.click(ERASE_DONE, "Done on the erase report", settle=5)


def leave_disk_utility(d: Recovery) -> None:
    """Back to the installer list. Disk Utility in Recovery has no close button.

    Cmd-Q, and then long enough for the menu to redraw: the reinstall click is the next
    thing that happens and a click into a window that is still closing goes nowhere.
    """
    d.n += 1
    log(f"#{d.n} key combo cmd-Q")
    qmpmod.key_combo(d.q, "meta_l", "q")
    time.sleep(5)
    # The menu is redrawn when the utility closes; the Reinstall click is the next thing
    # that happens and a click into a window that is still closing goes nowhere.
    d.wait(10, "the recovery menu has to come back")
    d.shot(f"after-{d.n:02d}-leave-disk-utility")


def reinstall(d: Recovery) -> bool:
    """Pick Reinstall from the menu and press Continue. False if the installer never came.

    ROW_Y[1] is "Reinstall macOS Sequoia". The menu order was read off a frame:

        y=240  Restore from Time Machine
        y=333  Reinstall macOS Sequoia
        y=425  Safari
        y=514  Disk Utility

    A click that lands here while the menu is still blank goes to whatever row the
    firmware puts at this point once it arrives - which is how run 41480111851 ended up
    in Time Machine System Restore while its log said "Disk Utility row".
    """
    d.click((ROW_X, ROW_Y[1]), "Reinstall macOS row", settle=5)
    d.click(CONTINUE_MENU, "recovery Continue", settle=30)
    # The recovery menu is replaced by the installer's own window.
    #
    # THIS ONE IS CHECKED TOO. Run 41496584150 pressed Reinstall and Continue, the screen
    # did not change for 180 seconds, this wait said so - and the driver went on to
    # "wait 25s - the installer window opens" and then to click Continue on the welcome
    # pane that was never there.
    #
    # The Reinstall click lands on the recovery menu, which is a window the previous step
    # only just dismissed with Cmd-Q. A menu that has not finished redrawing takes the
    # click on whatever row is under it, and the run is then in the wrong application -
    # which is what the whole "Disk Utility row" bug was.
    # WAIT FOR THE SCREEN TO SETTLE, NOT FOR IT TO DIFFER.
    #
    # Run 41498618924 used wait_for_change here and reported
    #
    #     the pane did not change in 180s - the installer window has to replace the
    #     recovery menu
    #     === THE INSTALLER WINDOW NEVER OPENED ===
    #
    # while its last frame reads
    #
    #     macOS Sequoia
    #     To set up the installation of macOS Sequoia, click Continue.
    #
    # The window HAD opened. wait_for_change compares the screen now against the screen
    # when it started sampling, and that baseline was taken five seconds after the
    # Reinstall click - with the menu already gone and the new window not yet painted.
    # Once the window finished, the sampled fingerprint happened to match that
    # transitional frame, so "different" never became true and the wait timed out on a
    # pane that was exactly where it should be.
    #
    # wait_until_drawn asks the question that has an answer: has the picture stopped
    # changing? A window that opened and settled passes it; one that never appeared
    # fails it, because the menu it is supposed to replace is where the picture stays.
    d.wait(10, "the installer window has to open and settle")

    # THE PANE AFTER THE WAIT IS CHECKED ONCE, BY BRIGHTNESS, NOT BY CHANGE.
    #
    # A fixed wait cannot say whether the window arrived, so one cheap question is asked
    # after it: is anything lit? The installer's welcome pane is drawn on a dark
    # background and measures in the twenties; a black screen or the recovery menu
    # underneath it do not look the same. This is the NOTHING_DRAWN test the rest of the
    # file already uses, not a new opinion about what a pane looks like.
    level = d.brightness()
    if level is not None and level < NOTHING_DRAWN:
        log("=== THE INSTALLER WINDOW NEVER APPEARED ===")
        log(f"  the screen is dark (mean grey {level}) after Reinstall and Continue")
        log("  the last frame shows what is on screen")
        return False
    return True


def wait_for_install_start(d: Recovery, timeout: int = 120) -> bool:
    """Wait until the target disk grows, which is the only proof the copy began.

    The screen cannot answer this. The progress pane appears before the first byte is
    written, and a pane that never accepted Continue looks exactly the same for the first
    few seconds. The disk does answer: an installation writes hundreds of megabytes
    immediately, and a pane that did nothing writes none.

    Returns False rather than raising, so the caller can say what it was looking at.
    """
    baseline = d.disk_allocated()
    if baseline is None:
        # No disk path to watch. The caller was given no way to verify; say so by
        # returning True and letting the stall rule below be the only judge, rather than
        # failing a run that might be fine.
        log("  no disk to measure; the start of the copy cannot be confirmed")
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        size = d.disk_allocated()
        if size is None:
            continue
        if size > baseline:
            log(f"  the disk grew by {(size - baseline) >> 20} MiB")
            return True
    log(f"  the disk did not grow in {timeout}s (still {baseline >> 20} MiB)")
    return False


def installer_panes(d: Recovery, seconds: int = 4 * 3600) -> bool:
    """Advance the installer until it reboots, or say why it did not.

    WHAT WAS WRONG WITH THE PREVIOUS VERSION, EXACTLY

    It clicked (1306, 810) eight times with 15-second waits and reported success. The
    frames of run 41435208961 show what that did: the sequence reached the licence
    agreement, clicked twice at a position that is outside the window it was looking at,
    and then the screen went black for 7.75 minutes while eight more clicks landed on
    nothing. `bake.py` had already moved on to the Setup Assistant driver, so the run
    spent its remaining time clicking coordinates at a black screen.

    Two things are different here:

        the screen is CHECKED before every action, so a black screen ends the sequence
        with a message instead of a click

        the sequence ENDS on evidence - the installer reboots when it starts copying -
        rather than after a count

    WHY A DARK SCREEN IS NOT THE QUESTION

    The installer's own transition is: agree to the licence, choose the disk, and then
    the screen goes black while it copies and reboots. So a dark screen is not a failure
    by itself - and, more importantly, a dark screen cannot distinguish the two cases,
    because a hung guest looks exactly like a copying one when nothing is drawn.

    THE DISK CAN. An installation writes gigabytes into the target qcow2; a guest that
    has stopped writes nothing. disk_allocated() reads st_blocks, which is what grows on
    a sparse file, and a stall is the answer.

    The previous version waited a flat 90 minutes with a comment about how long the copy
    takes. That number was invented - no run of this bake had ever reached an
    installation, so there was nothing to measure it from - and it is the wrong shape
    anyway: a guess about duration cannot tell slow from stopped.

    Returns True when the screen comes back, which on this path is the reboot into the
    installed system.
    """
    deadline = time.time() + seconds
    dark_since: float | None = None
    last_click = 0.0
    last_size: int | None = None
    grew_at: float | None = None
    # Set when the installer reaches its progress pane. From then on the screen is lit
    # and static, so the disk is the only thing that can be measured.
    started_install: float | None = None

    # THE PANES IN ORDER, each with the click that leaves it. Measured by hand on a live
    # guest; the comments next to the constants carry what each one looks like.
    #
    #   welcome        "To set up the installation of macOS Sequoia, click Continue."
    #                  -> Continue, lower right       (960, 712)
    #   licence        the agreement text, Agree/Disagree at the foot
    #                  -> Agree                        (1005, 713)
    #   licence sheet  the smaller dialog Agree raises, with no readable buttons
    #                  -> its lower right              (1060, 530)
    #   destination    the card for the erased disk, then Continue
    #                  -> the card FIRST               (850, 550)
    #                     then Continue                (1005, 712)
    #
    # The order is the sequence; none of these is a retry. A pane that does not advance
    # therefore ends the run rather than being clicked at again, which is what the
    # unchanged-screen check below is for.
    # EACH PANE IS WAITED FOR, NOT ASSUMED AFTER A SLEEP. See wait_for_change(): run
    # 41477776580 clicked welcome Continue, slept ten seconds, clicked Agree, and the
    # screen was still the welcome pane at 16:10:37 - three clicks later.
    # reinstall() already confirmed the installer window replaced the recovery menu, so
    # there is nothing to wait 25 seconds for here. The welcome pane is still given its
    # own wait, because the window painting and the pane painting are separate events.
    #
    # AND IT IS A WAIT FOR STABILITY, NOT FOR A CHANGE. Run 41506477596 is the case:
    #
    #     19:13:08  the screen settled after 10s (mean grey 28) - the installer window
    #               has to open and settle
    #     19:14:40  the pane did not change in 90s - the welcome pane has to be drawn
    #     19:14:40  === THE WELCOME PANE NEVER APPEARED ===
    #
    # and its last frame is the welcome pane, reading
    #
    #     macOS Sequoia
    #     To set up the installation of macOS Sequoia, click Continue.
    #
    # The window settled ON the welcome pane, so wait_for_change's baseline was already
    # the thing it was waiting for. There is nothing to change into.
    d.wait(10, "the welcome pane has to be on screen")

    d.click(INSTALLER_CONTINUE, "welcome Continue", settle=5)

    # SIXTY SECONDS, NOT TEN, AND RUN 41626151499 IS THE MEASUREMENT.
    #
    # Ten seconds was not merely short - it was short by a factor that moved every
    # click one pane out of step, and the run still looked like it was working:
    #
    #     11:45:12  #13 click (960, 712) welcome Continue
    #     11:45:28  #14 click (1005, 713) Agree      the welcome pane was STILL THERE
    #     11:45:34  f97  the licence pane appears     six seconds after the Agree
    #     11:45:53  #15 click (1060, 530) sheet Agree on licence text - no sheet yet
    #     11:46:09  #16 click (850, 550) disk card    on licence text
    #     11:46:14  #17 click (1005, 712) Continue    this landed on Agree and
    #     11:46:19  f106 the licence sheet finally appears
    #     11:48:35  the disk did not grow in 120s (still 157 MiB)
    #
    # So the sheet was raised by #17 and nothing clicked it: #14 had already spent the
    # one click meant for Agree on the welcome pane, at (1005, 713) - fifteen pixels to
    # the right of that pane's Continue - and the two clicks after it fell on licence
    # text that reacts to nothing. The mouse never moved between #14 and #17 because
    # both land within a pixel of the same point. What looked like "the sheet's Agree
    # is dead" was this sequence being one step behind from #14 onward.
    #
    # The measurement is the gap itself: Continue at 11:45:12, licence drawn by
    # 11:45:34. Twenty-two seconds, and the run that worked waited 35 (04c1123's log,
    # "the screen settled after 35s - the licence pane has to be on screen"). Sixty is
    # that with room, and it is a fixed wait because every dynamic wait in this file has
    # been tried here: wait_for_change never settles on a macOS screen (the wallpaper
    # and the clock keep the fingerprint moving) and wait_until_drawn returns while the
    # OLD pane is still up, which is exactly the pane #14 must not click.
    d.wait(60, "the licence pane has to be on screen")

    log("licence: Agree")
    d.click(AGREE, "Agree", settle=5)

    # THE SHEET COMES UP OVER THE SAME PANE. Its own Agree is at (1060, 530) and is the
    # one that matters - the pane's Agree disabled itself when it raised the sheet.
    #
    # Confirmed before clicking it: a click on a sheet that never opened lands on the
    # licence text underneath, which does nothing and looks exactly like the sheet's
    # button being ignored.
    d.wait(10, "the licence sheet has to be on screen")

    # TEN SECONDS BETWEEN THE TWO AGREE CLICKS, which is what run 41558268694 needed.
    #
    # The sequence is: the licence pane's own Agree raises a smaller sheet, and THAT
    # sheet's Agree is the one that matters. Measured on that run:
    #
    #     01:22:17  #14 click (1005, 713) Agree          the pane's
    #     01:22:43    the sheet was on screen (20s)
    #     01:22:43  #15 click (1060, 530) Agree in sheet  the sheet's, same second
    #
    # The wait above confirmed the sheet had SETTLED, and the click went out in the same
    # second it settled. A sheet that has stopped repainting its picture is not
    # necessarily one that has finished becoming clickable - macOS animates these in, and
    # the button is disabled until the animation ends.
    log("licence sheet: Agree")
    d.wait(10, "the sheet finishes becoming clickable")
    d.click(AGREE_SHEET, "Agree in the sheet", settle=5)

    # A PANE THAT DID NOT ARRIVE ENDS THE SEQUENCE. Run 41493723813 is the reason: the
    # sheet Agree was clicked, the screen stayed on the licence for ninety seconds, this
    # wait said so - and the driver went on to click the disk card and Continue anyway.
    # Those two clicks landed on a licence pane that has no such controls, the disk was
    # never written, and the failure surfaced two panes later as "the disk did not grow".
    #
    # A FIXED WAIT, NOT A WAIT FOR CHANGE, BECAUSE THE CHANGE TEST PROVED TOO STRICT.
    #
    # Two runs with wait_for_change here failed everywhere, not just at this pane: the
    # recovery menu "never changed", Disk Utility "never changed", the installer window
    # "never changed" - because the wallpaper, the clock and the animating dialogs keep
    # the fingerprint moving, so it never settled into the two identical samples that
    # function wanted. A check that fails on screens that are working is worse than no
    # check.
    #
    # So ten seconds, which is what this sequence used before. It is a guess about how
    # long a pane takes - but it is a guess that has produced fifteen complete
    # installations, and the disk-growth check below is the real proof that the
    # destination was accepted.
    d.wait(10, "the destination pane has to be on screen")

    # THE DESTINATION PANE, AND THE CLICK THAT HAS TO BE PROVEN.
    #
    # This is where the previous run failed while reporting success. It clicked the card
    # and Continue, then went straight into "watching the disk" - and the disk was never
    # asked whether anything had started. The last frame of run 41468749665 shows the pane
    # still reading
    #
    #     Select the disk where you want to install macOS.
    #     [Back] [Continue]
    #
    # with no disk listed at all, which means the erase-and-reboot path left the installer
    # without a destination to offer.
    #
    # SO THE CLICK IS VERIFIED BEFORE THE WAIT BEGINS. A disk that has not grown in
    # START_EVIDENCE_SECONDS has not started installing, whatever the screen says - and
    # saying so in twenty seconds is the difference between reading a failure and reading
    # ninety minutes of one.
    log("destination: select the card, then Continue")

    before = d.disk_allocated()
    d.click(DISK_CARD, "the main disk card", settle=5)
    d.click(PANE_CONTINUE, "destination Continue", settle=20)

    log("confirming the installation actually started")
    started = wait_for_install_start(d, timeout=START_EVIDENCE_SECONDS)
    if not started:
        log("=== THE INSTALLER DID NOT START ===")
        log("  the destination pane was accepted and nothing was written")
        log("  the last frame shows what the pane was offering")
        return False

    # THAT IS THE WHOLE SEQUENCE - EVERY CLICK THAT CHOOSES ANYTHING HAS BEEN MADE.
    #
    # What follows is not more clicking. It is waiting for the copy the installer has
    # already committed to, and it returns when the disk stops growing, which is the end
    # of the installation and the point at which the guest reboots into the system it
    # wrote. bake.py then drives the Setup Assistant against that system.
    #
    # THIS USED TO STOP THREE MINUTES IN and call the sequence verified, on the argument
    # that the remaining forty minutes added no information. That argument was right about
    # the CLICKS and wrong about the BAKE: a disk three minutes into a copy is not a
    # macOS, so nothing could be published and run 41579186251 ended with a verified
    # sequence and no artifact. The size floor below is what makes stopping here mean
    # "finished" rather than "stalled".
    log("the installation is running; waiting for the disk to stop growing")
    baseline = d.disk_allocated()
    if baseline is None:
        log("  no disk to measure, so the end of the copy cannot be detected")
        return False

    # THE INSTALLATION IS FINISHED WHEN THE DISK STOPS GROWING.
    #
    # Not after a fixed number of minutes. dockur-macos states its own measurement in
    # verify-golden.yml:
    #
    #     storage settled at 27306 MiB after 3120 s
    #
    # 52 minutes and 26.7 GiB. A fixed 180 seconds - what this used to wait - stops at
    # 5.7 GiB, which is 4% of the way in.
    #
    # The settle rule: no growth for SETTLE_SECONDS while the disk is above
    # MIN_FINISHED_MIB. Both halves matter. Growth stopping early means the install
    # failed and the disk is idle; growth stopping late is the normal end. The size
    # floor separates them without needing to know what macOS draws.
    last = baseline
    settled_since = None
    finish_by = time.time() + MAX_INSTALL_SECONDS

    while time.time() < finish_by:
        time.sleep(DISK_SAMPLE_SECONDS)

        # THE WIZARD IS THE END, AND IT COMES BEFORE THE DISK SETTLES.
        #
        # Run 41660619717 is the measurement. The Setup Assistant appeared at 15:59:26 -
        # frame 532, mean grey 229, the country list - and the disk kept growing until
        # 16:10:48. This loop only looked at the disk, so the bake waited eleven minutes
        # and twenty-two seconds on a machine that had already finished and was sitting on
        # the wizard, then declared the install complete and only afterwards noticed the
        # wizard had been on screen the whole time.
        #
        # A guest on the first Setup Assistant page has finished installing: that page is
        # what the installer reboots into when it is done. So the picture is checked on
        # every sample, and seeing it ends the wait immediately - the disk size at that
        # moment is not interesting, and the SETTLE_SECONDS rule below exists only for the
        # case where the picture cannot be read.
        if d.frames and assistant.reached_assistant(os.path.join(d.frames, "shots")):
            size = d.disk_allocated()
            log(f"=== THE SETUP ASSISTANT IS ON SCREEN ===")
            log(f"  the disk holds {0 if size is None else size >> 20} MiB; the wizard is")
            log("  the end of the install, not the disk settling")

            # THIRTY SECONDS, BY INSTRUCTION, SO THE PUBLISHED DISK IS NOT CAUGHT MID-TRANSITION.
            #
            # The wizard appearing means the installer has rebooted into the system it
            # wrote - but the machine is still finishing that boot when the first readable
            # frame arrives: filesystems are being mounted, the setup daemon is writing its
            # state, and the disk is still taking writes at 15:59 in the run above.
            # Stopping the guest at the first frame would publish a disk that was stopped
            # while still starting.
            #
            # Thirty seconds of the wizard sitting still costs nothing against the eleven
            # minutes this loop used to wait for a disk that had already finished, and it
            # is the settle the rest of this file gives every other pane transition.
            d.wait(30, "the wizard has to settle before the machine is stopped")
            return True

        size = d.disk_allocated()
        if size is None:
            continue
        if size > last:
            log(f"  {size >> 20} MiB (+{(size - last) >> 20} MiB)")
            last = size
            settled_since = None
            continue

        if settled_since is None:
            settled_since = time.time()
            log(f"  no growth at {size >> 20} MiB; "
                f"waiting {SETTLE_SECONDS}s to see whether it is done")
            continue

        if time.time() - settled_since < SETTLE_SECONDS:
            continue

        elapsed = time.time() - settled_since
        big_enough = (size >> 20) >= MIN_FINISHED_MIB
        log(f"=== THE DISK HAS STOPPED GROWING ===")
        log(f"  {size >> 20} MiB, unchanged for {elapsed:.0f}s")
        if not big_enough:
            log(f"  that is below the {MIN_FINISHED_MIB} MiB a finished macOS occupies,")
            log("  so the installer stopped early rather than completing")
            return False
        log("  the installation is complete")
        return True

    log(f"=== THE DISK NEVER SETTLED IN {MAX_INSTALL_SECONDS // 60} MINUTES ===")
    log(f"  it holds {d.disk_allocated() >> 20} MiB and was still changing")
    return False

    return True


def run_installation(qmp_path: str, frames_dir: str | None,
                     disk_path: str | None = None) -> int:
    """Drive Recovery to a finished installation. Returns a process exit code.

    A non-zero code is what tells bake.py not to start the Setup Assistant driver: on a
    black screen that driver would click coordinates at nothing for an hour, which is
    exactly what the first version did.
    """
    d = Recovery(qmp_path, frames_dir, disk_path)
    log("connected; coordinates for the clicks, a brightness probe for the state")
    try:
        if not disk_utility(d):
            return 1
        select_and_erase(d)
        leave_disk_utility(d)
        if not reinstall(d):
            return 1
        if not installer_panes(d):
            log("the installation did not finish")
            return 1
        log(f"installation complete after {d.n} actions; the guest should now reboot "
            f"into the installed system")
        return 0
    finally:
        d.close()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--qmp", required=True, help="the drive QMP socket")
    p.add_argument("--frames", default=None, help="where to save a frame after each action")
    p.add_argument("--disk", default=None,
                   help="the target qcow2; without it a dark screen can only be "
                        "timed, not measured")
    args = p.parse_args(argv)

    return run_installation(args.qmp, args.frames, args.disk)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
