"""Install GhostBSD from its official ISO, through gbi, its GTK installer.

    python3 bsd/ghostbsd/install.py --iso out/iso/GhostBSD-24.10.1.iso \
                                    --out out/ghostbsd-mate.qcow2

WHY GHOSTBSD AND NOT FREEBBSD

FreeBSD has no graphical installer. bsdinstall draws dialog(1) menus on a text console, and
bsd/freebsd/bake.py installs through its `script` target - the same path the menu wizard takes,
with the prompts skipped. There is no GUI there to drive, so freebsd.yml is unchanged.

GhostBSD is FreeBSD with MATE and its own GTK installer (gbi). It is the BSD-family entry that
actually has a graphical install path, which is the one worth driving by position.

THE MATE IMAGE, NEVER THE XFCE ONE. GhostBSD publishes both. They are the same base with a
different desktop, so baking both would answer the same question twice.

============================================================================
THE ONE THING THAT MAKES THIS DRIVER POSSIBLE
============================================================================

QEMU's usb-tablet is an absolute-positioning HID device, and on most guests that is all a
coordinate driver needs. On GhostBSD it does nothing, and the reason is worth writing down
because it is not a missing driver:

    ugen0.2: <QEMU Tablet Adomax Technology Co., Ltd> at usbus0
    uhid0:   <QEMU QEMU USB Tablet, class 0/0, rev 2.00/2.00, addr 1> on usbus0

The tablet IS attached - to uhid(4). It reports HID subclass 0 / protocol 0, which is the
non-boot protocol; ums(4) accepts only subclass 1 / protocol 2, declines it, and uhid takes
the raw reports without ever writing a position into the input layer. The pointer then sits
at the top-left corner of the root window whatever coordinate the host sends.

Relative motion is not a fix either. qmp.click_relative() walks a PS/2 mouse out of a corner,
and it does move the cursor - a tooltip appeared where one was expected - but X applies its
own acceleration curve to relative deltas, so the error accumulates and a 30-pixel icon is
missed. Measured twice.

The fix is one loader variable:

    set hw.usb.usbhid.enable=1

With it, usbhid + hms claim the tablet at enumeration and absolute coordinates work: measured
by clicking Applications at (60,12) and seeing all six submenus open, then double-clicking
Install GhostBSD at (88,240) and watching gbi appear.

IT HAS TO BE SET IN THE LOADER, NOT AT RUNTIME. This was measured the hard way: from a root
shell, `sysctl hw.usb.usbhid.enable=1` succeeds (the value goes 0 -> 1) but changes nothing,
because the sysctl is read when a device is enumerated and this device is already open by
uhid. `usbconfig -d ugen0.2 reset` does not rebind it either. The loader runs before
enumeration, so the loader is where it goes.

And the loader is reachable: QMP key events reach it, which is not true of everything on this
guest. Verifying that first is what turned this from a guess into a driver - the space key
cleared the autoboot countdown on the boot menu.

============================================================================
THE SEQUENCE
============================================================================

        loader prompt   set hw.usb.usbhid.enable=1 / boot
        X driver menu   scfb  (UEFI framebuffer; the others find no hardware)
        desktop         double-click Install GhostBSD
        gbi             welcome -> timezone -> ... -> install

Everything after the loader is a position, for the reason ubuntu's first draft proved: a
driver that decides each pane from the words tesseract read skipped five panes in one second
when a title did not contain the word it looked for, and then waited out the whole timeout. A
fixed sequence has one failure mode - being wrong about a position - and that is found on the
first run and fixed by changing one number.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import compress     # noqa: E402
import keys         # noqa: E402
import lastframe    # noqa: E402
import live         # noqa: E402
import ocr          # noqa: E402
import qmp as Q     # noqa: E402
import vm           # noqa: E402

# --- measured on a live GhostBSD 24.10.1 MATE session, 1920x1080 --------------------------

# The desktop icon. Double-clicked, and the gap between the two clicks matters: 80 ms opens
# gbi and 500 ms does not. MATE reads two presses further apart than its double-click
# interval as two single clicks, and a single click only selects the icon.
INSTALL_ICON = (88, 240)

# THE DESKTOP PUTS `Install GhostBSD` AND `Trash` IN EITHER ORDER, SO THE ICON IS FOUND BY
# ITS OWN PIXELS, NOT BY ITS ROW.
#
# Measured on two runs of this same image, both at 1920x1080:
#
#     run 42441278741   Computer (88,110)  Home (88,188)  Install GhostBSD (88,240)  Trash (88,340)
#     run 42502676624   Computer (88,110)  Home (88,188)  Trash (88,240)  Install GhostBSD (88,340)
#
# - the two icons swap rows between boots. The old code pressed (88,240) unconditionally, so
# on the second layout it double-clicked `Trash` and opened Caja; the wizard never started,
# every pane click then landed on a file manager, and the run ended with
# `=== THE DISK IS EMPTY (0 MiB) ===`. That is the failure this replaces, and it is exactly
# the kind a fixed coordinate cannot survive: the screen is correct, the number is not.
#
# The installer's icon is a TEAL DISC - the only saturated cyan on this desktop - so it is
# located by scanning that colour in the icon column. `(88, 240)` and `(88, 340)` are the two
# rows it has been seen at; the search covers both and anything in between.
INSTALL_ICON_ROWS = (240, 340)

DOUBLE_CLICK_GAP = 0.08

# The X driver menu that GhostBSD shows when it cannot start X itself. scfb draws on the
# UEFI framebuffer, which is the one adapter this machine actually has; the default (intel)
# and the others find no hardware and drop back here. Four Downs from the top entry.
SCFB_DOWNS = 4

# gbi's own buttons, in the 1920x1080 window it opens centred.
#
# MEASURED PANE BY PANE ON A LIVE GhostBSD 24.10.1 MATE SESSION, one frame each. The wizard
# is a fixed-size window at (557,271)..(1360,802), and its footer does not move:
#
#     Back (1025,781)   Cancel (1150,781)   Next/Install (1293,781)
#
# THE ENTIRE FLOW, and it is nine panes:
#
#     Welcome To GhostBSD!      Language                      Next
#     Keyboard Setup            layout + model                Next
#     Time Zone Selection       continent + city              Next
#     Installation Type         Full disk configuration       Next
#     ZFS Configuration         THE DISK CHECKBOX             Next
#     Boot Option               FreeBSD UEFI loader only      Next
#     User Admin Setup          four fields                   Install
#     Installation in progress  (the installer writes the disk)
#     (window closes)
#
# THIS DRIVER USED TO STOP AFTER THE FIRST PANE. It double-clicked the desktop icon, read
# `gbi welcome`, and returned True - so every run produced a 0 MiB disk and "succeeded".
GBI_BACK = (1025, 781)
GBI_CANCEL = (1150, 781)
GBI_NEXT = (1293, 781)

# ZFS CONFIGURATION: the `Check` box of the only drive row. `Next` is DISABLED until it is
# ticked - measured: the button is grey at 01:25:07 and black at 01:25:20, one click apart.
ZFS_DISK_CHECK = (592, 495)

# USER ADMIN SETUP: four fields, and the note above them says the root password is set to
# the same one.
GBI_USER = {
    "real name": (959, 457),
    "user name": (959, 503),
    "password": (959, 595),
    "verify": (959, 643),
}

# THE INSTALL RUN'S OWN DURATION, AS A NUMBER - see install_to_disk.
INSTALL_SECONDS = 1800

# --- the loader ---------------------------------------------------------------
#
# "3. Escape to loader prompt" IS THE KEY THAT IS USED, and the space key that the menu
# itself advertises ("[Space] to pause") is NOT - see _press_until_prompt for the CI run
# that settled it. Space pauses a countdown that has usually already expired by the time
# this driver has a QMP connection, and it means something else once the menu is up.
LOADER_PROMPT_KEY = "3"
USBHID_SETTING = "set hw.usb.usbhid.enable=1"


def log(msg: str) -> None:
    print(f"[ghostbsd {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Driver:
    def __init__(self, qmp_path: str, work: str, *, width: int = 1920,
                 height: int = 1080) -> None:
        shots = os.path.join(work, "shots")
        os.makedirs(shots, exist_ok=True)
        deadline = time.time() + 180
        while not os.path.exists(qmp_path):
            if time.time() > deadline:
                raise SystemExit(f"no QMP socket at {qmp_path} after 180s")
            time.sleep(1)
        self.screen = ocr.Screen(qmp_path, shots, width=width, height=height)
        self.q = self.screen.q
        self.work = work
        self.n = 0
        self.words: list[tuple] = []

    def close(self) -> None:
        """Release the QMP connection before lib/steps.py opens its own.

        qemu serves one client per socket, so the YAML walk cannot start while this driver
        still holds it - the same boundary macos/15/bake.py documents.
        """
        try:
            self.screen.close()
        except Exception:                              # noqa: BLE001
            pass

    def look(self, label: str) -> None:
        self.n += 1
        ppm = self.screen.grab(f"{self.n:03d}-{label}")
        self.words = self.screen.text(ppm) if ppm else []
        log(f"  {label}: {' | '.join(w[0] for w in self.words[:20])}")

    def install_icon(self) -> tuple:
        """Where `Install GhostBSD` actually is, from the teal disc it draws.

        THE ICON ITSELF IS THE ONLY RELIABLE MARK. Its row swaps with `Trash` between boots
        (see INSTALL_ICON_ROWS), so a constant is wrong half the time and no reading of the
        LABEL helps - the label is drawn in the same font as `Trash`, and both are white on
        the same wallpaper.

        The disc is a saturated cyan circle, roughly (60,150,170) at its rim, and it is the
        only such colour in the left column. Scanning for it in the icon column and taking
        the middle of what is found answers with a coordinate that is right for either
        layout. If nothing matches, the first measured row is returned - the caller then
        behaves exactly as the old fixed constant did rather than pressing nothing.
        """
        ppm = self.screen.grab(f"{self.n:03d}-install-icon")
        if not ppm:
            return INSTALL_ICON
        try:
            from PIL import Image
        except ImportError:
            return INSTALL_ICON
        try:
            with Image.open(ppm) as image:
                rgb = image.convert("RGB")
                width, _ = rgb.size
                px = rgb.load()
                rows = []
                for y in range(40, 420):
                    run = best = 0
                    for x in range(30, 160):
                        r, g, b = px[x, y]
                        # the teal of the disc, and nothing else on this desktop
                        if r < 110 and g > 95 and b > 95 and g > r + 30 and b > r + 25:
                            run += 1
                            best = max(best, run)
                        else:
                            run = 0
                    if best >= 10:
                        rows.append(y)
        except OSError:
            return INSTALL_ICON
        if not rows:
            log("  the installer icon was not found by its colour; using the measured row")
            return INSTALL_ICON
        # one band per icon: group the matching rows that are within a few pixels
        bands, cur = [], [rows[0]]
        for y in rows[1:]:
            if y - cur[-1] <= 8:
                cur.append(y)
            else:
                bands.append((cur[0], cur[-1]))
                cur = [y]
        bands.append((cur[0], cur[-1]))
        top = min(bands, key=lambda band: band[1] - band[0])
        centre = (top[0] + top[1]) // 2
        log(f"  the installer icon is at (88,{centre}) - found by its teal disc, "
            f"bands {bands}")
        return (88, centre)

    def click(self, xy, why: str, settle: float = 2.0) -> None:
        log(f"click {xy} - {why}")
        Q.click(self.q, *xy)
        time.sleep(settle)

    def double_click(self, xy, why: str, settle: float = 20.0) -> None:
        """Two presses 80 ms apart, in one pointer position.

        WHY NOT Q.click() TWICE. Each Q.click() moves the pointer, presses and releases;
        called twice the presses end up farther apart than MATE's double-click interval and
        the icon is only selected. Measured: 500 ms between clicks did nothing, 80 ms opened
        gbi on the first try.
        """
        log(f"double-click {xy} - {why}")
        x, y = xy
        Q._send(self.q, "double press", [
            {"type": "abs", "data": {"axis": "x", "value": Q.to_abs(x, 1920)}},
            {"type": "abs", "data": {"axis": "y", "value": Q.to_abs(y, 1080)}},
            {"type": "btn", "data": {"down": True, "button": "left"}},
        ])
        time.sleep(DOUBLE_CLICK_GAP)
        Q._send(self.q, "double release", [
            {"type": "btn", "data": {"down": False, "button": "left"}},
        ])
        time.sleep(DOUBLE_CLICK_GAP)
        Q._send(self.q, "double press 2", [
            {"type": "btn", "data": {"down": True, "button": "left"}},
        ])
        time.sleep(DOUBLE_CLICK_GAP)
        Q._send(self.q, "double release 2", [
            {"type": "btn", "data": {"down": False, "button": "left"}},
        ])
        time.sleep(settle)

    def type_line(self, text: str, why: str) -> None:
        log(f"type {text!r} - {why}")
        unsupported = keys.type_text(self.q, text, per_char=0.05)
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r} - no qcode for those characters")
        Q.key(self.q, "ret")

    def clear_and_type(self, xy, value: str) -> None:
        """Click the field, empty it, then type into it."""
        Q.click(self.q, *xy)
        time.sleep(1.2)
        Q.key_combo(self.q, "ctrl", "a")
        time.sleep(0.3)
        for _ in range(40):
            Q.key(self.q, "backspace")
            time.sleep(0.02)
        unsupported = keys.type_text(self.q, value, per_char=0.05)
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(0.6)


def _at_loader_prompt(drv: Driver) -> bool:
    """Whether the screen is the loader's own OK prompt.

    THE MARKER IS THE PROMPT'S HELP LINE, which is two sentences that appear nowhere else:

        Type '?' for a list of commands, 'help' for more detailed help.
        OK _

    Matching "OK" alone would be unsafe - it is two letters and turns up inside other words
    after OCR - so the test is on "detailed help" or "list of commands".
    """
    joined = " ".join(w[0].lower() for w in drv.words)
    return "detailed help" in joined or "list of commands" in joined


def _clear_line(drv: Driver) -> None:
    """Empty whatever is on the loader's command line.

    KEYS PRESSED TO REACH THE PROMPT CAN LAND ON IT. "3" is what escapes the menu, and once
    the prompt is up every further "3" is typed as text - so the line can hold a few of them
    by the time the screen is read. Ctrl-U is the loader's kill-line, and sending it before
    the real command is cheaper than parsing what might be there.
    """
    Q.key_combo(drv.q, "ctrl", "u")
    time.sleep(0.4)


def _press_until_prompt(drv: Driver, seconds: float = 90.0) -> bool:
    """Reach the loader's OK prompt from the boot menu, by pressing the menu's own hotkey.

    WHY "3" AND NOT SPACE FIRST.

    The first version of this paused the countdown with space and then pressed "3". It
    failed on CI, and the log says why: qemu started at 10:27:19 and the space keys were
    sent from 10:27:19 to 10:27:34, at 0.5 s intervals - which is OVMF's own initialisation,
    where no menu exists yet. The menu first appeared at 10:27:37, by which time the keys
    were spent, the countdown was never paused, and the "3" that followed did nothing.

    Space also has a meaning in this menu, so pressing it blind is not the neutral act the
    first version assumed. "3" selects "Escape to loader prompt" directly, and on the OVMF
    screen it means nothing at all - so the same key is safe before the menu and correct
    at it.

    THE PROMPT IS VERIFIED BEFORE ANYTHING IS TYPED. Typing "set hw.usb.usbhid.enable=1"
    into the MENU instead of the prompt would be read as hotkeys - the string contains
    "1", which selects "Boot Multi user" - so the check is not a formality.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        drv.look("boot")
        if _at_loader_prompt(drv):
            log("the loader prompt is up")
            return True

        joined = " ".join(w[0].lower() for w in drv.words)
        # THE MENU NAMES THE ENTRY THIS PRESSES. If these words are on screen the countdown
        # is running (or paused) and "3" is the right key. If they are not, the boot has
        # either not reached the menu yet or has already started - so the key is held back
        # until the menu is actually there.
        # THE MENU SAYS `Boot Multi user` AND A BARE `Escape`, NOT `escape to loader`.
        #
        # MEASURED ON RUN 6eeff43, which failed the loader step twice in a row with both rounds
        # exhausting their 90 seconds while the menu was plainly on screen:
        #
        #     13:18:37  boot: Welcome | to | GhostBSD | Boot | Multi | user | [Enter] |
        #                     Boot | Single | user | Escape
        #
        # The test was `"multi user" in joined and "escape to loader" in joined`, and the menu
        # carries no `loader` word at all - so the hotkey was never pressed, the countdown ran
        # out, and the guest booted straight past the prompt. `multi user` IS there, and
        # `escape` alone is the other half.
        if "multi user" in joined and "escape" in joined:
            Q.key(drv.q, LOADER_PROMPT_KEY)
            time.sleep(1.2)
        else:
            time.sleep(1.5)
    return False


