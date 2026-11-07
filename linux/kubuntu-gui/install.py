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
import vm           # noqa: E402
import lastframe    # noqa: E402

# --- measured on a live Kubuntu 26.04.1 desktop ISO, 1920x1080 ---------------------------
WELCOME_INSTALL = (1326, 841)   # the welcome screen's own "Install Kubuntu" button
NEXT = (1775, 1040)             # footer Next, and the green Install on Summary
BACK = (1676, 1040)

# The Partitions pane. "Erase disk" is the first radio; its label is not clickable, the circle is.
ERASE_DISK_RADIO = (208, 73)
INSTALL_NOW = (1058, 535)       # the confirmation dialog's button

# The Users pane.
ACCOUNT = {
    "name": (294, 48),
    "login": (294, 115),
    "host": (294, 183),
    "password": (294, 250),
    "confirm": (500, 250),
}
LOGIN_AUTOMATICALLY = (208, 292)


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

    def click(self, xy, why: str, settle: float = 1.0) -> None:
        log(f"click {xy} - {why}")
        Q.click(self.q, *xy)
        time.sleep(settle)

    def click_twice(self, xy, why: str, settle: float = 12.0) -> None:
        """Click, then click again. The first may only give the window focus."""
        self.click(xy, why, settle=0.8)
        self.click(xy, f"{why} (second click)", settle=settle)

    def alt_n(self, why: str, settle: float = 12.0) -> None:
        log(f"alt+n - {why}")
        Q.key_combo(self.q, "alt", "n")
        time.sleep(settle)

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
            "-append", "console=ttyS0,115200n8 --- console=ttyS0,115200n8",
        ],
        shape=vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
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
            # 1. THE LIVE DESKTOP'S OWN INSTALL BUTTON.
            drv.click_twice(WELCOME_INSTALL, "Install Kubuntu on the live desktop", settle=25.0)
            drv.look("welcome")

            # 2. WELCOME -> LOCATION
            drv.click_twice(NEXT, "Welcome: Next", settle=12.0)
            drv.look("location")

            # 3. LOCATION -> KEYBOARD
            drv.click_twice(NEXT, "Location: Next", settle=12.0)
            drv.look("keyboard")

            # 4. KEYBOARD -> CUSTOMIZE
            drv.click_twice(NEXT, "Keyboard: Next", settle=12.0)
            drv.look("customize")

            # 5. CUSTOMIZE -> PARTITIONS
            drv.click_twice(NEXT, "Customize: Next (Full installation)", settle=15.0)
            drv.look("partitions")

            # 6. ERASE THE DISK. The radio, then Next, then the confirmation dialog.
            drv.click(ERASE_DISK_RADIO, "Partitions: Erase disk", settle=4.0)
            drv.look("erase-disk")
            drv.click_twice(NEXT, "Partitions: Next", settle=15.0)
            drv.look("users")

            # 7. THE ACCOUNT FORM.
            log("filling the account form")
            drv.type_into(ACCOUNT["name"], args.full_name)
            drv.type_into(ACCOUNT["login"], args.full_name)
            drv.type_into(ACCOUNT["host"], "kubuntu")
            drv.type_into(ACCOUNT["password"], args.password)
            drv.type_into(ACCOUNT["confirm"], args.password)
            drv.click(LOGIN_AUTOMATICALLY, "log in automatically", settle=3.0)
            drv.look("account-filled")
            drv.click_twice(NEXT, "Users: Next", settle=15.0)
            drv.look("summary")

            # 8. SUMMARY -> INSTALL, and the confirmation dialog's Install Now.
            drv.click_twice(NEXT, "Summary: Install", settle=15.0)
            drv.look("confirm-dialog")
            drv.click(INSTALL_NOW, "Continue with installation", settle=25.0)
            drv.look("installing")

            # 9. WAIT FOR THE FINISH PANE.
            log("waiting for the installer to finish")
            started = time.time()
            while time.time() - started < args.timeout:
                drv.look("finish")
                joined = " ".join(w[0].lower() for w in drv.words)
                if "all done" in joined or "restart" in joined or "finish" in joined:
                    log("=== INSTALL COMPLETE ===")
                    ok = True
                    break
                time.sleep(20)
            if not ok:
                log("=== THE INSTALLER NEVER REPORTED COMPLETION ===")
                log(f"  last screen: {' | '.join(w[0] for w in drv.words[:24])}")
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
