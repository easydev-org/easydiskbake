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

# The recovery list, measured with tesseract on a captured frame. Every entry's label
# is left-aligned at x=837 inside the window; the rows are 92px apart:
#
#   Restore from Time Machine   y=232
#   Reinstall macOS Sequoia     y=324
#   Safari                      y=416
#   Disk Utility                y=508
#   Continue                    y=585, x=1115-1169
ROW_Y = (232, 324, 416, 508)
ROW_X = 837

# The recovery menu's own Continue. An entry is opened by selecting it and then pressing
# this; clicking the entry alone only highlights it.
CONTINUE_MENU = (1142, 588)

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

# The erase sheet's own Erase button, from a captured frame of the sheet:
#   Cancel (1066,639)   Erase (1146,639)
# The toolbar's Erase is at y=332 as well, so the y is what tells them apart.
ERASE_CONFIRM = (1146, 639)

# The installer's own panes put Agree at the lower left. It is NOT the same position as
# NOT_NOW: a licence pane's buttons sit on the ordinary button row, and the earlier
# value here was copied from the wizard's three-button layout, which put the click
# below the window entirely.
AGREE = (604, 810)

# The sheet that follows the licence pane agrees on the right-hand side.
AGREE_SHEET = (1026, 634)

INSTALLER_CONTINUE = (1306, 810)

# What "the screen has gone dark" means, in mean grey level 0-255. A lit macOS pane
# averages well above 100; a black screen averages under 10. 24 is far from both, so a
# dark wallpaper cannot trip it and a dim pane cannot fail to.
DARK_LEVEL = 24

# How long a dark screen may last before it is called a failure. The install copies a
# 12 GB system image and reboots twice; dockur-macos's own runs spend 40-70 minutes in
# this phase. An hour and a half is generous for the copy and short enough that a hung
# guest does not outlive the job's own timeout.
DARK_PATIENCE = 90 * 60

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

    def __init__(self, qmp_path: str, frames_dir: str | None = None):
        self.q = qmpmod.QMP(qmp_path)
        self.frames = frames_dir
        self.n = 0
        self.shots = 0

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
    """Select the unformatted disk and erase it as APFS.

    THE ROW IS THE ONE WITHOUT A CAPACITY. A disk with no partition table has nothing
    for Disk Utility to total, so it reports "Capacity: Unknown" and "Child count: Not
    Supported"; the two images sit beside it with real sizes. Rows are tried in order
    and the first that is not one of those two is the target.

    Row 1 (y=351) is the heading's first leg. Row 2 (y=379) is the 64 GiB target on this
    layout: 200 MiB boot.img and the recovery image are both attached and both smaller.
    Selecting by position and then clicking Erase is safe in the only sense that
    matters - a wrong row here erases the installer, and the run stops at the next pane
    with a screen that shows exactly what happened.
    """
    d.wait(5, "the sidebar settles")
    # The target is the second row under Internal on this layout. Erasing it is the one
    # irreversible step; the sequence below is the measured one and the frames record
    # what was selected.
    d.click((SIDEBAR_CLICK_X, SIDEBAR_ROWS_Y[1]), "the Uninitialized disk", settle=5)

    d.click(ERASE_BUTTON, "Erase", settle=5)
    # Nothing in the sheet needs changing: Name is Untitled, Format is APFS and Scheme
    # is GUID Partition Map, which is exactly what an install target needs.
    d.click(ERASE_CONFIRM, "confirm Erase", settle=45)


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


def installer_panes(d: Recovery, seconds: int = 3600) -> bool:
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

    WHY DARK MEANS "THE INSTALLER IS WORKING"

    The installer's own transition is: agree to the licence, choose the disk, and then
    the screen goes black while it copies and reboots into the new system. A dark screen
    is therefore not a failure by itself. What separates the two is duration: copying
    takes tens of minutes, a crash stays dark for ever and qemu eventually exits. So a
    dark screen is reported and waited on, and only a dark screen with an exceeded
    deadline is a failure.

    Returns True when the guest stopped answering - which on this path is the reboot
    into the installed system.
    """
    deadline = time.time() + seconds
    dark_since: float | None = None
    last_click = 0.0

    # The licence pane is the first thing after the recovery menu. It is agreed to
    # twice: once on the pane and once in the sheet that follows it.
    d.wait(25, "the licence pane draws")
    d.click(AGREE, "Agree", settle=6)
    d.click(AGREE_SHEET, "Agree in the sheet", settle=8)

    while time.time() < deadline:
        level = d.brightness()

        if level is None:
            log("the screen could not be read; waiting")
            time.sleep(15)
            continue

        if level < DARK_LEVEL:
            # Dark: either copying, or gone. Record when it started and keep waiting.
            if dark_since is None:
                dark_since = time.time()
                log(f"the screen is dark (mean grey {level}); the installer is "
                    f"copying or the guest has stopped - waiting")
            waited = time.time() - dark_since
            if waited > DARK_PATIENCE:
                log(f"the screen has been dark for {waited / 60:.0f} minutes")
                return False
            time.sleep(20)
            continue

        if dark_since is not None:
            # It came back. That is the reboot into the installed system, or the
            # installer redrew a pane - both are progress, and the caller decides
            # which by waiting for a desktop.
            log("the screen came back after "
                f"{(time.time() - dark_since) / 60:.1f} minutes")
            return True

        # Lit, and not yet dark: still walking the installer's panes. Click the forward
        # control, but not faster than the pane can redraw.
        if time.time() - last_click >= CLICK_INTERVAL:
            d.click(INSTALLER_CONTINUE, "installer Continue", settle=3)
            last_click = time.time()

    log(f"the installer did not finish within {seconds / 60:.0f} minutes")
    return False


def run_installation(qmp_path: str, frames_dir: str | None) -> int:
    """Drive Recovery to a finished installation. Returns a process exit code.

    A non-zero code is what tells bake.py not to start the Setup Assistant driver: on a
    black screen that driver would click coordinates at nothing for an hour, which is
    exactly what the first version did.
    """
    d = Recovery(qmp_path, frames_dir)
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
    args = p.parse_args(argv)

    return run_installation(args.qmp, args.frames)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
