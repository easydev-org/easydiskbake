"""Turn a macOS Setup Assistant disk into a disk that reaches its desktop.

    python3 macos/15/desktop.py --disk out/macos15.qcow2 --out out/macos15-desktop.qcow2

WHAT THE INPUT IS

the macos15 workflow's setup job: a disk with a complete macOS on it that boots into the
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

# TWO IMPORTS, FROM TWO PLACES, AND THE `from lib import` FORM CANNOT REACH THE SECOND.
#
# `from lib import machine` asks lib/ for a module called machine. It is not there - it is
# macos/15/machine.py, this file's neighbour - so the import fails before the first line of
# the bake runs:
#
#     ImportError: cannot import name 'machine' from 'lib' (unknown location)
#
# which is run 41771020818's drive step, twelve seconds after it started.
#
# `from lib import desktop` has the mirror-image problem: sys.path has macos/15 first, so
# that name resolves to THIS file and lib/desktop.py is unreachable by name. It is loaded
# by path below, under a name that cannot collide.
from lib import compress, live as livelib, smoke, vm  # noqa: E402

import machine  # noqa: E402  - macos/15/machine.py, beside this file


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
    p.add_argument("--copy", action="store_true",
                   help="copy the input disk instead of moving it; the default is to "
                        "move, because the disk normally came from ghcr and the "
                        "registry protects it")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--user", default=os.environ.get("MACOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MACOS_PASSWORD", "admin"))
    p.add_argument("--assistant-timeout", type=int, default=45 * 60)
    p.add_argument("--desktop-timeout", type=int, default=30 * 60)
    p.add_argument("--inspect-after", type=int, default=None,
                   help="stop this many seconds after the wizard's last click, keep the "
                        "recording and publish nothing (exit 3). For looking at what the "
                        "wizard actually did instead of waiting out --desktop-timeout")
    args = p.parse_args(argv)

    out = args.out or args.disk.replace(".qcow2", "-desktop.qcow2")
    work = args.work or os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"disk={args.disk} out={out}")
    if not os.path.isfile(args.disk):
        raise SystemExit(f"no disk at {args.disk}")

    # MOVED, NOT COPIED. The setup disk came from ghcr, so the registry still holds it: a local
    # copy protects nothing and costs a real 28 GiB write on ext4 - five minutes on every run -
    # to duplicate a file that is already somewhere else. --copy keeps the old behaviour for the
    # case where this disk really is the only copy.
    import working_copy as wc

    disk = wc.working_copy(args.disk, os.path.join(work, "desktop.qcow2"),
                           copy=args.copy, note=log)

    # THE RECOVERY IMAGE IS AN INPUT EVEN THOUGH THIS STAGE DOES NOT BOOT FROM IT.
    #
    # opencore.build() takes Apple's APFS driver out of the recovery image and installs it
    # into boot.img, which travels with the machine. Without that driver the guest cannot
    # mount what the installer wrote - so the image is required even though no cdrom is
    # attached here.
    #
    # A BAKE STAGE DOES NOT INHERIT ANOTHER STAGE'S INPUTS. bake.py fetches this at its own
    # start; desktop.py assumed it would be present and died at the first build step:
    #
    #     opencore.OpenCoreError: no recovery image at .../out/recovery.dmg, and the APFS
    #     driver can only come from one; the guest would install a macOS it cannot boot
    #
    # which is run 41773825483. The tag and the file name are bake.py's, because the two
    # stages must agree on which image was used: a driver out of a different release is a
    # different boot path.
    log("=== recovery image ===")
    import iso as isolib
    import opencore

    recovery = opencore.recovery_image(work)
    if not (os.path.isfile(recovery) and os.path.getsize(recovery) > 0):
        name = "sequoia"
        tag = f"macos-recovery-{name}"
        log(f"  not present; pulling {tag}")
        try:
            isolib.fetch_tag(tag, recovery)
        except isolib.IsoError as exc:
            raise SystemExit(
                f"=== NO RECOVERY IMAGE AND IT COULD NOT BE PULLED ===\n"
                f"  {exc}\n"
                f"  The APFS driver comes out of it; without one the disk this stage"
                f" produces cannot boot. Run macos-recovery-publish.yml for 15 first."
            ) from exc
    log(f"  {recovery} ({os.path.getsize(recovery) >> 20} MiB)")

    # THE BOOT ASSETS. opencore.build() needs the recovery image only to take Apple's
    # APFS driver out of it - the driver is installed into boot.img, which travels with
    # the machine, not with the disk.
    log("=== OpenCore, firmware, APFS driver ===")
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
         os.environ.get("SNAPSHOT_INTERVAL", "1")],
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
        if args.inspect_after is not None:
            # LOOK AT THE RECORDING INSTEAD OF WAITING OUT THE TIMEOUT.
            #
            # The wizard has finished - drive.py returns after its last click - and whether a
            # desktop appears is exactly the question the recording answers. Waiting 1800s to
            # learn that the last pane did not dismiss is 1800s spent on an answer that is
            # already on screen.
            #
            # The guest is stopped after this many seconds, the recording and last frame are
            # uploaded, and the disk is NOT published: exit code 3, which no stage treats as
            # success. That is the difference between "inspect this" and "this is a disk".
            log(f"inspection stop: {args.inspect_after}s after the sequence, then stop")
            time.sleep(args.inspect_after)
            import lastframe
            shot = os.path.join(os.path.dirname(os.path.abspath(frames)), "inspect.ppm")
            try:
                client = __import__("qmp").QMP(guest.paths.drive)
                try:
                    client.execute("screendump", filename=shot)
                finally:
                    client.close()
                lastframe.convert(shot, os.path.join(
                    os.path.dirname(os.path.abspath(frames)), "last-frame.png"))
            except (OSError, RuntimeError) as exc:
                log(f"  the screenshot could not be taken: {exc}")
            log("=== STOPPED FOR INSPECTION; the disk is not published ===")
            log("  the recording is the evidence: look at the last frame it kept")
            return 3
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
    # THE RECORDING'S OWN FRAMES, NOT drive.py's SCREENSHOTS.
    #
    # lib/snapshots.py writes frame-NNNN.jpg into `frames` - that is the directory that gets
    # one per second for the whole run, and the one the video is built from. `frames/shots`
    # is where drive.py puts the handful of pictures it takes around its own clicks, and it
    # holds a few dozen files from the wizard, none of them the desktop.
    #
    # `reached_desktop` looks at the newest five. Pointed at frames/shots it was looking at
    # wizard panes forever, which is exactly what run 41893311699 did for its whole
    # 1800-second budget while the desktop was on screen: the frame uploaded as that run's
    # last-frame.png passes the identical test in one call.
    shots = frames
    deadline = time.time() + timeout
    started = time.time()
    while time.time() < deadline:
        if not guest.alive():
            log("qemu exited before a desktop appeared")
            return False
        if _wallpaper_test().reached_desktop(shots, kind="macos"):
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
