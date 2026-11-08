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
   the footer button is greyed out. The click below is made on the ROW, at a measured
   coordinate, and nothing is read to decide whether it took.

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

# PARTITIONS: `Erase disk` IS THE RADIO BUTTON'S CIRCLE, AT (653, 340).
#
# THE OLD NUMBER WAS (1043, 298) AND IT IS THE `Swap` DROPDOWN. Measured on this ISO's own
# frame: that point is inside the combo box labelled `Swap (no Hibernate)`, so the click
# OPENED A LIST instead of selecting a partitioning scheme. The list then sat over the
# footer, the following click at (1325, 801) went into it, and the wizard never left
# Partitions - which is what every Manjaro run since e08b233 did, logging
#
#     click (1043, 298) - Partitions: Erase disk
#     click (1325, 801) - Partitions: Next
#     filling the account form          <- on the Partitions pane
#
# The layout it left behind is visible in the frame: `Swap (no Hibernate)` selected and an
# extra 6.37 GiB swap partition in the "After" bar, which is the dropdown's doing.
#
# On UEFI the radio is already filled, so the click is a confirmation - but it has to be a
# confirmation of the RIGHT control.
ERASE_DISK = (653, 340)

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

    def text(self) -> str:
        """Everything OCR read, for the LOG ONLY.

        IT DECIDES NOTHING. This driver - like ubuntu-gui and kubuntu-gui - advances on a
        measured coordinate and a measured wait; the reading exists so that a failed run
        can be diagnosed from its log without the recording. See walk_calamares for the two
        detectors that were tried here and both made the run worse.
        """
        return " ".join(w[0].lower() for w in self.words)

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
    """Click through Manjaro's nine panes: A MEASURED COORDINATE, THEN A MEASURED WAIT.

    NOTHING IS READ TO DECIDE ANYTHING. Each step is a number in this file and a duration;
    the screen is read afterwards and the reading goes into the log, where it is useful for
    a post-mortem and dangerous as a decision. This is the rule ubuntu-gui and kubuntu-gui
    already follow, and the reason both of them are green.

    WHAT WENT WRONG WHEN THIS DRIVER DID DECIDE, TWICE:

      a word per pane - `expect="office"` was TRUE ON THE USERS PANE, because its own
      content mentions LibreOffice, so run 42398673800 "advanced" to Office Suite while
      standing on Users and sat there for 908 seconds;

      the whole OCR text before against after - prose does not read identically twice, so
      that is true while standing still, and run 42413739470 reported every step from
      Welcome to `the install confirmation's Install Now` as an advance while it had never
      left Users.

    A NUMBER AND A WAIT CANNOT MAKE EITHER MISTAKE. Every coordinate below was taken from a
    frame of this ISO, and this window keeps ONE size - measured on Welcome, Location,
    Keyboard, Partitions, Users, Office Suite and Summary, the footer button is at the same
    place on all of them. Manjaro's Calamares does not resize per pane the way CachyOS'
    does, so one Next coordinate is right for the whole wizard.
    """
    # 1. THE WELCOME WINDOW'S BUTTON. Clicked twice: the live session draws that window
    #    while this driver is arriving from a fixed wait, and the first press on an
    #    unfocused window is taken as focus.
    drv.double_click(LAUNCH_INSTALLER, "Launch installer", settle=35.0)
    drv.look("calamares welcome")
    if "welcome" not in drv.text():
        log("the installer did not open on the first double-click; trying once more")
        drv.double_click(LAUNCH_INSTALLER, "Launch installer (second attempt)", settle=40.0)
        drv.look("calamares welcome")
    if "welcome" not in drv.text():
        log("=== THE INSTALLER DID NOT OPEN ===")
        log(f"  screen: {drv.text()[:200]}")
        return False

    # 2. WELCOME -> LOCATION.
    drv.click(NEXT_DEFAULT, "Welcome: Next", settle=18.0)
    drv.look("location")

    # 3. LOCATION -> KEYBOARD. The default zone is kept: whatever the installer read from
    #    the network at startup stays. Changing it is one more field that can go wrong for
    #    no gain, and the bake's job is a disk that installs, not a particular region.
    log("location left at the installer's default")
    drv.click(NEXT_DEFAULT, "Location: Next", settle=18.0)
    drv.look("keyboard")

    # 4. KEYBOARD -> PARTITIONS.
    drv.click(NEXT_DEFAULT, "Keyboard: Next", settle=18.0)
    drv.look("partitions")

    # 5. PARTITIONS. On UEFI `Erase disk` is already selected and the layout already has
    #    its 300 MiB EFI partition; the click is a confirmation that costs nothing when it
    #    is already set, and fixes it when it is not.
    drv.click(ERASE_DISK, "Partitions: Erase disk", settle=6.0)
    drv.look("erase disk selected")
    drv.click(NEXT_DEFAULT, "Partitions: Next", settle=20.0)
    drv.look("users")

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
    drv.click(NEXT_DEFAULT, "Users: Next", settle=20.0)
    drv.look("office suite")

    # 7. OFFICE SUITE. THE ROW, NOT THE HIGHLIGHT - see the module docstring. Without this
    #    click Next is disabled and every retry is swallowed.
    log("selecting No Office Suite (the row itself; the highlight is not a selection)")
    drv.click(NO_OFFICE_SUITE, "Office Suite: No Office Suite", settle=6.0)
    drv.look("office suite selected")
    drv.click(NEXT_OFFICE, "Office Suite: Next", settle=20.0)
    drv.look("summary")

    # 8. SUMMARY -> INSTALL, then the modal's own button.
    drv.click(INSTALL_BUTTON, "Summary: Install", settle=18.0)
    drv.look("confirm install")
    # The modal is not checked for. A conditional here is what stopped three Kubuntu runs
    # on a screen that was correct; the click lands on blank space if it is not up.
    drv.click(CONFIRM_INSTALL_NOW, "the install confirmation's Install Now", settle=30.0)
    drv.look("installing")

    # 9. UNTIL IT SAYS SO.
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
        except Exception:                              # noqa: BLE001
            pass
        drv.look("installing")
        current = drv.text()
        # COMPLETION IS THE SENTENCE, NOT THE HEADING.
        #
        # The Finish pane's heading is `All done.` in a large font, and tesseract does not
        # read it: measured on a completed local install, the whole pane OCRs as
        #
        #     manjar you may now re: environment. tart now * 3:58 pm * 11/7/26
        #
        # with no `all done` anywhere. `you may now` is the body sentence, it is unique to
        # this pane, and it reads cleanly.
        if ("all done" in current or "you may now" in current
                or "has been installed" in current
                or "安装成功" in current or "已安装在" in current or "现在重启" in current
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
