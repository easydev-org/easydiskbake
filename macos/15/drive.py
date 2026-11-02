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
UNITED_STATES = (764, 615)

# Transfer pane, four cards. The fourth is "Set up as new", and it is what keeps this
# Mac off the transfer path. Left-aligned at x=743 where the radio marks sit.
SET_UP_AS_NEW = (743, 714)

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
        leaf = f"mac-{self.shots:03d}-{safe}.ppm"
        try:
            self.q.execute("screendump", filename=os.path.join(self.frames, leaf))
        except (OSError, RuntimeError) as exc:
            log(f"screendump failed at {label}: {exc}")

    def click(self, xy: tuple[int, int], why: str = "", settle: float = 1.5) -> None:
        self.n += 1
        log(f"#{self.n} click {xy} {why}")
        qmpmod.click(self.q, *xy)
        time.sleep(settle)
        self.shot(f"after-{self.n:02d}-{why}")

    def key(self, name: str, times: int = 1, settle: float = 0.5) -> None:
        self.n += 1
        log(f"#{self.n} key {name} x{times}")
        for _ in range(times):
            qmpmod.key(self.q, name)
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
    d.key("pgdn", times=5, settle=0.8)
    d.key("pgup", settle=0.8)
    d.click(UNITED_STATES, "the United States row")
    d.key("ret", settle=2.0)
    # THE COUNTRY PANE'S EXIT IS THIS CONTINUE, NOT THE ROW CLICK. After the row click and
    # Return the pane is still the country list; it only becomes the transfer pane after
    # this click, which is why it belongs to this step.
    d.click(CONTINUE, "country Continue", settle=8.0)


def set_up_as_new(d: Driver) -> None:
    d.wait(8, "transfer pane settles")
    # Card 4 directly. Three Downs also work, but a click at the measured position needs
    # nothing else to be true: it selects the row it lands on.
    d.click(SET_UP_AS_NEW, "card 4, Set up as new", settle=2.5)
    d.click(CONTINUE, "transfer Continue", settle=8.0)


def languages(d: Driver) -> None:
    d.wait(8)
    d.click(CONTINUE, "languages Continue", settle=8.0)


def accessibility(d: Driver) -> None:
    d.wait(8)
    d.key("ret", settle=1.5)
    d.click(CONTINUE, "accessibility Continue", settle=8.0)


def privacy_then_not_now(d: Driver) -> None:
    """Data & Privacy, then the pane it opens: Not Now, never Continue.

    Continue there leads to a pairing handshake with a Mac that does not exist - thirteen
    seconds and a failure alert that covers the way out. Not Now is pressed straight after
    Continue so it lands while the pane is up.
    """
    d.wait(8)
    d.click(CONTINUE, "privacy Continue", settle=2.0)
    d.click(NOT_NOW_LEFT, "Not Now on the pairing pane", settle=2.5)
    d.click(NOT_NOW_LEFT, "Not Now again, in case the first was early", settle=2.5)


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
        d.click(xy, label, settle=1.0)
        d.clear_field()
        d.keys(value, per_char=0.4)
        d.wait(0.8, "let the field settle")
    d.click(CONTINUE, "account Continue", settle=3.0)


def apple_account(d: Driver) -> None:
    d.wait(6, "apple account pane settles")
    d.click(NOT_NOW, "Set Up Later")
    d.click(SHEET_SKIP, "sheet: Skip", settle=2.5)


def tail(d: Driver) -> None:
    """After the account: Continue at the usual place, with a Return before each.

    Location services, time zone, analytics, Siri, FileVault, appearance and the finishing
    panes all take the same action. The window is fixed, so this is a count, not a loop over
    anything observed.
    """
    for i in range(12):
        d.wait(5, f"pane {i + 1} settles")
        d.key("ret", settle=2.0)
        d.click(CONTINUE, f"pane {i + 1} Continue", settle=2.5)


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
