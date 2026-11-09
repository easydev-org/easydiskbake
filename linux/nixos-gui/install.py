"""Install NixOS from its GRAPHICAL ISO, through the Calamares that image carries.

    python3 linux/nixos-gui/install.py --iso out/iso/nixos-graphical.iso \
                                      --out out/nixos-gui.qcow2

WHY THIS EXISTS, AND WHY IT IS NOT linux/nixos/bake.py

linux/nixos/bake.py installs the MINIMAL image by typing `nixos-install` at a serial
console. That is the right shape for a base system, and it is why the minimal image was
chosen for it: the smaller image has no graphical session at all.

THE GRAPHICAL IMAGE IS A DIFFERENT INSTALLER, AND IT IS A GUI ONE. Its ISO carries
`calamares` and the `calamares-nixos-extensions` package, whose settings.conf is the
sequence

    welcome, locale, keyboard, users, packagechooser, notesqml@unfree, partition, summary
    -> partition, mount, nixos, users, umount
    -> finished

- a six-pane wizard with a footer, exactly like Manjaro's, CachyOS's and EndeavourOS's.
So this is a `-gui` bake in the same sense they are: it boots a live desktop, drives the
installer, and is judged by the disk it wrote.

WHAT IT INSTALLS, AND THE TWO ANSWERS THAT ARE NOT THE DEFAULTS

    the desktop      GNOME. `packagechooser.conf` lists GNOME, Plasma, Xfce and others with
                     `default: gnome`, so the first card is already chosen - the click below
                     is a confirmation, and it is made because a pane whose list has a
                     selection that looks like a default has been wrong before in this
                     repository (Manjaro's Office Suite, CachyOS's Bootloader).
    the account      docker/admin. `users.conf` sets `doAutologin: false` and requires a
                     password of at least eight characters OR `allowWeakPasswords` - both
                     `allowWeakPasswords: true` and `allowWeakPasswordsDefault: false` are
                     set, so `admin` is accepted only after the warning is acknowledged.

THE WALK IS A YAML FILE - see steps/install.yaml. It is a sequence because Calamares does
not skip panes, and the numbers in it were measured on THIS image's own frames.
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
import lastframe    # noqa: E402
import live         # noqa: E402
import ocr          # noqa: E402
import qmp as Q     # noqa: E402
import uefi         # noqa: E402
import vm           # noqa: E402

# --- measured on a live NixOS 26.05 GNOME session, 1920x1080, UEFI -------------------------
#
# Calamares' window is fixed in size and its footer does not move with the content, so one
# Next coordinate serves every pane whose content fills the window. The steps file carries
# the per-pane numbers and the reasons; this block only names the two the driver needs by
# name: the launcher's own button and the footer used for the pane-height survey.
INSTALLER_ICON = (0, 0)          # set from the frame the first time; see steps/install.yaml

DESKTOP_SECONDS = 300


def log(msg: str) -> None:
    print(f"[nixos-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Driver:
    """The look/click/text interface lib/steps.py drives, over this guest's QMP socket."""

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

    def close(self) -> None:
        """Release QMP before lib/steps.py opens its own: qemu serves one client."""
        try:
            self.screen.close()
        except Exception:                              # noqa: BLE001
            pass


def wait_for_desktop(drv: Driver, timeout: int) -> bool:
    """Until the GNOME session has drawn its desktop.

    A MEASURED WAIT, THEN A READING. The live session takes about 100 s to reach GDM on
    this image, and the walk cannot start until the desktop is up. The wait is bounded by
    DESKTOP_SECONDS and by --timeout, and it is a reading rather than a sleep because the
    runner is slower than a local machine.
    """
    log(f"waiting up to {DESKTOP_SECONDS}s for the live desktop (measured: about 100s)")
    deadline = time.time() + min(DESKTOP_SECONDS, timeout)
    while time.time() < deadline:
        drv.look("boot")
        text = drv.text()
        if "install" in text or "nixos" in text:
            log("the live desktop is up")
            return True
        time.sleep(15)
    log("=== THE LIVE DESKTOP DID NOT APPEAR ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:20])}")
    return False


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=None)
    p.add_argument("--disk-size", default="64G")
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--timeout", type=int, default=5400)
    p.add_argument("--user", default=os.environ.get("NIXOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("NIXOS_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "nixos-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    # THE ISO BOOTS ITSELF. Unlike the answer-file bakes, this image's own kernel and initrd
    # are not extracted: the graphical medium boots the way it does for a person, through its
    # own GRUB, and the Calamares that boots is the one on the squashfs. Extracting the kernel
    # would bypass the menu entry that sets up the live session's tmpfs, which is what the
    # installer runs from.
    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            # TRY THE MEDIUM FIRST. The disk is empty, so the firmware's own order would find
            # nothing to boot; naming it removes the difference between machines.
            "-boot", "d",
        ],
        # UEFI, ALWAYS. The graphical ISO's GRUB entry is the EFI one, and the installer
        # writes a systemd-boot/ESP layout that expects the firmware to find it there.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                     vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "nixos-gui"),
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
            # THE WALK IS A YAML FILE, the same shape every other -gui bake here uses. The
            # driver is closed first because qemu serves one QMP client per socket.
            drv.close()
            import steps
            walk_path = os.path.join(HERE, "steps", "install.yaml")
            log(f"=== the walk: {os.path.basename(walk_path)} ===")
            ok, ran = steps.run_file(
                walk_path, guest.paths.drive, work=work,
                user=args.user, password=args.password,
                disk_path=disk,
            )
            log(f"{len(ran)} step(s) ran" if ok
                else f"the walkthrough failed after {len(ran)} step(s)")
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

    # A DISK WITH NOTHING ON IT IS NOT A SUCCESS. A written system is gigabytes; an unwritten
    # sparse image is kilobytes.
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