def set_usbhid_in_loader(drv: Driver) -> bool:
    """Reach the loader prompt and set the one variable that makes absolute pointing work.

    WHY THIS IS NOT IN lib/qmp.py. The variable is GhostBSD's problem, not QEMU's: the
    tablet is a correct absolute-positioning device and every Linux guest here uses it
    unchanged. Only this one kernel declines it, so only this driver works around it.

    A MISSED MENU IS RETRIED ONCE, WITH A RESET. If the countdown won the race the guest
    boots without the variable, and the only way back to the loader is a reset.
    """
    for attempt in (1, 2):
        if attempt == 2:
            log("the loader prompt was missed; resetting and trying once more")
            drv.q.execute("system_reset")
            time.sleep(2.0)
        if _press_until_prompt(drv):
            break
        if attempt == 2:
            log("=== THE LOADER PROMPT NEVER APPEARED ===")
            log(f"  last screen: {' | '.join(w[0] for w in drv.words[:20])}")
            return False

    # ANY "3" THAT ARRIVED AFTER THE PROMPT IS ON THE LINE. Clear it before the command,
    # or the loader sees "33set hw.usb..." and rejects it.
    _clear_line(drv)

    # THE SETTING IS THE POINT OF THIS FUNCTION: without it every click below lands on the
    # top-left corner of the screen and the run looks like a driver bug.
    log(f"loader: {USBHID_SETTING}")
    unsupported = keys.type_text(drv.q, USBHID_SETTING, per_char=0.05)
    if unsupported:
        log(f"  COULD NOT TYPE {unsupported!r}")
        return False
    Q.key(drv.q, "ret")
    time.sleep(1.5)
    drv.look("after set")

    # THE SET IS CONFIRMED BY ITS OWN SILENCE. A loader `set` prints nothing on success and
    # an error line on failure, so a screen that is still the prompt - with no "no such
    # command" or "unknown" on it - is the confirmation.
    joined = " ".join(w[0].lower() for w in drv.words)
    if "no such" in joined or "unknown" in joined:
        log("=== THE LOADER REJECTED THE SETTING ===")
        log(f"  screen: {joined[:200]}")
        return False

    # THE PROMPT HAS TO STILL BE THERE BEFORE "boot" IS TYPED INTO IT.
    #
    # Run 42398681489 logged `the loader prompt is up`, typed the setting, and then showed
    # the BOOT MENU again at `after set`:
    #
    #     17:59:53  the loader prompt is up
    #     17:59:53  loader: set hw.usb.usbhid.enable=1
    #     17:59:59  after set: Boot | Multi user | [Enter] | ... | 7. | Boot Options
    #
    # so the four characters of "boot" went to a menu, where each letter is a hotkey, and
    # the machine then did something the driver never asked for. Typing a command into a
    # menu is worse than not booting at all: the keys are choices.
    #
    # THE FIX IS TO RE-ENTER THE PROMPT, NOT TO RETRY THE BOOT. `3` is the menu's own
    # "escape to loader prompt" hotkey, so one press puts it back; `_clear_line` then wipes
    # whatever the failed attempt left on the command line.
    if not _at_loader_prompt(drv):
        log("  the prompt disappeared after `set`; re-entering it before boot")
        for _ in range(3):
            Q.key(drv.q, LOADER_PROMPT_KEY)
            time.sleep(1.5)
            drv.look("re-entering the prompt")
            if _at_loader_prompt(drv):
                break
        if not _at_loader_prompt(drv):
            log("=== THE LOADER PROMPT COULD NOT BE RE-ENTERED ===")
            log(f"  screen: {' | '.join(w[0] for w in drv.words[:24])}")
            return False
        _clear_line(drv)

    log("loader: boot")
    keys.type_text(drv.q, "boot", per_char=0.05)
    Q.key(drv.q, "ret")
    return True


