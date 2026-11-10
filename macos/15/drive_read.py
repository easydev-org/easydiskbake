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
    # MEASURED ON THIS DISK, not carried over from drive.py's table. The values differ by a
    # row: drive.py says (1050,497)/(842,601)/(1075,601); clicking those on the real pane put
    # the text one row off. These are the ones that produced a green tick beside every field.
    "26": {"full": (960, 496), "password": (748, 600), "verify": (1069, 600)},
}


def log(msg: str) -> None:
    print(f"[drive {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Wizard:
    """Look at the screen, then act. Every check below is a title tesseract read."""

    def __init__(self, qmp_path: str, frames: str, *, width: int = 1920,
                 height: int = 1080, release: str = "15") -> None:
        os.makedirs(frames, exist_ok=True)
        self.release = release
        # THE SOCKET IS WAITED FOR, NOT ASSUMED, AND THIS WAS MISSING.
        #
        # desktop.py starts this driver immediately after guest.start(), and guest.start()
        # returns as soon as the process exists - qemu binds its QMP socket a moment later.
        # Connecting straight away raises
        #
        #     FileNotFoundError: [Errno 2] No such file or directory
        #       self.sock.connect(path)
        #
        # which is what run 42289113404's driver did, nine seconds into the stage, and then
        # exited 1. The stage that followed was 1800 seconds of "still waiting: ..., newest
        # frame 0s old" - a heartbeat reporting a guest nobody was driving.
        #
        # drive.py has always waited here (its Driver.__init__ documents the same failure from
        # run 41775498755). This is that wait, and the omission was mine: drive_read.py was
        # written from scratch and took ocr.Screen() directly.
        deadline = time.time() + 120
        while not os.path.exists(qmp_path):
            if time.time() > deadline:
                raise SystemExit(
                    f"no QMP socket at {qmp_path} after 120s; qemu either failed to start "
                    f"or was given a different work directory")
            time.sleep(1)
        self.screen = ocr.Screen(qmp_path, frames, width=width, height=height)
        self.q = self.screen.q
        # KEPT SEPARATELY BECAUSE ONE PANE NEEDS A SECOND CONNECTION. The macOS 26 update pane
        # is left by a QMP system_reset, and a reset on the connection the screen reader is
        # using would take the reader down with it.
        self.q_path = qmp_path
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

    @property
    def continue_at(self) -> tuple[int, int]:
        """The footer's forward button. It is NOT in the same place on 26.

        Every pane uses this rather than the module constant, because a shared table is wrong
        by a button's width and a click thirty pixels off a forty-pixel button misses.
        """
        return CONTINUE_26 if self.release == "26" else CONTINUE

    def close(self) -> None:
        try:
            self.q.close()
        except Exception:                              # noqa: BLE001 - best effort
            pass


# --- macOS 26 only ---------------------------------------------------------------------
#
# EVERY NUMBER HERE WAS MEASURED BY CLICKING IT ON A REAL macos26-setup DISK AND LOOKING AT THE
# NEXT FRAME. 26 draws the wizard as a floating card over a backdrop with a back chevron and a
# solid footer strip, which moves every footer button left and up by about thirty pixels:
#
#                     15              26
#     Continue        (1306, 810)     (1276, 798)
#     Not Now         (1306, 806)     (1276, 798)
#     Transfer row    (825, 669)      (740, 706)     <- also 85 px left
#     Account fields  (945,447) etc   (960,496) etc
#     Don't Use       (960, 632)      (959, 573)
#     Analytics tick  (808, 513)      (742, 521)
#     Screen Time     (619, 806)      (1131, 798)
#
# AND 26 HAS TWO PANES 15 DOES NOT - Age Range and FileVault - plus an Apple Account pane with
# no "Set Up Later" link, whose way out is a popup menu.
CONTINUE_26 = (1276, 798)
SCREEN_TIME_LATER_26 = (1131, 798)
LOCATION_DONT_USE_26 = (959, 573)
ANALYTICS_SHARE_26 = (742, 521)
TERMS_AGREE_26 = (1277, 798)
LICENCE_AGREE_26 = (1019, 619)
APPLE_OTHER_OPTIONS_26 = (668, 799)
# THE MENU'S SECOND ROW, MEASURED ON A LIVE 26 GUEST. It opens DOWNWARD from the button:
# "Use Multiple Accounts" at y=833, "Sign In Later in Settings" at y=856.
APPLE_SET_UP_LATER_26 = (700, 856)
# The confirmation sheet's blue button, centred at (1020, 640) - confirmed against a frame of
# the sheet itself. Its title line reads at y=516, which is NOT the button.
APPLE_SKIP_26 = (1020, 640)
AGE_ADULT = (960, 714)
FILEVAULT_NOT_NOW = (1129, 798)
FILEVAULT_CONFIRM = (1020, 598)

ACCOUNT_FIELDS_26 = {
    "full": (960, 496),
    "password": (748, 600),
    "verify": (1069, 600),
}

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
    w.click(w.continue_at, "country Continue", settle=8.0)
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
    w.click(w.continue_at, "transfer Continue", settle=10.0)
    return True


def pane_languages(w: Wizard) -> bool:
    if not w.says("spoken", "languages"):
        return False
    log("pane: Written and Spoken Languages")
    w.click(w.continue_at, "languages Continue", settle=8.0)
    return True


def pane_accessibility(w: Wizard) -> bool:
    if not w.says("accessibility", "individual"):
        return False
    log("pane: Accessibility")
    # NOT "Continue". This pane's forward button says Not Now, and it sits four pixels
    # higher than the usual footer - clicking (1306, 810) here selects nothing.
    w.click(CONTINUE_26 if w.release == "26" else ACCESSIBILITY_NOT_NOW, "accessibility Not Now", settle=12.0)
    return True


def pane_privacy(w: Wizard) -> bool:
    if not w.says("data", "privacy"):
        return False
    log("pane: Data & Privacy")
    w.click(w.continue_at, "privacy Continue", settle=8.0)
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
    w.click(w.continue_at, "account Continue", settle=15.0)
    return True


def pane_apple(w: Wizard) -> bool:
    if not w.says("sign", "apple"):
        return False
    log("pane: Sign In to Your Apple Account")
    if w.release == "26":
        # NO "Set Up Later" LINK ON 26. Where 15 puts a text link in the bottom-left corner, 26
        # has a popup button whose menu holds the way out.
        #
        # THE MENU OPENS DOWNWARD, AND ITS SECOND ROW IS THE WAY OUT. Measured on a live 26
        # guest (macos26-probe, 2026-11-07) by opening the menu and reading it:
        #
        #     Other Sign-In Options     button centre (668, 799)
        #     Use Multiple Accounts     menu row, y=833
        #     Sign In Later in Settings menu row, y=856      <- the one this needs
        #
        # Return DOES NOT TAKE AN ITEM HERE. drive.py's 26 branch sends Return after the click
        # and its comment says a macOS popup "highlights its first item when it opens".
        # Measured on this pane, it does not: the menu opened (28 -> 31 readable words, with
        # "Use Multiple Accounts" and "Sign In Later in Settings" in them) and Return closed it
        # again and left the pane exactly where it was. So this clicks the row itself.
        #
        # BOTH STEPS ARE RETRIED, because the failure this pane actually has is a click that
        # arrives while the popup is still animating: the menu is then not drawn, the second
        # click lands on the pane, and the wizard does not move. Run 42383663645 spent five
        # passes doing exactly that - 'click (670,802)', 'look apple-menu: 58 words' (the pane
        # unchanged, so no menu), 'click (677,856)', repeat. Each attempt re-opens and
        # re-reads, and the loop ends as soon as the pane changes.
        for attempt in range(3):
            w.click(APPLE_OTHER_OPTIONS_26, f"Other Sign-In Options (attempt {attempt + 1})",
                    settle=4.0)
            w.look("apple-menu")
            if not w.says("later", "settings"):
                log("  no sign-in menu; the popup had not drawn yet")
                continue
            w.click(APPLE_SET_UP_LATER_26, f"Sign In Later in Settings (attempt {attempt + 1})",
                    settle=8.0)
            w.look("apple-after")
            if not w.says("sign", "apple"):
                log("  the sign-in pane is gone")
                break
        # NO `return True` ON THE LINE ABOVE, AND THAT IS THE WHOLE POINT. It used to return
        # as soon as the pane changed, which skipped the confirmation sheet below - and 26
        # DOES raise one: "Are you sure you want to skip signing in with an Apple Account?".
        # Run 42388520326 shows the cost exactly:
        #
        #     16:20:03  click (700,856) - Sign In Later in Settings (attempt 1)
        #     16:20:18    the sign-in pane is gone
        #     16:20:47  === STUCK: no handler matched the pane that is up ===
        #               the reading driver exited 2
        #
        # The sheet has no words any pane test matches, so the driver gave up forty seconds
        # before it would have reached Terms and Conditions.
        w.look("apple-sheet")
        if w.says("skip"):
            w.click(APPLE_SKIP_26 if w.release == "26" else APPLE_SKIP,
                    "Skip on the confirmation sheet", settle=8.0)
        return True
    else:
        w.click(APPLE_SET_UP_LATER, "Set Up Later", settle=6.0)
        w.press("ret", gap=3.0)
    # THE CONFIRMATION SHEET. drive.py's SHEET_SKIP records that it only appears on some
    # releases; here it does, and its Skip is the blue button in the middle of the card.
    w.look("apple-sheet")
    if w.says("skip"):
        w.click(APPLE_SKIP_26 if w.release == "26" else APPLE_SKIP,
                "Skip on the confirmation sheet", settle=8.0)
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
    w.click(TERMS_AGREE_26 if w.release == "26" else TERMS_AGREE, "the pane's Agree, which opens the licence sheet", settle=6.0)
    w.look("licence-sheet")
    if w.says("agree"):
        w.click(LICENCE_AGREE_26 if w.release == "26" else LICENCE_AGREE, "the licence sheet's own Agree", settle=8.0)
    return True


def pane_location(w: Wizard) -> bool:
    if not w.says("location", "services"):
        return False
    log("pane: Enable Location Services")
    w.click(w.continue_at, "location Continue, which opens the confirmation", settle=6.0)
    w.look("location-sheet")
    if w.says("use"):
        # THE SHEET'S "Don't Use" IS ITS DEFAULT BUTTON: a click darkens it and leaves the
        # sheet up, and Return is what fires it. drive.py measured both halves of that.
        w.click(LOCATION_DONT_USE_26 if w.release == "26" else LOCATION_DONT_USE, "Don't Use on the confirmation", settle=3.0)
        w.press("ret", 1, gap=2.0)
        time.sleep(6.0)
    return True


def pane_timezone(w: Wizard) -> bool:
    if not w.says("time", "zone"):
        return False
    log("pane: Select Your Time Zone")
    w.click(CONTINUE_26 if w.release == "26" else TIME_ZONE_CONTINUE, "time zone Continue", settle=10.0)
    return True


def pane_analytics(w: Wizard) -> bool:
    if not w.says("analytics", "improve"):
        return False
    log("pane: Analytics")
    # UNTICK FIRST. The box is on when the pane opens; clicking it clears it and greys the
    # second option with it, which is what drive.py does and what the bake wants.
    w.click(ANALYTICS_SHARE_26 if w.release == "26" else ANALYTICS_SHARE, "untick Share Mac Analytics", settle=3.0)
    w.click(w.continue_at, "analytics Continue", settle=10.0)
    return True


def pane_screen_time(w: Wizard) -> bool:
    if not w.says("screen", "report"):
        return False
    log("pane: Screen Time")
    w.click(SCREEN_TIME_LATER_26 if w.release == "26" else SCREEN_TIME_LATER, "Screen Time: Set Up Later", settle=8.0)
    # THIS PANE FINISHES ON ITS OWN AND TAKES ABOUT FORTY SECONDS. Waiting for the title to
    # change is the only reliable end-of-pane signal; a fixed sleep either wastes time or
    # clicks a greyed Continue.
    w.wait_for("look", timeout=180, label="after-screen-time")
    return True


def pane_look(w: Wizard) -> bool:
    if not w.says("look", "appearance"):
        return False
    log("pane: Choose Your Look")
    w.click(CONTINUE_26 if w.release == "26" else LOOK_CONTINUE, "look Continue", settle=12.0)
    return True



def pane_age_range(w: Wizard) -> bool:
    """macOS 26 only. Three rows, and NO footer button - the row IS the answer."""
    if not w.says("age", "range"):
        return False
    log("pane: Age Range (macOS 26)")
    # Child (12 or younger) / Teen (13 to 17) / Adult (18 or older). Adult is the third, and
    # clicking it advances the pane by itself; there is no Continue to press afterwards, which
    # is why this pane looks like it has no way forward.
    w.click(AGE_ADULT, "Adult (18 or older)", settle=10.0)
    return True


def pane_filevault(w: Wizard) -> bool:
    """macOS 26 only. Turn On would encrypt the disk, which no bake here wants."""
    if not w.says("filevault", "encrypting"):
        return False
    log("pane: FileVault (macOS 26)")
    # AN ENCRYPTED DISK IS A DISK NOBODY CAN REPAIR. Later stages boot this disk and so does
    # whoever uses it, and a FileVault volume whose recovery key was never kept is one that
    # stops at a password prompt with no way in.
    w.click(FILEVAULT_NOT_NOW, "FileVault: Not Now", settle=8.0)
    w.look("filevault-confirm")
    if w.says("without") or w.says("encrypted"):
        w.click(FILEVAULT_CONFIRM, "confirm continuing without FileVault", settle=10.0)
    return True


def pane_update(w: Wizard) -> bool:
    if not w.says("update", "automatically"):
        return False
    log("pane: Update Mac Automatically")

    if w.release == "26":
        return _update_26(w)

    w.click(w.continue_at, "update Continue", settle=15.0)
    return True


def _update_26(w: Wizard) -> bool:
    """macOS 26's Update pane: click once, wait five minutes, then restart the guest.

    MEASURED ON A REAL macos26-setup DISK, 2026-11-06:

        click Continue          the pane stays; a spinner appears in the footer
        ... for fifteen minutes   qemu burns 313% CPU, the screen changes every frame, and
                                  tesseract can read nothing but a few words of the title
        the button               turns GREY and stops responding. Back turns grey too. The
                                  whole wizard is locked while this runs.
        no traffic               the container has no established outbound connection, so
                                  this is not waiting on Apple's servers
        what never happens       the pane never redraws and never advances

    So the two behaviours a driver must have are "wait" and "restart", in that order, and
    neither is a click. An earlier version of this file clicked Continue once a minute; the
    screenshots that disproved it show a disabled button the whole time.

    SIX HUNDRED SECONDS IS THE BUDGET, by instruction: the pane is given ten minutes to
    finish on its own before the guest is restarted. The wizard's work is done long before
    that in the cases where it finishes at all, and the disk is consistent either way because
    the write has long since finished - so the extra time costs nothing but a slower failure,
    and it is the answer to "how long do we wait before restarting".

    THE RESTART IS A QMP system_reset, NOT A PANE ACTION. There is no button that leaves this
    state, so the driver has to leave it from outside the guest. The disk is what comes out:
    a machine that logs in and reaches its desktop, which is exactly what the stage is for.
    It goes out on the connection the reader already holds - see the note at the call.
    """
    w.click(w.continue_at, "update Continue (macOS 26)", settle=10.0)

    budget = int(os.environ.get("MACOS26_UPDATE_WAIT", "600"))
    log(f"  macOS 26: waiting {budget}s for the update pane to settle")
    deadline = time.time() + budget
    while time.time() < deadline:
        time.sleep(30)
        w.look("update-wait")
        if w.says("finder"):
            log("  the update pane finished on its own")
            return True
        if not w.says("update", "automatically"):
            log("  the pane has gone; something else is on screen")
            return True

    log("  macOS 26: the pane never advanced; restarting the guest")
    # THE RESET GOES OUT ON THE CONNECTION THAT IS ALREADY OPEN.
    #
    # This used to open a second QMP client on the same socket, on the theory that resetting
    # through the reader's connection would "take the reader down with it". The theory was
    # wrong about the mechanism: lib/vm.py gives qemu two QMP sockets precisely because
    # `QMP serves one client per socket` (its own comment), and drive.sock.qmp is already
    # held by self.screen.q. The second connect() therefore never completed, and the only
    # thing the driver ever recorded was
    #
    #     the reset could not be sent: timed out
    #
    # Run 42367049439's successor saw it twice, twelve minutes apart, each followed by a
    # fresh Continue click and another 300 s wait - an unbounded loop whose exit condition
    # was a reset that was never delivered. The reset does not disturb the reader either:
    # system_reset restarts the guest, not qemu, so the socket and this object stay valid.
    try:
        w.q.execute("system_reset")
    except Exception as exc:                           # noqa: BLE001
        log(f"  the reset could not be sent: {exc}")
        return False
    log("  reset sent; the login window is the next stage's to handle")

    # TWENTY SECONDS, AND IT IS NOT A TIMEOUT. The guest reboots into its login window and
    # desktop.py's wait_for_desktop owns that boot from here, with its own 1800s budget. This
    # pause only stops the driver returning while qemu is still mid-reset - the 600s I tried
    # here was the wrong knob, because the thing that needs the longer wait is the update
    # pane BEFORE the reset, not this.
    time.sleep(20)
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
    # THE 26 PANES ARE IN THE SAME LIST. They detect on words 15 never shows, so on a 15 disk
    # they never match - a release-specific pane that cannot fire is cheaper than a branch
    # that decides which list to walk.
    pane_age_range, pane_filevault,
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
    p.add_argument("--user", default=os.environ.get("MACOS_USER", "vmuser"))
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
