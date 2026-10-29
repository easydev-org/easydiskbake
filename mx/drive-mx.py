"""Walk MX's graphical installer with OCR and the pointer.

    drive-mx.py <qmp.socket> <workdir>

WHAT THIS IS DRIVING

MX's live desktop opens a "MX Welcome" window; the first card in it, "Install MX
Linux", starts the installer. A probe of this exact image captured that window at
1280x800 - four screenshots, 537 KB each - so the layout below is measured, not
guessed:

    MX Welcome  MX-25.3 "Infinity"
    [About this system] [Manual] [About]
    User demo, password: demo. Superuser root, password: root.

    Install MX Linux | FAQ
    Forums           | Users Manual
    ...

THE LOOP, AND WHY IT LOOKS LIKE THIS

Every step reads the screen, and the pane's text is remembered. If two consecutive
steps read the same thing, the run is stuck - dockur-macos records what that costs
without: it clicked the word "later" inside a sentence for nineteen consecutive
steps, and every log line looked like progress.

There is also a hard stop. A step limit turns "it is doing something" into "it has
done sixty things", which is a number this project can act on.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from ocr import Screen, find_button  # noqa: E402
from keys import type_text  # noqa: E402

W, H = 1280, 800

# The welcome window's own cards, matched as whole phrases.
#
# find_button's three passes are built for a wizard whose forward button sits in the
# lower-right corner. MX Welcome is a grid of cards spread across the window, and the
# first run showed what that does: 'install' matched the Popular Apps card at
# (423,698), which opened MX Package Installer, whose own title contains the word
# 'install' - so the loop clicked Install, got a package manager, closed it, and
# started again. Nineteen steps of that.
#
# A card is identified by its full title on one line, not by a word.
# The installer's Default User Login pane, measured from a captured frame.
#
# This pane is where the GUI walk stalled for six steps in the run that produced a disk
# with the live session's own account on it:
#
#   step 12: Default User Login Paused for required ope!
#
# 'Paused for required operations' means the pane is waiting for input it will not get
# from a Next click. The fields, in pixels, from that same frame:
#
#   y=264  Default user login name:   (827, 264)
#   y=300  Default user password:     (827, 300)
#   y=336  Confirm user password:     (827, 336)
#   y=375  [ ] Root (administrator) Account     (477, 375)
#   y=483  [ ] Autologin                        (477, 483)
#
# and the button row on this pane sits at y=662, not the y=693 measured on the
# confirmation pane - clicking 693 here hits the empty area below the buttons.
USERNAME_FIELD = (827, 264)
PASSWORD_FIELD = (827, 300)
PASSWORD_CONFIRM_FIELD = (827, 336)
AUTOLOGIN_CHECKBOX = (477, 483)
USER_PANE_NEXT = (970, 662)

ACCOUNT_USER = "docker"
ACCOUNT_PASS = "admin"

WELCOME_CARD = "install mx linux"
START_CARD = ("start the installer",)

# Labels that only ever appear on controls.
#
# The rule from dockur-macos: a word that also appears in body copy gets clicked as
# if it were a button. "Continue" is safe on an installer's pages; "Install" is safe
# on the welcome window because it is a card title, not prose.
WELCOME = ("install mx linux", "install", "installer")
# The installer's own controls, read off its pages rather than guessed.
#
# A screenshot of the final page shows exactly three:
#
#   [Close]   [Back]   [Start]
#
# and 'Start' is the one that begins the install - it is not a Next, and the first
# run stopped on that page for four steps because the label list did not contain it.
#
# 'close' is deliberately NOT here: on this page it abandons the installation, and a
# run that clicks it looks identical to one making progress until the disk turns out
# to be empty. It is only used to dismiss a dialog, further down.
INSTALLER_FORWARD = ("next", "continue", "forward", "start")
INSTALLER_AGREE = ("agree", "accept", "i agree")
INSTALLER_FINISH = ("finish", "close", "done", "reboot now", "restart now")
INSTALLER_SKIP = ("skip", "not now", "later")
ALL = (WELCOME + INSTALLER_FORWARD + INSTALLER_AGREE + INSTALLER_FINISH
       + INSTALLER_SKIP + ("ok", "yes", "begin", "start"))

# Where the forward control sits on the pages that have one. Measured on the MX
# Welcome window's card grid; the installer's own pages are handled by OCR, so this
# is only the fallback for a pane whose label could not be read.
# Where the forward control sits on the installer's pages.
#
# MEASURED FROM A SCREENSHOT of the final page, which the run's own frames captured:
#
#   bottom button row, y 681..705
#   Close (511,693)   Back (886,693)   Start (971,693)
#
# The previous value, (1120,745), was a guess and wrong by 149 pixels in x and 52 in y.
# find_button's second pass accepts anything within 30px of this x, so it matched
# nothing, and the run reported 'no control found' for six consecutive steps on a page
# whose Start button is plainly visible.
PRIMARY = (971, 693)


def find_phrase(words, phrase):
    """Locate a card or button by its whole title, on one line.

    find_button is for wizards with a corner control. A card grid is not that: the
    cards are spread across the window and their titles are the only identifier.

    OCR splits a title across words, so consecutive words on the same line are joined
    back up and the joined text is compared. A title of one word is matched directly.

    Returns ((x, y), matched_text) or (None, None).
    """
    target = phrase.lower().strip()
    want = target.split()

    band = sorted(words, key=lambda w: (w[2], w[1]))

    for start in range(len(band)):
        chunk = band[start:start + len(want)]
        if len(chunk) != len(want):
            continue
        # All on one line: OCR puts each word at its own baseline, and a card title
        # is one line of text.
        if any(abs(chunk[0][2] - w[2]) > 14 for w in chunk[1:]):
            continue
        joined = " ".join(w[0] for w in chunk).strip(" .,:;").lower()
        if joined == target or joined.startswith(target):
            xs = [w[1] for w in chunk]
            ys = [w[2] for w in chunk]
            return ((sum(xs) // len(xs), sum(ys) // len(ys)),
                    " ".join(w[0] for w in chunk))

    return (None, None)


def main():
    qmp_socket, workdir = sys.argv[1], sys.argv[2]
    log_path = os.path.join(workdir, "drive.log")

    def log(msg):
        line = "[drive %s] %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        with open(log_path, "a") as fh:
            fh.write(line + "\n")

    screen = Screen(qmp_socket, workdir=workdir, width=W, height=H)
    log("qmp connected")

    # The desktop appears a few seconds after login; OCR on a half-drawn frame
    # returns half the words.
    time.sleep(20)

    previous = None
    stuck = 0

    for step in range(1, 121):
        ppm = screen.grab("s%02d" % step)
        words = screen.text(ppm)
        joined = " ".join(w[0] for w in words)
        # The whole pane, not the first 70 characters.
        #
        # The first run truncated at 70, and MX Welcome's header alone fills that much
        # - so every step of a card grid produced the same signature and the stuck
        # detector could not tell one pane from another.
        signature = joined[:200]
        log("step %d: %s" % (step, signature or "(nothing readable)"))

        if signature and signature == previous:
            stuck += 1
            log("step %d: same pane as last step (%d in a row)" % (step, stuck))
            if stuck >= 6:
                log("step %d: STUCK for %d steps - stopping" % (step, stuck))
                break
        else:
            stuck = 0
        previous = signature

        low = joined.lower()

        # The account pane: fill it in, because nothing else will.
        #
        # This has to come before the 'is it working' test below. That test looks for a
        # Pause control, and this pane is exactly where 'Paused for required operations'
        # appears - so it would be treated as busy and left alone for ever, which is what
        # happened: six steps of 'the installer is working (a Pause control); not
        # clicking' on a pane that was waiting for a username.
        if "default user login" in low or "default user account" in low:
            done = getattr(main, "user_filled", False)
            if not done:
                log("step %d: account pane; filling in %s/%s"
                    % (step, ACCOUNT_USER, ACCOUNT_PASS))
                screen.click(*USERNAME_FIELD)
                time.sleep(1)
                bad = type_text(screen.q, ACCOUNT_USER)
                if bad:
                    log("step %d: COULD NOT TYPE %r - no qcode for those characters"
                        % (step, "".join(bad)))
                time.sleep(1)

                screen.click(*PASSWORD_FIELD)
                time.sleep(1)
                type_text(screen.q, ACCOUNT_PASS)
                time.sleep(1)

                screen.click(*PASSWORD_CONFIRM_FIELD)
                time.sleep(1)
                type_text(screen.q, ACCOUNT_PASS)
                time.sleep(1)

                # Autologin, so the finished disk boots to a desktop rather than a
                # greeter - which is what the previous disk did, listing the live
                # session's 'demo' user.
                screen.click(*AUTOLOGIN_CHECKBOX)
                time.sleep(1)

                log("step %d: filled; now Next at %s" % (step, USER_PANE_NEXT))
                screen.click(*USER_PANE_NEXT)
                time.sleep(8)
                main.user_filled = True
                ppm2 = screen.grab("s%02d-userfilled" % step)
                after = " ".join(w[0] for w in screen.text(ppm2))[:100]
                log("step %d: after filling the account pane: %s"
                    % (step, after or "(nothing readable)"))
                continue

        # While the install is running, click nothing.
        #
        # The timeline of a successful-looking run shows exactly what happened:
        #
        #   step 5:  Installation Confirmation            -> Start clicked, install begins
        #   step 5:  Create a swap file | 15% - Copying
        #   step 13: Default User Login 91% - Copying new
        #   step 13: now at ... Pause                    <- the forward button became Pause
        #   step 16-19: the same click, four more times
        #
        # This installer turns its forward button into Pause while it works, at the same
        # position. Clicking it stops the installation - which is why the disk held 8 GB
        # of data and no bootloader: the copy was interrupted, and GRUB is one of the
        # last things written.
        #
        # So a pane that says it is copying, or shows a percentage, or offers Pause, is
        # left alone. The loop keeps reading the screen until it changes by itself.
        # A percentage or a Pause button, and nothing else.
        #
        # The first version of this test also looked for "installing" and "copying",
        # and matched the CONFIGURATION page instead - "Installation Options" contains
        # the word, so the driver sat there for nine steps reporting 'the installer is
        # working' while a pane that needed a Next click waited for one.
        #
        # A percentage is unambiguous: it only appears while files are being copied. So
        # is a Pause control, which replaces the forward button at the same position.
        import re as _re
        pct = _re.search(r"\d{1,3}\s*%", low)
        working_on = None
        if pct:
            working_on = "percentage %s" % pct.group(0).strip()
        elif "pause" in low:
            working_on = "a Pause control"
        if working_on:
            log("step %d: the installer is working (%s); not clicking" % (step, working_on))
            time.sleep(12)
            continue

        # The welcome window first, and by its full card title.
        #
        # This has to come before find_button: that function's label list contains
        # "install", which is also the first word of a card called "Install MX Linux",
        # and the run that used it clicked Popular Apps instead - a package manager
        # whose own title contains "install", so the loop never escaped.
        card, card_text = find_phrase(words, WELCOME_CARD)
        if card is None:
            card, card_text = find_phrase(words, "start the installer")
        if card is not None:
            log("step %d: welcome window; clicking card %r at %s"
                % (step, card_text, card))
            screen.click(*card)
            time.sleep(10)
            ppm2 = screen.grab("s%02d-after" % step)
            after = " ".join(w[0] for w in screen.text(ppm2))[:90]
            log("step %d: now at %s" % (step, after or "(nothing readable)"))
            continue

        # A dialog with only [OK] on it blocks everything behind it. Press it before
        # anything else is considered - an "Invalid settings" style message looks
        # like a pane whose forward button is missing.
        if not words:
            log("step %d: nothing readable; pressing Return" % step)
            screen.key("ret")
            time.sleep(6)
            continue

        # Outside the welcome window the installer's own pages apply, and there the
        # forward control really is in the corner - so find_button is the right tool.
        target, label = find_button(words, INSTALLER_FORWARD + INSTALLER_AGREE
                                    + INSTALLER_FINISH + ("ok", "yes"), PRIMARY, height=H)

        # Last resort: the bottom-right button row.
        #
        # A screenshotted final page has exactly three controls in a row at y 681..705:
        # Close (511), Back (886), Start (971). The rightmost is always the forward one
        # on this installer, and OCR reads 'Start' unreliably - six steps of this run
        # reported 'no control found' on a page where Start is plainly visible.
        #
        # So if nothing matched by label, take the rightmost word in that row. It is a
        # narrower guess than a coordinate: it still has to be text OCR found, on a line
        # near the bottom, to the right of centre.
        if target is None:
            row = [w for w in words
                   if 0.82 * H < w[2] < 0.92 * H and w[1] > W * 0.60]
            if row:
                row.sort(key=lambda w: w[1])
                pick = row[-1]
                target, label = (pick[1], pick[2]), pick[0]
                log("step %d: no label matched; taking the rightmost bottom control %r at %s"
                    % (step, label, target))

        # When OCR reads nothing at all in the button row.
        #
        # This is what the diagnostic prints finally showed. A 126-word frame of the
        # installer, and the lowest word it found was at y=617 - while the button row
        # sits at y=681..705, plainly visible in the screenshot, and absent from the OCR
        # result entirely. tesseract cannot read that strip: small light text on the
        # installer's coloured bar, below its default sensitivity.
        #
        # That is why two runs reported 'no control found' for six steps each, and why
        # moving PRIMARY twice changed nothing - find_button was never reaching a word
        # to compare it against.
        #
        # So on a pane where the bottom strip has no words, the layout is the remaining
        # evidence: this installer puts its forward control at a fixed place on every
        # page, and it was measured from the captured frame at (971,693).
        if target is None and not row:
            target, label = PRIMARY, "primary (no OCR text in the button row)"
            log("step %d: button row is invisible to OCR; using the measured position %s"
                % (step, PRIMARY))

        if target is None:
            # Say what OCR actually returned, and where.
            #
            # Six steps of two separate runs reported 'no control found' on a page whose
            # Start button is plainly visible in the captured frame, and the log could
            # not say why because it never printed what OCR had found. Everything below
            # the fold is the answer to that question:
            log("step %d: no control found; pressing Return" % step)
            log("step %d: %d words read; the bottom third:" % (step, len(words)))
            for w in sorted(words, key=lambda z: (z[2], z[1])):
                if w[2] > H * 0.66:
                    log("        %r x=%d y=%d w=%d h=%d"
                        % (w[0], w[1], w[2], w[3], w[4]))
            screen.key("ret")
            time.sleep(6)
            continue

        log("step %d: clicking %r at %s (of %d words read)"
            % (step, label, target, len(words)))
        screen.click(*target)
        time.sleep(6)

        # The welcome window is gone once the installer owns the screen, and the
        # installer's own pages say so with their titles.
        ppm2 = screen.grab("s%02d-after" % step)
        after = " ".join(w[0] for w in screen.text(ppm2))[:70]
        log("step %d: now at %s" % (step, after or "(nothing readable)"))

        if any(k in after.lower() for k in
               ("partition", "install", "summary", "user", "location",
                "keyboard", "timezone", "filesystem", "mount")):
            log("step %d: the installer is showing its own pages" % step)

    screen.close()
    log("driver finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