def choose_scfb(drv: Driver, timeout: int) -> bool:
    """Take the scfb entry from GhostBSD's X driver menu, when it appears.

    IT MAY NOT APPEAR, and that is not a failure: the menu is shown when X cannot start on
    the default driver. On this machine (UEFI, std VGA) it always has, but the driver treats
    "the desktop is already up" and "the menu was answered" as the same success.
    """
    log("waiting for the desktop or the X driver menu")
    deadline = time.time() + timeout
    while time.time() < deadline:
        drv.look("boot")
        joined = " ".join(w[0].lower() for w in drv.words)

        # THE DESKTOP IS RECOGNISED BY ITS PANEL, AND ITS FIRST WORD IS THE ONE OCR GETS
        # WRONG.
        #
        # MATE draws its panel along the top as `Applications Places System`, and tesseract
        # returns `Ppplications` for the first word on every frame of a live session:
        #
        #     boot: @ | Ppplications | Places | System | Sun | Nov | 8, | 01:19 | 8
        #
        # so `"applications" in joined` was FALSE on a fully drawn desktop, the wait ran to
        # its deadline, and the run ended with `=== NEITHER THE DESKTOP NOR THE DRIVER MENU
        # APPEARED ===` - which is exactly what the last CI run did. `places` is read cleanly
        # and appears on no other screen in this boot.
        if "places" in joined:
            log("the MATE desktop is up")
            return True

        # THE MENU IS RECOGNISED BY ITS TITLE, NOT BY ONE OF ITS ENTRIES.
        #
        # Run 42364259880 looked for the word "scfb" and failed while the menu was on
        # screen: the selected row is drawn inverted and OCR read only that one, as
        # `Start Desktop with Legacy Intel dri`. The bottom four of the five entries were
        # invisible to it. Matching the title - which is present whatever row is selected -
        # is the question that actually has one answer.
        if "failed to auto" in joined or ("start desktop with" in joined
                                          and ("driver" in joined or "dri" in joined)):
            log("the X driver menu is up; choosing scfb (the UEFI framebuffer)")
            for _ in range(SCFB_DOWNS):
                Q.key(drv.q, "down")
                time.sleep(1.0)
            Q.key(drv.q, "ret")
            time.sleep(25)
            continue

        time.sleep(8)

    log("=== NEITHER THE DESKTOP NOR THE DRIVER MENU APPEARED ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:20])}")
    return False


