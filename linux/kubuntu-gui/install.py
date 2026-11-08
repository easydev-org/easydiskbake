"""Install Kubuntu from its desktop ISO, through Calamares.

    python3 linux/kubuntu-gui/install.py --iso out/iso/kubuntu-26.04.1-desktop-amd64.iso \
                                         --out out/kubuntu-gui.qcow2

WHY CALAMARES NEEDS ITS OWN DRIVER

Calamares is not Ubuntu's installer. Ubuntu's is a Flutter application that presents thirteen
panes as a floating card with a footer; Calamares presents nine in a sidebar layout with the
buttons at the very bottom right. The coordinates do not transfer and neither does the order:

    Welcome -> Location -> Keyboard -> Customize -> Partitions -> Users -> Summary -> Install

WHY THE SEQUENCE IS POSITIONS AND NOT DETECTORS

ubuntu-gui's first version decided each pane from the words tesseract read, and on its first CI
run one wrong pair of words skipped five panes in a second and then waited out the whole
timeout. drive.py's docstring has the argument:

    A fixed sequence has one failure mode - being wrong about a position - and that is found on
    the first run and fixed by changing one number. A reading driver has a failure mode per pane
    and per wallpaper.

So: click where the control was measured to be, wait for the next pane to settle, and read the
screen only to write it into the log.

TWO THINGS ABOUT CALAMARES THAT ARE NOT OBVIOUS

1. THE FIRST CLICK MAY ONLY FOCUS THE WINDOW. Measured on a live ISO: a click at the Next button
   did nothing, and the same click again after the window had focus advanced. The driver clicks
   twice with a short gap for every footer button, which is harmless when the first one works.

2. Alt+N IS A RELIABLE FALLBACK and is used for the panes where a click has been seen to miss.
   Calamares binds it to Next on every pane of the wizard.
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
import live         # noqa: E402
import ocr          # noqa: E402
import qmp as Q     # noqa: E402
import uefi         # noqa: E402
import vm           # noqa: E402
import lastframe    # noqa: E402

# --- measured on a live Kubuntu 26.04.1 desktop ISO, 1920x1080 ---------------------------
WELCOME_INSTALL = (1326, 841)   # the welcome screen's own "Install Kubuntu" button

# THE FOOTER BUTTONS, MEASURED FROM A CI FRAME AND NOT FROM A LOCAL ONE.
#
# The first version of these was measured by clicking around a local run until the wizard
# advanced, and it went to CI and did nothing at all: every pane's OCR still read the
# welcome screen, the sidebar still highlighted "Acoyida", and the driver walked all nine
# steps and then waited out the timeout. The frame CI uploaded as last-frame.png is what
# settled it - tesseract puts the "Siguiente" label at x 1729..1785, y 1052..1065, centre
# (1757, 1058). The old value was (1775, 1040): eighteen pixels right and eighteen up,
# which is above the button and on its right edge. A footer button is 35 px tall and these
# coordinates were outside all of it.
#
# CALAMARES RIGHT-ALIGNS THE FOOTER, so these move when the label changes width, which is
# one more reason the UI language is pinned below.
NEXT = (1757, 1058)             # footer "Next" / "Siguiente", and the green Install on Summary
BACK = (1662, 1058)
CANCEL = (1873, 1058)

# The Partitions pane. "Erase disk" is the first radio; its label is not clickable, the circle is.
ERASE_DISK_RADIO = (208, 73)
INSTALL_NOW = (1058, 535)       # the confirmation dialog's button

# The Users pane.
# ============================================================================
# THE ACCOUNT FORM, AS FIVE FIXED POSITIONS MEASURED ONCE FROM A CI FRAME.
# ============================================================================
#
# THIS FILE DOES NOT LOOK ANYTHING UP AT RUN TIME. Every coordinate below came from a
# screenshot and is a number; the screen is read afterwards only to say in the log what
# happened and to decide whether a step worked.
#
# That rule is not stylistic. Run 42364250527 searched for the confirmation dialog's
# button by its label and clicked whatever position the search returned - the Summary
# pane's own prose contains the word it was looking for, so it clicked a sentence at
# (896, 424), the install never started, and the run waited out its ninety-minute timeout.
# A driver that reads the screen to decide WHERE to click can be talked into clicking
# prose; a driver that reads it to decide WHETHER the click worked cannot.
#
# THE FIRST VERSION OF THESE WAS MEASURED ON A WINDOWED INSTALLER and every one of them
# landed on blank panel when CI ran the same wizard full-screen. These five were read off
# the Users pane in the frame CI uploaded, and four of them were then confirmed by a run
# that filled the form with them:
#
#     login     (296, 120)      password  (296, 254)
#     host      (296, 188)      confirm   (500, 254)
#
# `name` is the one above `login`, and the rows are 68 px apart.
ACCOUNT = {
    "name": (296, 52),
    "login": (296, 120),
    "host": (296, 188),
    "password": (296, 254),
    "confirm": (500, 254),
}

# THE `Log in automatically without asking for the password.` CHECKBOX. Its box is at the
# left of that line, measured from the same frame: the label starts at x 221 and the box is
# 18 px left of it.
LOGIN_AUTOMATICALLY = (203, 297)

# CALAMARES' SIDEBAR IS 190 px WIDE AND LISTS EVERY PANE AT ALL TIMES. Anything that
# decides whether the wizard advanced has to look right of this, or it will read the name
# of the pane it is waiting for before the pane arrives.
#
# 190 IS MEASURED, NOT GUESSED, AND IT MATTERS THAT IT IS EXACT. The sidebar is x 0..189
# (checked against a CI frame: the dark panel ends at x=189 and the page background starts
# at 190). A looser value is not a harmless safety margin here - at 220 it swallowed the
# first word of every form label, because the labels begin at x=196. The phrase matcher
# then read "is your name?" instead of "What is your name?" and found nothing.
SIDEBAR_WIDTH = 190


def log(msg: str) -> None:
    print(f"[kubuntu-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Driver:
    def __init__(self, qmp_path: str, work: str, *, width: int = 1920,
                 height: int = 1080) -> None:
        shots = os.path.join(work, "shots")
        os.makedirs(shots, exist_ok=True)
        deadline = time.time() + 120
        while not os.path.exists(qmp_path):
            if time.time() > deadline:
                raise SystemExit(f"no QMP socket at {qmp_path} after 120s")
            time.sleep(1)
        self.screen = ocr.Screen(qmp_path, shots, width=width, height=height)
        self.q = self.screen.q
        self.work = work
        self.n = 0
        self.words: list[tuple] = []

    def look(self, label: str) -> None:
        self.n += 1
        ppm = self.screen.grab(f"{self.n:03d}-{label}")
        self.words = self.screen.text(ppm) if ppm else []
        log(f"  {label}: {' | '.join(w[0] for w in self.words[:18])}")

    def screen_text(self, content_only: bool = False) -> str:
        """The words on screen, optionally without the sidebar.

        THE SIDEBAR HAS TO BE EXCLUDED AND THIS IS THE WHOLE REASON. Calamares lists every
        pane down the left at all times, so "Location", "Keyboard", "Partitions", "Summary"
        and "Finish" are on screen from the welcome pane onwards. A marker taken from that
        list matches before the wizard has done anything, which is exactly how the first
        version of the finish loop would have declared success on its first poll.

        The content area is everything right of the sidebar, which is 190 px wide here and
        fixed by Calamares rather than by the window.
        """
        if not content_only:
            return " ".join(w[0].lower() for w in self.words)
        return " ".join(w[0].lower() for w in self.words if w[1] > SIDEBAR_WIDTH)

    def click(self, xy, why: str, settle: float = 1.0) -> None:
        log(f"click {xy} - {why}")
        Q.click(self.q, *xy)
        time.sleep(settle)

    def close(self) -> None:
        """Release the QMP connection before lib/steps.py opens its own.

        qemu serves one client per socket, so the YAML walk cannot start while this driver
        still holds it - the same boundary macos/15/bake.py documents.
        """
        try:
            self.screen.close()
        except Exception:                              # noqa: BLE001
            pass

    def click_twice(self, xy, why: str, settle: float = 12.0) -> None:
        """Click, then click again. The first may only give the window focus."""
        self.click(xy, why, settle=0.8)
        self.click(xy, f"{why} (second click)", settle=settle)

    def alt_n(self, why: str, settle: float = 12.0) -> None:
        log(f"alt+n - {why}")
        Q.key_combo(self.q, "alt", "n")
        time.sleep(settle)

    def advance(self, pane: str, *, settle: float = 12.0,
                marker: str = "") -> bool:
        """Leave `pane`, and REFUSE TO CONTINUE if the screen did not change.

        THIS IS THE FIX FOR THE FIRST CI RUN, and it is the most important method here.
        That run clicked Next nine times at a coordinate eighteen pixels outside the
        button, and reported nothing wrong: every step logged, every pane "visited", and
        the screen never changed once. The only thing that ended it was the overall
        timeout - ninety minutes later, with the installer still on its welcome pane.

        A step that did not move is not a slow step. Calamares repaints within a second of
        a working click - panes in this run took 1-2 s - so a second attempt is enough, and
        after that the run is over. Failing in thirty seconds with the pane named is worth
        far more than a timeout that names nothing.

        ONE CLICK FIRST, NOT TWO, AND THAT IS A CHANGE OF MIND. click_twice() exists
        because a first click on an unfocused window is sometimes taken only as focus. On
        THIS wizard the second click is a liability: Calamares advances in one to two
        seconds, so two clicks 0.8 s apart land the second one on the NEXT pane's own Next
        button and skip a pane entirely - silently, having configured somewhere it did not
        mean to. The retry below gets the same protection against the focus problem without
        ever pressing twice blind.
        """
        before = self.screen_text(content_only=True)

        # THREE ATTEMPTS, IN THE ORDER A PERSON WOULD TRY THEM: a click, the click again in
        # case the first only focused the window, then Alt+N - which Calamares binds to Next
        # on every pane. Alt+N is not first because on the Summary pane it starts the
        # installation, and a shortcut fired into the wrong pane is worse than a repeated
        # click.
        for attempt, how in ((1, "click"), (2, "click again"), (3, "alt+n")):
            if how == "alt+n":
                self.alt_n(f"{pane}: Next, by keyboard", settle=settle)
            else:
                self.click(NEXT, f"{pane}: Next ({how})", settle=settle)
            self.look(f"after {pane}")
            if self._moved(before, marker):
                return True

        log(f"=== {pane.upper()} DID NOT ADVANCE, BY CLICK OR BY KEYBOARD ===")
        log(f"  before: {before[:200]}")
        log(f"  now   : {self.screen_text(content_only=True)[:200]}")
        return False

    def _moved(self, before: str, marker: str) -> bool:
        """Whether the CONTENT moved, which is the only thing a click on Next changes.

        A DIFFERENCE, NOT A MARKER, AND THE SIDEBAR IS WHY. Every pane name is permanently
        visible in Calamares' sidebar, so a marker drawn from it is already true before the
        click. What a working Next does is replace the whole content area, and what a click
        that landed outside every control does is nothing at all - including, measured in
        the first CI run, for nine clicks in a row.

        `marker` is kept as an optional extra: when the caller names a word that appears in
        the pane being REACHED, matching it in the content area is stronger evidence than a
        difference. It is checked in the content area for the same reason.
        """
        now = self.screen_text(content_only=True)
        if marker:
            return marker.lower() in now
        return bool(now) and now != before

    def type_into(self, xy, text: str) -> None:
        Q.click(self.q, *xy)
        time.sleep(1.5)
        # CTRL+A, NOT SUPER+A. Super+A opens GNOME's application grid on Ubuntu and swallowed
        # the following keystrokes; Calamares runs under KDE but ctrl is the portable choice.
        for _ in range(2):
            Q.key_combo(self.q, "ctrl", "a")
            time.sleep(0.4)
            Q.key(self.q, "backspace")
            time.sleep(0.3)
        for ch in text:
            Q.key(self.q, "spc" if ch == " " else ch)
            time.sleep(0.3)
        time.sleep(1.0)


def wait_for_live_desktop(drv: Driver, timeout: int) -> bool:
    """Until the ISO's live session has finished booting.

    THE MARKER IS THE WELCOME SCREEN'S OWN BUTTON, because that is the thing the next click
    needs. Booting a live session takes one to three minutes and the desktop appears well
    before it is ready to take input.
    """
    log("waiting for the live desktop")
    deadline = time.time() + timeout
    while time.time() < deadline:
        drv.look("boot")
        if drv.words and any("kubuntu" in w[0].lower() for w in drv.words):
            log("the live session is up")
            return True
        time.sleep(10)
    log("=== THE LIVE DESKTOP DID NOT APPEAR ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:24])}")
    return False


def walk_calamares(drv: Driver, args) -> bool:
    """Click through Calamares, and stop the first time the wizard does not move.

    THE RETURN VALUE IS EARNED, NOT ASSUMED. The first CI run of this driver had no return
    value from this part at all: it clicked nine measured positions, none of which advanced
    anything, and then reported success into a timeout. Every step below either confirms
    that the next pane arrived or ends the run.
    """
    # 1. THE LIVE DESKTOP'S OWN INSTALL BUTTON.
    drv.click_twice(WELCOME_INSTALL, "Install Kubuntu on the live desktop", settle=25.0)
    drv.look("welcome")

    # 2..5. THE FOUR PANES THAT ONLY NEED Next.
    #
    # NO MARKERS HERE, DELIBERATELY. These four panes are only distinguished by their
    # content, and the words that name them live in the sidebar where they are always
    # true. Driver.advance therefore asks the stronger question: did the content area
    # change at all?
    for pane, marker in (
        ("welcome", ""),
        ("location", ""),
        ("keyboard", ""),
        ("customize", ""),
    ):
        if not drv.advance(pane, settle=12.0, marker=marker):
            log("=== THE WIZARD STOPPED ADVANCING ===")
            return False

    # 6. ERASE THE DISK. The radio is not followed by a repaint, so it is clicked and the
    # pane is then left by the footer; there is nothing to confirm between those two.
    drv.click(ERASE_DISK_RADIO, "Partitions: Erase disk", settle=4.0)
    drv.look("erase-disk")
    if not drv.advance("partitions", settle=15.0):
        log("=== THE PARTITIONS PANE DID NOT ADVANCE ===")
        return False

    # 7. THE ACCOUNT FORM. FIVE CLICKS AT FIVE MEASURED POSITIONS.
    #
    # Nothing is searched for. See the ACCOUNT table above for where the numbers came from
    # and for what looking them up at run time cost.
    log("filling the account form")
    drv.look("account form")
    values = {
        "name": args.full_name,
        "login": args.full_name,
        "host": "kubuntu",
        "password": args.password,
        "confirm": args.password,
    }
    for field, spot in ACCOUNT.items():
        log(f"  {field} at {spot}")
        drv.type_into(spot, values[field])

    drv.click(LOGIN_AUTOMATICALLY, "log in automatically", settle=3.0)
    drv.look("account-filled")

    if not drv.advance("users", settle=15.0):
        log("=== THE USERS PANE DID NOT ADVANCE ===")
        return False

    # 8. SUMMARY -> INSTALL, then the confirmation dialog's own button.
    #
    # TWO CLICKS AT TWO MEASURED POSITIONS. NOTHING IS LOOKED UP.
    #
    # Run 42364250527 searched the screen for its button label and clicked a position the
    # search returned: the Summary pane's own prose contains the words it searched for, so
    # it clicked (896, 424) - a sentence - and Calamares never started installing. Three
    # minutes of work, then eighty-seven minutes of waiting out the timeout.
    #
    # THE LESSON IS THE DRIVER'S WHOLE DESIGN: a coordinate is measured once, offline, and
    # then it is a number in this file. Reading the screen to decide WHERE to click makes a
    # driver that can be talked into clicking prose; reading it to decide WHETHER the click
    # worked is safe, and is what the check below does.
    drv.click(NEXT, "Summary: Install", settle=12.0)
    drv.look("confirm-dialog")

    # NO CHECK HERE, AND THAT IS DELIBERATE. THE CLICK BELOW IS A COORDINATE.
    #
    # Three runs in a row stopped at this line because the check could not read the modal:
    #
    #   run 42364250527  looked for the button label, found it in the Summary's PROSE, and
    #                    clicked a sentence at (896, 424) - the install never started
    #   run 42371430501  looked for "confirm", which is neither the title nor the button
    #   run 42372430467  looked for "install now" and tesseract returned only the Summary
    #                    text BEHIND the modal - the dialog was there, at (1058, 535), on
    #                    the frame the run uploaded
    #
    # The lesson is the rule this driver now follows everywhere: THE SCREEN IS READ TO
    # DECIDE WHETHER A STEP WORKED, NEVER WHETHER TO TAKE IT. A conditional here makes the
    # run stop on a screen that is correct; a coordinate cannot.
    #
    # So the dialog's button is clicked where it was measured. If the dialog did not open,
    # the click lands on the Summary pane, nothing happens, and the finish loop's stall
    # detector reports it in fifteen minutes with the screen named - which is the right
    # place to notice, because that is the first point where the outcome is knowable.
    drv.click(INSTALL_NOW, "the confirmation dialog's Install now", settle=25.0)
    drv.look("installing")

    # 9. WAIT FOR THE FINISH PANE.
    #
    # "finish" IS NOT A MARKER. Calamares' sidebar lists every pane including the last, so
    # the word "Finish" is on screen from the welcome pane onwards - the first version of
    # this loop would have declared success the instant the sidebar was drawn. It happened
    # not to fire only because that run's UI was Asturian and the word was "Fin", which is
    # luck rather than design.
    #
    # THE MARKER IS THE SUMMARY PANE GOING AWAY. Copying begins when Calamares leaves it,
    # and from then on the sidebar no longer highlights it.
    log("waiting for the installer to finish")
    started = time.time()
    left_summary = False
    # A SCREEN THAT HAS NOT CHANGED IN FIFTEEN MINUTES IS A DEAD RUN, NOT A SLOW ONE.
    #
    # Run 42364250527 spent eighty-seven of its ninety minutes showing the same Summary
    # text, one identical OCR line every twenty seconds, and the only thing that ended it
    # was the overall timeout. The install it was waiting for had never started.
    #
    # FINISHING A CALAMARES INSTALL ALWAYS CHANGES THE SCREEN: the progress bar moves, the
    # package name changes, the pane changes. Fifteen minutes is several times longer than
    # the slowest step measured here, so a plateau that long means nothing is happening.
    stalled_since = time.time()
    previous = ""
    while time.time() - started < args.timeout:
        drv.look("finish")
        joined = drv.screen_text(content_only=True)
        if joined != previous:
            previous, stalled_since = joined, time.time()
        if "all done" in joined or "reboot now" in joined or "restart" in joined:
            log("=== INSTALL COMPLETE ===")
            return True
        # A PANE WITH ALMOST NO WORDS AT THE END OF CALAMARES IS THE FINISH BUTTON. The
        # last screen is one sentence and one button; the summary it replaced had dozens.
        if left_summary and drv.words and len(drv.words) <= 6:
            log("=== THE INSTALLER REACHED ITS LAST PANE ===")
            return True
        if "summary" not in joined:
            left_summary = True

        stalled = int(time.time() - stalled_since)
        if stalled > 900:
            log(f"=== THE SCREEN HAS NOT CHANGED IN {stalled}s ===")
            log("  the installer is not working; waiting longer will not change that")
            log(f"  last screen: {joined[:200]}")
            return False
        time.sleep(20)

    log("=== THE INSTALLER NEVER REPORTED COMPLETION ===")
    log(f"  last screen: {drv.screen_text(content_only=True)[:200]}")
    return False


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=None)
    p.add_argument("--disk-size", default="64G")
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--timeout", type=int, default=5400)
    p.add_argument("--full-name", default=os.environ.get("KUBUNTU_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("KUBUNTU_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "kubuntu-gui.qcow2")
    # compress.run(), not a bare qemu-img: see lib/compress.py on the Android SDK's 2.12.0
    # shadowing /usr/bin on PATH. The same mistake made the ubuntu-gui flatten fail at the
    # last step of a complete install.
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    kernel = os.path.join(work, "vmlinuz")
    initrd = os.path.join(work, "initrd")
    if not (os.path.exists(kernel) and os.path.exists(initrd)):
        for member in ("casper/vmlinuz", "casper/initrd"):
            subprocess.run(["7z", "e", "-y", f"-o{work}", args.iso, member], check=True,
                           stdout=subprocess.DEVNULL)
        log("kernel and initrd extracted")

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            "-kernel", kernel,
            "-initrd", initrd,
            # THE UI LANGUAGE IS PINNED, AND THIS IS NOT COSMETIC.
            #
            # The first CI run of this driver came up in Asturian - sidebar "Acoyida",
            # footer "Siguiente", everything. Calamares falls back to the FIRST entry of its
            # own language list when the session's locale is not one it knows, and
            # "asturianu" sorts first. Nothing failed: the run reported no error, and only
            # the uploaded last frame showed what had happened.
            #
            # It matters twice. Every detection below reads English words, and Calamares
            # right-aligns its footer, so a different language moves the buttons the
            # coordinates in this file were measured at. Pinning the locale makes the frame
            # this driver was measured against the frame it actually gets.
            #
            # "---" KEEPS CASPER FROM EATING THE REST. Everything before it is read by the
            # live session's own boot scripts; the duplicate console line after it reaches
            # the kernel, so the installer's output is on ttyS0 from the first line.
            "-append", "console=ttyS0,115200n8 locale=en_US.UTF-8 "
                       "keyboard-configuration/layoutcode=us "
                       "--- console=ttyS0,115200n8",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "kubuntu-gui"),
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
        if not wait_for_live_desktop(drv, args.timeout):
            log("=== THE LIVE DESKTOP NEVER APPEARED ===")
        else:
            # THE WALK IS A YAML FILE NOW - see steps/install.yaml. The three steps that
            # need a READING between attempts (advance, and the finish loop with its stall
            # detector) are `python:` extensions in steps/panes.py beside it, which is where
            # a measured exception belongs.
            #
            # THE QMP SOCKET IS ALREADY OPEN, so this driver is closed first: qemu serves one
            # client per socket and steps.run_file opens its own.
            drv.close()
            import steps
            walk_path = os.path.join(HERE, "steps", "install.yaml")
            log(f"=== the walk: {os.path.basename(walk_path)} ===")
            ok, ran = steps.run_file(
                walk_path, guest.paths.drive, work=work,
                # THIS DRIVER'S ACCOUNT OPTION IS --full-name, not --user: the display name
                # and the login are the same string here, which is not true on popos.
                user=args.full_name, password=args.password,
            )
            log(f"{len(ran)} step(s) ran" if ok
                else f"the walkthrough failed after {len(ran)} step(s)")
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
    # right coordinates on the wrong screen still "passed": endeavour-gui's run 42454363051
    # clicked the Asturian wizard's footer at the English coordinate, never left Welcome,
    # reported success, and uploaded `out/endeavour-gui.qcow2: 0 MiB`. The workflow was
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
