"""Install GhostBSD from its official ISO, through gbi, its GTK installer.

    python3 bsd/ghostbsd/install.py --iso out/iso/GhostBSD-24.10.1.iso \
                                    --out out/ghostbsd-gui.qcow2

WHY GHOSTBSD AND NOT FREEBBSD

FreeBSD has no graphical installer. bsdinstall draws dialog(1) menus on a text console, and
bsd/freebsd/bake.py installs through its `script` target - the same path the menu wizard takes,
with the prompts skipped. There is no GUI there to drive, so freebsd.yml is unchanged.

GhostBSD is FreeBSD with MATE and its own GTK installer (gbi). It is the BSD-family entry that
actually has a graphical install path, which is the one worth driving by position.

THE MATE IMAGE, NEVER THE XFCE ONE. GhostBSD publishes both. They are the same base with a
different desktop, so baking both would answer the same question twice.

============================================================================
THE ONE THING THAT MAKES THIS DRIVER POSSIBLE
============================================================================

QEMU's usb-tablet is an absolute-positioning HID device, and on most guests that is all a
coordinate driver needs. On GhostBSD it does nothing, and the reason is worth writing down
because it is not a missing driver:

    ugen0.2: <QEMU Tablet Adomax Technology Co., Ltd> at usbus0
    uhid0:   <QEMU QEMU USB Tablet, class 0/0, rev 2.00/2.00, addr 1> on usbus0

The tablet IS attached - to uhid(4). It reports HID subclass 0 / protocol 0, which is the
non-boot protocol; ums(4) accepts only subclass 1 / protocol 2, declines it, and uhid takes
the raw reports without ever writing a position into the input layer. The pointer then sits
at the top-left corner of the root window whatever coordinate the host sends.

Relative motion is not a fix either. qmp.click_relative() walks a PS/2 mouse out of a corner,
and it does move the cursor - a tooltip appeared where one was expected - but X applies its
own acceleration curve to relative deltas, so the error accumulates and a 30-pixel icon is
missed. Measured twice.

The fix is one loader variable:

    set hw.usb.usbhid.enable=1

With it, usbhid + hms claim the tablet at enumeration and absolute coordinates work: measured
by clicking Applications at (60,12) and seeing all six submenus open, then double-clicking
Install GhostBSD at (88,240) and watching gbi appear.

IT HAS TO BE SET IN THE LOADER, NOT AT RUNTIME. This was measured the hard way: from a root
shell, `sysctl hw.usb.usbhid.enable=1` succeeds (the value goes 0 -> 1) but changes nothing,
because the sysctl is read when a device is enumerated and this device is already open by
uhid. `usbconfig -d ugen0.2 reset` does not rebind it either. The loader runs before
enumeration, so the loader is where it goes.

And the loader is reachable: QMP key events reach it, which is not true of everything on this
guest. Verifying that first is what turned this from a guess into a driver - the space key
cleared the autoboot countdown on the boot menu.

============================================================================
THE SEQUENCE
============================================================================

        loader prompt   set hw.usb.usbhid.enable=1 / boot
        X driver menu   scfb  (UEFI framebuffer; the others find no hardware)
        desktop         double-click Install GhostBSD
        gbi             welcome -> timezone -> ... -> install

Everything after the loader is a position, for the reason ubuntu-gui's first draft proved: a
driver that decides each pane from the words tesseract read skipped five panes in one second
when a title did not contain the word it looked for, and then waited out the whole timeout. A
fixed sequence has one failure mode - being wrong about a position - and that is found on the
first run and fixed by changing one number.
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
import vm           # noqa: E402

# --- measured on a live GhostBSD 24.10.1 MATE session, 1920x1080 --------------------------

# The desktop icon. Double-clicked, and the gap between the two clicks matters: 80 ms opens
# gbi and 500 ms does not. MATE reads two presses further apart than its double-click
# interval as two single clicks, and a single click only selects the icon.
INSTALL_ICON = (88, 240)
DOUBLE_CLICK_GAP = 0.08

# The X driver menu that GhostBSD shows when it cannot start X itself. scfb draws on the
# UEFI framebuffer, which is the one adapter this machine actually has; the default (intel)
# and the others find no hardware and drop back here. Four Downs from the top entry.
SCFB_DOWNS = 4

# gbi's own buttons, in the 1920x1080 window it opens centred.
# These are on the welcome pane and have been the same shape on every pane since.
GBI_NEXT = (1297, 783)

# --- the loader ---------------------------------------------------------------

LOADER_PAUSE_KEY = "spc"     # "[Space] to pause", stated on the boot menu itself
LOADER_PROMPT_KEY = "3"      # "3. Escape to loader prompt"
USBHID_SETTING = "set hw.usb.usbhid.enable=1"


def log(msg: str) -> None:
    print(f"[ghostbsd {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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

    def click(self, xy, why: str, settle: float = 2.0) -> None:
        log(f"click {xy} - {why}")
        Q.click(self.q, *xy)
        time.sleep(settle)

    def double_click(self, xy, why: str, settle: float = 20.0) -> None:
        """Two presses 80 ms apart, in one pointer position.

        WHY NOT Q.click() TWICE. Each Q.click() moves the pointer, presses and releases;
        called twice the presses end up farther apart than MATE's double-click interval and
        the icon is only selected. Measured: 500 ms between clicks did nothing, 80 ms opened
        gbi on the first try.
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

    def type_line(self, text: str, why: str) -> None:
        log(f"type {text!r} - {why}")
        unsupported = keys.type_text(self.q, text, per_char=0.05)
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r} - no qcode for those characters")
        Q.key(self.q, "ret")


