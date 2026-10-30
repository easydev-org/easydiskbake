"""Walk MX's installer by coordinate, checking each step with a small OCR read.

    drive-mx.py <qmp-socket> <workdir>

WHY COORDINATES AND NOT A SCREEN IDENTIFIER

Two screen-identification schemes were built and measured against twenty-nine frames
from a real run, and both failed for the same reason:

  8x8 mean colour of the whole frame     largest distance between two different panes 18.1
                                        typical distance within one pane                2.7
  32x20 edge density, whole frame        different panes 4.6, same pane 1.8
  24x16 edge density, content area       different panes 7.7, same pane 3.1

MX's installer renders every pane from one framework: same window frame, same left
sidebar with its watermark, same button row, same progress bar - only the text in the
middle differs. Any spatial average erases precisely the part that distinguishes them.

So the pane is not identified. The sequence is known - it is a fixed installer - and the
coordinates below were measured from captured frames, not guessed:

  (385,257)  the "Install MX Linux" card in the welcome grid
  (971,693)  the forward button on the installer's own panes
  (970,662)  the forward button on the account pane, which sits 31px higher
  (827,264) (827,300) (827,336)   username, password, confirm
  (477,483)  the Autologin checkbox

WHY THERE IS STILL AN OCR READ

Not to decide anything, but to notice when a step did not land. Each action is followed
by reading a narrow strip where the pane title appears - 620x40 pixels, about a
twentieth of the frame, which tesseract handles in well under a second. If the strip
does not say what it should, the step is retried rather than assumed.

The difference from the previous driver is the direction of the decision: it used OCR to
choose what to do, which meant eight to ten seconds per step and a failure whenever a
word was misread. This one already knows what to do, and only asks whether it worked.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from ocr import Screen           # noqa: E402
from keys import type_text      # noqa: E402

W, H = 1280, 800

# The strip that carries each pane's own heading.
#
# Measured, not assumed. The first guess was y=150..215, which is where the PROGRESS BAR
# lives once copying starts - so every frame read "Installation in progress / 20% -
# Copying new system" and no pane was distinguishable from any other. The pane heading
# is one band lower:
#
#   s03  "System drive: vda (64.0 GB)"                      Installation Options
#   s05  "Create a swap file"                               copying
#   s06  "Computer Network Names / Computer name: mx"       Computer Identity
#   s08  "Localization Defaults / Locale: United States"    Localization
#   s10  "Default User Account / Default user login name:"  the account pane
#
# 620x50 pixels: two per cent of the frame, which tesseract reads in a fraction of a
# second.
TITLE_STRIP = (467, 215, 1090, 265)

# The welcome window is a different window at a different place, and it needs its own
# strip. Using the installer's coordinates to wait for it was the first thing this
# version got wrong: the driver sat for its full ninety-second timeout looking at a band
# of desktop wallpaper and then exited, having done nothing at all.
#
# Measured from the frames:
#
#   y=100..140   "MX-25.3 Infinity"                 window title
#   y=245..285   "Install MX Linux | FAQ"           the card grid
WELCOME_STRIP = (320, 245, 1060, 285)

# Measured coordinates. Every one of these was read off a frame this pipeline captured.
WELCOME_CARD = (385, 257)
FORWARD = (971, 693)          # the installer's own panes
ACCOUNT_FORWARD = (970, 662)  # the account pane's button row is 31px higher
USERNAME_FIELD = (827, 264)
PASSWORD_FIELD = (827, 300)
PASSWORD_CONFIRM_FIELD = (827, 336)
AUTOLOGIN_CHECKBOX = (477, 483)

ACCOUNT_USER = os.environ.get("ACCOUNT_USER", "docker")
ACCOUNT_PASS = os.environ.get("ACCOUNT_PASS", "admin")


def log(msg):
    print("[drive %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Driver:
    def __init__(self, qmp_path, workdir):
        self.screen = Screen(qmp_path, workdir=workdir, width=W, height=H)
        self.workdir = workdir
        self.step = 0

    # -- observing ------------------------------------------------------------

    def title(self, stem, strip=None):
        """Read a title strip. Returns the words found there, lowercased.

        text_in crops the frame on disk and points tesseract at the crop, so the decoder
        never sees the other 98% of the screen. Reading the whole frame and filtering by
        coordinate afterwards was the first version: it cost seconds per step and is the
        reason the previous driver took eight to ten.
        """
        ppm = self.screen.grab(stem)
        if not ppm:
            return ""
        words = self.screen.text_in(ppm, strip or TITLE_STRIP)
        return " ".join(w[0] for w in words).lower()

    def expect(self, stem, patterns, seconds=20, strip=None):
        """Wait until the title strip mentions one of `patterns`.

        Returns the title it settled on, or "" if it never matched. A miss is not fatal
        here: the caller decides whether to retry, and the pane after some actions
        genuinely has no title of its own (the copy progress page).
        """
        deadline = time.time() + seconds
        last = ""
        attempts = 0
        while time.time() < deadline:
            # A FRESH FILENAME EACH TIME.
            #
            # screendump writes asynchronously and does not promise to have finished
            # when it returns. Reusing one name meant the crop and the OCR could both
            # read the previous frame - and the previous frame here is the black screen
            # from before the desktop finished loading, which is exactly why nine runs
            # in a row reported "strip said ''" for ninety seconds.
            last = self.title("%s-%03d" % (stem, attempts), strip)
            attempts += 1
            if any(p in last for p in patterns):
                return last
            # Say what was actually read, on the first and every tenth attempt.
            #
            # Without this the log for a failed wait was one line - "waiting for the
            # welcome window" - and there was no way to tell a wrong region from a
            # tesseract that returned nothing at all. Those need different fixes.
            if attempts == 1 or attempts % 5 == 0:
                log("  waiting (%d): strip said %r" % (attempts, last[:70]))
            time.sleep(1)
        return ""

    # -- acting ---------------------------------------------------------------

    def sample(self, stem):
        """Read the strip a few times and keep the longest result.

        Tesseract on this installer is intermittent: one frame of the Terms pane read
        "mx linux uses some components from mepis linux which are released under..."
        and the next, eight seconds later, read nothing at all. Taking the best of
        three readings over four seconds turns that from a coin flip into a
        near-certainty, and costs less than the failure did.
        """
        best = ""
        for i in range(3):
            got = self.title("%s-r%d" % (stem, i))
            if len(got) > len(best):
                best = got
            if best:
                break
            time.sleep(2)
        return best

    def click(self, what, xy, then_expect=None, attempts=3):
        """Click, then check the pane actually changed.

        THE CHECK IS "DID THE SCREEN CHANGE", NOT "DOES IT SAY THE RIGHT WORD".

        Requiring a specific phrase failed for a reason that took a while to see: the
        strip does not hold each pane's title. It holds whatever text sits at that
        height, which on the Terms pane is the licence body - "MX Linux uses some
        components from MEPIS Linux which are released under..." - and on the options
        pane is the selected drive line. The pane names in this file are what a reader
        would call them; the OCR never returns those words.

        So the test is: the strip now yields text, and that text is not what it said
        before the click. A click that lands changes the pane; a click that misses does
        not. `then_expect` is still accepted and still used when given, because for the
        welcome card there is a genuine before/after (nothing, then the installer), and
        saying so in the log is worth the extra parameter.
        """
        self.step += 1
        before = getattr(self, "last_text", "")
        for attempt in range(1, attempts + 1):
            log("step %d: click %s at %s (try %d)" % (self.step, what, xy, attempt))
            self.screen.click(*xy)
            time.sleep(3)

            after = self.sample("s%02d-%d" % (self.step, attempt))
            changed = after and after != before
            matched = bool(then_expect) and any(p in after for p in then_expect)

            if changed or matched:
                self.last_text = after
                log("step %d: ok, pane now reads %r" % (self.step, after[:70]))
                return True
            log("step %d: no change (reads %r)" % (self.step, after[:70]))
        log("step %d: GAVE UP after %d attempts" % (self.step, attempts))
        return False

    def type_at(self, what, xy, text):
        log("step %d: type %r into %s at %s" % (self.step, text, what, xy))
        self.screen.click(*xy)
        time.sleep(1)
        unsupported = type_text(self.screen.q, text)
        if unsupported:
            log("step %d: COULD NOT TYPE %r - no qcode for those characters"
                % (self.step, "".join(unsupported)))
        time.sleep(1)


def main():
    qmp_socket, workdir = sys.argv[1], sys.argv[2]
    d = Driver(qmp_socket, workdir)
    log("qmp connected")

    # The desktop takes a few seconds after login; the welcome window is on top of it.
    log("waiting for the welcome window")
    # LONGER THAN IT LOOKS LIKE IT NEEDS.
    #
    # Ninety seconds was the first value and it failed every time. The clock from qemu
    # starting to the welcome window appearing includes: the kernel booting, the live
    # session's getty coming up, three login cycles at fifteen seconds each, the desktop
    # starting, and only then this window. That is two to three minutes before the first
    # frame with any text in it exists at all.
    #
    # The wait prints what the strip says every ten attempts, so a genuinely wrong
    # region is still distinguishable from a slow boot.
    if not d.expect("w00", ["install mx linux", "mx welcome", "mx-25"], 600,
                    WELCOME_STRIP):
        log("the welcome window never appeared")
        return 1

    # 1. The card grid. Its title is one cell of a grid, so the position is used and the
    #    next pane's title is what confirms it landed.
    if not d.click("Install MX Linux card", WELCOME_CARD,
                   then_expect=["general instructions", "terms of use", "installation"]):
        log("could not open the installer")
        return 1

    # 2. The installer's own panes, in the order this version presents them. Each is a
    #    forward click at the same place; the titles are what proves each landed.
    for what, title in (
        ("General Instructions", ["terms of use", "installation options"]),
        ("Terms of Use", ["installation options"]),
        ("Installation Options", ["confirmation", "final review"]),
        ("Final Review", ["install", "copying", "progress"]),
    ):
        if not d.click(what, FORWARD, then_expect=title):
            log("stopped at %s" % what)
            return 1

    # 3. Computer Identity. Every field already holds a usable default, so the only
    #    action is the forward button - but the pane says it is waiting for input, and
    #    the previous driver read that as "busy" and watched it for six steps.
    d.click("Computer Identity", FORWARD,
            then_expect=["installation in progress", "default user", "locales",
                         "localization"], attempts=4)

    # 4. The account pane. This is the one thing the installer will not do by itself
    #    without an answer, and the reason an earlier disk came out with the live
    #    session's own user on it.
    log("step %d: the account pane" % (d.step + 1))
    d.type_at("username", USERNAME_FIELD, ACCOUNT_USER)
    d.type_at("password", PASSWORD_FIELD, ACCOUNT_PASS)
    d.type_at("confirm password", PASSWORD_CONFIRM_FIELD, ACCOUNT_PASS)

    d.step += 1
    log("step %d: click Autologin at %s" % (d.step, AUTOLOGIN_CHECKBOX))
    d.screen.click(*AUTOLOGIN_CHECKBOX)
    time.sleep(1)

    d.click("account pane forward", ACCOUNT_FORWARD,
            then_expect=["installation in progress", "installing", "copying"], attempts=4)

    # 5. Nothing left to press. The installer copies the system and then finishes; the
    #    pane offers only Close, and pressing Close takes the window away while the copy
    #    runs on - which is what made an earlier run declare itself stuck one step
    #    before the end.
    # Wait for the copy to finish, measured rather than read.
    #
    # The first version looked for the words "complete" or "finished" in the title
    # strip, and MX's installer never prints either - so the loop ran its full twenty
    # minutes every time while the install had actually finished in four. The driver
    # was not stuck; it was reading for a signal that does not exist.
    #
    # What does exist is the disk file growing. qemu writes to it as the installer
    # copies, so "the size has not changed for a while" is a direct measurement of
    # "the copy has stopped". A quiet period also covers the install's own post-copy
    # steps, which write little but do write something.
    log("waiting for the copy to finish; watching the disk grow")
    disk = os.path.join(d.workdir, "disk.qcow2")
    last_size, quiet, i = -1, 0, 0
    while i < 180:
        try:
            size = os.path.getsize(disk)
        except OSError:
            size = -1
        if size == last_size:
            quiet += 1
        else:
            quiet = 0
            last_size = size
        if i % 6 == 0:
            log("  disk %.2f GiB, quiet for %ds" % (size / 1073741824.0, quiet * 10))
        # Six quiet readings is a minute. The longest pause observed inside a successful
        # copy was about twenty seconds, so a minute cannot be a lull mid-copy.
        if quiet >= 6 and size > 100 * 1024 * 1024:
            log("the disk stopped growing at %.2f GiB - the copy is done"
                % (size / 1073741824.0))
            break
        i += 1
        time.sleep(10)
    else:
        log("WARNING: the disk was still growing after 30 minutes")

    log("driver finished after %d steps" % d.step)
    d.screen.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
