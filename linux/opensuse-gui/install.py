"""Install openSUSE Tumbleweed from its KDE Live ISO, through YaST2's graphical installer.

    python3 linux/opensuse-gui/install.py --iso out/iso/openSUSE-...-Media.iso \
                                         --out out/opensuse-gui.qcow2

YAST2, NOT CALAMARES, AND THE DIFFERENCE COST A WRONG GUESS.

openSUSE's installer is YaST2. Calamares is not involved anywhere, and the two are not
similar: Calamares is a linear wizard with a footer, YaST2 is a single window with a
navigation list down the left that can be entered at any point. This driver was written
against a guess and corrected against a frame, which is why the docstring says so.

THE INSTALLER IS ONE DESKTOP ICON AND IT RUNS AS ROOT.

The live session's icon is

    /usr/share/applications/live-installation.desktop
    Exec=kdesu -u root /usr/sbin/start-install.sh

SO IT HAS TO BE STARTED THROUGH kdesu, AND THIS IS THE PART THAT MATTERS.

The first attempt ran `/usr/sbin/start-install.sh` from a terminal and YaST2 came up in
TEXT mode with

    Your computer does not fulfill all requirements for a graphical installation.
    There is either less than 96 MB memory or the X server could not be started.

which is YaST2's honest way of saying it had no DISPLAY it trusted. The X server was
running the whole time - it was that shell, started from a terminal that had been opened by
the driver rather than by the session, that carried no way to reach it. Double-clicking the
icon goes through kdesu and the same binary opens graphically.

TWO THINGS ON THIS WIZARD ARE NOT DEFAULTS, AND BOTH ARE CHECKED.

1. System Role HAS NO SELECTION UNTIL ONE IS CLICKED. The pane lists five roles and
   `Desktop with KDE Plasma` looks highlighted, but Continue answers

       Select one of the available roles to continue.

   until the radio button itself is clicked. The highlight is the row the mouse is over.

2. THE WEAK-PASSWORD DIALOG ANSWERS TO THE KEYBOARD, NOT TO THE MOUSE. `admin` trips
   YaST2's strength check, which asks `Really use this password?` in a modal. Clicking its
   `Yes` did nothing twice in a row - measured - while Return accepted it, because Yes is
   the default button.
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

# --- measured on a live openSUSE Tumbleweed KDE session, 1920x1080 -------------------------

INSTALL_ICON = (267, 34)
DOUBLE_CLICK_GAP = 0.08

# THE LIVE MEDIUM'S BOOT TIME, AS A NUMBER. Measured: about 180 s from qemu start to a
# clickable installer icon on this image. The wait is longer than that - see
# wait_for_live_desktop for why a reading cannot replace it.
DESKTOP_SECONDS = 240

# YAST2'S FOOTER. Back / Next (or Install) at the bottom right of its window.
NEXT = (1596, 859)
BACK = (1517, 859)

# `Online Repositories` asks whether to activate the network repositories. No: an install
# from the medium alone is reproducible, and the driver has no reason to pull an update
# stream whose contents change daily.
ONLINE_REPOS_NO = (1004, 614)

# SYSTEM ROLE'S FIRST RADIO BUTTON - the KDE Plasma row. Its x is the circle, not the label.
ROLE_KDE_PLASMA = (588, 333)

# THE LOCAL USER FORM.
USER_FULL_NAME = (1106, 418)
USER_PASSWORD = (1106, 540)
USER_CONFIRM = (1106, 604)

# THE FINAL BUTTON is `Install`, at the same place Next has been all along.
INSTALL = (1587, 859)


def log(msg: str) -> None:
    print(f"[opensuse-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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

    def type_into(self, xy, value: str) -> None:
        Q.click(self.q, *xy)
        time.sleep(1.2)
        unsupported = keys.type_text(self.q, value, per_char=0.1)
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(0.8)


def wait_for_live_desktop(drv: Driver, timeout: int) -> bool:
    """Wait a MEASURED TIME for the KDE desktop, then stop waiting.

    NO OCR DECISION, AND THIS IS THE THIRD DESIGN TRIED.

    1. The first version waited for the word "Installation". The desktop came up and
       tesseract read the icon labels as `Rome` and `Uporade` - nine-pixel text under an
       icon is its worst case - so the driver waited forever on a screen that was ready.

    2. The second waited for KDE's panel clock (`AM`/`PM`). Under ocr.Screen's own psm 3
       the whole desktop reads as three words, `in | to | ome`; the clock is not among
       them. That is what run after run logged while a complete desktop was on screen.

    THE TIME IS THE MEASUREMENT. qemu starts, the live medium boots, KDE initialises and
    the installer icon is clickable - about three minutes on this image, measured on four
    local runs and every CI run that got this far. Three minutes is a number; "has the
    desktop drawn" is a question OCR cannot be relied on to answer here.

    THE BUDGET IS DOUBLED RATHER THAN TRUSTED. If the machine is slow the click lands early
    and does nothing, so the wait is 240 s where the measurement says 180 s. And the double
    click is retried once below, because a click into a desktop that is still drawing is the
    only failure this cannot see coming.
    """
    log(f"waiting {DESKTOP_SECONDS}s for the live desktop (measured: about 180s)")
    deadline = time.time() + min(DESKTOP_SECONDS, timeout)
    while time.time() < deadline:
        drv.look("boot")
        time.sleep(20)
    # THE LAST FRAME IS READ FOR THE LOG ONLY. Nothing below branches on it.
    drv.look("desktop")
    log(f"  screen after the wait: {' | '.join(w[0] for w in drv.words[:12])}")
    return True


def walk_yast2(drv: Driver, args) -> bool:
    """Click through YaST2, checking each pane is the one expected."""
    # 1. THE ICON, THROUGH kdesu. See the module docstring: this is what makes YaST2 come up
    #    graphically, and running the same script from a shell does not.
    #
    #    TRIED TWICE, BECAUSE A CLICK INTO A STILL-DRAWING DESKTOP DOES NOTHING AND SAYS
    #    NOTHING. The wait before this is 240 s against a measured 180, so the first attempt
    #    should land - but a desktop that answered the first double-click by merely selecting
    #    the icon looks exactly like one that ignored it, and a second attempt costs 40 s.
    drv.double_click(INSTALL_ICON, "Installation desktop icon", settle=40.0)
    drv.look("yast welcome")
    if "language" not in drv.text():
        log("YaST2 did not open on the first double-click; trying once more")
        drv.double_click(INSTALL_ICON, "Installation desktop icon (second attempt)",
                         settle=45.0)
        drv.look("yast welcome")
    if "language" not in drv.text():
        log("=== YAST2 DID NOT OPEN ===")
        log(f"  screen: {drv.text()[:200]}")
        return False

    # 2. WELCOME -> SYSTEM PROBING. YaST2 probes the machine and then asks about the online
    #    repositories. The probing is not instant, so this waits for the question.
    drv.click(NEXT, "Welcome: Next", settle=25.0)
    drv.look("probing")

    if "online repositories" in drv.text():
        drv.click(ONLINE_REPOS_NO, "Online Repositories: No", settle=15.0)
        drv.look("system role")

    # 3. SYSTEM ROLE. THE RADIO BUTTON, NOT THE ROW - see the module docstring.
    log("selecting Desktop with KDE Plasma (its radio button, which starts empty)")
    drv.click(ROLE_KDE_PLASMA, "role: Desktop with KDE Plasma", settle=4.0)
    drv.look("role selected")
    drv.click(NEXT, "System Role: Next", settle=25.0)
    drv.look("partitioning")

    if "suggested partitioning" in drv.text():
        # THE SUGGESTED LAYOUT IS THE RIGHT ONE: GPT, a BIOS boot partition, btrfs for / and
        # swap. Accepting it is one click and changing it is several screens.
        drv.click(NEXT, "Suggested Partitioning: Next", settle=25.0)
        drv.look("timezone")

    # 4. TIME ZONE. THE DEFAULT IS KEPT, exactly as on every other installer here: it is
    #    already a US zone on this cluster and the requirement is a booting disk.
    log("time zone left at the installer's default")
    drv.click(NEXT, "Clock and Time Zone: Next", settle=20.0)
    drv.look("local user")

    # 5. THE LOCAL USER.
    drv.type_into(USER_FULL_NAME, args.user)
    drv.type_into(USER_PASSWORD, args.password)
    drv.type_into(USER_CONFIRM, args.password)
    drv.look("user filled")

    # THE WEAK-PASSWORD DIALOG TAKES THE KEYBOARD. See the module docstring: clicking Yes
    # does nothing, Return accepts it.
    drv.click(NEXT, "Local User: Next", settle=12.0)
    drv.look("weak password")
    if "too simple" in drv.text() or "really use this password" in drv.text():
        log("the password is weak; accepting it with Return (its default button)")
        Q.key(drv.q, "ret")
        time.sleep(20)
        drv.look("after weak password")

    if "installation settings" not in drv.text():
        log("=== THE INSTALLATION SETTINGS PANE DID NOT APPEAR ===")
        log(f"  screen: {drv.text()[:200]}")
        return False

    # 6. INSTALL.
    drv.click(INSTALL, "Install", settle=30.0)
    drv.look("installing")

    # 7. UNTIL IT IS DONE. YaST2 shows a progress bar and then a `Congratulations` pane; the
    #    disk is written when the bar finishes, and that pane is the only thing on screen
    #    that says so.
    log("waiting for the installer to finish")
    started = time.time()
    while time.time() - started < args.timeout:
        drv.look("installing")
        joined = drv.text()
        if "congratulations" in joined or "installation complete" in joined:
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
    p.add_argument("--user", default=os.environ.get("OPENSUSE_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("OPENSUSE_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "opensuse-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    kernel = os.path.join(work, "vmlinuz")
    initrd = os.path.join(work, "initrd")
    if not (os.path.exists(kernel) and os.path.exists(initrd)):
        for member in ("boot/x86_64/loader/linux", "boot/x86_64/loader/initrd"):
            subprocess.run(["7z", "e", "-y", f"-o{work}", args.iso, member], check=True,
                           stdout=subprocess.DEVNULL)
        for produced, wanted in ((os.path.join(work, "linux"), kernel),
                                 (os.path.join(work, "initrd"), initrd)):
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
            # THE VOLUME LABEL IS THE ISO'S OWN. `rd.live.image` is dracut's half of the same
            # contract Fedora uses; without both, the initramfs cannot find its squashfs.
            "-append", "root=live:CDLABEL=openSUSE_Tumbleweed_KDE_Live rd.live.image "
                       "locale=en_US.UTF-8 quiet splash=silent systemd.show_status=yes",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "opensuse-gui"),
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
            ok = walk_yast2(drv, args)
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
