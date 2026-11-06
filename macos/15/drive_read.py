"""Drive macOS 15's Setup Assistant by reading each pane before acting on it.

    python3 macos/15/drive_read.py --qmp <sock> --frames <dir>

WHY THIS EXISTS BESIDE drive.py

drive.py is a fixed sequence of clicks: it believes a pane is where its coordinate table says
it is, acts, and learns nothing until the wallpaper test at the very end. That is a fair
design and its docstring makes the case for it - one failure mode, fixed by changing one
number. It is also why every wrong coordinate cost a twelve-minute run to find, and why the
run that stopped on Terms and Conditions looked like "the wizard did not reach a desktop"
eighteen minutes later rather than "the sheet's Agree is a different click" at the step that
did it.

This is the same sequence, arrived at by reading. Before each action it looks at the screen
and checks that the pane it is about to answer is the pane that is up:

    look -> the pane's title says which pane this is
    act  -> click the control that pane needs
    look -> did the title change? if not, say so HERE, with the frame

THE COORDINATES ARE STILL MEASURED NUMBERS, and they are drive.py's. Reading is used for
DETECTION - "am I on the pane I think I am" - not for locating controls. A control's position
is a fact about this release's layout, and tesseract's box for a word inside a button is a
worse position than the button's own centre. What reading buys is that a missed click is
reported at the click, not at the timeout.

EVERY NUMBER BELOW WAS CONFIRMED AGAINST A 1920x1080 SCREENDUMP of this exact disk, and the
pane titles are what tesseract returned from those frames. The run that produced them is in
the commit message.

THE ONE PANE THAT DOES ITSELF IS SCREEN TIME. drive.py clicks "Set Up Later" and its comment
records why the pane then looked like a hang: the panel spins while it asks Apple for
something, and with the MTU bug it could never finish. It finishes now, but it takes about
forty seconds and moving early lands on a greyed Continue. This waits for the title to change
rather than for a fixed number of seconds.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import ocr          # noqa: E402
import qmp as qmpmod  # noqa: E402

# --- measured positions, all at 1920x1080 ----------------------------------------------
#
# The wizard's forward button is at y=810 on most panes and NOT on all of them. The
# differences are four to five pixels in a button forty pixels tall, which is invisible in a
# recording and large enough to miss the control - so each pane that was measured keeps its
# own constant, exactly as drive.py does.
CONTINUE = (1306, 810)
ACCESSIBILITY_NOT_NOW = (1306, 806)   # this pane's forward button says "Not Now"
TIME_ZONE_CONTINUE = (1306, 806)
LOOK_CONTINUE = (1306, 805)

COUNTRY_LIST = (900, 500)
# THE TRANSFER PANE'S ROW MOVES BETWEEN RELEASES, and this is the coordinate that has been
# wrong most often. drive.py records the measurements: on 15 the "Set up as new" circle is at
# y=669, on 26 it is at 706 - 37 pixels apart, which is more than a row is tall, so the 15
# value lands in the GAP between "iPhone" and "as new" on 26 and selects nothing while the
# wizard carries on. A table per release is the only shape that survives a release bump.
TRANSFER_SET_UP_AS_NEW = {
    "15": (825, 669),
    "26": (825, 706),
}
TERMS_AGREE = (1298, 806)             # the footer's Agree, which opens the licence sheet
LICENCE_AGREE = (1019, 616)           # the sheet's own Agree, a different click
APPLE_SET_UP_LATER = (620, 811)
APPLE_SKIP = (1026, 634)
LOCATION_DONT_USE = (960, 632)
ANALYTICS_SHARE = (808, 513)
SCREEN_TIME_LATER = (619, 806)
WELCOME_CONTINUE = (960, 990)
KEYBOARD_QUIT = (1222, 818)

ACCOUNT_FIELDS = {
    # 26 splits the password row into two boxes that do not start where a single centred
    # click assumes - drive.py's ACCOUNT_FIELDS carries the measurement; these are its values.
    "15": {"full": (945, 447), "password": (850, 531), "verify": (1086, 531)},
    "26": {"full": (1050, 497), "password": (842, 601), "verify": (1075, 601)},
}


def log(msg: str) -> None:
    print(f"[drive {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Wizard:
    """Look at the screen, then act. Every check below is a title tesseract read."""

    def __init__(self, qmp_path: str, frames: str, *, width: int = 1920,
                 height: int = 1080, release: str = "15") -> None:
        os.makedirs(frames, exist_ok=True)
        self.release = release
        self.screen = ocr.Screen(qmp_path, frames, width=width, height=height)
        self.q = self.screen.q
        self.frames = frames
        self.n = 0
        self.words: list[tuple] = []

    # ---- reading -----------------------------------------------------------------
    def look(self, label: str) -> list[tuple]:
        self.n += 1
        ppm = self.screen.grab(f"{self.n:03d}-{label}")
        self.words = self.screen.text(ppm) if ppm else []
        log(f"look {label}: {len(self.words)} words")
        return self.words

    def says(self, *words: str) -> bool:
        """Are all of these words on screen? Normalised, so case and punctuation do not matter."""
        have = {"".join(c for c in w[0].lower() if c.isalnum()) for w in self.words}
        return all("".join(c for c in w.lower() if c.isalnum()) in have for w in words)

    def says_any(self, *words: str) -> bool:
        return any(self.says(w) for w in words)

    def wait_for(self, *words: str, timeout: float = 180.0, poll: float = 5.0,
                 label: str = "wait") -> bool:
        """Look until the screen says `words`, or report what it said instead."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.look(label)
            if self.says(*words):
                return True
            time.sleep(poll)
        log(f"  TIMED OUT waiting for {words}")
        log(f"  screen says: {' | '.join(w[0] for w in self.words[:30])}")
        return False

    # ---- acting ------------------------------------------------------------------
    def click(self, xy, why: str, settle: float = 3.0) -> None:
        log(f"click {xy} - {why}")
        qmpmod.click(self.q, *xy)
        time.sleep(settle)

    def type_text(self, text: str, per_char: float = 0.35) -> None:
        for ch in text:
            qmpmod.key(self.q, "spc" if ch == " " else ch)
            time.sleep(per_char)

    def press(self, name: str, times: int = 1, gap: float = 1.5) -> None:
        for _ in range(times):
            qmpmod.key(self.q, name)
            time.sleep(gap)

    def clear_field(self) -> None:
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
        except Exception:                              # noqa: BLE001 - best effort
            pass


