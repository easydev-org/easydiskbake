"""The one macOS ssh step that is not a command: the System Settings sequence.

WHY THIS IS A THIN DELEGATION AND NOT A REWRITE

macos/15/ssh.py's enable_remote_login() is thirty lines of clicks and about two hundred lines of
comment explaining why each one is where it is - and the comments are the valuable part:

    the search field is clicked after Spotlight has drawn, because a click that arrives first
    leaves the pane on the Sharing OVERVIEW and every later click is one screen off

    the full-disk-access switch IS ALREADY ON when Remote Login is turned on, and pressing it
    again turns it off - which is the bug that made the clean fail with `diskutil apfs trim WAS
    REFUSED (exit 1)`, six runs after the stage started working

    `Done` closes the pane so the daemon settles rather than holding the change in a sheet

None of that is expressible as `at:` steps, because two of the three are "wait for a redraw that
has no marker" and one is "look at this switch and do NOT press it". So the sequence stays in
ssh.py and the YAML names it.

WHY IT TAKES A guest AND NOT THE STEP DRIVER

enable_remote_login(guest) is the existing signature and it reads guest.paths.drive. Rather than
change that function's interface to suit this module - which would make the Python driver and the
walkthrough disagree about what a guest is - this builds the one attribute it needs. A copy of
the click sequence would be a second set of measurements, and this repository has paid for that
kind of duplication more than once.
"""

from __future__ import annotations

import os
import sys
import time

REPO = os.environ.get("REPO") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for path in (REPO, os.path.join(REPO, "lib"), os.path.join(REPO, "macos", "15")):
    if path not in sys.path:
        sys.path.insert(0, path)


class _Socket:
    """The one attribute enable_remote_login() reads: a drive QMP socket path."""

    def __init__(self, drive: str) -> None:
        self.drive = drive


class _Guest:
    def __init__(self, drive: str) -> None:
        self.paths = _Socket(drive)


def enable_remote_login(driver, step) -> bool:
    """Turn Remote Login on through System Settings, then read the preference back.

    The function below raises SystemExit when there is no QMP socket, which is right for a bake
    and wrong for a walkthrough - a step that returns False is reported by the caller with the
    screen, and an exception would take the whole run down without it. So it is caught.

    NO FILE NAMES THIS ANY MORE, AND THAT IS NOT AN OVERSIGHT. The switch sequence is `at:` steps
    in macos/15/steps/ssh.yaml now: every one of them is a measurement, and a measurement belongs
    in the file that reads it rather than behind a Python name. This stays because
    macos/15/settings_probe.py and the notes in ssh.py's enable_remote_login() still describe the
    same sequence, and deleting the only typed caller would let the two drift.
    """
    import ssh as sshstage

    try:
        # THE CONNECTION IS SHARED, NOT REOPENED. qemu's -qmp unix:... accepts ONE client, so a
        # call that opens a second while the walkthrough holds the first dies in a greeting
        # timeout - see enable_remote_login's own note.
        sshstage.enable_remote_login(_Guest(driver.qmp_path), q=driver.q)
    except SystemExit as exc:
        driver.log(f"  enable_remote_login stopped: {exc}")
        return False

    # THE SWITCH AND THE DAEMON ARE TWO FACTS. A frame of the pane after Done says the pane
    # closed; only the port says sshd came up, and that is `require_ssh`'s job in the YAML.
    driver.look("remote-login-done")
    return True


# --- THE THREE THINGS A BOOTED macOS NEEDS BEFORE A CLICK MEANS ANYTHING --------------------
#
# THEY ARE `python:` STEPS BECAUSE EACH ONE IS A LOOP OVER THE RECORDING, and a loop is the one
# shape the step vocabulary does not have. `until:` waits for WORDS on screen; a login window has
# no words - tesseract reads zero of them on the whole frame, which is why lib/desktop.py crops
# the password field's twelve-pixel placeholder and enlarges it six times. That reader already
# exists and is the same one macos/15/desktop.py uses, so these three name it rather than
# reimplementing a second opinion about what a login window looks like.


