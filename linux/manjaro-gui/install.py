"""Install Manjaro from its KDE Live ISO, through Calamares.

    python3 linux/manjaro-gui/install.py --iso out/iso/manjaro-kde-26.1.0-260812-linux71.iso \
                                        --out out/manjaro-gui.qcow2

MANJARO'S CALAMARES IS ITS OWN BUILD, AND THREE OF ITS PANES DO NOT BEHAVE LIKE THE
OTHERS IN THIS REPOSITORY.

Everything below was measured on a UEFI machine on 2026-11-07, driving Manjaro Linux
26.1.0 "Bian-May" end to end to `All done`.

1. THE INSTALLER OPENS FROM `Manjaro Hello`, a window the live session opens by itself.
   Its `Launch installer` button is the only thing needed from the desktop.

2. THE FOOTER IS NOT AT ONE HEIGHT ON EVERY PANE. Next is at y 801 on the five panes whose
   content fills the window, and at y 794 on Office Suite, whose help text sits below the
   list and pushes the buttons up. A single constant would miss by seven pixels there -
   measured twice, because the first attempt at 801 did nothing at all.

3. OFFICE SUITE'S LIST HAS A SELECTION THAT LOOKS LIKE A DEFAULT AND IS NOT. `No Office
   Suite` is drawn highlighted from the moment the pane opens, but Next stays disabled
   until the row itself is clicked:

       No Office Suite      <- appears selected
       LibreOffice
       FreeOffice

   This is the fourth instance of the same trap in this repository - CachyOS's Bootloader
   and Partitions, Garuda's System Role, and now this - and the tell is always the same:
   the footer button is greyed out. See `advance()` below for what the driver does instead
   of trusting the highlight.

4. THE USERS PANE HAS TWO PASSWORD PAIRS. `Use the same password for the administrator
   account` is UNCHECKED by default here, unlike Garuda and CachyOS, so the root password
   has its own two fields. Filling only the user's pair leaves Next disabled.
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
import uefi         # noqa: E402
import vm           # noqa: E402

# --- measured on a live Manjaro 26.1.0 KDE session, 1920x1080, UEFI ------------------------

LAUNCH_INSTALLER = (960, 760)

# THE FOOTER, PER PANE. See the module docstring for why these are not one number.
NEXT_DEFAULT = (1325, 801)
NEXT_OFFICE = (1334, 794)
INSTALL_BUTTON = (1318, 799)

# Partitions: `Erase disk` is already selected on UEFI, so this is only a confirmation
# click - it costs nothing when the radio is already filled.
ERASE_DISK = (1043, 298)

# The two modals.
CONFIRM_INSTALL_NOW = (1055, 537)

# THE OFFICE SUITE LIST ITEM. Clicking it is what enables the footer.
NO_OFFICE_SUITE = (700, 288)

# THE USERS PANE.
ACCOUNT = {
    "name": (741, 320),
    "login": (741, 385),
    "host": (741, 450),
    "password": (724, 523),
    "confirm": (944, 523),
    "root password": (741, 650),
    "root confirm": (944, 650),
}

# THE LIVE MEDIUM'S BOOT TIME, AS A NUMBER - see wait_for_desktop.
DESKTOP_SECONDS = 240

# CALAMARES' SIDEBAR LISTS EVERY PANE AT ALL TIMES, AND IT IS NOT AT THE LEFT EDGE.
#
# The panel spans x 447..630 on this ISO - it is a CENTRED window, not a full-screen one -
# so a cutoff of 250 filtered almost nothing: `Office` read at x=482 and `Suite` at x=528
# were kept as CONTENT. `expect="office"` was therefore TRUE ON THE USERS PANE, and run
# 42408641011 sat on Users for half an hour logging `installing: ... What is your name?`
# because every advance reported success without moving.
#
# Measured on run 42408641011's own frame: sidebar words at x 482-626, content words from
# x 1139. 700 is past the panel's right edge and well short of the content's, so the
# filter keeps only what the pane itself says.
SIDEBAR_WIDTH = 700


def log(msg: str) -> None:
    print(f"[manjaro-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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

    def look(self, label: str) -> None:
        self.n += 1
        ppm = self.screen.grab(f"{self.n:03d}-{label}")
        self.words = self.screen.text(ppm) if ppm else []
        log(f"  {label}: {' | '.join(w[0] for w in self.words[:18])}")

    def text(self, content_only: bool = False) -> str:
        """Everything OCR read, optionally WITHOUT Calamares' sidebar.

        THE SIDEBAR HAS TO BE EXCLUDABLE AND THIS IS THE BUG THAT MADE THIS NECESSARY.
        Calamares lists every pane down the left at all times - `Welcome Location Keyboard
        Partitions Users Office Suite Summary Install Finish` - so `expect="office"` was
        TRUE ON THE USERS PANE. Run 42398673800 advanced from Users with that expectation,
        landed on Office Suite, and the driver believed it had already arrived: it clicked
        `No Office Suite` and then the footer, was still on Users, and sat there until the
        stall detector ended the run 908 seconds later.

        The cutoff is SIDEBAR_WIDTH, and it has to be past the panel's right edge rather
        than at the window's left border - see the constant.
        """
        if not content_only:
            return " ".join(w[0].lower() for w in self.words)
        return " ".join(w[0].lower() for w in self.words if w[1] > SIDEBAR_WIDTH)

    def click(self, xy, why: str, settle: float = 3.0) -> None:
        log(f"click {xy} - {why}")
        Q.click(self.q, *xy)
        time.sleep(settle)

    def double_click(self, xy, why: str, settle: float = 20.0) -> None:
        log(f"double-click {xy} - {why}")
        x, y = xy
        Q._send(self.q, "press", [
            {"type": "abs", "data": {"axis": "x", "value": Q.to_abs(x, 1920)}},
            {"type": "abs", "data": {"axis": "y", "value": Q.to_abs(y, 1080)}},
            {"type": "btn", "data": {"down": True, "button": "left"}},
        ])
        time.sleep(0.08)
        Q._send(self.q, "release", [{"type": "btn", "data": {"down": False, "button": "left"}}])
        time.sleep(0.08)
        Q._send(self.q, "press 2", [{"type": "btn", "data": {"down": True, "button": "left"}}])
        time.sleep(0.08)
        Q._send(self.q, "release 2", [{"type": "btn", "data": {"down": False, "button": "left"}}])
        time.sleep(settle)

    def clear_and_type(self, xy, value: str) -> None:
        Q.click(self.q, *xy)
        time.sleep(1.2)
        Q.key_combo(self.q, "ctrl", "a")
        time.sleep(0.4)
        for _ in range(40):
            Q.key(self.q, "backspace")
            time.sleep(0.02)
        unsupported = keys.type_text(self.q, value, per_char=0.1)
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(0.7)

    def advance(self, name: str, xy, *, settle: float = 18.0,
                expect: str = "", max_tries: int = 3) -> bool:
        """Click a footer button until the pane changes, and give up saying where.

        THE THIRD TRY IS FOR A DISABLED BUTTON, NOT FOR A MISSED CLICK. Manjaro's
        Calamares greys out its footer when a pane's own selection is missing - Office
        Suite is the one that matters here - and a greyed button swallows clicks without
        any feedback. Retrying costs seconds; assuming the click landed costs the run.

        `expect` is a phrase that only the NEXT pane shows - but it is NEVER THE WHOLE TEST.
        The content area has to have moved as well, and that is the second fix for a bug
        that cost half an hour.

        Run 42408641011 sat on Users with `expect="office"` reporting success every poll.
        The pane's own words include a LibreOffice line that OCR reads as `Office`, so the
        phrase was true on Users, on Office Suite and everywhere in between, and the driver
        never noticed it had not moved. A phrase is evidence only in combination with a
        change: `office` ON a pane that no longer says `What is your name?` is the Office
        Suite pane; `office` on the same pane is the pane it started on.

        WITHOUT `expect` THE CHANGE ALONE IS THE TEST, which is what a click on Next does
        when it works and does nothing when it does not.
        """
        before = self.text(content_only=True)
        for attempt in range(1, max_tries + 1):
            self.click(xy, f"{name}: Next (attempt {attempt})", settle=settle)
            self.look(f"after {name}")
            current = self.text(content_only=True)
            if current and current != before and (not expect or expect in current):
                return True
        # ALT+N IS CALAMARES' OWN BINDING FOR NEXT, AND IT IS THE FALLBACK THAT WAS MISSING.
        #
        # Calamares sizes its window to the pane's content, so the footer moves: on one ISO
        # the same `Next` was 49 px lower on the Location pane than on Welcome, and a
        # coordinate measured on one pane landed on the map of the next. CachyOS' run
        # 42408635655 clicked nine times at a fixed point and never left Location.
        #
        # Manjaro has not shown that failure yet, but it is the same wizard on the same
        # kind of pane, and the fallback costs one keystroke. It is tried LAST because on
        # the Summary pane Alt+N starts the installation.
        Q.key_combo(self.q, "alt", "n")
        time.sleep(settle)
        self.look(f"after {name} by keyboard")
        current = self.text(content_only=True)
        if current and current != before and (not expect or expect in current):
            return True
        log(f"=== {name.upper()} DID NOT ADVANCE, BY CLICK OR BY KEYBOARD ===")
        log(f"  expected: {expect!r}")
        log(f"  screen: {current[:220]}")
        return False


def wait_for_desktop(drv: Driver, timeout: int) -> bool:
    """Wait a MEASURED TIME, then stop.

    The same design as the other live-ISO drivers: a desktop is a band of pixels, not a
    word, and OCR over a 1920x1080 KDE session returns a handful of fragments that change
    between runs. 240 s against a measured 180, and the double-click below is retried.
    """
    log(f"waiting {DESKTOP_SECONDS}s for the live desktop (measured: about 180s)")
    deadline = time.time() + min(DESKTOP_SECONDS, timeout)
    while time.time() < deadline:
        drv.look("boot")
        time.sleep(20)
    drv.look("desktop")
    log(f"  screen after the wait: {drv.text()[:120]}")
    return True


def walk_calamares(drv: Driver, args) -> bool:
    """Click through Manjaro's nine panes."""
    # 1. THE WELCOME WINDOW'S BUTTON. Retried: the live session draws that window while
    #    this driver is arriving from a fixed wait, so the first press can land on a window
    #    still opening.
    for attempt in (1, 2):
        drv.double_click(LAUNCH_INSTALLER, "Launch installer", settle=35.0)
        drv.look("calamares welcome")
        if "welcome" in drv.text() and "installer" in drv.text():
            break
    if "welcome" not in drv.text():
        log("=== THE INSTALLER DID NOT OPEN ===")
        log(f"  screen: {drv.text()[:200]}")
        return False

    # 2. WELCOME -> LOCATION. The pane names are stable in English, which the installer's
    #    own default (American English) gives us.
    if not drv.advance("welcome", NEXT_DEFAULT, settle=15.0):
        return False

    # 3. LOCATION -> KEYBOARD. The default zone is kept: whatever the installer read from
    #    the network at startup stays. Changing it is one more field that can go wrong for
    #    no gain, and the bake's job is a disk that installs, not a particular region.
    log("location left at the installer's default")
    if not drv.advance("location", NEXT_DEFAULT, settle=15.0):
        return False

    # 4. KEYBOARD -> PARTITIONS.
    if not drv.advance("keyboard", NEXT_DEFAULT, settle=15.0):
        return False

    # 5. PARTITIONS. On UEFI `Erase disk` is already selected and the layout already has
    #    its 300 MiB EFI partition; the click is a confirmation that costs nothing when it
    #    is already set, and fixes it when it is not.
    drv.click(ERASE_DISK, "Partitions: Erase disk", settle=5.0)
    drv.look("erase disk selected")
    if not drv.advance("partitions", NEXT_DEFAULT, settle=20.0):
        return False

    # 6. THE ACCOUNT. Two password pairs - see the module docstring.
    log("filling the account form")
    drv.clear_and_type(ACCOUNT["name"], args.user)
    drv.clear_and_type(ACCOUNT["login"], args.user)
    drv.clear_and_type(ACCOUNT["host"], args.user)
    drv.clear_and_type(ACCOUNT["password"], args.password)
    drv.clear_and_type(ACCOUNT["confirm"], args.password)
    drv.clear_and_type(ACCOUNT["root password"], args.password)
    drv.clear_and_type(ACCOUNT["root confirm"], args.password)
    drv.look("account filled")
    if not drv.advance("users", NEXT_DEFAULT, settle=20.0):
        return False

    # 7. OFFICE SUITE. THE ROW, NOT THE HIGHLIGHT - see the module docstring. Without this
    #    click Next is disabled and every retry is swallowed.
    log("selecting No Office Suite (the row itself; the highlight is not a selection)")
    drv.click(NO_OFFICE_SUITE, "Office Suite: No Office Suite", settle=5.0)
    drv.look("office suite selected")
    if not drv.advance("office suite", NEXT_OFFICE, settle=20.0):
        return False

    # 8. SUMMARY -> INSTALL, then the modal's own button.
    drv.click(INSTALL_BUTTON, "Summary: Install", settle=18.0)
    drv.look("confirm install")
    # The modal is not checked for. A conditional here is what stopped three Kubuntu runs
    # on a screen that was correct; the click lands on blank space if it is not up.
    drv.click(CONFIRM_INSTALL_NOW, "the install confirmation's Install Now", settle=30.0)
    drv.look("installing")

    # 9. UNTIL IT SAYS SO, WITH A STALL DETECTOR.
    log("waiting for the installer to finish")
    started = time.time()
    stalled_since = time.time()
    previous = ""
    while time.time() - started < args.timeout:
        # NUDGE THE POINTER, OR THE SCREEN BLANKS AND EVERY READING IS BLACK.
        #
        # Run 42404589611 clicked Install Now at 19:15:28 and then read a solid black frame
        # for the next twenty-five minutes, until the stall detector ended the run at 912
        # seconds. A desktop session with no input blanks its display; a blanked display has
        # no words, so "the installer is still working" and "the installer finished" look
        # identical. One pixel of movement per poll keeps the session awake - the same fix,
        # for the same reason, as the Ubuntu verification run.
        try:
            Q.move(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                              # noqa: BLE001
            pass
        drv.look("installing")
        current = drv.text()
        if current != previous:
            previous, stalled_since = current, time.time()
        # COMPLETION IS THE SENTENCE, NOT THE HEADING.
        #
        # The Finish pane's heading is `All done.` in a large font, and tesseract does not
        # read it: measured on a completed local install, the whole pane OCRs as
        #
        #     manjar you may now re: environment. tart now * 3:58 pm * 11/7/26
        #
        # with no `all done` anywhere. So the test was one the screen could never satisfy,
        # and a correct install would have been thrown away at the timeout. `you may now`
        # is the body sentence, it is unique to this pane, and it reads cleanly.
        if ("all done" in current or "you may now" in current
                or "has been installed" in current):
            log("=== INSTALL COMPLETE ===")
            return True
        stalled = int(time.time() - stalled_since)
        if stalled > 900:
            log(f"=== THE SCREEN HAS NOT CHANGED IN {stalled}s ===")
            log(f"  last screen: {current[:200]}")
            return False
        time.sleep(20)

    log("=== THE INSTALLER NEVER REPORTED COMPLETION ===")
    log(f"  last screen: {drv.text()[:200]}")
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
    p.add_argument("--user", default=os.environ.get("MANJARO_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MANJARO_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "manjaro-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            # TRY THE MEDIUM FIRST. The disk is empty, so the firmware's own order would
            # find nothing to boot; naming it removes the difference between machines.
            "-boot", "d",
        ],
        # UEFI, AND THAT IS NOT COSMETIC: on the BIOS path Garuda refused to install at all
        # and produced disks with an MBR and no ESP. Manjaro's own ISO gives an ESP and GPT
        # when it is booted this way, which is what its images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                     vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "manjaro-gui"),
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
        if wait_for_desktop(drv, args.timeout):
            ok = walk_calamares(drv, args)
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

    if ok and os.path.exists(disk):
        log(f"compressing {disk} -> {args.out}")
        compress.compress(disk, args.out)
        log(f"{args.out}: {os.path.getsize(args.out) >> 20} MiB")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
