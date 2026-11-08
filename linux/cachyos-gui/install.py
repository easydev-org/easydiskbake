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

# --- MEASURED ON A LIVE CACHYOS SESSION, 1920x1080, PANE BY PANE ---------------------------
#
# EVERY NUMBER BELOW WAS TAKEN FROM A FRAME, ONE PANE AT A TIME, and that is the point of
# this block: this wizard's footer IS NOT AT ONE PLACE.
#
# Calamares sizes its window to the pane's content. On this ISO the window is 1100 px wide
# and about 680 px tall on Welcome, and it GROWS by 49 px when the Location pane's map
# appears, and by another 106 px when the Summary pane's overview text arrives. The same
# `Next` button therefore sits at y 747, then y 796, then y 902 - three places.
#
# THAT IS WHY RUN 42408635655 NEVER LEFT THE LOCATION PANE. The driver clicked (1364, 751)
# nine times: on Welcome that is the Next button, and on Location it is 45 px above it,
# inside the map. The selected region wandered Chicago -> Detroit -> Antarctica and the
# wizard did not move at all, while every step logged as though it had.

# `CachyOS Hello` opens by itself; this is the button at the bottom of it. Clicked TWICE:
# the first press on an unfocused window is taken as focus, which is the same measurement
# the Kubuntu driver records.
LAUNCH_INSTALLER = (960, 743)

# THE WELCOME PANE'S Next. The window is at its smallest here - no map, no overview.
NEXT_WELCOME = (1352, 747)

# EVERY PANE FROM LOCATION THROUGH USERS. The window has grown to fit the map and stays at
# that size for the rest of the wizard; measured on Location, Keyboard, Bootloader,
# Partitions, Desktop, Packages and Users, all identical.
NEXT = (1352, 796)

# THE SUMMARY PANE BRINGS THE `Install` BUTTON, at the bottom of a window that grew again
# for the overview text. y 902, not 796 - 106 px lower than the pane before it.
INSTALL = (1365, 902)

# THE PARTITIONS PANE: `Erase disk` is the FIRST radio button, and its circle is at
# (465, 367) - NOT the row's text. Clicking the text selects nothing; the highlight is not
# a selection. Below it is `Manual partitioning`, which is what a click 36 px too low picks
# instead, and that produces a disk nobody asked for.
ERASE_DISK = (465, 367)

# THE CONFIRMATION THAT SUMMARY PUTS UP - `Continue with installation?` /
# `Are you sure you want to continue?` - with `Install now` at (1018, 620) and `Back` to
# its right. A modal, drawn over the Summary pane.
INSTALL_NOW = (1018, 620)

# PARTITIONS PUTS UP NO CONFIRMATION ON THIS ISO: measured, `Erase disk` then Next goes
# straight to Desktop. The click below is kept as a no-op that lands on blank space, for
# the builds that do ask - it costs a quarter of a second and cannot press anything else.
CONFIRM_ERASE = (1010, 640)

