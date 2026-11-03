"""Turn a macOS Setup Assistant disk into a disk that reaches its desktop.

    python3 macos/15/desktop.py --disk out/macos15.qcow2 --out out/macos15-desktop.qcow2

WHAT THE INPUT IS

macos-15_setup-bake's output: a disk with a complete macOS on it that boots into the
Setup Assistant. Not a disk that reaches a desktop - the wizard has never been answered.

WHAT THIS DOES

Boots that disk and drives the wizard by coordinates: country, transfer, account,
Apple ID, and the panes after it. Then it waits for a desktop, which is the only proof
that the wizard finished rather than that a click landed.

WHY THE WIZARD IS DRIVEN RATHER THAN ANSWERED

macOS has no unattended format. There is no answer file, no autoinstall, nothing to hand
it - Setup Assistant is a GUI and the only way through it is to click it. drive.py holds
the coordinates and the reasoning for each; every one of them was measured from a frame
this pipeline captured, and none was guessed.

WHY THE WAIT IS SPLIT FROM THE CLICKS

drive.py's sequence has a fixed length and clicks on a schedule. That is right for the
panes, and it says nothing about whether the machine afterwards is at a desktop or at the
login window with a dialog over it. lib/desktop.py answers that, and it is the same
wallpaper test the Windows bake uses - a light top and a busy dark bottom.

THE BOOT MUST BE THE INSTALLED SYSTEM, NOT THE RECOVERY IMAGE. So no -cdrom and no
recovery disk: the machine is the target disk and OpenCore, exactly as it will be when
somebody uses it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

# `desktop` IS NOT IMPORTED FROM lib HERE, AND IT CANNOT BE BY NAME.
#
# This file is macos/15/desktop.py, and sys.path has macos/15 first so that `machine` and
# `opencore` resolve - which means `from lib import desktop` finds THIS file, not
# lib/desktop.py, and fails with "cannot import name 'machine' from 'lib'". Run
# 41599405096 died on exactly that, before a single line of the bake.
#
# lib/desktop.py is the wallpaper test. It is loaded under a different name below.
from lib import compress, live as livelib, machine, smoke, vm  # noqa: E402


def _wallpaper_test():
    """lib/desktop.py, loaded under a name that cannot collide with this file."""
    import importlib.util

    path = os.path.join(REPO, "lib", "desktop.py")
    spec = importlib.util.spec_from_file_location("lib_desktop", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def log(message: str) -> None:
    print(f"[macos-desktop {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True,
                   help="the setup-stage disk: macOS installed, at the Setup Assistant")
    p.add_argument("--out", default=None)
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--user", default=os.environ.get("MACOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MACOS_PASSWORD", "admin"))
    p.add_argument("--assistant-timeout", type=int, default=45 * 60)
    p.add_argument("--desktop-timeout", type=int, default=30 * 60)
    args = p.parse_args(argv)

    out = args.out or args.disk.replace(".qcow2", "-desktop.qcow2")
    work = args.work or os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"disk={args.disk} out={out}")
    if not os.path.isfile(args.disk):
        raise SystemExit(f"no disk at {args.disk}")

    # A COPY, so a failed wizard leaves the setup-stage disk intact for the next attempt.
    disk = os.path.join(work, "desktop.qcow2")
    log(f"copying {args.disk} -> {disk}")
    copied = subprocess.run(["cp", "--reflink=auto", args.disk, disk],
                            capture_output=True, text=True)
    if copied.returncode != 0:
        raise SystemExit(
            f"could not copy the disk: {(copied.stderr or copied.stdout).strip()[:300]}")

    # THE BOOT ASSETS. opencore.build() needs the recovery image only to take Apple's
    # APFS driver out of it - the driver is installed into boot.img, which travels with
    # the machine, not with the disk.
    log("=== OpenCore, firmware, APFS driver ===")
    import opencore

    assets = opencore.build("15", work)
    log(f"identity {assets['identity']['serial']} / {assets['identity']['mac']}")

    # NO RECOVERY DISK. machine.drives() wants one, so this does not call it: a desktop
    # stage that booted the installer again would be asking a different question.
    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            "-drive", f"file={disk},if=none,format=qcow2,id=target",
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
            "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
        ],
        shape=machine.shape(assets, cpus=args.cpus, mem_mb=args.mem),
        stdout=os.path.join(work, "console.log"),
    ).start()

    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    snap = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         guest.paths.record, frames,
         os.environ.get("SNAPSHOT_INTERVAL", "5"), "2000"],
        stdout=open(os.path.join(frames, "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )

    # THE RECORDING IS KEPT CURRENT, so a cancelled run still has one - see lib/live.py.
    # The wizard is driven by clicks and its failures are only ever visible in a video.
    live = livelib.start(
        [os.path.join(frames, "shots"), frames],
        os.path.join(work, "install.mp4"),
        os.path.join(work, "last-frame.png"),
        every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "20")),
    )

    arrived = False
    try:
        log("=== the Setup Assistant: drive.py ===")
        try:
            subprocess.run(
                [sys.executable, os.path.join(HERE, "drive.py"),
                 "--qmp", guest.paths.drive, "--frames", frames,
                 "--user", args.user, "--password", args.password],
                check=True, timeout=args.assistant_timeout,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            log(f"the Setup Assistant driver failed: {exc}")

        log("waiting for a desktop")
        arrived = wait_for_desktop(guest, frames, args.desktop_timeout)
    finally:
        guest.stop(acpi_first=arrived)
        snap.terminate()
        try:
            snap.wait(timeout=5)
        except subprocess.TimeoutExpired:
            snap.kill()
        live.stop(final=True)

    if not arrived:
        log("=== THE WIZARD DID NOT REACH A DESKTOP ===")
        log("  the disk is not published: a half-set-up macOS cannot be used")
        return 1

    if not smoke.check_disk(disk, root_fs="apfs"):
        log("=== THE DISK FAILED ITS CHECKS; refusing to produce an artifact ===")
        return 1

    log("compressing")
    compress.compress(disk, out)
    log(f"BAKED macos15 desktop -> {out} ({os.path.getsize(out) >> 20} MiB)")
    log("  this disk reaches its desktop without any wizard; the ssh stage can use it")
    return 0


def wait_for_desktop(guest: vm.Guest, frames: str, timeout: int) -> bool:
    """Wait until the wallpaper test passes, then let the guest settle.

    THE SAME TEST AS THE WINDOWS BAKE, deliberately. lib/desktop.py explains its three
    thresholds and why a colour match alone does not work. Sequoia's default wallpaper is
    a blue-to-purple gradient with the light end at the top, which is the shape they
    describe.

    WHAT THIS DEPENDS ON: that the wizard has FINISHED. While it is up the screen is a
    white pane and fails all three tests - correctly, because it is not a desktop.
    drive.py's last action is the one that dismisses the final pane.

    THE SETTLE IS PART OF THE ARRIVAL. macOS writes hard for the first minute after the
    first login - mds, the login item registry, the first Time Machine snapshot - and
    stopping the guest in the middle of that is what produces a disk that needs to repair
    itself on every later boot.
    """
    shots = os.path.join(frames, "shots")
    deadline = time.time() + timeout
    started = time.time()
    while time.time() < deadline:
        if not guest.alive():
            log("qemu exited before a desktop appeared")
            return False
        if _wallpaper_test().reached_desktop(shots):
            log(f"a macOS desktop is on screen ({(time.time() - started):.0f}s)")
            settle = int(os.environ.get("DESKTOP_SETTLE_SECONDS", "30"))
            log(f"letting the guest settle for {settle}s before stopping it")
            time.sleep(settle)
            if not guest.alive():
                log("the guest exited while settling - not keeping the disk")
                return False
            return True
        time.sleep(5)
    log(f"timed out after {timeout}s")
    return False


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
