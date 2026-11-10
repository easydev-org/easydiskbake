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


# THE WINDOWS THAT CAN COVER THE WHOLE SCREEN AFTER A LOGIN, AND WHY EACH IS A REAL FAILURE.
#
# `is_macos_desktop` reads Finder's menu bar, and that is the right test - it says what the
# MACHINE is doing rather than how its wallpaper reflects light. But it is a test about the TOP
# STRIP, and a sheet or a window that covers the screen takes the strip with it. Run 42831281414
# is what that costs:
#
#     [steps 15:03:23] === wait for the desktop
#     [steps 15:08:56]   === NO DESKTOP AFTER LOGGING IN ===
#     [desktop] menu bar reads ['oe'], finder=no -> not a macos desktop
#
# and the last frame is a VoiceOver Tutorial sheet - "The VoiceOver Modifier", 1 of 11, a
# "Practice the VoiceOver modifier" hint in the corner - filling the screen over a machine that
# had logged in successfully. The password was right; the desktop was there; a window was on top
# of it. The stage threw away a working disk for it.
#
# VOICEOVER IS ENABLED BY THE KEYBOARD ASSISTANT. The dialogs are the same class of problem
# (lib/steps.py's own note about the Keyboard Setup Assistant eating the next keystroke), and
# they arrive for the same reason: this guest has no Apple keyboard, macOS asks what to do about
# that, and one of the answers turns on an accessibility overlay that then teaches itself.
#
# Escape is the documented way out of the tutorial (`Press the escape key to hear how to set up
# your Mac with VoiceOver.` is printed on the sheet itself), and Command-Q closes the app. Both
# are sent, because the sheet answers Escape and a full app would not.
VOICEOVER_KEYS = ("esc", "meta_l+q", "esc")


def dismiss_post_login_windows(driver, step) -> bool:
    """Close the overlays macOS puts up on a first login, before judging the desktop.

    THE JUDGEMENT IS `wait_for_desktop`, ONE STEP LATER, and it is a test on the menu bar. This
    step exists so that test is about the machine rather than about which window happens to be
    on top of it.

    THE CLICKS ARE HARMLESS ON A CLEAN DESKTOP: they land on wallpaper, which is what the
    Keyboard Setup Assistant's Quit has always relied on (see ssh.py's three-click note - one
    click can arrive before the dialog is drawn).
    """
    import qmp as qmpmod

    q = qmpmod.QMP(driver.qmp_path)
    try:
        # THE KEYBOARD ASSISTANT FIRST. It is the one that ENABLES the VoiceOver tutorial, so
        # closing it first is what stops a second sheet from arriving behind the first.
        import ssh as sshstage

        sshstage.dismiss_keyboard_assistant(_Guest(driver.qmp_path), q=q)

        # AND THEN WHATEVER ACCESSIBILITY SHEET IS ON TOP. Not conditional on recognising it:
        # a keystroke on a desktop with no sheet is a keystroke, and the price of guessing is
        # one lost keystroke while the price of not guessing is the whole stage.
        for one in VOICEOVER_KEYS:
            driver.log(f"  sending {one!r} to close a post-login overlay")
            # `key_combo` TAKES THE KEYS AS SEPARATE ARGUMENTS - it builds the chord from held
            # state (modifiers down, key down/up, modifiers up in reverse). Passing the string
            # "meta_l+q" would ask QMP for a keycode that does not exist.
            if "+" in one:
                qmpmod.key_combo(q, *one.split("+"))
            else:
                qmpmod.key(q, one)
            time.sleep(2)
    finally:
        q.close()
    driver.look("post-login-windows-dismissed")
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

    # A WINDOW CAN ARRIVE AFTER THE DISMISSER RAN, AND DID.
    #
    # `dismiss the overlays macOS puts up on a first login` runs before this step, and on run
    # 42831281414 the VoiceOver tutorial was still on screen 300 seconds later - either because
    # it arrived late or because the sheet ignores a keystroke that lands while it is drawing.
    # Waiting the full budget on it is a stage that fails over a machine that is logged in, so
    # the wait retries the dismissal at intervals and says it is doing so.
    import qmp as qmpmod

    retry_every = 60.0
    next_retry = started + retry_every
    q = None
    try:
        while time.time() - started < ceiling:
            # THE RECORDING, NOT `shots/`. `shots/` is where the driver leaves the handful of
            # pictures it takes around its own clicks; the sampler writes frame-NNNN.* one per
            # second for the whole run, and that is the sequence that has this boot's desktop in.
            if desktop.reached_desktop(frames, kind="macos"):
                driver.log(f"a macOS desktop is on screen ({time.time() - started:.0f}s)")
                return True

            if time.time() >= next_retry:
                next_retry = time.time() + retry_every
                driver.log(f"  still not a desktop after {time.time() - started:.0f}s; "
                           f"closing anything on top of it and looking again")
                try:
                    q = q or qmpmod.QMP(driver.qmp_path)
                    for one in VOICEOVER_KEYS:
                        if "+" in one:
                            qmpmod.key_combo(q, *one.split("+"))
                        else:
                            qmpmod.key(q, one)
                        time.sleep(1)
                except (OSError, RuntimeError) as exc:      # noqa: BLE001
                    driver.log(f"  the dismissal keys could not be sent: {exc}")
                    q = None
            time.sleep(5)
    finally:
        if q is not None:
            try:
                q.close()
            except Exception:                            # noqa: BLE001
                pass
    driver.log("=== NO DESKTOP AFTER LOGGING IN ===")
    driver.log("  the password was typed and the screen still is not a desktop; check the")
    driver.log("  recording - the login window shakes its field when the password is wrong")
    # AND SAY WHAT THE SCREEN IS, BECAUSE "not a desktop" IS NOT A DIAGNOSIS.
    #
    # Run 42831281414's log said only "no desktop", and the answer was on the last frame: a
    # VoiceOver Tutorial sheet over a logged-in machine. Reading the menu bar here makes the
    # log say which of the two failures it was - a window on top of a desktop, or a machine
    # that never got past the login window.
    from lib import desktop as deskmod

    newest = deskmod.newest_frame(frames)
    if newest:
        driver.log(f"  the newest frame is {newest}")
        deskmod.is_macos_desktop(newest, explain=True)
    return False
