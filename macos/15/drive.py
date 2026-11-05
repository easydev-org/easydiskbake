"""Drive macOS 15's Setup Assistant with a fixed sequence of clicks and keys.

    python3 macos/15/drive.py --qmp /tmp/run/drive.sock.qmp --frames out/frames

NO LOOKING. This module never takes a screenshot to decide anything, never reads a pixel,
never asks the screen a question. Every action is at a recorded position after a recorded
delay, and the flow is the same on every boot because the wizard's geometry is.

WHY FIXED AND NOT OCR, EVEN THOUGH 15 IS THE RELEASE OCR WORKED ON

dockur-macos drove 14 and 15 by reading the screen, and wrote a coordinate-only replacement
anyway. Its own reasoning is the argument:

    the pane right after Data & Privacy offers Not Now and a greyed Continue, and Continue
    starts a pairing handshake that waits thirteen seconds and fails. The Not Now that avoids
    it exists for about two seconds, so no screenshot-and-decide loop can act inside that
    window.

    both pills are grey-on-grey or white-on-white, and tesseract returns nothing for either

    every "look first" added a way to be wrong: a Tab walk that reached the power controls and
    shut the machine down, a Down that moved the highlight off the target, a colour scan that
    accepted grey and missed the white enabled button

A fixed sequence has one failure mode - being wrong about a position - and that is found on
the first run and fixed by changing one number. A reading driver has a failure mode per pane
and per wallpaper.

WHAT IT ASSUMES

    1920x1080 guest display          the coordinates below are measured at this size
    the wizard reaches Setup Assistant exactly once, about two minutes after boot

Both are facts about a cold boot of a recovery image, which is what this bakes.

WHERE THE COORDINATES COME FROM

dockur-macos's scripts/drive-fixed.py, which measured every one from a captured frame. Each
carries the frame it came from in a comment. They are not derived from a screenshot here,
because there is no macOS guest on the machine this was written on - and a coordinate that
looks plausible is indistinguishable from one that is right until it is tried.
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
    print(f"[macos-drive {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- measured positions, all at 1920x1080 -------------------------------------------
#
# The wizard's forward button, in the same place on every pane of every release.
CONTINUE = (1306, 810)

# Country pane: an empty part of the list to give it focus, then the United States row.
# The list selects nothing on a click; typing jumps by first letter and comes to rest
# near "T", five Page Downs put the U section on screen, and the row is committed by
# clicking it and pressing Return. Return alone does nothing, and a Tab walk from here
# reaches the wizard's power controls - which shut the machine down twice before that
# was understood.
COUNTRY_LIST = (900, 500)

# THE TRANSFER PANE'S RADIO ROWS, CONVERTED CORRECTLY FROM THE RECORDING.
#
# THE RECORDING IS 1280x800 AND THE GUEST IS 1920x1080, and the two factors are NOT the same:
#
#     x   1920 / 1280 = 1.5
#     y   1080 /  800 = 1.35
#
# The first version of this used 1.5 for both, so the row it aimed at was 10px off per row
# and the click landed on "Set up with iPhone or iPad" instead of the row above it. That is
# the third time a coordinate in this file has been wrong in a way that selected a
# neighbouring row, which is why the numbers below name the factor they came from.
#
#     From a Mac, Time Machine, or startup disk   y=444 -> 599
#     From a Windows PC                           y=463 -> 625
#     Set up with iPhone or iPad                  y=482 -> 651
#     Set up as new                               y=501 -> 676
#
# x=548 is the radio column, and 548 * 1.5 = 822.
# THE RADIO CIRCLES THEMSELVES, not the text beside them, and these are the numbers that
# were confirmed by eye against a 1920x1080 grid built from this run's own frame.
#
#     (825, 597)  From a Mac, Time Machine, or startup disk
#     (825, 621)  From a Windows PC
#     (825, 645)  Set up with iPhone or iPad
#     (825, 669)  Set up as new
#
# MEASURED BY SCANNING THE FRAME FOR THE CIRCLES, not by eye. In the recording - which is
# 1280x800, the GUEST's actual resolution, not the 1920x1080 the plist asks for - the four
# circles span these rows at x=543..556:
#
#     y=434..450   From a Mac          centre 442
#     y=455..465   From a Windows PC   centre 460
#     y=472..483   Set up with iPhone  centre 478
#     y=490..501   Set up as new       centre 496
#
# and 496 * 1080/800 = 669. The previous value, 679, was ten pixels low - which is more than
# half a circle's diameter, so the click landed below the control and the first row stayed
# selected. That is the same failure as the very first attempt with (743, 714), one decimal
# place smaller.
#
# WHY THE CIRCLE AND NOT THE LABEL. A macOS radio button is about 16px across, and the click
# has to land on it - clicking the words selects nothing, which is what made an earlier
# attempt look like it had worked while the first row stayed filled in. The label starts at
# x≈845 and is not part of the control.
TRANSFER_ROWS = {
    "From a Mac": (825, 597),
    "From a Windows PC": (825, 621),
    "iPhone": (825, 645),
    "Set up as new": (825, 669),
}
FIRST_RADIO = TRANSFER_ROWS["From a Mac"]
UNITED_STATES = (764, 615)

# THE TRANSFER PANE HAS NO COORDINATE HERE, DELIBERATELY.
#
# It is a vertical radio list - "From a Mac, Time Machine, or startup disk", "From a Windows
# PC", "Set up with iPhone or iPad", "Set up as new" - with the first row selected when the
# pane opens. set_up_as_new() sends three Downs and presses Continue, because keys walk the
# list the widget drew and a coordinate does not.
#
# The coordinate this replaced was (743, 714), documented as "the fourth card". The pane is
# about 120px tall and 714 is below its last row, so the click selected "Set up with iPhone
# or iPad" and the wizard opened a pairing handshake with a device that does not exist. Two
# runs ended there: 41777784309 under a case-sensitive-filesystem alert, 41782331835 under
# "Unable to Pair Computers". Same wrong row, two different alerts.

# The account form. OCR gives the LABELS, which sit 20px above their fields; the values
# below are the field boxes, measured from a vertical scan at x=900 - each is a band of
# white between two (243,243,243) dividers:
#
#     y=486..510   Full Name field
#     y=520..544   Account Name field
#     y=614..622   Password fields
#     y=658..      Hint field
#
# The first version of dockur's table was 38-68px too high, so every click landed on the
# label above the box it meant and the full name received "hi" instead of "docker".
# MEASURED FROM THE RECORDING, WRITTEN IN 1920x1080 UNITS.
#
# The recording is 1280x800 - the guest's actual resolution - and the form sits at these rows
# in it:
#
#     Full name      (630, 331)
#     Account name   (630, 353)      <- filled from Full name by macOS itself
#     Password       (567, 393)
#     Verify         (724, 393)
#     Hint           (650, 412)      <- not used: optional, and the wizard accepts it empty
#
# converted by x*1.5, y*1.35, which is what these constants are.
#
# THE PREVIOUS VALUES WERE 66 TO 111 PIXELS LOW, and that is why every field ended up holding
# the same word: run 41799999986 typed docker, admin, admin, hint and the frame shows "hint" in
# Full name, empty Password, and "hint" in Hint - four clicks that all landed on or below the
# last field, so each one typed into the field the one before had already focused.
FULL_NAME = (945, 447)
PASSWORD = (850, 531)
VERIFY = (1086, 531)

# The "Are you sure you want to skip sign in?" sheet - Skip is the right-hand of its pair.
SHEET_SKIP = (1026, 634)

# Not Now on a two-button pane: the pairing pane after Data & Privacy. Continue is
# disabled there, so Not Now is the only control that moves the wizard.
NOT_NOW_LEFT = (1132, 796)

# THE LICENCE SHEET'S AGREE, MEASURED FROM ITS OWN FRAME.
#
# The Terms and Conditions pane puts a modal sheet over a long document, and the sheet has its
# own pair:
#
#     (875..928, 610)   Disagree
#     (1001..1037, 610) Agree        <- this, centre (1019, 616)
#
# The pane's own Agree is at the bottom right, behind the sheet. Clicking it while the sheet is
# up does nothing - which is what run 41816298797 did six times, and its last frame is the sheet
# still there with the pointer on the button underneath.
LICENCE_AGREE = (1019, 616)

# THE APPLE ACCOUNT PANE'S "Set Up Later", MEASURED FROM ITS OWN FRAME.
#
# The link's blue text spans x=389..437 at y=601 in the 1280x800 recording, so its centre is
# 413 - which is 620 in the 1920x1080 units these constants use. The old value, 604, mapped to
# 403: inside the text, but three pixels from its left edge, and the click did nothing.
NOT_NOW = (620, 811)


class Driver:
    """The click/key/type vocabulary, with a frame saved after each action.

    Frames are RECORDED, never read. Nothing here inspects them; they exist so a run that
    goes wrong can be looked at afterwards, step by step, which is how every coordinate
    above was found in the first place.
    """

    def __init__(self, qmp_path: str, frames_dir: str | None = None):
        # THE SOCKET IS WAITED FOR, NOT ASSUMED.
        #
        # vm.Guest.start() returns as soon as the process exists; qemu binds its sockets a
        # moment later. Connecting immediately raises
        #
        #     FileNotFoundError: [Errno 2] No such file or directory
        #     self.sock.connect(path)
        #
        # which is how run 41775498755's desktop stage failed - after its pull and its
        # boot assets had both succeeded, so the run looked late-broken when it was a race
        # at the very first line of the driver. windows/11/ssh.py waits for the same reason;
        # this is that wait.
        deadline = time.time() + 120
        while not os.path.exists(qmp_path):
            if time.time() > deadline:
                raise SystemExit(
                    f"no QMP socket at {qmp_path} after 120s; qemu either failed to start "
                    f"or was given a different work directory"
                )
            time.sleep(1)
        self.q = qmpmod.QMP(qmp_path)
        self.frames = frames_dir
        self.n = 0
        self.shots = 0

    def wait(self, seconds: float, why: str = "") -> None:
        log(f"wait {seconds:g}s" + (f" - {why}" if why else ""))
        time.sleep(seconds)

    def wait_for(self, ready, what: str, *, timeout: float, interval: float = 5.0,
                 note=None) -> str | None:
        """Poll the screen until `ready(path)` returns something truthy, and return it.

        A FIXED SLEEP IS A GUESS ABOUT SOMEBODY ELSE'S MACHINE, and this sequence already
        paid for two of them. The recovery menu was given 60 seconds (run 41480111851),
        then 120 (run 41498618924), then 240 - and the third number was chosen the same way
        as the first two, by adding margin to the slowest boot that had been measured. Run
        41992924955 was still on the Apple logo with its progress bar at 8% when the 240
        expired on a slower runner, so the sequence clicked into a boot screen, the click
        landed on whatever row was there, and everything after it described the wrong
        window.

        WHAT THIS DOES INSTEAD. It takes a predicate that reads a frame and answers a
        question about it, and it polls until that answer is true or the timeout expires.
        The timeout becomes a backstop for "this will never happen" rather than the
        mechanism for "wait until this happens", so a slow machine costs a slow run and not
        a wrong one. `ready` gets the path to a freshly saved frame; it returns whatever it
        found, which is returned to the caller - for OCR that is the text, for a pixel test
        it can be True.

        THE FRAMES ARE ALSO KEPT. Every poll calls shot(), so the recording shows what the
        wait saw. That is what turned 41992924955's Apple-logo frame into a diagnosis
        instead of another guess about a number.

        `note` is called with each miss, so a log reader can tell "still booting" from
        "the picker never came up" without waiting for the deadline.
        """
        started = time.time()
        deadline = started + timeout
        attempt = 0
        last = None
        while time.time() < deadline:
            attempt += 1
            path = self.shot(f"wait-{attempt:02d}-{what}")
            if path:
                try:
                    last = ready(path)
                except Exception as exc:            # noqa: BLE001 - a read, not a verdict
                    log(f"  {what}: the check raised {exc}")
                    last = None
                if last:
                    # ELAPSED SINCE THE POLLING STARTED, not `timeout - remaining`: those
                    # are equal only when the deadline has been reached, so the first
                    # version logged "seen after 0s" for every success it ever had.
                    log(f"  {what}: seen after {time.time() - started:.0f}s "
                        f"(attempt {attempt})")
                    return last
                if note:
                    note(path)
            time.sleep(interval)
        log(f"  {what}: NOT seen within {timeout:.0f}s ({attempt} attempts)")
        return None

    def shot(self, label: str) -> str | None:
        """Save a frame. Returns its path, or None when the screenshot failed.

        THE PATH IS RETURNED SO A CALLER COULD READ IT. Nothing does today - every frame here
        is for the record only - but the filename is built here, and a caller that wants the
        image should not have to rebuild it from a second copy of this format string.
        """
        if not self.frames:
            return None
        self.shots += 1
        safe = "".join(c if c.isalnum() else "-" for c in label[:40]).strip("-") or "step"
        path = os.path.join(self.frames, f"mac-{self.shots:03d}-{safe}.ppm")
        try:
            self.q.execute("screendump", filename=path)
        except (OSError, RuntimeError) as exc:
            log(f"screendump failed at {label}: {exc}")
            return None
        return path

    def click(self, xy: tuple[int, int], why: str = "", settle: float = 2.5) -> None:
        """Click, then wait 2.5s by default before the next action.

        THE WAIT IS PART OF THE ACTION. A wizard pane that has just been dismissed is still
        animating under the pointer, and a click sent into that animation lands on the pane
        that is leaving. The previous default was 1.5s and several callers passed 1.0; the
        frames after each action in run 41784951651 show the pane half-faded in more than one
        of them.
        """
        self.n += 1
        log(f"#{self.n} click {xy} {why}")
        qmpmod.click(self.q, *xy)
        time.sleep(settle)
        self.shot(f"after-{self.n:02d}-{why}")

    def key(self, name: str, times: int = 1, settle: float = 2.0) -> None:
        """Press one key `times` times, waiting `settle` seconds after EACH press.

        THE WAIT IS BETWEEN PRESSES, NOT ONLY AFTER THE LAST ONE. This looped with
        `time.sleep(settle)` inside it, so it did wait - but every caller passed 0.5 or 1.0,
        and a radio list that redraws on each arrow key saw the next one before it had. Three
        Downs in quick succession are indistinguishable from one Down for a widget that
        coalesces key repeats, and the pane then advances with the first row still selected.

        Two seconds, and the default is the point: a caller that passes nothing gets the slow
        version rather than the fast one. `d.key("down", times=3)` is the only use where the
        spacing decides whether the wizard moves at all.
        """
        self.n += 1
        log(f"#{self.n} key {name} x{times}, {settle:g}s between")
        for i in range(times):
            qmpmod.key(self.q, name)
            if i < times - 1:
                time.sleep(settle)
        # One more wait after the last press, so the caller's next action is not racing it.
        time.sleep(settle)

    def keys(self, text: str, per_char: float = 0.3) -> None:
        """Type one character at a time.

        0.3s: at 0.15 the guest dropped them, which on the country list showed up as the
        jump landing on T instead of U.
        """
        self.n += 1
        log(f"#{self.n} type {text!r}")
        for ch in text:
            qmpmod.key(self.q, "spc" if ch == " " else ch)
            time.sleep(per_char)

    def clear_field(self) -> None:
        """Empty the focused field before typing into it.

        Appending produced "dockerdocker" in an earlier run: Cmd-A and one backspace does
        not always take on these fields, so the sequence is Cmd-A, backspace, twice, then a
        run of backspaces at 0.12s - fast enough and slow enough to be seen by a field that
        validates as it is edited.
        """
        self.n += 1
        log(f"#{self.n} clear field")
        for _ in range(2):
            qmpmod.key_combo(self.q, "meta_l", "a")
            time.sleep(0.5)
            qmpmod.key(self.q, "backspace")
            time.sleep(0.4)
        for _ in range(24):
            qmpmod.key(self.q, "backspace")
            time.sleep(0.12)
        time.sleep(0.8)

    def close(self) -> None:
        try:
            self.q.close()
        except OSError:
            pass


# --- the sequence -------------------------------------------------------------------


def pick_country(d: Driver) -> None:
    d.wait(30, "let the country pane finish drawing")
    d.click(COUNTRY_LIST, "focus the country list")
    d.keys("united states")
    d.key("pgdn", times=5, settle=2.0)
    d.key("pgup", settle=2.0)
    d.click(UNITED_STATES, "the United States row")
    d.key("ret", settle=3.0)
    # THE COUNTRY PANE'S EXIT IS THIS CONTINUE, NOT THE ROW CLICK. After the row click and
    # Return the pane is still the country list; it only becomes the transfer pane after
    # this click, which is why it belongs to this step.
    d.click(CONTINUE, "country Continue", settle=8.0)


def set_up_as_new(d: Driver) -> None:
    """Focus the radio list, then THREE DOWNS. The row is never clicked directly.

    THE PANE, from the recording of run 41782331835 at 37s:

        Transfer Your Data to This Mac
          How do you want to transfer your information?
            (o) From a Mac, Time Machine, or startup disk
            ( ) From a Windows PC
            ( ) Set up with iPhone or iPad
            ( ) Set up as new
                                            [ Back ] [ Continue ]

    The first row is selected when the pane opens. Three Downs move the selection to the last
    row, which is the whole action.

    WHY A CLICK AT THE LAST ROW WAS WRONG, TWICE

    First (743, 714) was documented as "card 4" and is below the list's last row, so it
    selected "Set up with iPhone or iPad" - which opens a pairing handshake with a device
    that does not exist. Runs 41777784309 and 41782331835 both ended there.

    Then it became a click on a recomputed row position, and that was wrong as well, because
    the conversion from the recording's 1280x800 to the guest's 1920x1080 is not uniform:
    x scales by 1.5 and y by 1.35. A row aimed at with 1.5 for both lands one row off, which
    is the same failure with a different number.

    Keys do not have that problem. They walk the list the widget drew, so the target is
    "three rows down" rather than a position - and a position is wrong every time anything
    above it moves.

    WHY THE LIST HAS TO BE CLICKED FIRST, AND WHY THAT IS NOT THE SAME AS CLICKING THE ROW

    A key event goes to whatever has focus. When this pane appears, nothing in it does: the
    keyboard focus is still on the Continue button the previous pane's click left it on, and
    Down on a button moves nothing. That is why runs 41784951651 and 41787686321 sent three
    Downs that moved nothing at all.

    pick_country() has always done it correctly and is the proof: it clicks COUNTRY_LIST
    before typing "united states", because typing into an unfocused list does nothing either.

    So the click here lands on the FIRST row, which is ALREADY SELECTED - it changes nothing
    on screen, its only job is to give the list focus. Then three Downs, then Continue.
    """
    d.wait(8, "transfer pane settles")

    # CLICK THE CIRCLE OF THE LAST ROW. Not the label, and not three Downs.
    #
    # Three Downs were tried twice and moved nothing, because no control had keyboard focus
    # when the pane appeared - the focus was still on the Continue button the previous pane's
    # click had left it on. Clicking the circle focuses the group and selects the row in one
    # action, which is what a person does with this pane.
    #
    # The coordinate is the circle itself, confirmed by eye against a 1920x1080 grid built
    # from this run's own frame: see TRANSFER_ROWS. The label starts at x~845 and is not part
    # of the control - a click on the words selects nothing, which is how an earlier attempt
    # looked like it had worked while the first row stayed filled in.
    d.click(TRANSFER_ROWS["Set up as new"], "Set up as new (the radio circle)", settle=2.5)

    # FIVE SECONDS FOR THE PANE TO ACCEPT IT, THEN CONTINUE. No check on what is on screen:
    # the sequence is fixed, and a reading step in the middle of it is the thing this file
    # avoids. What the next pane turns out to be is answered by the recording.
    d.wait(5, "the radio selection registers")
    d.click(CONTINUE, "transfer Continue", settle=8.0)


def languages(d: Driver) -> None:
    d.wait(8)
    d.click(CONTINUE, "languages Continue", settle=8.0)


def accessibility(d: Driver) -> None:
    d.wait(8)
    d.key("ret", settle=3.0)
    d.click(CONTINUE, "accessibility Continue", settle=8.0)


def privacy_then_not_now(d: Driver) -> None:
    """Data & Privacy, then the pane it opens: Not Now, never Continue.

    Continue there leads to a pairing handshake with a Mac that does not exist - thirteen
    seconds and a failure alert that covers the way out. Not Now is pressed straight after
    Continue so it lands while the pane is up.
    """
    d.wait(8)
    d.click(CONTINUE, "privacy Continue", settle=2.0)
    d.click(NOT_NOW_LEFT, "Not Now on the pairing pane", settle=3.0)
    d.click(NOT_NOW_LEFT, "Not Now again, in case the first was early", settle=3.0)


def account(d: Driver, user: str, password: str) -> None:
    """The account form, then Continue.

    ONLY THE SHORT NAME IS LEFT ALONE. macOS fills Account Name from Full Name as you
    type, so clicking into it, clearing it and retyping fights the auto-fill - and a field
    it thinks is invalid is enough for the wizard to refuse with "You haven't provided all
    of the requested information."

    0.4s per character rather than 0.25: this form validates as it is typed, and characters
    arriving faster than it can check get dropped.
    """
    d.wait(6, "account pane settles")
    # THE HINT IS LEFT ALONE, AND IT IS OPTIONAL.
    #
    # The form marks it "(optional)" and the wizard accepts the pane with it empty - measured
    # from run 41809206003's own frames, where the successful account had a hint only because
    # this typed one. Filling it costs a click, a clear, six keystrokes and a two-second
    # settle on a form that validates every keystroke, which is eight seconds of the slowest
    # part of the wizard for a field nobody reads.
    for xy, value, label in (
        (FULL_NAME, user, "full name"),
        (PASSWORD, password, "password"),
        (VERIFY, password, "verify"),
    ):
        # 2.5s after the click into a field, and 2s after typing into it: this form
        # validates on every keystroke, and the next field's click arriving while the
        # previous one is still validating is how "dockerdocker" got into a run.
        d.click(xy, label, settle=2.5)
        d.clear_field()
        d.keys(value, per_char=0.4)
        d.wait(2.0, "let the field settle")
    d.click(CONTINUE, "account Continue", settle=3.0)


def apple_account(d: Driver) -> None:
    """Leave the Apple Account pane without signing in.

    "Set Up Later" is a text link in the bottom-left corner, not a button, and clicking it has
    not been enough: run 41802589394 clicked it at (604, 810) - which maps to (403, 600) on the
    1280x800 screen, inside the 389..437 the words occupy - and the pane never moved. The next
    thirteen actions were spent clicking Continue on a button that is disabled while the email
    field is empty, which is why the recording is 34 seconds of one screen.

    So the click is followed by Return: on this pane Return activates the focused control, and
    the click has just focused it. Two ways to leave, one of which is a key.
    """
    # CREATING THE ACCOUNT TAKES SEVENTY-TWO SECONDS ON THE RUN IT WAS MEASURED ON, AND THAT
    # IS NOT A BOUND. It was measured from run 41809206003's own recording, one frame every
    # two seconds:
    #
    #     316s   "Creating account..."          <- what appears after the form's Continue
    #     390s   "Sign In to Your Apple Account" <- the pane this function needs
    #
    # and the drive log puts that Continue at 10:24:21 against a recording that started at
    # 10:19:03, which is video second 318 - so the gap is 72 seconds on that machine.
    #
    # THE PREVIOUS VALUES WERE 6, THEN 25, THEN 80, AND 80 WAS STILL NOT ENOUGH.
    #
    # Run 41855475794 clicked Continue at 15:32:37 and clicked Set Up Later at 15:34:00 - 80
    # seconds later - and the recording shows the form still spinning. `Set Up Later` only
    # exists on the Apple Account pane; on a form that has not finished it is whatever is
    # underneath, so the click went somewhere else and the wizard came back to the account
    # pane and stayed there. The last frame is "Sign In to Your Apple Account", which is
    # EARLIER than the run before it reached.
    #
    # 180 SECONDS, because the number that matters is not how long it takes but how long it
    # can take: this is a first boot on a shared CI runner with a cold disk, and the account
    # creation writes a home directory, generates keys and talks to the keychain. Waiting too
    # long costs idle seconds and is recoverable; clicking too early throws away the run.
    d.wait(180, "apple account pane: the account is being created")
    d.click(NOT_NOW, "Set Up Later", settle=2.0)
    d.key("ret", settle=3.0)

    # THE SHEET ONLY EXISTS IF THE CLICK WORKED. When the pane has already gone, this
    # coordinate is empty space on the next pane - harmless - and when the sheet is up it is
    # its Skip button. Kept because the sheet does appear on some releases.
    d.click(SHEET_SKIP, "sheet: Skip, if there is one", settle=2.0)

    # AND A LAST RETURN, for the same reason: if the sheet was there, Skip focused it.
    d.key("ret", settle=2.0)


def tail(d: Driver) -> None:
    """After the account: Continue at the usual place, with a Return before each.

    SIX, NOT TWELVE, AND THE TWELVE IS WHY RUN 41784951651 WENT BACKWARDS.

    The wizard's remaining panes - location services, time zone, analytics, Siri, FileVault,
    appearance, then the finish - are four to six on this release. The loop clicked Continue
    twelve times regardless, and once the wizard had nothing left, the same coordinate is
    Back's neighbour in a fixed footer: the recording ends on the TRANSFER pane, fifteen
    panes after the account form, which is the only way to get there from the end without
    walking backwards.

    Counting to the maximum instead of to the measured count is the same mistake as the
    fixed coordinate: it is right only while the thing it counts does not change. Six is
    what this release needs; a pane that needs the seventh will stop visible and say so,
    which is recoverable, where overshooting silently undoes a finished wizard.
    """
    # TWELVE BECAME SIX BECAUSE THE WIZARD HAS FOUR TO SIX PANES LEFT HERE, and clicking past
    # the end lands on Back's neighbour in the fixed footer - which is how run 41784951651
    # ended fifteen panes back on the transfer pane.
    #
    # NOTHING HERE READS THE SCREEN. The count and the coordinate are fixed, and whether the
    # wizard finished is answered by the recording rather than by a check in the middle of
    # the sequence.
    # TEN, NOT SIX. Measured from run 41826976026, which is the first run to get past the
    # licence: the six this used to do ended on "Update Mac Automatically", and the wizard
    # still had Siri, FileVault, the appearance pane and the finish after it. Six was chosen
    # when the sequence never got that far.
    for i in range(10):
        d.wait(6, f"pane {i + 1} settles")
        # THE LICENCE SHEET, IF IT IS UP. It is modal and covers the pane's own Agree, so a
        # click at CONTINUE lands on the sheet's edge and nothing moves. This point is empty
        # space on any pane without a sheet, which is why it costs nothing to do every time.
        d.click(LICENCE_AGREE, f"pane {i + 1}: Agree on the licence sheet, if it is up",
                settle=2.0)
        d.key("ret", settle=2.0)
        d.click(CONTINUE, f"pane {i + 1} Continue", settle=3.0)

    # A LAST LONG WAIT, BECAUSE THE FINISHING PANES WRITE BEFORE THEY DRAW.
    #
    # After the last Continue the wizard creates the account and the login items and then
    # fades to the desktop. Five runs have ended their recording on a wizard pane that was
    # still doing that work, and the frame taken at the end looked exactly like a wizard that
    # never moved. Twenty seconds is what the transition takes when the disk is slow.
    d.wait(20, "the wizard finishes and the desktop appears")


def run_sequence(qmp_path: str, frames_dir: str | None, user: str, password: str) -> None:
    d = Driver(qmp_path, frames_dir)
    log("connected; fixed sequence, no reads")
    try:
        d.wait(120, "guest boots into Setup Assistant")
        pick_country(d)
        set_up_as_new(d)
        languages(d)
        accessibility(d)
        privacy_then_not_now(d)
        account(d, user, password)
        apple_account(d)
        tail(d)
        log(f"sequence complete after {d.n} actions")
    finally:
        d.close()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--qmp", required=True, help="the drive QMP socket")
    p.add_argument("--frames", default=None, help="where to save a frame after each action")
    p.add_argument("--user", default=os.environ.get("MACOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MACOS_PASSWORD", "admin"))
    args = p.parse_args(argv)

    run_sequence(args.qmp, args.frames, args.user, args.password)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
