"""Install CachyOS from its live ISO, through Calamares.

    python3 linux/cachyos-gui/install.py --iso out/iso/cachyos-desktop-linux-260809.iso \
                                         --out out/cachyos-gui.qcow2

CACHYOS IS AN ARCHISO DERIVATIVE AND CALAMARES IS ITS INSTALLER.

The live medium is archiso's, so the kernel and initrd are where archiso puts them
(`/arch/boot/x86_64/`) and the initramfs finds its squashfs through
`archisobasedir=arch archisolabel=<the ISO's volume id>`. The installer itself is
Calamares, the same program Kubuntu uses - so this driver has the same shape as
linux/kubuntu-gui and shares its rules.

THE INSTALLER IS LAUNCHED FROM A WELCOME WINDOW, NOT FROM A DESKTOP ICON.

`CachyOS Hello` opens by itself on the live desktop, and its bottom row is a single
`Launch installer` button. That is the entry point: no icon to find, and its position is
measured from the frame rather than read from the screen.

THE LANGUAGE IS PINNED ON THE KERNEL COMMAND LINE, WHICH IS THE ONLY PLACE THAT WORKS.

The first local run of this driver came up in Chinese, and switching it by hand was seven
keystrokes through an alphabetical list with no search box - Arabic, Assamese, Asturian, ...
Belarusian, Bulgarian, Bangla, Catalan, Chinese, Croatian, Czech, Danish, German, Greek,
... English. Landing on the wrong row changes every label on every later pane, so
`locale=en_US.UTF-8` is passed to the kernel and Calamares starts in English.

CALAMARES IS ELEVEN PANES HERE, AND ONLY FOUR NEED ANYTHING.

    Welcome  Location  Keyboard  Bootloader  Partitions  Desktop  Packages  Users
    Summary  Install  Finish

Bootloader, Desktop and Packages keep their defaults: GRUB to the disk Calamares chose, the
KDE Plasma desktop this ISO carries, and the package set its own profile names. The four
that do not are Welcome (a click), Location (default kept), Partitions (erase the disk) and
Users (the account).

NOTHING IS LOOKED UP AT RUN TIME. Every position below was measured from a frame, and the
screen is read afterwards only to say what happened - never to choose where to click.
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

# --- measured on a live CachyOS session, 1920x1080 ----------------------------------------

# `CachyOS Hello` opens by itself; this is the button at the bottom of it. Clicked TWICE:
# the first press on an unfocused window is taken as focus, which is the same measurement
# the Kubuntu driver records.
LAUNCH_INSTALLER = (960, 743)

# CALAMARES' FOOTER, measured on this ISO's own window. CachyOS ships a dark theme and a
# narrower window than Kubuntu's, so these are NOT Kubuntu's numbers.
NEXT = (1364, 751)
BACK = (1248, 751)

# THE PARTITIONS PANE: `Erase disk` is the first radio button.
ERASE_DISK = (750, 431)

# THE CONFIRMATION DIALOG that Partitions puts up before writing anything.
CONFIRM_ERASE = (1010, 640)

# THE CONFIRMATION DIALOG THAT SUMMARY puts up. Same shape, its own position.
INSTALL_NOW = (1010, 640)

# THE USERS PANE. Calamares draws the same five fields on every distribution, but the
# window geometry differs, so these were measured on CachyOS' own frame.
ACCOUNT = {
    "name": (830, 380),
    "login": (830, 449),
    "host": (830, 517),
    "password": (830, 585),
    "confirm": (830, 653),
}

# THE LIVE MEDIUM'S BOOT TIME, AS A NUMBER. Measured: about 170 s from qemu start to a
# clickable desktop on this image. The wait is longer than that - see wait_for_desktop.
DESKTOP_SECONDS = 230


def log(msg: str) -> None:
    print(f"[cachyos-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
    log(f"waiting {DESKTOP_SECONDS}s for the live desktop (measured: about 170s)")
    deadline = time.time() + min(DESKTOP_SECONDS, timeout)
    while time.time() < deadline:
        drv.look("boot")
        time.sleep(20)
    drv.look("desktop")
    log(f"  screen after the wait: {drv.text()[:120]}")
    return True


def walk_calamares(drv: Driver, args) -> bool:
    """Click through Calamares' eleven panes."""
    # 1. THE WELCOME WINDOW'S OWN BUTTON, through the desktop it belongs to.
    drv.click_twice(LAUNCH_INSTALLER, "Launch installer", settle=35.0)
    drv.look("calamares welcome")
    if "welcome" not in drv.text():
        log("the installer did not open on the first double-click; trying once more")
        drv.click_twice(LAUNCH_INSTALLER, "Launch installer (second attempt)", settle=40.0)
        drv.look("calamares welcome")
    if "welcome" not in drv.text():
        log("=== THE INSTALLER DID NOT OPEN ===")
        log(f"  screen: {drv.text()[:200]}")
        return False

    # 2. WELCOME -> LOCATION
    drv.click(NEXT, "Welcome: Next", settle=15.0)
    drv.look("location")

    # 3. LOCATION -> KEYBOARD. The default location is kept: it is whatever Calamares
    #    detected, and the requirement is a disk that installs, not a particular country.
    log("location left at the installer's default")
    drv.click(NEXT, "Location: Next", settle=15.0)
    drv.look("keyboard")

    # 4. KEYBOARD -> BOOTLOADER
    drv.click(NEXT, "Keyboard: Next", settle=15.0)
    drv.look("bootloader")

    # 5. BOOTLOADER -> PARTITIONS. GRUB goes to the disk Calamares chose; no change.
    drv.click(NEXT, "Bootloader: Next", settle=15.0)
    drv.look("partitions")

    # 6. ERASE THE DISK, then the confirmation that names it.
    drv.click(ERASE_DISK, "Partitions: Erase disk", settle=5.0)
    drv.look("erase disk selected")
    drv.click(NEXT, "Partitions: Next", settle=12.0)
    drv.look("confirm erase")
    # THE DIALOG IS NOT CHECKED FOR. A conditional here is how three Kubuntu runs stopped
    # on a screen that was correct; the click lands on blank space if the dialog is not up.
    drv.click(CONFIRM_ERASE, "the erase confirmation's Continue", settle=20.0)
    drv.look("desktop profile")

    # 7. DESKTOP PROFILE -> PACKAGES. The ISO's own profile (KDE Plasma) is kept.
    drv.click(NEXT, "Desktop: Next (KDE Plasma)", settle=15.0)
    drv.look("packages")

    # 8. PACKAGES -> USERS. The default package set is kept.
    drv.click(NEXT, "Packages: Next (default set)", settle=20.0)
    drv.look("users")

    # 9. THE ACCOUNT.
    log("filling the account form")
    drv.type_into(ACCOUNT["name"], args.user)
    drv.type_into(ACCOUNT["login"], args.user)
    drv.type_into(ACCOUNT["host"], args.user)
    drv.type_into(ACCOUNT["password"], args.password)
    drv.type_into(ACCOUNT["confirm"], args.password)
    drv.look("account filled")
    drv.click(NEXT, "Users: Next", settle=20.0)
    drv.look("summary")

    # 10. SUMMARY -> INSTALL, then the dialog's own button.
    drv.click(NEXT, "Summary: Install", settle=15.0)
    drv.look("confirm install")
    drv.click(INSTALL_NOW, "the install confirmation's Install now", settle=25.0)
    # NUDGE THE POINTER, OR THE SCREEN BLANKS AND EVERY READING IS BLACK. A desktop
    # session with no input blanks its display, and a blanked display has no words -
    # so "still installing" and "finished" look identical. One pixel per poll.
    try:
        Q.move(drv.q, 400 + (int(time.time()) % 40), 300)
    except Exception:                              # noqa: BLE001
        pass

    drv.look("installing")

    # 11. UNTIL IT SAYS SO, WITH A STALL DETECTOR. Finishing always changes the screen - a
    #     progress bar moves, a package name changes - so a content area that reads the same
    #     for fifteen minutes means the installer is not working.
    log("waiting for the installer to finish")
    started = time.time()
    stalled_since = time.time()
    previous = ""
    while time.time() - started < args.timeout:
        drv.look("installing")
        current = drv.text()
        if current != previous:
            previous, stalled_since = current, time.time()
        if "all done" in current or "reboot now" in current or "restart now" in current:
            log("=== INSTALL COMPLETE ===")
            return True
        if time.time() - stalled_since > 900:
            log("=== THE SCREEN HAS NOT CHANGED IN 900s ===")
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
    p.add_argument("--user", default=os.environ.get("CACHYOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("CACHYOS_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "cachyos-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    # ARCHISO'S OWN LAYOUT: /arch/boot/x86_64/{vmlinuz-linux-cachyos,initramfs-linux-cachyos.img}
    kernel = os.path.join(work, "vmlinuz")
    initrd = os.path.join(work, "initrd")
    if not (os.path.exists(kernel) and os.path.exists(initrd)):
        for member in ("arch/boot/x86_64/vmlinuz-linux-cachyos",
                       "arch/boot/x86_64/initramfs-linux-cachyos.img"):
            subprocess.run(["7z", "e", "-y", f"-o{work}", args.iso, member], check=True,
                           stdout=subprocess.DEVNULL)
        for produced, wanted in (
                (os.path.join(work, "vmlinuz-linux-cachyos"), kernel),
                (os.path.join(work, "initramfs-linux-cachyos.img"), initrd)):
            if os.path.exists(produced) and produced != wanted:
                os.replace(produced, wanted)
        log("kernel and initrd extracted")

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            "-kernel", kernel,
            "-initrd", initrd,
            # LOCALE IS THE FIX FOR A CHINESE INSTALLER, and the volume id is this ISO's own
            # PVD label. NO copytoram: it copies the whole squashfs into RAM and this image's
            # is larger than the guest's memory, which is what broke the EndeavourOS run.
            "-append", "archisobasedir=arch archisolabel=COS_202608 "
                       "locale=en_US.UTF-8 quiet",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "cachyos-gui"),
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