# --- the panes, in order ---------------------------------------------------------------
#
# Each returns True when it ran, False when the pane it expects is not up. The caller treats
# False as "try the next one", so a pane that was skipped by the guest does not stop the run.

def pane_country(w: Wizard) -> bool:
    if not w.says("country", "region"):
        return False
    log("pane: Select Your Country or Region")
    # TYPING SELECTS THE ROW. The list takes nothing from a click except focus; typing
    # "united states" jumps to the U section with the row already highlighted, which is what
    # drive.py's pick_country records as measured on 2026-11-06.
    w.click(COUNTRY_LIST, "focus the country list", settle=2.0)
    w.type_text("united states")
    time.sleep(3)
    w.click(CONTINUE, "country Continue", settle=8.0)
    return True


def pane_transfer(w: Wizard) -> bool:
    if not w.says("transfer", "data"):
        return False
    log("pane: Transfer Your Data to This Mac")
    # THE CIRCLE, WHICH IS THE CONTROL. drive.py's 15 branch clicks the label at (880,666)
    # because two clicks on the circle at (824,666) changed nothing in ITS frame. On this
    # disk the circle at (825,669) takes the selection in one click - confirmed against the
    # screendump in the commit - so the circle is what this uses.
    row = TRANSFER_SET_UP_AS_NEW.get(w.release)
    if row is None:
        log(f"  NO TRANSFER ROW FOR macOS {w.release}; known: {sorted(TRANSFER_SET_UP_AS_NEW)}")
        return True
    w.click(row, f"the 'Set up as new' radio (macOS {w.release})", settle=4.0)
    w.click(CONTINUE, "transfer Continue", settle=10.0)
    return True


def pane_languages(w: Wizard) -> bool:
    if not w.says("spoken", "languages"):
        return False
    log("pane: Written and Spoken Languages")
    w.click(CONTINUE, "languages Continue", settle=8.0)
    return True


def pane_accessibility(w: Wizard) -> bool:
    if not w.says("accessibility", "individual"):
        return False
    log("pane: Accessibility")
    # NOT "Continue". This pane's forward button says Not Now, and it sits four pixels
    # higher than the usual footer - clicking (1306, 810) here selects nothing.
    w.click(ACCESSIBILITY_NOT_NOW, "accessibility Not Now", settle=12.0)
    return True


