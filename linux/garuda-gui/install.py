"""Install Garuda Linux from its live ISO, through Calamares.

    python3 linux/garuda-gui/install.py --iso out/iso/garuda-kde-lite-...iso \
                                        --out out/garuda-gui.qcow2

GARUDA'S MEDIUM IS MISO, NOT ARCHISO, AND THAT IS THE FIRST THING TO KNOW.

An archiso image is booted with `archisobasedir=arch archisolabel=<volume id>` handed to its
own kernel. Garuda's is built by garuda-tools/buildiso, its initramfs looks for a label of
its own, and a hand-written archiso command line sends it to

    dracut-mount[358]: Warning: Can't mount root filesystem
    Entering emergency mode.

Measured, not guessed: that is exactly what a local run with `archisobasedir=garuda
archisolabel=GARUDA_KDELITE_TEMERAIRE` produced. So this driver boots the ISO AS ITS OWN
BOOTLOADER INTENDS - no -kernel, no -initrd, no -append, `-boot d` - and the firmware finds
the El Torito entry itself. The same rule is why CachyOS, Fedora and openSUSE work.

THE INSTALLER IS CALAMARES AND IT IS LAUNCHED FROM A DESKTOP ICON.

`Install Garuda Linux` sits under the top-left corner of the live KDE desktop at (41, 46)
and is DOUBLE-clicked: one press only selects it. Nothing else opens by itself.

NOTHING IS LOOKED UP AT RUN TIME. Every position below was measured from a frame of THIS
ISO, one pane at a time; the screen is read afterwards to say what happened, never to
choose where to click. The wizard's window is fixed at (447,171)..(1470,857) on
1920x1080, so its footer is at one place for every pane - unlike CachyOS', which resizes
per pane and needed three different Next coordinates.

    Welcome  Location  Keyboard  Partitions  Users  Summary  Install  Finish

Bootloader, Desktop and Packages are not panes here: this ISO's Calamares profile skips
them, and the bootloader goes to the disk the partitioning scheme names.
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

# --- MEASURED ON A LIVE GARUDA SOARING KDE LITE SESSION, 1920x1080 -------------------------
#
# ONE FOOTER COORDINATE FOR THE WHOLE WIZARD. Garuda's Calamares window is fixed at
# (447,171)..(1470,857) and does not resize per pane, so `Next`/`Install` sits at
# (1319, 836) on every one of them - measured on Welcome, Location, Keyboard, Partitions,
# Users and Summary.

# THE DESKTOP ICON, in the top-left corner of the live session. Double-clicked: 80 ms opens
# Calamares, and one press only selects the icon.
INSTALL_ICON = (41, 46)
DOUBLE_CLICK_GAP = 0.08

NEXT = (1319, 836)
NEXT_SUMMARY = (1316, 836)
BACK = (1231, 836)

# THE PARTITIONS PANE: `Erase disk` is the FIRST radio button and its circle is at
# (655, 273) - the row's text is not a control, and the row below it is `Manual
# partitioning`, which would produce a disk nobody asked for.
ERASE_DISK = (655, 273)

# THE MODAL SUMMARY PUTS UP - `Continue with Installation?` with `Install Now` and
# `Go Back`. Measured: Install Now is (1068, 566).
CONFIRM_INSTALL_NOW = (1068, 566)

# THE USERS PANE. Five fields; `Use the same password for the administrator account` is
# ticked by default here, so there is no second password pair.
ACCOUNT = {
    "name": (744, 254),
    "login": (744, 320),
    "host": (744, 386),
    "password": (742, 456),
    "confirm": (946, 456),
}

# THE LIVE MEDIUM'S BOOT TIME, AS A NUMBER. Measured: about 190 s from qemu start to a
# clickable desktop on this image. The wait is longer than that.
DESKTOP_SECONDS = 260


def log(msg: str) -> None:
    print(f"[garuda-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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

    def double_click(self, xy, why: str, settle: float = 30.0) -> None:
        """Two presses 80 ms apart at ONE position - a real double-click.

        click_twice() IS NOT ONE AND THAT IS WHY GARUDA'S RUN FAILED. It presses, waits
        0.8 s, then presses again: KDE's double-click interval is 400 ms, so the desktop
        reads that as two single clicks - the first selects the icon, the second starts the
        selection over - and the installer never opens. Measured on GhostBSD's MATE, whose
        interval is the same size: 80 ms opens its installer, 500 ms does not.
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
    log(f"waiting {DESKTOP_SECONDS}s for the live desktop (measured: about 190s)")
    deadline = time.time() + min(DESKTOP_SECONDS, timeout)
    while time.time() < deadline:
        drv.look("boot")
        time.sleep(20)
    drv.look("desktop")
    log(f"  screen after the wait: {drv.text()[:120]}")
    return True