def _hold_boot_menu(drv: Driver, seconds: float = 15.0) -> bool:
    """Pause the autoboot countdown, by pressing space across the window it can appear in.

    PRESS FIRST, ASK LATER, AND THAT IS NOT SLOPPINESS.

    The menu is drawn about 1.5 s after qemu starts and autoboots four seconds later,
    which is stated on the menu itself ("Autoboot in 4 seconds. [Space] to pause"). One
    OCR pass costs a screendump, a 1.5 s settle and tesseract - comfortably longer than
    the whole countdown. A driver that looks and then decides is therefore always late,
    every time, on every machine. This one was, until the press moved in front of the look.

    Spamming the key is safe in both directions: space arriving while OVMF is still
    drawing is ignored, and space arriving after the pause is ignored by a menu that is
    no longer counting. Measured - one space left the menu sitting with the countdown
    line gone, which is what "paused" looks like.
    """
    started = time.time()
    while time.time() - started < seconds:
        Q.key(drv.q, LOADER_PAUSE_KEY)
        time.sleep(0.5)
    drv.look("boot menu")
    joined = " ".join(w[0].lower() for w in drv.words)
    return "boot" in joined and ("options" in joined or "loader" in joined)


def set_usbhid_in_loader(drv: Driver) -> bool:
    """Pause the boot menu, drop to the loader prompt, and enable absolute pointing.

    WHY THIS IS NOT IN lib/qmp.py. The variable is GhostBSD's problem, not QEMU's: the
    tablet is a correct absolute-positioning device and every Linux guest here uses it
    unchanged. Only this one kernel declines it, so only this driver works around it.

    A MISSED MENU IS RETRIED ONCE. The window is four seconds wide and the first attempt
    has to construct the driver and its QMP connection first, so a slow start is a real
    possibility rather than a hypothetical. A reset costs a second; a bake that failed
    because a key arrived late costs ninety minutes.
    """
    for attempt in (1, 2):
        if attempt == 2:
            log("the menu was missed; resetting and trying the loader once more")
            drv.q.execute("system_reset")
            time.sleep(1.0)
        if _hold_boot_menu(drv):
            break
        if attempt == 2:
            log("=== THE BOOT MENU NEVER APPEARED ===")
            log(f"  last screen: {' | '.join(w[0] for w in drv.words[:20])}")
            return False

    log("the boot menu is paused; escaping to the loader prompt")
    Q.key(drv.q, LOADER_PROMPT_KEY)
    time.sleep(2.5)
    drv.look("loader prompt")

    # THE SETTING IS THE POINT OF THIS FUNCTION: without it every click below lands on the
    # top-left corner of the screen and the run looks like a driver bug.
    log(f"loader: {USBHID_SETTING}")
    unsupported = keys.type_text(drv.q, USBHID_SETTING, per_char=0.05)
    if unsupported:
        log(f"  COULD NOT TYPE {unsupported!r}")
        return False
    Q.key(drv.q, "ret")
    time.sleep(1.5)
    drv.look("after set")

    log("loader: boot")
    keys.type_text(drv.q, "boot", per_char=0.05)
    Q.key(drv.q, "ret")
    return True


