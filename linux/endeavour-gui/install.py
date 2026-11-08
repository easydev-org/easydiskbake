"""Install EndeavourOS from its live ISO, through Calamares.

    python3 linux/endeavour-gui/install.py --iso out/iso/EndeavourOS_Titan-Nova-...iso \
                                           --out out/endeavour-gui.qcow2

THE INSTALLER IS REACHED THROUGH ENDEAVOUROS' OWN WELCOME WINDOW, NOT A DESKTOP ICON.

The live session opens `Welcome` with an INSTALL tab whose first row is
`Start the Installer` at (735, 425). That opens a small `Choose installation method` dialog
with `Online` and `Offline`; OFFLINE is the right answer for a bake - it installs from the
medium alone, with no mirror round-trip whose contents change daily - and its button is
(1069, 590). Calamares then opens.

THE MEDIUM IS BOOTED AS ITS OWN BOOTLOADER INTENDS - no -kernel, no -initrd, no -append.
The image is archiso-based but its own grub.cfg carries the volume label and the
archisobasedir, and a hand-written line has cost other drivers here two runs each. `-boot d`
and OVMF are all this needs.

THE WIZARD'S FOOTER MOVES WITH THE BUTTON'S LABEL, SO THE NUMBERS ARE THE ENGLISH ONES.

Calamares sizes its footer buttons to their text. Measured on this ISO: with a Sinhala
label `Next` begins at x 1271 and its centre is (1340, 873); with the English `Next` the
same button is centred at (1406, 873). CI runs the installer in English - the runners'
egress is in the United States, and the language comes from Calamares' own geoip probe - so
the coordinates below are the English ones. The left edge of every footer control is a
number that does not move, and the three are: Back 1245, Next/Install 1320, Cancel 1460.

    Welcome  Location  Keyboard  Bootloader  Partitions  Users  Summary  Install  Finish

Bootloader keeps its default (GRUB to the disk Calamares chose). Location and Keyboard keep
theirs: the requirement is a disk that installs, not a particular region.
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

# --- MEASURED ON A LIVE EndeavourOS TITAN-NOVA SESSION, 1920x1080 -------------------------

# ENDEAVOUROS' OWN WELCOME WINDOW. `Start the Installer` is the INSTALL tab's first row, and
# the second click is on the `Offline` button of the dialog it opens.
START_INSTALLER = (735, 425)
OFFLINE = (1069, 590)

# THE FOOTER, IN ENGLISH. See the module docstring for why the language changes these
# numbers and why English is the one CI sees. Measured on Welcome, Location, Keyboard,
# Bootloader, Partitions, Users and Summary of this ISO.
NEXT = (1340, 873)
NEXT_SUMMARY = (1319, 873)

# THE PARTITIONS PANE: `Erase disk` is the FIRST radio button; its circle is at (566, 258).
# The row's text is not a control, and the row below it is `Manual partitioning`.
ERASE_DISK = (566, 258)

# THE MODAL SUMMARY PUTS UP - `Continue with Installation?`, `Install Now` / `Go Back`.
CONFIRM_INSTALL_NOW = (1059, 542)

# THE USERS PANE. Five fields; `Reuse user password as root password` is ticked by default.
ACCOUNT = {
    "name": (847, 302),
    "login": (847, 356),
    "host": (847, 433),
    "password": (700, 513),
    "confirm": (982, 513),
}

# THE LIVE MEDIUM'S BOOT TIME, AS A NUMBER. Measured: about 200 s from qemu start to a
# clickable welcome window on this image.
DESKTOP_SECONDS = 270


def log(msg: str) -> None:
    print(f"[endeavour-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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

    def text(self) -> str:
        return " ".join(w[0].lower() for w in self.words)

    def click(self, xy, why: str, settle: float = 3.0) -> None:
        log(f"click {xy} - {why}")
        Q.click(self.q, *xy)
        time.sleep(settle)

    def click_twice(self, xy, why: str, settle: float = 20.0) -> None:
        self.click(xy, why, settle=0.8)
        self.click(xy, f"{why} (second click)", settle=settle)

    def type_into(self, xy, value: str) -> None:
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
        time.sleep(0.6)


def wait_for_desktop(drv: Driver, timeout: int) -> bool:
    """Wait a MEASURED TIME, then stop waiting.

    The same design as linux/opensuse-gui's wait and for the same reason: desktop chrome is
    a band of pixels, not a word, and OCR over an archiso KDE desktop returns a handful of
    fragments that change between runs. A duration measured on this image is a number; a
    reading is a guess. 230 s against a measured 170 s, and the click that follows is
    retried once.
    """
    log(f"waiting {DESKTOP_SECONDS}s for the live desktop (measured: about 200s)")
    deadline = time.time() + min(DESKTOP_SECONDS, timeout)
    while time.time() < deadline:
        drv.look("boot")
        time.sleep(20)
    drv.look("desktop")
    log(f"  screen after the wait: {drv.text()[:120]}")
    return True


def walk_calamares(drv: Driver, args) -> bool:
    """Click through EndeavourOS' Calamares: A MEASURED COORDINATE, THEN A MEASURED WAIT.

    NOTHING IS READ TO DECIDE ANYTHING. Every step is a number in this file and a duration;
    the screen is read afterwards and the reading goes into the log, where it is useful for
    a post-mortem and dangerous as a decision. That is the rule ubuntu-gui and kubuntu-gui
    follow, and the reason both of them are green.
    """
    # 1. THE WELCOME WINDOW'S `Start the Installer`, then the method dialog's `Offline`.
    #
    #    ONE CLICK ON EACH, NOT TWO, AND THE SECOND CLICK WAS THE BUG.
    #
    #    The driver pressed `Start the Installer` twice: the first opened the method dialog,
    #    and the second - a second later, on the SAME coordinate - landed on the Welcome
    #    window behind it, whose row at that height is `Latest release info`. That opened
    #    Firefox on a 404 page, which then covered everything: run 4244... logged
    #
    #        installing: 2026-08-15 | a | (c) | 404 | | | Discovery | x | Firefox | Privacy
    #
    #    for the rest of its life. A coordinate is only right for the screen it was measured
    #    on, and the screen had changed between the two presses.
    #
    #    So: press once, wait for the dialog, press the dialog's own button once, wait for
    #    Calamares. Nothing is read to decide whether to press.
    drv.click(START_INSTALLER, "Start the Installer", settle=20.0)
    drv.look("method dialog")
    drv.click(OFFLINE, "Offline", settle=55.0)
    drv.look("calamares welcome")
    if "welcome" not in drv.text():
        log("=== THE INSTALLER DID NOT OPEN ===")
        log(f"  screen: {drv.text()[:200]}")
        return False

    # 2. WELCOME -> LOCATION
    drv.click(NEXT, "Welcome: Next", settle=20.0)
    drv.look("location")

    # 3. LOCATION -> KEYBOARD. The default location is kept: it is whatever Calamares
    #    detected, and the requirement is a disk that installs, not a particular country.
    log("location left at the installer's default")
    drv.click(NEXT, "Location: Next", settle=20.0)
    drv.look("keyboard")

    # 4. KEYBOARD -> BOOTLOADER.
    drv.click(NEXT, "Keyboard: Next", settle=20.0)
    drv.look("bootloader")

    # 5. BOOTLOADER -> PARTITIONS. GRUB goes to the disk Calamares chose; no change.
    drv.click(NEXT, "Bootloader: Next", settle=20.0)
    drv.look("partitions")

    # 6. ERASE THE DISK. THE RADIO BUTTON'S CIRCLE - the row's text is not a control.
    drv.click(ERASE_DISK, "Partitions: Erase disk", settle=8.0)
    drv.look("erase disk selected")
    drv.click(NEXT, "Partitions: Next", settle=22.0)
    drv.look("users")

    # 7. THE ACCOUNT. Five fields; the root password is reused by default.
    log("filling the account form")
    drv.type_into(ACCOUNT["name"], args.user)
    drv.type_into(ACCOUNT["login"], args.user)
    drv.type_into(ACCOUNT["host"], args.user)
    drv.type_into(ACCOUNT["password"], args.password)
    drv.type_into(ACCOUNT["confirm"], args.password)
    drv.look("account filled")
    drv.click(NEXT, "Users: Next", settle=22.0)
    drv.look("summary")

    # 8. SUMMARY -> INSTALL, then the modal's own button.
    drv.click(NEXT_SUMMARY, "Summary: Install", settle=18.0)
    drv.look("confirm install")
    drv.click(CONFIRM_INSTALL_NOW, "the install confirmation's Install Now", settle=30.0)
    drv.look("installing")

    # 9. UNTIL IT SAYS SO.
    #
    # THE ONLY DECISION IN THIS FUNCTION, AND IT IS THE SAFE KIND. Completion is a TERMINAL
    # condition: there is nothing after it to skip, so matching the words the installer
    # prints at the end cannot miss a pane.
    #
    # A CLICK, NOT A POINTER MOVE, keeps the session awake: an idle desktop raises its lock
    # screen, and a locked desktop has no installer on it.
    log("waiting for the installer to finish")
    started = time.time()
    while time.time() - started < args.timeout:
        try:
            Q.click(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                          # noqa: BLE001
            pass
        drv.look("installing")
        current = drv.text()
        if ("all done" in current or "you may now" in current
                or "reboot now" in current or "restart now" in current):
            log("=== INSTALL COMPLETE ===")
            return True
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
    p.add_argument("--user", default=os.environ.get("ENDEAVOUR_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("ENDEAVOUR_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "endeavour-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            # NO -kernel, NO -initrd, NO -append. The ISO's own grub.cfg carries its
            # volume label and archisobasedir; a hand-written line has cost two runs on
            # each of the drivers that tried it.
            "-boot", "d",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "endeavour-gui"),
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
