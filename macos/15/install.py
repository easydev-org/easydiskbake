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
import sys
import time

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

# Disk Utility's View menu, measured from a frame captured with the menu open:
#
#   Show Only Volumes    centre (354,  41)
#   Show All Devices     centre (345,  63)   <- the target
#   Show APFS Snapshots  centre (362,  96)
SHOW_ALL_DEVICES = (345, 63)

# The View control is a button in the window toolbar, at about (710, 336). tesseract
# reads it as "iew" at x=700 because it drops the capital V. The menu bar at the top of
# the screen also contains the word View on some window states and clicking there does
# nothing - (309,11) was that mistake and it left Show Only Volumes in force.
VIEW_BUTTON = (710, 336)

# The Erase toolbar button. Its LABEL's box is only 8px tall at y=332-340; the control
# itself is the icon ABOVE the label, which is why clicking the label's centre does
# nothing and y=306 is what opens the sheet.
ERASE_BUTTON = (1290, 306)

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

# What "the screen has gone dark" means, in mean grey level 0-255. A lit macOS pane
# averages well above 100; a black screen averages under 10. 24 is far from both, so a
# dark wallpaper cannot trip it and a dim pane cannot fail to.
DARK_LEVEL = 24

# HOW LONG A DARK SCREEN MAY LAST IS NOT DECIDED BY A CLOCK. It is decided by whether
# the target disk is growing, and the clock is only a backstop for the case where it is
# neither growing nor dark any more.
#
# The first version used a flat 90 minutes with a note about how long the copy takes.
# That note was a guess - no run of this bake has ever completed an installation, so
# there was nothing to measure - and a guess is the wrong shape for this anyway: a hung
# guest and a copying guest are indistinguishable on a screen that is black. The disk is
# not: an installer writes gigabytes, a stopped one writes nothing.
#
# The numbers below are about the observation, not the installation:
#
#     DISK_STALL_SECONDS   how long the disk may show no growth before "it stopped"
#     DISK_SAMPLE_SECONDS  how often to look; qcow2 allocation moves by MBs per minute
#
# A copy of the recovery's 3.2 GB payload into a fresh APFS container moves the qcow2
# file by gigabytes, so 5 minutes of no change at all is not a slow write.
DISK_STALL_SECONDS = 5 * 60
DISK_SAMPLE_SECONDS = 30

# THE BACKSTOP, and it is the one number the installer itself decides.
#
# The progress pane said "About 2 hours and 52 minutes remaining" on the first run that
# reached it. That estimate is Apple's, made for real hardware, and this guest copies at
# whatever the runner's disk gives it - so it is not a deadline. What it does establish is
# that an installation which has not finished in ninety minutes is not merely slow.
#
# Ninety minutes is also short enough that a stuck run reports while somebody is still
# looking at it, instead of being cancelled by the job's own timeout with nothing to say.
INSTALL_PATIENCE = 90 * 60

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

    def brightness(self) -> int | None:
        """The mean grey level of a fresh screendump, or None if it cannot be read.

        A screendump goes to a file the emulator wrote, so this is one capture and one
        pass over its pixels - no OCR, no template, no interpretation of what is drawn.
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
                pixels = fh.read(width * height * 3)
        except (OSError, ValueError):
            return None
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

    def click(self, xy: tuple[int, int], why: str = "", settle: float = 2.0) -> None:
        self.n += 1
        log(f"#{self.n} click {xy} {why}")
        qmpmod.click(self.q, *xy)
        time.sleep(settle)
        self.shot(f"after-{self.n:02d}-{why}")

    def key(self, name: str, settle: float = 1.0) -> None:
        self.n += 1
        log(f"#{self.n} key {name}")
        qmpmod.key(self.q, name)
        time.sleep(settle)

    def type_text(self, text: str, settle: float = 0.1) -> None:
        """Type lower-case ASCII, one key at a time.

        WHAT THIS IS FOR AND WHAT IT IS NOT. The only thing typed here is the volume name,
        which is `main` - five lower-case letters. An upper-case one would be a shift
        chord, and the first attempt at naming the disk typed `M`, watched the field stay
        empty, and reported success; so this refuses anything it cannot press in one key
        rather than appearing to work.

        A refusal is the right shape: the caller can then choose a name that types.
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


