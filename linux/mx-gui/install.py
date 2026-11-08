"""Install MX Linux from its Live ISO, through minstall, its GTK installer.

    python3 linux/mx-gui/install.py --iso out/iso/MX-25.3_Xfce_x64.iso \
                                    --out out/mx-gui.qcow2

MX IS NEITHER CALAMARES NOR UBIQUITY. Its installer is `minstall`, and it is a different
program with a different shape: THREE questions first (terms, installation type, a final
confirmation), and then - while the disk is already being written - it ASKS THE REST of its
questions one page at a time. There is no "all the questions, then start" phase.

    Terms of Use              Next
    Select type of installation   `Regular install using the entire disk` preselected  Next
    Installation Confirmation  Start
    --- the disk is being written from here on ---
    Create a swap file        Next      (progress 29% already)
    Computer Identity         Next      (`mx` pre-filled)
    Localization Defaults     Next      (`United States - American English` pre-filled)
    Default User Login        Next      (login name, password, password again)
    ...

EVERY COORDINATE IS MEASURED ON THIS IMAGE at 1920x1080, from a live session, one frame per
pane. Nothing is read to decide anything; the screen is read afterwards and the reading goes
into the log.

THE MEDIUM IS ANTIx'S, NOT CASPER'S. The kernel is at `antiX/vmlinuz` and the initrd at
`antiX/initrd.gz`; both are extracted and handed to qemu, which is how this image's own
isolinux entry boots it. The ISO is attached as its own cdrom as well.
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

# --- MEASURED ON A LIVE MX-25.3 XFCE SESSION, 1920x1080 -----------------------------------
#
# MX Welcome is open by itself on the live desktop, and its `Install MX Linux` card at
# (543,378) starts minstall. The installer window is (320,170)..(1570,930) and its footer
# never moves: Close (759,889), Back (1338,889), Next/Start (1482,889).
# THE INSTALLER'S OWN SIDEBAR ICON, NOT THE WELCOME WINDOW'S CARD.
#
# `MX Welcome` draws a grid of cards and `Install MX Linux` is one of them, but a press at its
# centre - measured in CI run 42476000228 - opened `Frequently Asked Questions` in qpdfview
# instead: the cards are close together and the press landed one row off. The desktop's OWN
# panel icon is a single small target with nothing beside it:
#
#     the dock on the left edge, `Installer` at (81, 259)
#
# verified locally, where it opened the installer on the second press.
INSTALLER_ICON = (81, 259)

NEXT = (1482, 889)
BACK = (1338, 889)
CLOSE = (759, 889)

# THE CONFIRMATION'S OWN BUTTON, on the last page before the disk is touched.
START = (1478, 889)

# DEFAULT USER LOGIN PAGE.
ACCOUNT = {
    "login": (1100, 303),
    "password": (1100, 336),
    "confirm": (1100, 370),
}

# THE LIVE DESKTOP'S BOOT TIME. Measured: about 90 s on this image (antiX is small), and the
# wait is that with room.
DESKTOP_SECONDS = 210

# THE INSTALL RUN. MX writes the disk while it is still asking its questions, so the run ends
# when the last question is answered and the installer's own `Finished` page appears; the
# duration is a bound on the whole of that.
INSTALL_SECONDS = 1800


def log(msg: str) -> None:
    print(f"[mx-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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

    def click(self, xy, why: str, settle: float = 3.0) -> None:
        log(f"click {xy} - {why}")
        Q.click(self.q, *xy)
        time.sleep(settle)

    def double_click(self, xy, why: str, settle: float = 20.0) -> None:
        """Two presses 80 ms apart at one position - a real double-click."""
        log(f"double-click {xy} - {why}")
        x, y = xy
        Q._send(self.q, "d press", [
            {"type": "abs", "data": {"axis": "x", "value": Q.to_abs(x, 1920)}},
            {"type": "abs", "data": {"axis": "y", "value": Q.to_abs(y, 1080)}},
            {"type": "btn", "data": {"down": True, "button": "left"}},
        ])
        time.sleep(0.08)
        Q._send(self.q, "d release", [
            {"type": "btn", "data": {"down": False, "button": "left"}}])
        time.sleep(0.08)
        Q._send(self.q, "d press 2", [
            {"type": "btn", "data": {"down": True, "button": "left"}}])
        time.sleep(0.08)
        Q._send(self.q, "d release 2", [
            {"type": "btn", "data": {"down": False, "button": "left"}}])
        time.sleep(settle)

    def clear_and_type(self, xy, value: str) -> None:
        """Click the field, empty it completely, then type."""
        Q.click(self.q, *xy)
        time.sleep(1.2)
        Q.key_combo(self.q, "ctrl", "a")
        time.sleep(0.4)
        for _ in range(60):
            Q.key(self.q, "backspace")
            time.sleep(0.02)
        unsupported = keys.type_text(self.q, value, per_char=0.08)
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(0.8)


def wait_for_live_desktop(drv: Driver, timeout: int) -> bool:
    """Until MX Welcome is up.

    MX draws its own welcome window on the live desktop and the installer starts from it, so
    that window is the thing to wait for. Measured: `MX Welcome` and `Install MX Linux` are
    both on it, and neither word appears anywhere else in this boot.
    """
    log(f"waiting {DESKTOP_SECONDS}s for the live desktop (measured: about 90s)")
    deadline = time.time() + min(DESKTOP_SECONDS, timeout)
    while time.time() < deadline:
        drv.look("boot")
        if "mx" in drv.text() and "install" in drv.text():
            log("the live desktop is up")
            return True
        time.sleep(15)
    log("=== THE LIVE DESKTOP DID NOT APPEAR ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:20])}")
    return False


def walk_minstall(drv: Driver, args) -> bool:
    """Click through minstall: a measured coordinate and a measured wait, each time."""
    # 1. `Install MX Linux` IN THE WELCOME WINDOW. TWO PRESSES, AND THE SECOND IS THE ONE
    #    THAT OPENS IT.
    #
    # Measured locally and then in CI, identically: one click at (543,378) leaves the welcome
    # window up - the CI frame still read `MX Welcome | About this system | Manual | About |
    # MX-25.3 "Infinity"` twenty-eight seconds later - while a second press opens the
    # installer. The first press is spent giving the window focus.
    #
    # The reading after the pair is a LOG LINE, not a gate: the installer's window title is
    # `MX Linux Installer`, and a title is the kind of thing a translated or re-themed build
    # can change. The empty-disk check at the end is the verdict.
    drv.click(INSTALLER_ICON, "desktop: Installer", settle=3.0)
    drv.click(INSTALLER_ICON, "desktop: Installer (second press)", settle=28.0)
    drv.look("installer")
    if "installer" not in drv.text():
        log("  (the installer window was not read - continuing on coordinates anyway)")

    # 2. TERMS OF USE -> INSTALLATION TYPE.
    drv.click(NEXT, "Terms of Use: Next", settle=14.0)
    drv.look("installation type")

    # 3. INSTALLATION TYPE. `Regular install using the entire disk` is preselected and the
    #    only drive is the guest's own `vda (64.0 GB)`.
    log("installation type left at `Regular install using the entire disk`")
    drv.click(NEXT, "Installation Type: Next (entire disk)", settle=16.0)
    drv.look("confirmation")

    # 4. INSTALLATION CONFIRMATION -> START. THIS IS THE POINT OF NO RETURN: `Start` formats
    #    the drive, and MX begins writing immediately.
    drv.click(START, "Installation Confirmation: Start", settle=25.0)
    drv.look("installing")

    # 5. THE QUESTIONS THAT COME WHILE THE DISK IS BEING WRITTEN. Each is `Next`, and the
    #    defaults on every one of them are the right answer for a bake:
    #
    #      Create a swap file        swap file ticked, 2048 MB
    #      Computer Identity         computer name `mx`
    #      Localization Defaults     `United States - American English`
    #
    #    The account page is the only one with fields this driver has to fill.
    for label in ("swap file", "computer identity", "localization"):
        drv.click(NEXT, f"{label}: Next (defaults kept)", settle=18.0)
        drv.look(f"after {label}")

    # 6. DEFAULT USER LOGIN. Three fields, and the password pair must match or `Next` stays
    #    grey. Typed with clear_and_type so a pre-filled field cannot survive.
    log("filling the default user account")
    drv.clear_and_type(ACCOUNT["login"], args.user)
    drv.clear_and_type(ACCOUNT["password"], args.password)
    drv.clear_and_type(ACCOUNT["confirm"], args.password)
    drv.look("account filled")
    drv.click(NEXT, "Default User Login: Next", settle=25.0)
    drv.look("after the account")

    # 7. UNTIL IT IS DONE. MX ends on a `Finished` page with a `Reboot` button; the disk is
    #    complete before it. A measured wait covers the rest of the pages this driver does
    #    not name, and the completion is a READ because it is terminal - there is nothing
    #    after it to skip.
    log(f"waiting up to {INSTALL_SECONDS}s for the installer to finish")
    started = time.time()
    while time.time() - started < min(INSTALL_SECONDS, args.timeout):
        try:
            Q.click(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                          # noqa: BLE001
            pass
        drv.look("installing")
        current = drv.text()
        if ("finished" in current or "reboot" in current
                or "installation complete" in current):
            log("=== INSTALL COMPLETE ===")
            return True
        # EVERY REMAINING PAGE IS `Next`, so the footer is pressed each poll as well: MX's
        # later questions (bootloader, services, ...) all advance with the same button.
        try:
            Q.click(drv.q, *NEXT)
        except Exception:                          # noqa: BLE001
            pass
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
    p.add_argument("--user", default=os.environ.get("MX_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MX_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "mx-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    # ANTIx'S LAYOUT: /antiX/vmlinuz and /antiX/initrd.gz.
    kernel = os.path.join(work, "vmlinuz")
    initrd = os.path.join(work, "initrd")
    if not (os.path.exists(kernel) and os.path.exists(initrd)):
        for member in ("antiX/vmlinuz", "antiX/initrd.gz"):
            subprocess.run(["7z", "e", "-y", f"-o{work}", args.iso, member], check=True,
                           stdout=subprocess.DEVNULL)
        for produced, wanted in ((os.path.join(work, "vmlinuz"), kernel),
                                 (os.path.join(work, "initrd.gz"), initrd)):
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
            # MX'S OWN isolinux LINE, with the language and time zone pinned. `quiet splash`
            # are the menu's defaults; `lang`/`tz` are what its boot menu passes.
            "-append", "quiet splash lang=en_US tz=UTC",
        ],
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                     vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "mx-gui"),
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
            ok = walk_minstall(drv, args)
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

    if ok and os.path.exists(disk) and os.path.getsize(disk) < 64 * 2**20:
        log(f"=== THE DISK IS EMPTY ({os.path.getsize(disk) >> 20} MiB) - "
            "THE INSTALL DID NOT HAPPEN ===")
        ok = False

    if ok and os.path.exists(disk):
        log(f"compressing {disk} -> {args.out}")
        compress.compress(disk, args.out)
        log(f"{args.out}: {os.path.getsize(args.out) >> 20} MiB")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
