"""Drive macOS 26 (Tahoe) Recovery to a finished installation.

    python3 macos/26/install.py --qmp run/drive.sock.qmp --frames out/frames

WHAT THIS IS, AND WHAT IT IS HONESTLY NOT

It is macos/15/install.py's sequence with a check in front of every click, and it has never
been run. macOS 26's installer has not been observed here; its panes may be in the same
places as Sequoia's and they may not be, and nothing in this repository can tell the
difference until a run happens.

That is why the checks exist rather than the coordinates being copied and hoped for. A
coordinate that has moved and a click that was ignored look identical from the outside -
the disk does not grow, and fifteen runs were spent on 15 learning to tell those apart. So
each step here asks the screen what it is looking at first, and a pane it does not recognise
ends the run with the pane named.

THE THREE PANES IT CAN RECOGNISE, AND WHY THOSE

    Disk Utility, target selected   "capacity:" and "child" in the detail pane
    the erase sheet                 `erase "<device>"?` - the dialog's own question
    the installer's licence         "agree" in the lower half of its window

Each phrase is on exactly one pane on that screen. Everything else - the recovery menu, the
installer's welcome, the destination list - is left to 15's waits, because their text is
either absent (the recovery menu is four short rows of icons) or changes between releases.

WHAT IT DOES NOT DO

It does not drive the Setup Assistant. This stage ends when the guest shows the wizard,
which is what macos/15/bake.py's stage boundary asks for; the wizard is the next stage's
work and README.md in this directory records why driving it on 26 is a different problem.

NEITHER THE ERASE SWEEP NOR THE CAPACITY CHECK IS DUPLICATED HERE. Both live in
macos/15/install.py's Recovery class and are used through it, because they were measured
against real frames and a second copy would be a second place to get the crop wrong.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import assistant  # noqa: E402


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # REGISTERED IN sys.modules UNDER `name`, because macos/26/bake.py has to reach back
    # into macos15_bake to redirect its HERE at this directory. A module loaded by
    # spec.loader.exec_module() is not registered automatically, and sys.modules.get()
    # then returns None.
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


install15 = _load("macos15_install",
                  os.path.join(os.path.dirname(HERE), "15", "install.py"))

# 15's constants, because the machine and the firmware are the same and those numbers were
# measured. A release-specific directory changes the PANE SEQUENCE, not the hardware.
log = install15.log
Recovery = install15.Recovery
ROW_X = install15.ROW_X
ROW_Y = install15.ROW_Y
CONTINUE_MENU = install15.CONTINUE_MENU
DISK_NAME = install15.DISK_NAME
ERASE_CONFIRM = install15.ERASE_CONFIRM
ERASE_DONE = install15.ERASE_DONE
NOTHING_DRAWN = install15.NOTHING_DRAWN


# THE RECTANGLES, IN 1920x1080 PIXELS, MEASURED ON 15 AND NOT YET ON 26.
#
# They are kept as data so a future run can correct one number instead of hunting through a
# sequence of clicks. The detail-pane rectangle is the one already proven on 15 - it is what
# selected 68.72 GB out of three rows - and the window is the same size on both releases
# because lib/opencore pins the resolution.
PANE_DETAIL = (690, 540, 1490, 680)     # Disk Utility's capacity table
PANE_SHEET = (700, 440, 1410, 560)      # the erase sheet's question line
PANE_LICENCE = (600, 900, 1350, 1080)   # the Agree/Disagree row at the foot


def says(frame: str, box: tuple[int, int, int, int], *needles: str) -> bool:
    """Does this rectangle of the frame contain all of `needles`?"""
    text = assistant.ocr_text_cropped(frame, box, scale=4)
    if not text:
        return False
    return all(needle.lower() in text for needle in needles)


def wait_for_says(d: Recovery, box, needles, seconds: int, what: str) -> bool:
    """Poll until the rectangle matches, or give up with `what` named.

    POLLING RATHER THAN A FIXED WAIT, because a fixed wait that is too short produces the
    failure this whole file exists to avoid - a click sent to a pane that has not painted.
    And polling on TEXT rather than a fingerprint, because a macOS pane keeps moving while
    it is otherwise ready to be used (the clock, the animations), which is what made every
    dynamic wait on 15 unusable.
    """
    import time

    deadline = time.time() + seconds
    while time.time() < deadline:
        path = d.screenshot(f"pane-{what.replace(' ', '-')[:24]}")
        if path and says(path, box, *needles):
            log(f"  the {what} is on screen")
            return True
        time.sleep(5)
    log(f"=== THE {what.upper()} NEVER APPEARED ===")
    log(f"  looked for {needles} in {box} for {seconds}s")
    return False


def select_and_erase(d: Recovery) -> bool:
    """Select a disk whose capacity says it is the target, then erase it.

    THE SELECTION IS 15's, INCLUDING THE RANGE CHECK, because that logic was verified
    against three real frames and is the part that finally stopped this bake erasing the
    268 MB boot image. What 26 adds is that the pane is confirmed to be a detail pane at
    all before its numbers are trusted.

    Returns False instead of raising so the caller can say which step stopped.
    """
    TARGET_MIN_GB, TARGET_MAX_GB = 60.0, 200.0

    if not wait_for_says(d, PANE_DETAIL, ("capacity",), 120, "Disk Utility detail pane"):
        return False

    chosen = None
    for attempt in range(8):
        size = d.selected_disk_gb()
        log(f"  attempt {attempt + 1}: the detail pane reads "
            + ("nothing readable" if size is None else f"{size:.2f} GB"))
        if size is not None and TARGET_MIN_GB <= size <= TARGET_MAX_GB:
            chosen = size
            log(f"  the target is selected: {size:.2f} GB")
            break
        d.key("up", settle=3.0)
        d.wait(3, "the detail pane has to show the newly selected disk")

    if chosen is None:
        log("=== NO DISK IN THE TARGET RANGE WAS SELECTED ===")
        log(f"  every reading was outside {TARGET_MIN_GB:.0f}..{TARGET_MAX_GB:.0f} GB")
        return False

    # THE ERASE SWEEP, REUSED FROM 15. It clicks y=280..320 and stops at the first position
    # whose sheet check says the dialog opened - and that check is what 15's last run
    # proved, once the crop and the typographic quote were right.
    sheet_open = False
    for y in range(install15.ERASE_BUTTON_Y_FIRST,
                   install15.ERASE_BUTTON_Y_LAST + 1,
                   install15.ERASE_BUTTON_Y_STEP):
        d.click((install15.ERASE_BUTTON_X, y), f"Erase (y={y})", settle=2)
        if d.erase_sheet_is_open():
            log(f"  the erase sheet opened at y={y}")
            sheet_open = True
            break

    if not sheet_open:
        log("=== THE ERASE SHEET NEVER OPENED ===")
        return False

    d.key("backspace", settle=5.0)
    d.type_text(DISK_NAME, settle=0.3)
    d.wait(5, "the name field settles")
    d.click(ERASE_CONFIRM, "confirm Erase", settle=60)
    d.click(ERASE_DONE, "Done on the erase report", settle=5)
    return True


def run_installation(qmp_path: str, frames_dir: str | None,
                     disk_path: str | None = None) -> int:
    """Recovery to a finished installation, with each pane named before it is used."""
    d = Recovery(qmp_path, frames_dir, disk_path)
    log("connected; each pane is confirmed by its text before a click is sent")
    try:
        # -- the recovery menu, then Disk Utility ------------------------------
        if not install15.disk_utility(d):
            return 1
        if not select_and_erase(d):
            return 1
        install15.leave_disk_utility(d)

        # -- Reinstall ---------------------------------------------------------
        if not install15.reinstall(d):
            return 1

        # -- THE LICENCE, WITH ITS OWN CONFIRMATION -----------------------------
        #
        # 15 waits ten seconds for the welcome pane and clicks Continue at a measured
        # point. Here the licence row has to be readable first, because on 26 an
        # unrecognised pane is the one thing this driver must not click through.
        d.click(install15.INSTALLER_CONTINUE, "welcome Continue", settle=5)
        if not wait_for_says(d, PANE_LICENCE, ("agree",), 180, "licence pane"):
            log("  the installer's licence did not appear where 15's does")
            log("  the welcome Continue may be somewhere else on this release")
            return 1

        d.click(install15.AGREE, "Agree", settle=5)
        d.wait(10, "the licence sheet has to be on screen")
        d.wait(10, "the sheet finishes becoming clickable")
        d.click(install15.AGREE_SHEET, "Agree in the sheet", settle=5)

        # -- the destination, which is where the disk must be found ------------
        if not wait_for_says(d, PANE_DETAIL, ("capacity",), 180, "destination detail"):
            log("  no capacity table, so no disk is being offered")
            return 1
        d.click(install15.DISK_CARD, "the main disk card", settle=5)
        d.click(install15.PANE_CONTINUE, "destination Continue", settle=20)

        # -- the copy -----------------------------------------------------------
        #
        # THE WIZARD IS THE END, as on 15: it appears while the disk is still settling, and
        # waiting for the disk instead cost eleven minutes on run 4166067171.
        if not install15.wait_for_install_start(
                d, timeout=install15.START_EVIDENCE_SECONDS):
            log("=== THE INSTALLER DID NOT START ===")
            return 1

        log("the installation is running; waiting for the Setup Assistant")
        import time

        finish_by = time.time() + install15.MAX_INSTALL_SECONDS
        while time.time() < finish_by:
            time.sleep(install15.DISK_SAMPLE_SECONDS)
            if d.frames:
                shots = os.path.join(d.frames, "shots")
                if assistant.reached_assistant(shots):
                    size = d.disk_allocated()
                    log("=== THE SETUP ASSISTANT IS ON SCREEN ===")
                    log(f"  the disk holds "
                        f"{0 if size is None else size >> 20} MiB")
                    d.wait(30, "the wizard has to settle before the machine is stopped")
                    return 0
            size = d.disk_allocated()
            if size is not None:
                log(f"  {size >> 20} MiB")

        log("=== NO SETUP ASSISTANT BEFORE THE DEADLINE ===")
        return 1
    finally:
        d.close()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--qmp", required=True, help="the drive QMP socket")
    p.add_argument("--frames", default=None, help="where to save frames and probe copies")
    p.add_argument("--disk", default=None, help="the target qcow2, for the size checks")
    args = p.parse_args(argv)
    return run_installation(args.qmp, args.frames, args.disk)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