def _frames_dir(driver) -> str:
    """Where the sampler is writing.

    `frames_dir` IS THE STAGE'S OWN RECORDING DIRECTORY, set by lib/steps.py's run_file from the
    `frames=` argument. It is NOT `driver.work` - a CI stage keeps its frames somewhere the
    upload step already collects, and a step that guessed <work>/frames would read a directory
    nobody is writing to and wait out its whole budget on stale pictures. That exact mistake cost
    this stage fifteen minutes once (run 42020622755).
    """
    frames = getattr(driver, "frames_dir", "") or ""
    if frames:
        return frames
    return os.path.join(getattr(driver, "work", "") or ".", "frames")


def wait_for_login_window(driver, step) -> bool:
    """Wait until the login window is drawn, or the ceiling passes.

    A FIXED SLEEP IS THE WRONG SHAPE AND WAS MEASURED AS ONE. A booted macOS shows its login
    window two to four minutes after qemu starts; the stage used to sleep a flat 120 - the BOTTOM
    of that range - and on a slower runner typed into a window that had not been drawn, which
    reads later as "the password was wrong".

    NOT A FAILURE WHEN IT DOES NOT APPEAR. The keystrokes after this step go somewhere and are
    lost; the verdict is `wait for the desktop` two steps later. `optional: true` in the file says
    the same thing, and this returns True either way so the log is the only difference.
    """
    from lib import desktop

    ceiling = float(step.get("timeout") or os.environ.get("MACOS_LOGIN_DELAY", "240"))
    frames = _frames_dir(driver)
    started = time.time()
    driver.log(f"waiting up to {ceiling:.0f}s for the login window")
    while time.time() - started < ceiling:
        if desktop.reached_lock_screen(frames):
            driver.log(f"  the login window is up after {time.time() - started:.0f}s")
            return True
        time.sleep(5)
    driver.log(f"  no login window in {ceiling:.0f}s; typing anyway, which is what the "
               f"fixed wait always did")
    return True


def dismiss_keyboard_assistant(driver, step) -> bool:
    """Click Quit on the "Keyboard Setup Assistant" dialog, if it is up.

    IT EATS THE NEXT KEYSTROKE, and that is why this is here rather than in a comment. The dialog
    is part of the FIRST DESKTOP SESSION, so it is dismissed after the password and before
    Spotlight - three clicks four seconds apart, because a click that arrives before the dialog is
    drawn lands on wallpaper and looks identical in the log (run 42162859846).
    """
    import ssh as sshstage

    sshstage.dismiss_keyboard_assistant(_Guest(driver.qmp_path), q=driver.q)
    return True


def wait_for_desktop(driver, step) -> bool:
    """Wait until a macOS desktop is on screen. This is the verdict on the password.

    THE WALLPAPER TEST IS THE ONLY THING THAT CAN CONFIRM THE PASSWORD WAS ACCEPTED. If it was
    not, the ssh step later fails with a connection error that reads like a broken sshd, and the
    recording - where the login window shakes its field - is the only place the real cause is
    visible.

    FIVE MINUTES, because this is a first session on a disk that has never had one: the Dock is
    built, Spotlight starts indexing fourteen gigabytes, and the window server composites a
    wallpaper it has not cached. A run that gave up after 180s (42383829203) reported failure over
    a screen that was already a clean desktop.
    """
    from lib import desktop

    ceiling = float(step.get("timeout") or os.environ.get("MACOS_DESKTOP_TIMEOUT", "300"))
    frames = _frames_dir(driver)
    started = time.time()
    driver.log(f"waiting up to {ceiling:.0f}s for the desktop")
    while time.time() - started < ceiling:
        # THE RECORDING, NOT `shots/`. `shots/` is where the driver leaves the handful of pictures
        # it takes around its own clicks; the sampler writes frame-NNNN.* one per second for the
        # whole run, and that is the sequence that has this boot's desktop in it.
        if desktop.reached_desktop(frames, kind="macos"):
            driver.log(f"a macOS desktop is on screen ({time.time() - started:.0f}s)")
            return True
        time.sleep(5)
    driver.log("=== NO DESKTOP AFTER LOGGING IN ===")
    driver.log("  the password was typed and the screen still is not a desktop; check the")
    driver.log("  recording - the login window shakes its field when the password is wrong")
    return False