def choose_scfb(drv: Driver, timeout: int) -> bool:
    """Take the scfb entry from GhostBSD's X driver menu, when it appears.

    IT MAY NOT APPEAR, and that is not a failure: the menu is shown when X cannot start on
    the default driver. On this machine (UEFI, std VGA) it always has, but the driver treats
    "the desktop is already up" and "the menu was answered" as the same success.
    """
    log("waiting for the desktop or the X driver menu")
    deadline = time.time() + timeout
    while time.time() < deadline:
        drv.look("boot")
        joined = " ".join(w[0].lower() for w in drv.words)

        if "applications" in joined and "places" in joined:
            log("the MATE desktop is up")
            return True

        # THE MENU NAMES ITS OWN ENTRIES, which is a far better signal than the title above
        # them: "scfb" appears once, as "Start Desktop with SCFB driver", and does not appear
        # on the desktop or anywhere else in the boot. Matching "failed" instead would also
        # match the title it sits under, and a boot that merely printed an error would be
        # answered with four Down keys meant for a list that was never drawn.
        if "scfb" in joined:
            log("the X driver menu is up; choosing scfb (the UEFI framebuffer)")
            for _ in range(SCFB_DOWNS):
                Q.key(drv.q, "down")
                time.sleep(1.0)
            Q.key(drv.q, "ret")
            time.sleep(25)
            continue

        time.sleep(8)

    log("=== NEITHER THE DESKTOP NOR THE DRIVER MENU APPEARED ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:20])}")
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
    p.add_argument("--firmware-code", default=os.environ.get(
        "FIRMWARE_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd"))
    p.add_argument("--firmware-vars", default=os.environ.get(
        "FIRMWARE_VARS", "/usr/share/OVMF/OVMF_VARS_4M.fd"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "ghostbsd-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    # THE FIRMWARE PAIR IS THE GUEST'S NVRAM, so the vars file is a writable COPY. A shared
    # template would carry one guest's boot entries into the next run.
    if not (os.path.isfile(args.firmware_code) and os.path.isfile(args.firmware_vars)):
        raise SystemExit(
            f"=== UEFI FIRMWARE NOT FOUND ===\n"
            f"  code: {args.firmware_code}\n  vars: {args.firmware_vars}\n"
            f"GhostBSD's loader is built for the EFI system partition: with SeaBIOS the ISO "
            f"drops to a loader prompt instead of the installer.")
    vars_copy = os.path.join(work, "ghostbsd.vars")
    if os.path.exists(vars_copy):
        os.unlink(vars_copy)
    import shutil
    shutil.copyfile(args.firmware_vars, vars_copy)
    log(f"vars: writable copy of {os.path.basename(args.firmware_vars)}")

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
        ],
        shape=vm.Shape(
            cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
            firmware_code=args.firmware_code, firmware_vars=vars_copy,
            resolution=(1920, 1080),
            vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1"),
        ),
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
        if not set_usbhid_in_loader(drv):
            log("=== THE LOADER STEP FAILED ===")
        elif choose_scfb(drv, args.timeout):
            # 1. THE INSTALLER, FROM THE DESKTOP ICON.
            drv.double_click(INSTALL_ICON, "Install GhostBSD", settle=25.0)
            drv.look("gbi welcome")
            ok = True
        else:
            log("=== THE LIVE DESKTOP DID NOT COME UP ===")
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