def walk_calamares(drv: Driver, args) -> bool:
    """Click through Garuda's Calamares: A MEASURED COORDINATE, THEN A MEASURED WAIT.

    NOTHING IS READ TO DECIDE ANYTHING. Every step is a number in this file and a duration;
    the screen is read afterwards and the reading goes into the log, where it is useful for
    a post-mortem and dangerous as a decision. That is the rule ubuntu-gui and kubuntu-gui
    follow, and the reason both of them are green.

    TWO DETECTORS WERE TRIED ON THE SIBLING CACHYOS DRIVER AND BOTH WERE WORSE THAN NONE:

      a word per pane - Calamares' headings are images or large fonts, and tesseract reads
      them unevenly; a correct advance was reported as `DID NOT ADVANCE`;

      the whole text before against after - prose does not read identically twice, so that
      is true while standing still, and a run reported every step as an advance while it
      had never left the first pane.

    What is left is the only thing that cannot lie: the button's own measured position.
    """
    # 1. THE DESKTOP ICON.
    drv.double_click(INSTALL_ICON, "Install Garuda Linux", settle=40.0)
    drv.look("calamares welcome")
    if "welcome" not in drv.text():
        log("the installer did not open on the first double-click; trying once more")
        drv.double_click(INSTALL_ICON, "Install Garuda Linux (second attempt)",
                         settle=45.0)
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

    # 4. KEYBOARD -> PARTITIONS.
    drv.click(NEXT, "Keyboard: Next (Generic 105-key PC, US)", settle=20.0)
    drv.look("partitions")

    # 5. ERASE THE DISK. THE RADIO BUTTON'S CIRCLE - the row's text is not a control.
    drv.click(ERASE_DISK, "Partitions: Erase disk", settle=8.0)
    drv.look("erase disk selected")
    drv.click(NEXT, "Partitions: Next", settle=20.0)
    drv.look("users")

    # 6. THE ACCOUNT. Five fields, one password pair - the administrator box is ticked.
    log("filling the account form")
    drv.type_into(ACCOUNT["name"], args.user)
    drv.type_into(ACCOUNT["login"], args.user)
    drv.type_into(ACCOUNT["host"], args.user)
    drv.type_into(ACCOUNT["password"], args.password)
    drv.type_into(ACCOUNT["confirm"], args.password)
    drv.look("account filled")
    drv.click(NEXT, "Users: Next", settle=22.0)
    drv.look("summary")

    # 7. SUMMARY -> INSTALL, then the modal's own button. Measured on this ISO:
    #    Summary's `Install` (1316, 836), the modal's `Install Now` (1068, 566).
    drv.click(NEXT_SUMMARY, "Summary: Install", settle=18.0)
    drv.look("confirm install")
    drv.click(CONFIRM_INSTALL_NOW, "the install confirmation's Install Now", settle=30.0)
    drv.look("installing")

    # 8. UNTIL IT SAYS SO.
    #
    # THE ONLY DECISION IN THIS FUNCTION, AND IT IS THE SAFE KIND. Completion is a TERMINAL
    # condition: there is nothing after it to skip, so matching the words the installer
    # prints at the end cannot miss a pane.
    #
    # A CLICK, NOT A POINTER MOVE, keeps the session awake: an idle desktop raises its lock
    # screen, and a locked desktop has no installer on it - mint-gui's last frame was a
    # full-window lock screen while a correct install finished underneath.
    log("waiting for the installer to finish")
    started = time.time()
    while time.time() - started < args.timeout:
        try:
            Q.click(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                          # noqa: BLE001
            pass
        drv.look("installing")
        current = drv.text()
        # THE HEADING IS NOT RELIABLE, THE SENTENCE IS: `All done.` is a large font that
        # tesseract skips, and the body sentence under it reads cleanly. See manjaro-gui.
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
    p.add_argument("--user", default=os.environ.get("GARUDA_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("GARUDA_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "garuda-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            # NO -kernel, NO -initrd, NO -append. THIS MEDIUM IS MISO, NOT ARCHISO, and a
            # hand-written archiso command line sends it to an emergency shell:
            #
            #     dracut-mount[358]: Warning: Can't mount root filesystem
            #     Entering emergency mode.
            #
            # measured on a local run with `archisobasedir=garuda
            # archisolabel=GARUDA_KDELITE_TEMERAIRE`. Its own grub.cfg knows the right
            # values, and the firmware finds that through the El Torito catalogue.
            "-boot", "d",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "garuda-gui"),
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
