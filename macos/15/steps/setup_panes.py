"""The macOS setup panes that are programs, not clicks.

WHY THIS FILE IS SO THIN, AND WHY THAT IS THE FINDING

macos/15/install.py is 1694 lines. Almost none of it is a sequence of clicks, and the part
that is has already been extracted into setup.yaml. What is left is here, as six calls into
the functions install.py already has:

    open_disk_utility       wait for the recovery menu to be DRAWN, not for a duration
    open_reinstall          the same menu, a different row
    erase_the_disk          a sheet with no OCR-readable buttons, a capacity check, and a
                            disk that must be identified by its SIZE
    install                 disk growth as evidence, because a black screen cannot tell
                            "copying 12 GB" from "hung"

WHY NOT REWRITE THEM AS `at:` STEPS - the honest answer

Each of those four is a decision, not an action, and every one of them was measured the hard
way:

    the recovery menu needs "wait until drawn" because TWO runs clicked into a blank
    framebuffer and landed in Time Machine System Restore. A `settle: 60` and a `settle: 120`
    were both tried and both failed, which is why install.py says "240 also failed, which is
    why this no longer waits at all".

    the erase sheet's buttons are drawn, not text, so finding them is a crop and a sweep of
    twenty-one positions - install.py's own comment.

    the target disk is chosen by CAPACITY, because a frame of the sidebar showed two rows and
    a fixed click erased the 268 MB recovery volume instead of the 68 GB target. That is a
    read-then-decide, and it is the one place a decision before a click is CORRECT - the click
    is not "go here", it is "erase disk of this size".

So these are `python:` steps and the YAML names them. That split is the answer to "can this be
YAML": the SEQUENCE can, and these four cannot, and pretending otherwise would put a decision
inside a coordinate.

DELEGATION RATHER THAN A SECOND COPY. Each function below imports the real one and passes the
driver through. A reimplementation would be a second set of measurements that could disagree
with the first, which is the failure mode every module in this repository warns about.
"""

from __future__ import annotations

import os
import sys

REPO = os.environ.get("REPO") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if os.path.join(REPO, "lib") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "lib"))
if os.path.join(REPO, "macos", "15") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "macos", "15"))


def _recovery(driver):
    """install.py's Recovery, built around the driver's own QMP connection.

    THE DRIVER ALREADY OWNS THE SCREEN, so this does not open a second QMP client for the
    frames - it hands Recovery the same socket path and lets it make its own, which is what
    install.py's class expects and what keeps `.shot()` writing into the same work directory.

    `disk_path` IS THE TARGET qcow2 AND IT MATTERS. install.py's installer_panes() watches how
    much the file has grown, because that is the only thing that can tell "copying 12 GB" from
    "hung" while the screen is black. A driver with no disk path can only report the screen,
    so this says so rather than passing None and getting a stage that times out instead of
    failing.
    """
    import install

    frames = os.path.join(driver.work, "frames")
    os.makedirs(frames, exist_ok=True)
    qmp_path = getattr(driver, "qmp_path", "")
    disk = getattr(driver, "disk_path", None)
    if not qmp_path:
        raise RuntimeError(
            "this pane needs the driver's QMP path; construct Driver(...) with qmp_path set")
    return install.Recovery(qmp_path, frames, disk)


def open_disk_utility(driver, step) -> bool:
    import install

    d = _recovery(driver)
    try:
        return bool(install.disk_utility(d))
    finally:
        d.close()


def open_reinstall(driver, step) -> bool:
    import install

    d = _recovery(driver)
    try:
        return bool(install.reinstall(d))
    finally:
        d.close()


def erase_the_disk(driver, step) -> bool:
    """Erase, then leave Disk Utility so the recovery menu can be used again.

    `leave_disk_utility` IS PART OF ERASING. The sheet has to be closed before any later click
    lands where it was aimed - install.py records that without it every subsequent click lands
    on the sheet.
    """
    import install

    d = _recovery(driver)
    try:
        install.select_and_erase(d)
        install.leave_disk_utility(d)
        return True
    finally:
        d.close()


def install_macos(driver, step) -> bool:
    """Agree, choose the disk, and wait for the copy - by watching the disk, not the screen."""
    import install

    d = _recovery(driver)
    try:
        return bool(install.installer_panes(d, int(step.get("timeout", 4 * 3600))))
    finally:
        d.close()
