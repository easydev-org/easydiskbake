"""The macOS setup panes that are programs, not clicks - and why there are only two.

macos/15/install.py is 1694 lines. The walk in steps/setup.yaml covers the sequence: the
recovery menu, opening Disk Utility, the installer's four panes, the waits between them. What
is left here is two things, and both were measured the hard way:

    select_target_disk   which disk to erase, decided by CAPACITY from a frame
    install_macos        whether the copy has finished, decided by DISK GROWTH

WHY THESE TWO ARE NOT `at:` STEPS

    SELECTING BY CAPACITY

        A fixed click erased the wrong disk and nobody noticed for fifty minutes. The frames
        from run 41685628982 end on the destination pane reading:

            macOS Base System   2.88 GB total
            main                268.4 MB total      <- the 268 MB boot image, not the target

        Disk Utility lists the 2.88 GB recovery volume, the 268 MB boot image and the 64 GB
        target, 28 pixels apart, and the row that is selected when the window opens is a
        RUNTIME state - two runs with identical clicks selected different disks. So the
        target is found by reading the detail pane's capacity and stepping up a row at a time
        until the number is one a target disk could have (60..200 GB, wide because --disk-size
        is an input).

        THIS IS THE ONE PLACE A READ BEFORE A CLICK IS CORRECT, and the reason is that the
        click is not "go here" - it is "erase the disk of this size". A coordinate cannot say
        that.

    THE COPY

        The installer goes black while it copies, so the screen cannot distinguish "copying
        12 GB" from "hung". install.py watches st_blocks on the target qcow2 instead, which is
        what grows on a sparse file, and stops when the wizard appears. The earlier version
        waited a flat 90 minutes with a comment about how long a copy takes - a number no run
        had ever measured, and the wrong shape anyway, because a duration cannot tell slow
        from stopped.

WHY THE ERASE IS ONE STEP AND NOT SEVEN

    select_and_erase() = capacity check, sweep the Erase button, name the volume, confirm,
    wait for the report, press Done. FIVE OF THOSE DEPEND ON THE ONE BEFORE HAVING LANDED:

        the name is typed into a field that only exists if the sheet opened
        the Done button is on a sheet that only exists if the confirm was accepted

    Splitting them into `at:` steps would mean a file where step 5 is a click into a dialog
    that step 3 may not have opened - and a click into nothing is what made six runs of this
    bake fail with "the disk did not grow". The sheet's own `expect_any` is in the YAML at the
    sweep, so the sequence still stops with the screen in the log if the button is never
    found; what stays inside is the five clicks that are only meaningful together.

DELEGATION, NOT A SECOND COPY. Every function below imports the real one and passes the
driver's socket through. A reimplementation would be a second set of measurements that could
disagree with the first.
"""

from __future__ import annotations

import os
import sys

REPO = os.environ.get("REPO") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for path in (REPO, os.path.join(REPO, "lib"), os.path.join(REPO, "macos", "15")):
    if path not in sys.path:
        sys.path.insert(0, path)


def _recovery(driver):
    """install.py's Recovery, built on the driver's own QMP socket.

    `disk_path` MATTERS. install.py's installer_panes() watches the target qcow2 grow, because
    that is the only thing that can tell "copying 12 GB" from "hung" while the screen is black.
    A driver without a disk path can only report the screen, so this says so rather than passing
    None and getting a stage that times out instead of failing.
    """
    import install

    frames = os.path.join(driver.work, "frames")
    os.makedirs(frames, exist_ok=True)
    qmp_path = getattr(driver, "qmp_path", "")
    if not qmp_path:
        raise RuntimeError(
            "this pane needs the driver's QMP path; construct Driver(...) with qmp_path set")
    return install.Recovery(qmp_path, frames, getattr(driver, "disk_path", None))


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


def select_target_disk(driver, step) -> bool:
    """Choose the target by capacity. Stops the run rather than guessing."""
    import install

    d = _recovery(driver)
    try:
        size = install.choose_target_disk(d)
        if size is None:
            return False
        driver.log(f"  the target is {size:.2f} GB")
        return True
    finally:
        d.close()


def erase_the_disk(driver, step) -> bool:
    """Sweep Erase, name the volume, confirm, press Done, then leave Disk Utility."""
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