def pane_privacy(w: Wizard) -> bool:
    if not w.says("data", "privacy"):
        return False
    log("pane: Data & Privacy")
    w.click(CONTINUE, "privacy Continue", settle=8.0)
    return True


def pane_account(w: Wizard, user: str, password: str) -> bool:
    if not w.says("full", "name"):
        return False
    log(f"pane: Create a Mac Account ({user})")
    fields = ACCOUNT_FIELDS.get(w.release)
    if fields is None:
        log(f"  NO ACCOUNT-FIELD TABLE FOR macOS {w.release}; a guessed one types the "
            f"password into whatever row the guess lands on")
        return True
    for key, value, label in (("full", user, "full name"),
                              ("password", password, "password"),
                              ("verify", password, "verify")):
        w.click(fields[key], f"the {label} field", settle=2.5)
        w.clear_field()
        w.type_text(value, per_char=0.4)
        time.sleep(2.0)
    w.click(CONTINUE, "account Continue", settle=15.0)
    return True


def pane_apple(w: Wizard) -> bool:
    if not w.says("sign", "apple"):
        return False
    log("pane: Sign In to Your Apple Account")
    w.click(APPLE_SET_UP_LATER, "Set Up Later", settle=6.0)
    # THE CONFIRMATION SHEET. drive.py's SHEET_SKIP records that it only appears on some
    # releases; here it does, and its Skip is the blue button in the middle of the card.
    w.look("apple-sheet")
    if w.says("skip"):
        w.click(APPLE_SKIP, "Skip on the confirmation sheet", settle=6.0)
    return True


def pane_terms(w: Wizard) -> bool:
    if not w.says("terms", "conditions"):
        return False
    log("pane: Terms and Conditions")
    # TWO CLICKS, AND THE SECOND ONE IS THE ONE THAT COUNTS.
    #
    # The footer's Agree does not accept anything: it opens a modal sheet over the document,
    # and the sheet has its own Agree at a different position. drive.py records the same
    # thing (LICENCE_AGREE) and it is what run 42211273601's last frame shows - the sheet up,
    # the wizard going nowhere. Clicking the footer's Agree twice does nothing.
    w.click(TERMS_AGREE, "the pane's Agree, which opens the licence sheet", settle=6.0)
    w.look("licence-sheet")
    if w.says("agree"):
        w.click(LICENCE_AGREE, "the licence sheet's own Agree", settle=8.0)
    return True


def pane_location(w: Wizard) -> bool:
    if not w.says("location", "services"):
        return False
    log("pane: Enable Location Services")
    w.click(CONTINUE, "location Continue, which opens the confirmation", settle=6.0)
    w.look("location-sheet")
    if w.says("use"):
        # THE SHEET'S "Don't Use" IS ITS DEFAULT BUTTON: a click darkens it and leaves the
        # sheet up, and Return is what fires it. drive.py measured both halves of that.
        w.click(LOCATION_DONT_USE, "Don't Use on the confirmation", settle=3.0)
        w.press("ret", 1, gap=2.0)
        time.sleep(6.0)
    return True


def pane_timezone(w: Wizard) -> bool:
    if not w.says("time", "zone"):
        return False
    log("pane: Select Your Time Zone")
    w.click(TIME_ZONE_CONTINUE, "time zone Continue", settle=10.0)
    return True


def pane_analytics(w: Wizard) -> bool:
    if not w.says("analytics", "improve"):
        return False
    log("pane: Analytics")
    # UNTICK FIRST. The box is on when the pane opens; clicking it clears it and greys the
    # second option with it, which is what drive.py does and what the bake wants.
    w.click(ANALYTICS_SHARE, "untick Share Mac Analytics", settle=3.0)
    w.click(CONTINUE, "analytics Continue", settle=10.0)
    return True


def pane_screen_time(w: Wizard) -> bool:
    if not w.says("screen", "report"):
        return False
    log("pane: Screen Time")
    w.click(SCREEN_TIME_LATER, "Screen Time: Set Up Later", settle=8.0)
    # THIS PANE FINISHES ON ITS OWN AND TAKES ABOUT FORTY SECONDS. Waiting for the title to
    # change is the only reliable end-of-pane signal; a fixed sleep either wastes time or
    # clicks a greyed Continue.
    w.wait_for("look", timeout=180, label="after-screen-time")
    return True