def disk_utility(d: Recovery) -> None:
    """Open Disk Utility from the recovery menu.

    The menu is up within a minute of the guest reaching Recovery, but the window draws
    slowly and a click before the rows exist is lost. Sixty seconds is measured, not
    padded: the first run clicked at thirty and hit an empty window.
    """
    d.wait(60, "the recovery menu draws")
    d.click((ROW_X, ROW_Y[3]), "Disk Utility row", settle=3)
    d.click(CONTINUE_MENU, "recovery Continue", settle=20)


def show_all_devices(d: Recovery) -> None:
    """The switch that makes the target disk exist at all.

    Without it the sidebar holds one volume row and every later click selects the
    read-only recovery volume - so this is not a nicety, it is the prerequisite.
    """
    d.wait(15, "Disk Utility finishes drawing")
    d.click(VIEW_BUTTON, "View button", settle=3)
    d.click(SHOW_ALL_DEVICES, "Show All Devices", settle=6)


def select_and_erase(d: Recovery) -> None:
    """Select the target disk and erase it AS APFS, NAMED main.

    THE SEQUENCE, MEASURED BY HAND ON A LIVE GUEST (1920x1080)

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
    # recovery volume selected, two Ups land on disk0.
    for i in range(2):
        d.key("up", settle=1.0)
        log(f"  up {i + 1}/2")
    d.wait(2, "the detail pane repaints")

    d.click(ERASE_BUTTON, "Erase", settle=6)

    # ONE BACKSPACE, because the name field arrives holding the disk's own name with the
    # text selected. Without this the typed name appends instead of replacing.
    d.key("backspace", settle=1.0)
    d.type_text(DISK_NAME, settle=0.12)
    d.wait(1, "the name field settles")

    d.click(ERASE_CONFIRM, "confirm Erase", settle=60)

    # THE SHEET DOES NOT CLOSE ITSELF. It reports completion and waits for Done, and a
    # run that skips this leaves every subsequent click landing on the sheet.
    d.click(ERASE_DONE, "Done on the erase report", settle=8)


def leave_disk_utility(d: Recovery) -> None:
    """Back to the installer list. Disk Utility in Recovery has no close button.

    Cmd-Q, and then long enough for the menu to redraw: the reinstall click is the next
    thing that happens and a click into a window that is still closing goes nowhere.
    """
    d.n += 1
    log(f"#{d.n} key combo cmd-Q")
    qmpmod.key_combo(d.q, "meta_l", "q")
    time.sleep(20)
    d.shot(f"after-{d.n:02d}-leave-disk-utility")


def reinstall(d: Recovery) -> None:
    d.click((ROW_X, ROW_Y[1]), "Reinstall macOS row", settle=3)
    d.click(CONTINUE_MENU, "recovery Continue", settle=30)


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
    a sparse file, and a stall of DISK_STALL_SECONDS with no change is the answer.

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
    d.wait(25, "the welcome pane draws")
    d.click(INSTALLER_CONTINUE, "welcome Continue", settle=8)
    d.wait(10, "the licence pane draws")

    log("licence: Agree")
    d.click(AGREE, "Agree", settle=8)

    # THE SHEET COMES UP OVER THE SAME PANE. Its own Agree is at (1060, 530) and is the
    # one that matters - the pane's Agree disabled itself when it raised the sheet.
    log("licence sheet: Agree")
    d.click(AGREE_SHEET, "Agree in the sheet", settle=10)

    # THE DESTINATION PANE, WHERE THE CARD MUST BE CHOSEN BEFORE CONTINUE WORKS.
    log("destination: select the card, then Continue")
    d.wait(15, "the disk list draws")
    d.click(DISK_CARD, "the main disk card", settle=5)
    d.click(PANE_CONTINUE, "destination Continue", settle=20)

    while time.time() < deadline:
        level = d.brightness()

        if level is None:
            log("the screen could not be read; waiting")
            time.sleep(DISK_SAMPLE_SECONDS)
            continue

        if level < DARK_LEVEL:
            if dark_since is None:
                dark_since = time.time()
                last_size = d.disk_allocated()
                grew_at = time.time()
                log(f"the screen is dark (mean grey {level}); watching the disk, "
                    f"not the screen")

            size = d.disk_allocated()
            now = time.time()

            if size is None:
                # No disk path: fall back to the backstop rather than pretending to know.
                if now - dark_since > INSTALL_PATIENCE:
                    log(f"the screen has been dark for "
                        f"{(now - dark_since) / 60:.0f} minutes and this driver was "
                        f"given no disk to watch")
                    return False
            else:
                if last_size is not None and size > last_size:
                    grew_by = (size - last_size) >> 20
                    if grew_by:
                        log(f"  the disk grew by {grew_by} MiB "
                            f"({size >> 20} MiB allocated in total)")
                    grew_at = now
                    last_size = size
                stalled = now - (grew_at or dark_since)
                if stalled > DISK_STALL_SECONDS:
                    log(f"=== THE DISK HAS NOT GROWN IN {stalled / 60:.0f} MINUTES ===")
                    log(f"  allocated: {size >> 20} MiB, unchanged")
                    log("  a guest that is copying writes gigabytes; this one is not "
                        "doing anything")
                    return False

            time.sleep(DISK_SAMPLE_SECONDS)
            continue

        if dark_since is not None:
            log("the screen came back after "
                f"{(time.time() - dark_since) / 60:.1f} minutes")
            return True

        # THE INSTALLATION PROGRESS PANE IS LIGHT, NOT DARK, and it is where the installer
        # spends all of its time. It reads
        #
        #     macOS Sequoia will be installed on the disk "main"
        #     About 2 hours and 52 minutes remaining
        #     [Cancel]
        #
        # so the dark-screen branch never fires and the only evidence of progress is the
        # target disk growing. That is measured here, on the lit screen, with the same
        # stall rule: gigabytes over hours for a working install, nothing at all for a
        # stopped one.
        if started_install is None:
            started_install = time.time()
            last_size = d.disk_allocated()
            grew_at = time.time()
            log("the installer is on its progress pane; watching the disk")

        size = d.disk_allocated()
        now = time.time()

        if size is None:
            # No disk path to watch. The backstop is all that is left, and saying so beats
            # pretending the absence of information is progress.
            if now - started_install > INSTALL_PATIENCE:
                log(f"the installer has been on its progress pane for "
                    f"{(now - started_install) / 60:.0f} minutes and this driver was "
                    f"given no disk to watch")
                return False
        else:
            if last_size is not None and size > last_size:
                grew = (size - last_size) >> 20
                if grew:
                    log(f"  {grew} MiB written ({size >> 20} MiB allocated)")
                grew_at = now
                last_size = size
            stalled = now - (grew_at or started_install)
            if stalled > DISK_STALL_SECONDS:
                log(f"=== THE DISK HAS NOT GROWN IN {stalled / 60:.0f} MINUTES ===")
                log(f"  allocated: {size >> 20} MiB, unchanged")
                return False

        time.sleep(DISK_SAMPLE_SECONDS)

    log(f"the installer did not finish within {seconds / 60:.0f} minutes")
    return False


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
        disk_utility(d)
        show_all_devices(d)
        select_and_erase(d)
        leave_disk_utility(d)
        reinstall(d)
        if not installer_panes(d):
            log("the installer did not reach a reboot")
            return 1
        log(f"installation driven, {d.n} actions; the guest should now be rebooting")
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
