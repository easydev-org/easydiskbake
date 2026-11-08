"""Install Pop!_OS from its Live ISO, through `pop-os-installer`, its GTK installer.

    python3 linux/popos-gui/install.py --iso out/iso/pop-os_24.04_amd64_intel_9.iso \
                                       --out out/popos-gui.qcow2

POP!_OS HAS AN INSTALLER OF ITS OWN - NOT CALAMARES, NOT UBIQUITY, NOT ANACONDA.

It is `pop-os-installer`, a GTK application that OPENS BY ITSELF on the live desktop, and its
window is a fixed-size card at (485,155)..(1435,860) with the same footer position on every
pane: Next / Select / Erase and Install at (1367, 817), Back at (1279, 817).

THE FLOW, measured pane by pane on a live Pop!_OS 24.04 session at 1920x1080:

    Select a Language      English preselected                      Select (1367,817)
    Languages (variants)   United States preselected                Select
    Keyboard Layout        English (US) preselected                 Select
    Keyboard (variants)    Default preselected - NOT Dvorak         Select
    Install                Clean Install / Custom (Advanced)        click Clean Install, Next
    Select a drive         `Virtio Block Device /dev/vda 64.0 GiB`  click the row, Erase and Install (1334,817)
    Create User Account     Full Name, User Name                    Next
    Create User Account     password + confirm                      Next
    Drive Encryption       Don't Encrypt / Set Password / Encrypt   Don't Encrypt (1091,817)
    Installing             Extracting Files (N%)

TWO THINGS THAT ARE NOT DEFAULTS, AND BOTH ARE HANDLED.

1. `Clean Install` IS A CHOICE, NOT THE DEFAULT. The Install pane offers `Clean Install` and
   `Custom (Advanced)` as two cards with neither selected, so the footer button does nothing
   until one is clicked. `Clean Install` is the right answer: it erases the drive this guest
   owns (`/dev/vda`, 64.0 GiB) and installs a fresh copy.

2. THE DRIVE MUST BE SELECTED before `Erase and Install` is enabled - measured, the button is
   grey until the `Virtio Block Device` row is clicked.

THE ACCOUNT IS TWO PANES. The first asks `Full Name` and `User Name`; the second asks for a
password twice. `admin` is short enough that the installer prints
`The password is shorter than 8 characters` beside the field, and continues anyway - the same
warning every other bake here accepts, and the same password.

NO ENCRYPTION. `Don't Encrypt` is clicked deliberately: an encrypted disk needs a passphrase
at boot, which is a second thing that can fail on a machine nobody is watching, and the bake's
question is whether the installer works, not whether the disk is confidential. The pane
defaults to `Encrypt`, so this is not a no-op.

THE MEDIUM IS BOOTED WHOLE - no -kernel, no -initrd, no -append, `-boot d` - the rule every GUI
driver here follows. OVMF is attached by lib/uefi.py.
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

# --- MEASURED ON A LIVE POP!_OS 24.04 SESSION, 1920x1080 --------------------------------
#
# THE WINDOW IS A FIXED-SIZE CARD AND ITS FOOTER DOES NOT MOVE. Measured on every pane:
#
#     Next / Select / Erase and Install      (1367, 817)
#     Back                                   (1279, 817)
#
# The installer OPENS BY ITSELF on the live desktop, so there is no launcher to press.

SELECT = (1367, 817)
BACK = (1279, 817)

# THE INSTALL PANE'S TWO CARDS. Neither is selected when the pane is drawn, so the footer
# button is dead until one is clicked. `Clean Install` erases the drive and installs fresh.
CLEAN_INSTALL = (1180, 456)

# THE DRIVE ROW on `Select a drive`, and the button that starts the write.
DRIVE_ROW = (1150, 500)
ERASE_AND_INSTALL = (1334, 817)

# THE ACCOUNT, TWO PANES. The first is Full Name and User Name; the second is the password
# twice. Measured on both.
ACCOUNT_NAME = {
    "full name": (1136, 538),
    "user name": (1136, 602),
}
ACCOUNT_PASSWORD = {
    "password": (1136, 452),
    "confirm": (1136, 558),
}

# DRIVE ENCRYPTION. The pane DEFAULTS TO `Encrypt`; this driver chooses `Don't Encrypt`.
DONT_ENCRYPT = (1091, 817)

# THE LIVE MEDIUM'S BOOT TIME. Measured: about 100 s on this image.
DESKTOP_SECONDS = 220

# THE INSTALL RUN. Measured: about 12 minutes from `Don't Encrypt` to the completion pane.
INSTALL_SECONDS = 2400


def log(msg: str) -> None:
    print(f"[popos-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
        log(f"  {label}: {' | '.join(w[0] for w in self.words[:20])}")

    def text(self) -> str:
        return " ".join(w[0].lower() for w in self.words)

    def click(self, xy, why: str, settle: float = 2.0) -> None:
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
        time.sleep(DOUBLE_CLICK_GAP)
        Q._send(self.q, "release", [{"type": "btn", "data": {"down": False, "button": "left"}}])
        time.sleep(DOUBLE_CLICK_GAP)
        Q._send(self.q, "press 2", [{"type": "btn", "data": {"down": True, "button": "left"}}])
        time.sleep(DOUBLE_CLICK_GAP)
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
        time.sleep(0.8)


def wait_for_installer(drv: Driver, timeout: int) -> bool:
    """Until `pop-os-installer` has drawn its language pane.

    IT OPENS BY ITSELF, so the wait is for the WINDOW rather than for a click. Measured: the
    desktop arrives about 100 s after qemu starts and the installer a few seconds later.
    """
    log(f"waiting {DESKTOP_SECONDS}s for the live desktop and the installer")
    deadline = time.time() + min(DESKTOP_SECONDS, timeout)
    while time.time() < deadline:
        drv.look("boot")
        joined = drv.text()
        if "select a language" in joined or "install pop" in joined:
            log("the installer is up")
            return True
        time.sleep(15)
    log("=== THE INSTALLER DID NOT APPEAR ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:20])}")
    return False


def walk_installer(drv: Driver, args) -> bool:
    """Click through `pop-os-installer`: a measured coordinate and a measured wait.

    NOTHING IS READ TO DECIDE ANYTHING. Every step is a number in this file and a duration;
    the screen is read afterwards and the reading goes into the log. The window is a
    fixed-size card, so one footer coordinate serves every pane.
    """
    # 1-4. THE FOUR SELECTION PANES, EACH WITH ITS DEFAULT ALREADY RIGHT:
    #
    #     Select a Language      `English`
    #     Languages (variants)   `United States`
    #     Keyboard Layout        `English (US)`
    #     Keyboard (variants)    `Default`   - NOT Dvorak, unlike Zorin's Ubiquity
    for pane in ("language", "language variant", "keyboard layout", "keyboard variant"):
        drv.click(SELECT, f"{pane}: Select", settle=18.0)
        drv.look(f"after {pane}")

    # 5. INSTALL. `Clean Install` must be clicked before the footer works: neither card is
    #    selected when the pane is drawn.
    log("choosing Clean Install (erases this guest's own drive)")
    drv.click(CLEAN_INSTALL, "Install: Clean Install", settle=6.0)
    drv.look("clean install selected")
    drv.click(SELECT, "Install: Next", settle=22.0)
    drv.look("select a drive")

    # 6. SELECT A DRIVE, then `Erase and Install`. The button is grey until the row is
    #    clicked - measured.
    log("selecting the guest's own virtio drive")
    drv.click(DRIVE_ROW, "Select a drive: Virtio Block Device", settle=6.0)
    drv.look("drive selected")
    drv.click(ERASE_AND_INSTALL, "Erase and Install", settle=30.0)
    drv.look("create user account")

    # 7. THE ACCOUNT, PANE ONE. `Full Name` COPIES INTO `User Name` AS IT IS TYPED, so both
    #    are set explicitly - measured: typing the name into both without clearing left
    #    `User Name` reading `dockerdocker`.
    log("filling the account name")
    drv.clear_and_type(ACCOUNT_NAME["full name"], args.user)
    drv.look("full name typed")
    drv.clear_and_type(ACCOUNT_NAME["user name"], args.user)
    drv.look("account name filled")
    drv.click(SELECT, "Create User Account: Next", settle=22.0)
    drv.look("create user account password")

    # 8. THE ACCOUNT, PANE TWO: the password twice. `admin` draws
    #    `The password is shorter than 8 characters` beside the field, and the same footer
    #    button continues - the warning every other bake here accepts.
    log("filling the account password")
    drv.clear_and_type(ACCOUNT_PASSWORD["password"], args.password)
    drv.clear_and_type(ACCOUNT_PASSWORD["confirm"], args.password)
    drv.look("password filled")
    drv.click(SELECT, "Create User Account: Next", settle=22.0)
    drv.look("drive encryption")

    # 9. DRIVE ENCRYPTION. THE PANE DEFAULTS TO `Encrypt`; this clicks `Don't Encrypt`: an
    #    encrypted disk needs a passphrase at boot, one more thing that can fail on a machine
    #    nobody is watching.
    log("choosing `Don't Encrypt` (the pane defaults to Encrypt)")
    drv.click(DONT_ENCRYPT, "Drive Encryption: Don't Encrypt", settle=35.0)
    drv.look("installing")

    # 10. A MEASURED WAIT. The progress pane shows `Extracting Files (N%)`; the percentage is
    #     the only text on it, and a completed disk must not depend on reading it.
    #
    #     MEASURED: about 12 minutes. FORTY is that with room for a slower runner.
    log(f"waiting up to {INSTALL_SECONDS}s for the install (measured: about 12 minutes)")
    started = time.time()
    while time.time() - started < min(INSTALL_SECONDS, args.timeout):
        try:
            Q.click(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                          # noqa: BLE001
            pass
        drv.look("installing")
        current = drv.text()
        if ("installation complete" in current or "install complete" in current
                or "restart" in current or "reboot" in current):
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
    p.add_argument("--user", default=os.environ.get("POPOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("POPOS_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "popos-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    # NO KERNEL OR INITRD IS EXTRACTED, AND THAT IS THE SAME RULE EVERY OTHER GUI DRIVER
    # HERE FOLLOWS: the ISO is booted whole, by its own bootloader, and the firmware finds
    # its El Torito entry. See the `extra=` list below.

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            # THE MEDIUM BOOTS AS ITS OWN BOOTLOADER INTENDS - no -kernel, no -initrd, no
            # -append. Its grub.cfg carries the casper arguments (boot=casper, the medium's
            # own uuid or label), and the firmware reads that through the El Torito
            # catalogue. A hand-written line was the source of two failures on this
            # repository's other drivers: a guessed `archisobasedir` sent Garuda to an
            # emergency shell, and `iso-scan/filename=` found nothing because the ISO is not
            # a file inside the medium it boots from.
            "-boot", "d",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "popos-gui"),
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
        if wait_for_installer(drv, args.timeout):
            ok = walk_installer(drv, args)
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