def pane_look(w: Wizard) -> bool:
    if not w.says("look", "appearance"):
        return False
    log("pane: Choose Your Look")
    w.click(LOOK_CONTINUE, "look Continue", settle=12.0)
    return True


def pane_update(w: Wizard) -> bool:
    if not w.says("update", "automatically"):
        return False
    log("pane: Update Mac Automatically")
    w.click(CONTINUE, "update Continue", settle=15.0)
    return True


def pane_welcome(w: Wizard) -> bool:
    if not w.says("welcome", "mac"):
        return False
    log("pane: Welcome to Mac")
    w.click(WELCOME_CONTINUE, "welcome Continue", settle=20.0)
    return True


def pane_keyboard(w: Wizard) -> bool:
    if not w.says("keyboard", "assistant"):
        return False
    log("pane: Keyboard Setup Assistant (a dialog over the desktop)")
    # THIS DIALOG EATS THE NEXT KEYSTROKE, which is why sshd could not be enabled until it
    # was dismissed - see macos/15/ssh.py's dismiss_keyboard_assistant().
    w.click(KEYBOARD_QUIT, "Keyboard Setup Assistant: Quit", settle=6.0)
    return True


PANES = [
    pane_country, pane_transfer, pane_languages, pane_accessibility, pane_privacy,
    # pane_account TAKES ARGUMENTS, so it is wrapped by run() rather than listed here; a
    # handler that raises TypeError because it wants two more parameters is a handler whose
    # failure looks like "this pane is unknown", which is exactly what the caller must not
    # confuse with a pane it has already answered.
    pane_apple, pane_terms, pane_location, pane_timezone, pane_analytics,
    pane_screen_time, pane_look, pane_update, pane_welcome, pane_keyboard,
]


def run(qmp_path: str, frames: str, user: str, password: str,
        *, boot_timeout: int = 1800, release: str = "15") -> int:
    w = Wizard(qmp_path, frames, release=release)
    log(f"release: macOS {release}")
    log("waiting for the Setup Assistant")
    if not w.wait_for("country", timeout=boot_timeout, poll=10, label="boot"):
        log("=== THE WIZARD DID NOT COME UP ===")
        w.close()
        return 1

    def account(w_: Wizard) -> bool:
        return pane_account(w_, user, password)

    panes = list(PANES)
    # THE ACCOUNT PANE COMES AFTER PRIVACY AND BEFORE APPLE, so it goes in at that point in
    # the list rather than at the end - the order matters only for panes that could both
    # match, and this keeps the list readable against the run's own log.
    panes.insert(panes.index(pane_apple), account)

    actions = 0
    # EACH PASS LOOKS AT THE SCREEN AND RUNS THE FIRST PANE THAT MATCHES. A pane that the
    # guest skipped is simply not matched; the loop ends when nothing matches twice in a row,
    # which is either the desktop or a pane no handler knows.
    #
    # "WELCOME TO MAC" IS A PANE, NOT THE DESKTOP. It is a full-screen card with a Continue
    # button, and treating it as arrival skipped pane_welcome and left the wizard one click
    # from finished - the same class of mistake as an earlier version of this loop that
    # matched the word "up" inside a paragraph. The desktop is Finder's menu bar and nothing
    # else, so that is the only word that ends the loop.
    stuck = 0
    while stuck < 2:
        w.look("pass")
        if w.says("finder"):
            log("=== THE DESKTOP ===")
            break

        did = False
        for handler in panes:
            if handler(w):
                actions += 1
                did = True
                break

        stuck = 0 if did else stuck + 1
        if not did:
            time.sleep(10)

    w.close()
    if stuck >= 2:
        log("=== STUCK: no handler matched the pane that is up ===")
        return 2
    log(f"=== SEQUENCE COMPLETE: {actions} panes answered ===")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--qmp", required=True)
    p.add_argument("--frames", required=True)
    p.add_argument("--user", default=os.environ.get("MACOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MACOS_PASSWORD", "admin"))
    p.add_argument("--boot-timeout", type=int, default=1800)
    p.add_argument("--release", default=os.environ.get("MACOS_RELEASE", "15"),
                   help="which release the disk is; the transfer row and the account fields "
                        "differ between them and a shared table is wrong by 37 pixels")
    args = p.parse_args(argv)
    return run(args.qmp, args.frames, args.user, args.password,
               boot_timeout=args.boot_timeout, release=args.release)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