def install_to_disk(drv: Driver, args) -> bool:
    """Walk gbi's nine panes: A MEASURED COORDINATE, THEN A MEASURED WAIT.

    NOTHING IS READ TO DECIDE ANYTHING. Every step is a number in this file and a duration;
    the screen is read afterwards and the reading goes into the log, where it is useful for
    a post-mortem and dangerous as a decision. This is the rule ubuntu and kubuntu
    follow, and the reason they are green while this driver kept "succeeding" without
    writing a disk.

    THE WHOLE FLOW WAS MEASURED ON A LIVE SESSION, one frame per pane - see the constants
    block. The window is fixed size, so one footer coordinate is right for all of them;
    the two panes that need a click of their own are noted where they happen.
    """
    # 1. WELCOME -> KEYBOARD. English is preselected.
    drv.click(GBI_NEXT, "Welcome: Next (English)", settle=12.0)
    drv.look("keyboard")

    # 2. KEYBOARD -> TIME ZONE. English (US) is preselected.
    drv.click(GBI_NEXT, "Keyboard: Next (English US)", settle=12.0)
    drv.look("time zone")

    # 3. TIME ZONE -> INSTALLATION TYPE. America/Adak is preselected; the requirement is a
    #    disk that boots, not a particular region, and this is the installer's own default.
    log("time zone left at the installer's default")
    drv.click(GBI_NEXT, "Time Zone: Next", settle=12.0)
    drv.look("installation type")

    # 4. INSTALLATION TYPE -> ZFS. `Full disk configuration` is preselected.
    drv.click(GBI_NEXT, "Installation Type: Next (Full disk configuration)", settle=14.0)
    drv.look("zfs configuration")

    # 5. ZFS CONFIGURATION. THE DISK MUST BE TICKED OR `Next` STAYS DISABLED - measured:
    #    the button is grey until the checkbox at (592, 495) is clicked.
    drv.click(ZFS_DISK_CHECK, "ZFS: tick the disk", settle=5.0)
    drv.look("disk ticked")
    drv.click(GBI_NEXT, "ZFS: Next", settle=14.0)
    drv.look("boot option")

    # 6. BOOT OPTION. `FreeBSD UEFI loader only` is preselected - the right answer on OVMF.
    drv.click(GBI_NEXT, "Boot Option: Next (FreeBSD UEFI loader only)", settle=14.0)
    drv.look("user admin")

    # 7. USER ADMIN SETUP. Four fields; the installer sets root's password to the same one.
    log("filling the user form")
    drv.clear_and_type(GBI_USER["real name"], args.user)
    drv.clear_and_type(GBI_USER["user name"], args.user)
    drv.clear_and_type(GBI_USER["password"], args.password)
    drv.clear_and_type(GBI_USER["verify"], args.password)
    drv.look("user filled")

    # 8. INSTALL. The same footer button, whose label has changed.
    drv.click(GBI_NEXT, "User Admin: Install", settle=25.0)
    drv.look("installing")

    # 9. A MEASURED WAIT. The `Installation in progress` pane is a slideshow and a progress
    #    bar; measuring "is it done" would be a detector, so the duration is the answer.
    #    MEASURED: from `Install` to the window closing is about 8 minutes on this ISO.
    #    THIRTY MINUTES is that with room for a slower runner.
    #
    #    THE POINTER IS CLICKED INSIDE THE LOOP: MATE blanks its own display when idle - the
    #    frame goes solid black, which is what a manual session showed after ten minutes -
    #    and a blanked display reads as an empty screen for every later step.
    log(f"waiting {INSTALL_SECONDS}s for the install (measured: about 8 minutes)")
    started = time.time()
    while time.time() - started < min(INSTALL_SECONDS, args.timeout):
        try:
            Q.click(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                          # noqa: BLE001
            pass
        drv.look("installing")
        time.sleep(20)

    log("=== THE INSTALL RUN ENDED, AS MEASURED ===")
    return True


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=None)
    p.add_argument("--disk-size", default="64G")
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--timeout", type=int, default=5400)
    # THE ACCOUNT THE INSTALLER CREATES. `vmuser`/`admin` is what every other bake here
    # uses; gbi also sets root's password to the same value, which it says above the form.
    #
    # THIS DEFAULT WAS `docker` AFTER commit 6eeff43 RENAMED EVERYTHING ELSE, because that
    # commit's sweep was over `steps/*.yaml` and `lib/`, and this file is neither. The disk it
    # produced carried whichever way this default pointed, and the ssh stage - which reads its
    # account from `steps/ssh.yaml`, a file the sweep DID change - then asked for `vmuser`
    # against a disk whose installer form had been filled with the old word.
    p.add_argument("--user", default=os.environ.get("GHOSTBSD_USER", "vmuser"))
    p.add_argument("--password", default=os.environ.get("GHOSTBSD_PASSWORD", "admin"))
    p.add_argument("--firmware-code", default=os.environ.get(
        "FIRMWARE_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd"))
    p.add_argument("--firmware-vars", default=os.environ.get(
        "FIRMWARE_VARS", "/usr/share/OVMF/OVMF_VARS_4M.fd"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "ghostbsd-mate.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    # THE FIRMWARE PAIR IS THE GUEST'S NVRAM, so the vars file is a writable COPY. A shared
    # template would carry one guest's boot entries into the next run.
    if not (os.path.isfile(args.firmware_code) and os.path.isfile(args.firmware_vars)):
        raise SystemExit(
            f"=== UEFI FIRMWARE NOT FOUND ===\n"
            f"  code: {args.firmware_code}\n  vars: {args.firmware_vars}\n"
            f"GhostBSD's loader is built for the EFI system partition: with SeaBIOS the ISO "
            f"drops to a loader prompt instead of the installer.")
    vars_copy = os.path.join(work, "ghostbsd.vars")
    if os.path.exists(vars_copy):
        os.unlink(vars_copy)
    import shutil
    shutil.copyfile(args.firmware_vars, vars_copy)
    log(f"vars: writable copy of {os.path.basename(args.firmware_vars)}")

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
        ],
        shape=vm.Shape(
            cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
            firmware_code=args.firmware_code, firmware_vars=vars_copy,
            resolution=(1920, 1080),
            vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1"),
        ),
        stdout=os.path.join(work, "console.log"),
    )

    def _stop(signum: int, _frame) -> None:
        log(f"signal {signum}: stopping the guest")
        try:
            guest.stop(acpi_first=False)
        except Exception:                              # noqa: BLE001
            pass
        os._exit(143)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _stop)

    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    guest.start()
    log(f"qemu pid {guest.pid}, vnc {guest.shape.vnc_bind}:{5900 + guest.shape.vnc_display}")

    snap = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         guest.paths.record, frames, os.environ.get("SNAPSHOT_INTERVAL", "2")],
        stdout=open(os.path.join(frames, "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )
    art = live.start([frames], os.path.join(work, "install.mp4"),
                     os.path.join(work, "last-frame.png"),
                     every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "30")))

    ok = False
    try:
        drv = Driver(guest.paths.drive, work)
        if not set_usbhid_in_loader(drv):
            log("=== THE LOADER STEP FAILED ===")
        elif choose_scfb(drv, args.timeout):
            # THE WALK IS A YAML FILE NOW - see steps/install.yaml. Its first step is a
            # `python:` extension in steps/panes.py, because the desktop puts
            # `Install GhostBSD` and `Trash` in either order and a fixed coordinate presses
            # the wrong one half the time; that extension finds the installer's icon by the
            # teal disc it draws, then does the 80 ms double-click.
            #
            # THE QMP SOCKET IS ALREADY OPEN, so this driver is closed first: qemu serves one
            # client per socket and steps.run_file opens its own.
            drv.close()
            import steps
            walk_path = os.path.join(HERE, "steps", "install.yaml")
            log(f"=== the walk: {os.path.basename(walk_path)} ===")
            ok, ran = steps.run_file(
                walk_path, guest.paths.drive, work=work,
                user=args.user, password=args.password,
            )
            log(f"{len(ran)} step(s) ran" if ok
                else f"the walkthrough failed after {len(ran)} step(s)")
        else:
            log("=== THE LIVE DESKTOP DID NOT COME UP ===")
    finally:
        try:
            ppm = os.path.join(work, "final.ppm")
            c = Q.QMP(guest.paths.drive)
            try:
                c.execute("screendump", filename=ppm)
            finally:
                c.close()
            lastframe.convert(ppm, os.path.join(work, "last-frame.png"))
        except Exception:                              # noqa: BLE001
            pass
        guest.stop(acpi_first=False)
        snap.terminate()
        art.stop()

    # A DISK WITH NOTHING ON IT IS NOT A SUCCESS, AND THAT WAS INVISIBLE.
    #
    # The caller only compresses when the walk reports success, so a driver that pressed the
    # right coordinates on the wrong screen still "passed": endeavour's run 42454363051
    # clicked the Asturian wizard's footer at the English coordinate, never left Welcome,
    # reported success, and uploaded `out/endeavour.qcow2: 0 MiB`. The workflow was
    # green and the artefact was empty.
    #
    # A written system is gigabytes; an unwritten sparse image is kilobytes. The threshold
    # is far from both.
    if ok and os.path.exists(disk) and os.path.getsize(disk) < 64 * 2**20:
        log(f"=== THE DISK IS EMPTY ({os.path.getsize(disk) >> 20} MiB) - THE INSTALL DID NOT HAPPEN ===")
        ok = False

    if ok and os.path.exists(disk):
        log(f"compressing {disk} -> {args.out}")
        compress.compress(disk, args.out)
        log(f"{args.out}: {os.path.getsize(args.out) >> 20} MiB")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
