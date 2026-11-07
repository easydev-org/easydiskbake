"""Install Linux Mint from its Live ISO, through Ubiquity, its GTK installer.

    python3 linux/mint-gui/install.py --iso out/iso/linuxmint-22.3-cinnamon-64bit.iso \
                                      --out out/mint-gui.qcow2

MINT IS A LIVE ISO, LIKE FEDORA AND openSUSE - AND UNLIKE DEBIAN.

There is no installer image: the medium boots Cinnamon, and the installer is an icon on the
desktop. So the machine reaches a desktop first, and everything below happens in that
session.

CASPER, BUT NOT THE SAME KERNEL ARGUMENTS AS UBUNTU.

Mint is an Ubuntu derivative and its live medium is casper's, so the kernel comes from
casper/vmlinuz and casper/initrd.lz. Its own isolinux entry passes

    boot=casper uuid=6e72f523-... quiet splash

where the uuid is that ISO's own filesystem UUID. THAT UUID IS NOT USED HERE, and the
difference matters: it identifies one particular build, so a new ISO breaks a driver that
copied it. Mint's menu also offers `iso-scan/filename=`, which is the USB-stick form - the
ISO is not a file inside the medium it boots from, so that finds nothing and drops to an
initramfs prompt. Measured, both: the run that tried it printed

    could not find the ISO /linuxmint-22.3-cinnamon-64bit.iso
    (initramfs)

`boot=casper` alone is what Ubuntu's own bake passes, and casper then finds the CDROM by
itself.

UBIQUITY IS SEVEN PANES, AND ONE OF THEM IS NOT A QUESTION AT ALL.

    Welcome            language
    Keyboard layout    English (US)
    Multimedia codecs  a checkbox, off by default
    Installation type  Erase disk and install Linux Mint   <- already selected
    Where are you?     a time zone
    Who are you?       name, username, password
    (install)          a progress bar with slides

THE TIME ZONE IS LEFT AT WHATEVER THE INSTALLER CHOSE. Ubiquity preselects a city from the
machine's location, and on this cluster that is Shanghai. Changing it is one more text field
and one more way to be wrong; the requirement is that the disk installs, not that it thinks
it is in a particular country. The same rule applies to every installer in this repository.

"SHORT PASSWORD" IS A WARNING, NOT A REFUSAL. `admin` is five characters and Ubiquity says
so, in red, next to the field - but Continue still works and the password is used. Measured:
the run that clicked through it installed, booted, and logged in with `admin`. That is the
reason this driver does not try to work around it.
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

# --- measured on a live Linux Mint 22.3 Cinnamon session, 1920x1080 ------------------------

# THE DESKTOP ICON. Double-clicked with an 80 ms gap: MATE and Cinnamon read two presses
# farther apart than their double-click interval as two single clicks, and a single click
# only selects the icon. The same measurement that set the GhostBSD driver's value.
INSTALL_ICON = (114, 47)
DOUBLE_CLICK_GAP = 0.08

# THE UBIQUITY FOOTER. Quit / Back / Continue (or Install Now), right-aligned inside the
# window, which Ubiquity centres on a 1920x1080 screen at a fixed size.
CONTINUE = (1306, 727)
BACK = (1192, 727)

# THE CONFIRMATION DIALOG, which has its own two buttons in its own smaller window.
WRITE_CONTINUE = (1269, 659)

# THE ACCOUNT FORM. Ubiquity puts the field to the RIGHT of its label, and the labels are
# short and fixed.
ACCOUNT = {
    "name": (965, 347),
    "host": (877, 375),
    "password": (877, 458),
    "confirm": (877, 492),
}


def log(msg: str) -> None:
    print(f"[mint-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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


def wait_for_live_desktop(drv: Driver, timeout: int) -> bool:
    """Until Cinnamon has drawn its panel.

    THE MARKER IS THE PANEL'S OWN MENU BUTTON AND CLOCK, which are on every Cinnamon desktop
    and nowhere else - the installer's window does not exist until this driver opens it, so
    there is nothing inside the installer to wait for.
    """
    log("waiting for the live desktop")
    deadline = time.time() + timeout
    while time.time() < deadline:
        drv.look("boot")
        joined = drv.text()
        if "install linux mint" in joined or ("menu" in joined and "mint" in joined):
            log("the live session is up")
            return True
        time.sleep(10)
    log("=== THE LIVE DESKTOP DID NOT APPEAR ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:24])}")
    return False


def walk_ubiquity(drv: Driver, args) -> bool:
    """Click through Ubiquity's seven panes, checking each one arrives."""
    # 1. WELCOME. `English` is selected; Continue.
    drv.double_click(INSTALL_ICON, "Install Linux Mint", settle=25.0)
    drv.look("welcome")
    if "welcome" not in drv.text():
        log("=== THE INSTALLER DID NOT OPEN ===")
        log(f"  screen: {drv.text()[:200]}")
        return False

    drv.click(CONTINUE, "Welcome: Continue (English)", settle=12.0)
    drv.look("keyboard")

    # 2. KEYBOARD LAYOUT. English (US) is selected; Continue.
    drv.click(CONTINUE, "Keyboard: Continue (English US)", settle=12.0)
    drv.look("codecs")

    # 3. MULTIMEDIA CODECS. The checkbox ships OFF and stays off: a bake does not need
    #    codecs, and enabling them is a download this run does not have to make.
    drv.click(CONTINUE, "Codecs: Continue (not installed)", settle=12.0)
    drv.look("installation type")

    # 4. INSTALLATION TYPE. `Erase disk and install Linux Mint` is already selected.
    drv.click(CONTINUE, "Installation type: Install Now", settle=15.0)
    drv.look("confirm")

    # 5. THE CONFIRMATION, CLICKED WHETHER OR NOT THE SCREEN WAS READ.
    #
    # Run 42372449152 stopped here: the conditional below decided from an OCR read whether
    # the `Write the changes to disks?` dialog had appeared, did not see it, and skipped the
    # click - then spent the rest of the run on the Installation type pane with the modal
    # possibly open behind it.
    #
    # A CONDITIONAL THAT GATES A CLICK IS THE SAME MISTAKE AS A LOOKUP THAT CHOOSES ONE, and
    # this repository has now paid for it four times (kubuntu three, mint one). The click
    # costs nothing when the dialog is absent: (1269, 659) is blank space on the pane, so a
    # stray click there selects nothing and types nothing.
    drv.click(WRITE_CONTINUE, "Write the changes to disks: Continue", settle=20.0)
    drv.look("timezone")

    # 6. WHERE ARE YOU? THE DEFAULT IS KEPT. See the module docstring: on this cluster it is
    #    Shanghai, and changing it is one more text field that can go wrong for no gain.
    log("time zone left at the installer's default")
    drv.click(CONTINUE, "Where are you: Continue (default kept)", settle=15.0)
    drv.look("who are you")

    # 7. WHO ARE YOU? The account, in the same shape every Linux bake here uses.
    drv.clear_and_type(ACCOUNT["name"], args.user)
    # THE HOSTNAME IS DERIVED FROM THE MACHINE MODEL - `docker-Standard-PC-Q35` - and is
    # replaced with the user name so the disk has one canonical name in it.
    drv.clear_and_type(ACCOUNT["host"], args.user)
    drv.clear_and_type(ACCOUNT["password"], args.password)
    drv.clear_and_type(ACCOUNT["confirm"], args.password)
    drv.look("account filled")

    # `Short password` is shown in red beside the field for `admin`, and Continuing anyway
    # is the measured, working answer - see the module docstring.
    drv.click(CONTINUE, "Who are you: Continue (short password accepted)", settle=20.0)
    drv.look("installing")

    # 8. UNTIL THE INSTALLER SAYS SO. Ubiquity ends on a dialog whose button is
    #    `Restart Now`; the progress page before it carries the package name it is on.
    #
    #    THE POINTER IS NUDGED EVERY POLL, AND THAT IS NOT COSMETIC. Run 42408624906 read
    #    the string `14:25` and then `14:26` and `14:34` - the session's own clock in the
    #    corner - for ten minutes, because Mint's lock screen had come down and its large
    #    clock was the only text on it. An idle desktop locks itself; a locked desktop
    #    shows no installer at all, and the run would have been thrown away after a
    #    successful install. One pixel of pointer movement per poll keeps it unlocked.
    log("waiting for the installer to finish")
    started = time.time()
    while time.time() - started < args.timeout:
        try:
            Q.move(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                          # noqa: BLE001
            pass
        drv.look("installing")
        joined = drv.text()
        if "restart now" in joined or "installation is complete" in joined:
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
    p.add_argument("--user", default=os.environ.get("MINT_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MINT_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "mint-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    kernel = os.path.join(work, "vmlinuz")
    initrd = os.path.join(work, "initrd")
    if not (os.path.exists(kernel) and os.path.exists(initrd)):
        for member in ("casper/vmlinuz", "casper/initrd.lz"):
            subprocess.run(["7z", "e", "-y", f"-o{work}", args.iso, member], check=True,
                           stdout=subprocess.DEVNULL)
        for produced, wanted in ((os.path.join(work, "vmlinuz"), kernel),
                                 (os.path.join(work, "initrd.lz"), initrd)):
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
            # `boot=casper` AND NOTHING ELSE - no uuid, no iso-scan. See the module
            # docstring for both measurements.
            "-append", "boot=casper locale=en_US.UTF-8 quiet splash",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "mint-gui"),
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
        if wait_for_live_desktop(drv, args.timeout):
            ok = walk_ubiquity(drv, args)
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
