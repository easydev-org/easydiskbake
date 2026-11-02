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

# The installer's own panes put Agree at the lower left and Continue at the lower right,
# the same row the wizard uses everywhere.
AGREE = (604, 810)
INSTALLER_CONTINUE = (1306, 810)


class Recovery:
    """The few actions this needs, each recording a frame afterwards.

    Frames are RECORDED, never read. A run that goes wrong is diagnosed afterwards by
    looking at them, which is how every position above was found in the first place.
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


def installer_panes(d: Recovery, panes: int = 8) -> None:
    """Work through the installer's own panes to Setup Assistant.

    A count, not a loop over anything observed. The panes are: the licence agreement
    (Agree, then Agree again in the sheet), the destination disk selector (which has
    the only disk preselected), and then Continue until the installer reboots into the
    new system. The reboot is the point at which this returns - the caller waits for
    Setup Assistant, not this function.
    """
    # The licence pane is the one with a fixed shape: Agree lower left, and a sheet
    # behind it that has to be agreed to again.
    d.wait(20, "the licence pane draws")
    d.click(AGREE, "Agree", settle=5)
    d.click(AGREE, "Agree in the sheet", settle=10)

    for i in range(panes):
        d.wait(15, f"installer pane {i + 1} settles")
        d.click(INSTALLER_CONTINUE, f"installer Continue {i + 1}", settle=25)


def run_installation(qmp_path: str, frames_dir: str | None) -> None:
    d = Recovery(qmp_path, frames_dir)
    log("connected; fixed sequence, no reads")
    try:
        disk_utility(d)
        show_all_devices(d)
        select_and_erase(d)
        leave_disk_utility(d)
        reinstall(d)
        installer_panes(d)
        log(f"installation driven, {d.n} actions; the guest should now be rebooting")
    finally:
        d.close()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--qmp", required=True, help="the drive QMP socket")
    p.add_argument("--frames", default=None, help="where to save a frame after each action")
    args = p.parse_args(argv)

    run_installation(args.qmp, args.frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
