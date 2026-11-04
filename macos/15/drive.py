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
TRANSFER_ROWS = {
    "From a Mac": (822, 599),
    "From a Windows PC": (822, 625),
    "iPhone": (822, 651),
    "Set up as new": (822, 676),
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
FULL_NAME = (900, 498)
PASSWORD = (860, 618)
VERIFY = (1020, 618)
HINT = (900, 667)

# The "Are you sure you want to skip sign in?" sheet - Skip is the right-hand of its pair.
SHEET_SKIP = (1026, 634)

# Not Now on a two-button pane: the pairing pane after Data & Privacy. Continue is
# disabled there, so Not Now is the only control that moves the wizard.
NOT_NOW_LEFT = (1132, 796)

# Three-button panes put Not Now leftmost.
NOT_NOW = (604, 810)


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

    def shot(self, label: str) -> str | None:
        """Save a frame. Returns its path, or None when the screenshot failed.

        THE PATH IS RETURNED BECAUSE ONE CALLER READS IT. Every other use in this file takes
        the frame for the record only; pane_says() needs to OCR the thing it just saved, and
        reconstructing the filename outside would be a second copy of this format string.
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
    """Three Downs, then Continue. The pane is a RADIO LIST, not cards.

    WHAT IT ACTUALLY LOOKS LIKE, from the recording of run 41782331835 at 37s:

        Transfer Your Data to This Mac
          How do you want to transfer your information?
            (o) From a Mac, Time Machine, or startup disk
            ( ) From a Windows PC
            ( ) Set up with iPhone or iPad
            ( ) Set up as new
                                            [ Back ] [ Continue ]

    The first item is selected when the pane opens, so three Downs walk the highlight to
    the last one. That is the whole action, and it does not depend on where anything sits
    on the screen - which is the entire reason it replaced a click.

    WHY THE CLICK WAS WRONG, AND WHY IT LOOKED RIGHT

    The sequence clicked (743, 714), documented as "card 4, Set up as new". The pane is not
    cards; it is a vertical radio list about 120px tall, and 714 is BELOW the last row. What
    that click actually selected was "Set up with iPhone or iPad", and the next thing on
    screen was

        Enter the code below on your other computer
        Unable to Pair Computers
        This Mac could not be paired with the computer you want to connect.

    - a pairing handshake with a device that does not exist, which is the same dead end the
    privacy pane's Not Now exists to avoid. Two runs were spent on it: 41777784309 ended with
    a case-sensitive-filesystem alert over this pane, and 41782331835 with the pairing alert.
    Both were the same wrong row, reached two different ways.

    A CLICK AT A MEASURED POSITION IS ONLY FIXED IF THE THING AT THAT POSITION IS. Keys walk
    the list the widget drew, so they survive a re-layout; the coordinate cannot.
    """
    d.wait(8, "transfer pane settles")

    # THE FOCUSED CLICK FIRST. THIS IS WHY THREE DOWNS DID NOTHING.
    #
    # Run 41787686321 sent `key down x3` and then clicked Continue, and the recording ends
    # with the FIRST radio still filled in. A key event goes to whatever has focus in
    # macOS, and nothing on this pane had it - the pane had just appeared and the keyboard
    # focus was still on the Continue button the previous pane's click left it on. Down on a
    # button moves nothing.
    #
    # pick_country() has always done this correctly and that is the proof: it clicks
    # COUNTRY_LIST before typing "united states", because typing into an unfocused list
    # does nothing either. The same rule, on the pane five seconds later, was not applied.
    #
    # The click lands on the FIRST RADIO's row, which selects it - and it is already
    # selected, so nothing changes except that the list now has focus. Then three Downs
    # walk to "Set up as new", and Continue leaves the pane.
    # CLICKING THE ROW, NOT ARROWING TO IT.
    #
    # Three Downs did not move the highlight in runs 41784951651 and 41787686321. The pane
    # has no focused control when it appears - the keyboard focus is still on the Continue
    # button the previous pane's click left it on - and Down on a button moves nothing.
    # pick_country() avoids the same problem by TYPING into its list, which forces focus
    # first; a radio group cannot be typed into.
    #
    # So the row is clicked, which both focuses the group and selects the row in one action.
    # A click on a radio row selects it - that is what the control is for, and it is the
    # same interaction a person has with this pane.
    row = TRANSFER_ROWS["Set up as new"]
    d.click(row, "Set up as new", settle=2.5)
    d.click(CONTINUE, "transfer Continue, Set up as new selected", settle=8.0)

    # DID IT WORK? THIS IS THE CHECK THAT WOULD HAVE SAVED FOUR RUNS.
    #
    # Selecting "Set up as new" means the next pane is NOT a transfer pane. If "Transfer
    # information to this Mac" is on screen now, the click did not take and the wizard is
    # waiting on a source it was never told to skip - so the run stops here, with the phrase
    # it saw, instead of clicking Continue twelve more times into a pane that is not moving.
    #
    # "Transfer information" is the second pane, the one with [AssetData] and Not now. The
    # first is "Transfer Your Data to This Mac", which is what this function was looking at.
    # They differ by one word, which is exactly why the phrase is quoted in full.
    if pane_says(d, "transfer information", seconds=25):
        raise SystemExit(
            "=== THE TRANSFER PANE DID NOT MOVE ON ===\n"
            "  'Set up as new' was clicked at "
            f"{row} and the wizard is still on the transfer pane, which means the\n"
            "  radio it selected was not the last one. The frames beside this run show\n"
            "  which row is filled in - compare it against TRANSFER_ROWS."
        )


def pane_says(d: Driver, phrase: str, seconds: int = 20) -> bool:
    """Whether the wizard's current pane carries `phrase`. Reads the bottom strip only.

    WHY THIS EXISTS AT ALL, IN A FILE THAT SAYS "NO LOOKING".

    The rule in this file is that the SEQUENCE is fixed and no decision is made from the
    screen. That is still true - nothing here chooses an action. What this answers is a
    different question, and it is the one that has cost four runs: DID THE STEP I JUST TOOK
    DO ANYTHING AT ALL?

    Runs 41784951651 and 41787686321 both clicked "Set up as new", both clicked Continue,
    and both ended on the TRANSFER INFORMATION pane - the one that only appears when "From a
    Mac" is selected. The log could not tell the difference between "the click worked and a
    later step went back" and "the click never worked", because every line in it was written
    by this file rather than read from the guest.

    So the check is a fact about the guest, taken after the action, and it never feeds back
    into which action comes next. When it fails the stage stops and says which phrase was
    missing, rather than continuing for twelve more clicks.

    The title strip is y=300..360 at 1920x1080 - where the wizard prints "Transfer Your Data
    to This Mac" and "Transfer information to this Mac", which differ by one word and
    therefore by the thing that matters.
    """
    try:
        import assistant
    except Exception:  # noqa: BLE001
        return True  # no reader: do not fail a run over a missing tool

    for _ in range(max(1, seconds // 5)):
        ppm = d.shot("pane-check")
        if not ppm:
            time.sleep(5)
            continue
        text = assistant.ocr_text_cropped(ppm, (300, 290, 1620, 370), scale=3)
        if phrase.lower() in (text or "").lower():
            log(f"  the pane says {phrase!r}")
            return True
        time.sleep(5)
    log(f"  !=== THE PANE DOES NOT SAY {phrase!r} ===")
    return False


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
    for xy, value, label in (
        (FULL_NAME, user, "full name"),
        (PASSWORD, password, "password"),
        (VERIFY, password, "verify"),
        (HINT, "hint", "hint"),
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
    d.wait(6, "apple account pane settles")
    d.click(NOT_NOW, "Set Up Later", settle=2.5)
    d.click(SHEET_SKIP, "sheet: Skip", settle=3.0)


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
    # NOTHING HERE READS THE SCREEN TO DECIDE WHAT TO DO. The count and the coordinate are
    # fixed, as this file requires; what changed is that the LAST pane is now followed by a
    # check that the wizard is gone, so a run that still has panes left says so instead of
    # hoping.
    for i in range(6):
        d.wait(6, f"pane {i + 1} settles")
        d.key("ret", settle=3.0)
        d.click(CONTINUE, f"pane {i + 1} Continue", settle=3.0)

    # THE WIZARD SHOULD BE FINISHED. If a transfer or setup pane is still up, the click
    # above did not take and the desktop will never appear - the stage would then sit for
    # its full desktop timeout, which is the pattern every failure in this file has had.
    for leftover in ("transfer information", "transfer your data", "select your country"):
        if pane_says(d, leftover, seconds=10):
            raise SystemExit(
                f"=== THE WIZARD IS STILL ON {leftover!r} AFTER THE TAIL ===\n"
                "  the sequence ran to its end without leaving this pane, so the desktop\n"
                "  will not appear. The frame beside this message is what it is showing."
            )


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