# THE USERS PANE. Calamares draws the same five fields on every distribution, but the
# window geometry differs, so these were measured on CachyOS' own frame.
ACCOUNT = {
    "name": (516, 348),
    "login": (516, 413),
    "host": (516, 478),
    "password": (516, 546),
    "confirm": (729, 546),
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
    """Click through Calamares' eleven panes: A MEASURED COORDINATE, THEN A MEASURED WAIT.

    NOTHING IS READ TO DECIDE ANYTHING. Every step is a number in this file and a duration;
    the screen is read afterwards and the reading goes into the log, where it is useful for
    a post-mortem and dangerous as a decision. This is the rule ubuntu-gui and kubuntu-gui
    already follow, and the reason both of them are green while this driver kept failing
    its own checks.

    TWO DETECTORS WERE TRIED HERE AND BOTH WERE WORSE THAN NO DETECTOR:

      a word per pane (`expect="boot loader"`) - Calamares' panes carry image headings and
      tesseract reads them unevenly; the Bootloader pane came back as
      `vax grub limine refind refind + ai` on a pane that had advanced correctly, and run
      42413733562 stopped there;

      the whole OCR text before against after (`current != before`) - prose does not read
      identically twice, so that is true while standing still; run 42413739470 reported
      every step from Welcome to `the install confirmation's Install now` as an advance
      while it was still on Users, and waited out its life for an installation that had
      never started.

    What is left is the thing that actually matters and cannot lie: THE FOOTER IS NOT IN
    ONE PLACE, so each pane gets its own coordinate. See the constants block - y 747 on
    Welcome, y 796 from Location through Users, y 902 on Summary.
    """
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

    # 2. WELCOME -> LOCATION. The window is at its smallest on this pane; NEXT_WELCOME.
    drv.click(NEXT_WELCOME, "Welcome: Next", settle=20.0)
    drv.look("location")

    # 3. LOCATION -> KEYBOARD. The default location is kept: it is whatever Calamares
    #    detected, and the requirement is a disk that installs, not a particular country.
    log("location left at the installer's default")
    drv.click(NEXT, "Location: Next", settle=20.0)
    drv.look("keyboard")

    # 4. KEYBOARD -> BOOTLOADER
    drv.click(NEXT, "Keyboard: Next", settle=22.0)
    drv.look("bootloader")

    # 5. BOOTLOADER -> PARTITIONS. GRUB goes to the disk Calamares chose; no change.
    drv.click(NEXT, "Bootloader: Next", settle=22.0)
    drv.look("partitions")

    # 6. ERASE THE DISK. THE RADIO BUTTON'S CIRCLE, at (465, 367) - the row's text is not a
    #    control, and 36 px lower is `Manual partitioning`.
    drv.click(ERASE_DISK, "Partitions: Erase disk", settle=10.0)
    drv.look("erase disk selected")
    drv.click(NEXT, "Partitions: Next", settle=20.0)
    drv.look("after partitions")
    # NO CONFIRMATION IS EXPECTED HERE ON THIS ISO - measured, Next goes straight to
    # Desktop. The click is kept for builds that do ask; on this one it lands on the
    # Desktop pane's blank space, which selects nothing.
    drv.click(CONFIRM_ERASE, "the erase confirmation's Continue (if it is shown)", settle=3.0)

    # 7. DESKTOP PROFILE -> PACKAGES. The ISO's own profile (Plasma Desktop) is kept.
    drv.click(NEXT, "Desktop: Next (Plasma Desktop)", settle=22.0)
    drv.look("packages")

    # 8. PACKAGES -> USERS. The default package set is kept.
    drv.click(NEXT, "Packages: Next (default set)", settle=25.0)
    drv.look("users")

    # 9. THE ACCOUNT. Five measured fields; see the ACCOUNT table.
    log("filling the account form")
    drv.type_into(ACCOUNT["name"], args.user)
    drv.type_into(ACCOUNT["login"], args.user)
    drv.type_into(ACCOUNT["host"], args.user)
    drv.type_into(ACCOUNT["password"], args.password)
    drv.type_into(ACCOUNT["confirm"], args.password)
    drv.look("account filled")
    drv.click(NEXT, "Users: Next", settle=25.0)
    drv.look("summary")

    # 10. SUMMARY. `Install` at (1365, 902), then the modal's own `Install now` at
    #     (1018, 620). Both measured; neither read.
    drv.click(INSTALL, "Summary: Install", settle=18.0)
    drv.look("confirm install")
    drv.click(INSTALL_NOW, "the install confirmation's Install now", settle=30.0)
    drv.look("installing")

    # 11. UNTIL IT SAYS SO.
    #
    # THE ONLY DECISION IN THIS FUNCTION, AND IT IS THE SAFE KIND. Completion is a TERMINAL
    # condition: there is nothing after it to skip, so matching the words the installer
    # prints at the end cannot miss a pane. Every other step above is a coordinate.
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
        #
        # AND IT IS CHECKED IN BOTH LANGUAGES THE INSTALLER ACTUALLY COMES UP IN. On the CI
        # runners the wizard is English; on a machine whose egress reports China it is
        # Simplified Chinese, and a completed install there reads `安装成功！` with
        # `CachyOS 已安装在您的电脑上了。` under it - measured on a local run that finished
        # exactly as it should. A completion test that only knew English would have thrown
        # that disk away at the timeout.
        if ("all done" in current or "you may now" in current
                or "reboot now" in current or "restart now" in current
                or "安装成功" in current or "已安装在" in current
                or "现在重启" in current
                or "todo listo" in current or "ya puede" in current
                or "reiniciar" in current or "instalación completada" in current
                or "instalacion completada" in current):
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
