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
it - Setup Assistant is a GUI and the only way through it is to click it. drive_read.py
holds the coordinates and reads each pane before acting on it; every number in it came
from a frame this pipeline captured, and none was guessed.

drive.py IS STILL HERE AND IS NO LONGER CALLED. It is the fixed sequence, and it is where
those numbers were first measured - its comments are the record of which coordinate was
wrong and which run showed it. It is kept as that record, not as a code path: two drivers
for one outcome meant a stalled stage could not say which of them was responsible.

WHY THE WAIT IS SPLIT FROM THE CLICKS

drive_read.py's sequence has a bounded length and clicks on a schedule between checks. That
is right for the panes, and it says nothing about whether the machine afterwards is at a
desktop or at the login window with a dialog over it. lib/desktop.py answers that, and it is
the same wallpaper test the Windows bake uses - a light top and a busy dark bottom.

THE BOOT MUST BE THE INSTALLED SYSTEM, NOT THE RECOVERY IMAGE. So no -cdrom and no
recovery disk: the machine is the target disk and OpenCore, exactly as it will be when
somebody uses it.
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

# `import machine` USED TO BE HERE AND IS NOT, because the guest's argv is
# lib/vm_session.py's now. It is still the module that knows TARGET_DRIVE from TARGET_DRIVE_PLAIN
# and where the OpenCore boot image goes; this file simply no longer spells that out itself.


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
    p.add_argument("--overlay", action="store_true",
                   help="treat --disk as a READ-ONLY base and write into a throwaway qcow2 "
                        "overlay; the base is never modified. See lib/overlay.py")
    p.add_argument("--copy", action="store_true",
                   help="copy the input disk instead of moving it; the default is to "
                        "move, because the disk normally came from ghcr and the "
                        "registry protects it")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--user", default=os.environ.get("MACOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MACOS_PASSWORD", "admin"))
    p.add_argument("--release", default=os.environ.get("MACOS_RELEASE", "15"),
                   help="which release the setup disk is; drive.py has a coordinate table "
                        "per release and a guessed one types into the wrong field")
    p.add_argument("--steps", default="",
                   help="the Setup Assistant walkthrough; macos/15/steps/sequoia.yaml. Required: "
                        "the pane file is the driver, and drive_read.py is kept only as the "
                        "table of measurements it was measured from.")
    p.add_argument("--ssh-port", type=int, default=2222,
                   help="the forwarded port this guest's 22 lands on; the machine has no sshd "
                        "yet at this stage, but the walkthrough's shutdown method and the"
                        "session both carry it")
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

    run_overlay = None
    if getattr(args, "overlay", False):
        import overlay as overlaymod

        run_overlay = overlaymod.Overlay(args.disk, work, name="desktop-overlay.qcow2")
        disk = run_overlay.open()
    else:
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
    import macos_recovery
    import opencore

    # THE FETCH IS ONE FUNCTION NOW. This stage used to carry its own copy, with
    # "sequoia" spelled out and the file name coming from opencore.recovery_image() -
    # while bake.py wrote recovery-sequoia.dmg. Both names are accepted, but a third
    # stage arriving with a third spelling is how a work directory ends up holding two
    # images of different releases. macos_recovery.fetch_published knows the tag, the
    # name and the error message, for every stage and every release.
    #
    # THE STAGE'S OWN RELEASE, NEVER A CONSTANT. This said "15", and the sign of it is in
    # the log line of run 42353977075, a macOS 26 desktop:
    #
    #     [recovery]   pulling macos-recovery-sequoia
    #
    # The image is not decoration here. opencore.build() takes Apple's APFS driver out of
    # it and installs that driver into boot.img, so a 26 disk was being booted through a
    # driver taken from 15's BaseSystem. The failure it produces is the one that run
    # recorded: the frame recorder kept writing (`newest frame 0s old`), the guest was
    # clearly alive, and the Setup Assistant never appeared - `login window seen: False`
    # for the whole 1800 seconds. Nothing in that log says "wrong APFS driver", which is
    # why it was read as a coordinate problem first.
    recovery = macos_recovery.fetch_published(args.release, work)
    log(f"  {recovery} ({os.path.getsize(recovery) >> 20} MiB)")

    # THE BOOT ASSETS. opencore.build() needs the recovery image only to take Apple's
    # APFS driver out of it - the driver is installed into boot.img, which travels with
    # the machine, not with the disk.
    log("=== OpenCore, firmware, APFS driver ===")
    # A HANDLER BEFORE THERE IS A GUEST TO STOP. This call downloads 848 MiB through
    # lib/opencore's fetch, and it is the longest stretch of a desktop stage that produces no
    # output of its own - lib/iso.py's progress lines go to stdout, but a transfer that
    # stalls produces none. Run 42280638394 was cancelled three times while sitting here and
    # never stopped, because nothing in this process was listening: the handler below is
    # installed after guest.start(), which is after this call.
    #
    # There is nothing to clean up at this point, so the handler only has to exit. os._exit
    # rather than sys.exit because a signal handler that returns into a blocked socket read
    # gets to hang again.
    def _stop_early(signum: int, _frame) -> None:
        log(f"signal {signum}: exiting during the boot-asset build (no guest yet)")
        os._exit(143)

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(_sig, _stop_early)

    assets = opencore.build(args.release, work)
    log(f"identity {assets['identity']['serial']} / {assets['identity']['mac']}")

    # THE MACHINE IS STARTED BY THE FILE'S `boot:` STEP, NOT HERE.
    #
    # What this stage keeps is what a step cannot be: the writable disk (or its overlay), the
    # recovery image OpenCore needs, the OpenCore assets, and the recording. The wizard, the
    # login, the desktop and the stop are in macos/15/steps/sequoia.yaml.
    #
    # THE SIGNAL HANDLER GOES WITH THE BOOT, and that is not a loss: qemu belongs to the
    # walkthrough now, and run_file stops it on the way out of any exit - including a step that
    # raised. What the handler below used to do was reach a guest this process owned; there is no
    # such guest any more.
    boot_options = {"assets": assets, "cpus": args.cpus, "mem_mb": args.mem}

    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)

    # THE RECORDING SOCKET IS COMPUTED, NOT ASKED FOR: this stage no longer starts qemu, so
    # there is no guest to read `paths.record` from yet, and the sampler has to be running BEFORE
    # the machine is. lib/snapshots.py waits for the socket to appear rather than requiring it.
    snap = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         vm.Paths(work).record, frames,
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
        # THE WALKTHROUGH OWNS THE MACHINE AND THE WIZARD.
        #
        # macos/15/steps/sequoia.yaml is a pane file with a preamble and an epilogue: `boot:`
        # before the first pane, the sixteen panes, then the login, the desktop wait and the
        # stop. See lib/steps.py's Walk.__init__.
        #
        # ONE DRIVER, AND IT IS THE READING ONE. drive.py used to run here and drive_read.py
        # after it - two paths to the same outcome, and run 42280638394's `drive the Setup
        # Assistant` step sat for half an hour with no way to tell which of the two was
        # responsible or which pane it was on. drive_read.py is kept as the table of
        # measurements, but only `--steps` runs.
        if not args.steps:
            raise SystemExit(
                "--steps is required now: the Setup Assistant is a pane file "
                "(macos/15/steps/sequoia.yaml). drive_read.py is kept as the measurement table "
                "and is not a driver this stage runs.")

        log(f"=== the Setup Assistant: steps {os.path.basename(args.steps)} ===")
        import steps as stepslib

        shutdown_method = "kill" if run_overlay is not None else "ssh"
        session = stepslib.MachineSession(
            args.release, disk, work,
            # NO SHARE: the disk at this stage has been installed but not configured, so nothing
            # in the guest mounts anything. `""` is vm_session's documented "do not serve one".
            share="",
            overlay=run_overlay, ssh_port=args.ssh_port,
            cpus=args.cpus, mem_mb=args.mem,
            boot_options=dict(boot_options, copy=run_overlay is None))
        ok, ran = stepslib.run_file(
            args.steps, "", work=work,
            user=args.user, password=args.password, release=args.release,
            # THE STAGE'S OWN FRAME DIRECTORY, for the same reason bake.py passes it: a
            # python: step that asks this stage's snapshotter a question must ask about the
            # frames this stage writes, not a directory it guessed.
            frames=frames, session=session,
            extra_vars={"args.shutdown": shutdown_method},
        )
        log(f"{len(ran)} step(s) answered" if ok
            else f"the walkthrough failed after {len(ran)} step(s)")

        # A WALKTHROUGH THAT DID NOT FINISH IS NOT A STAGE THAT CONTINUES.
        #
        # This used to log `ok` and carry on to wait_for_desktop(), which is a WALLPAPER test:
        # it says "something that looks like a desktop is on screen", and a wizard left one pane
        # from the end is exactly a machine that will never show one - or worse, one that happens
        # to be drawing one anyway. Either way the stage could succeed and publish a disk that
        # never finished setup.
        #
        # THE DESKTOP VERDICT IS THE FILE'S NOW (`wait for the desktop`), because the login that
        # has to happen first is two steps above it. What this stage does with a failure is
        # refuse to publish and say what to look at.
        if not ok:
            log("=== THE WIZARD DID NOT REACH A DESKTOP ===")
            log("  the disk is not published: a half-set-up macOS cannot be used")
            log("  the recording is the evidence - the last frame is the pane it stopped on")
            return 2
        arrived = True

        if args.inspect_after is not None:
            # LOOK AT THE RECORDING INSTEAD OF WAITING OUT THE TIMEOUT. The walkthrough got to
            # the desktop; this keeps the machine up for a while so the recording shows what
            # it looks like, then publishes nothing: exit code 3, which no stage treats as
            # success. That is the difference between "inspect this" and "this is a disk".
            log(f"inspection stop: {args.inspect_after}s, then stop")
            time.sleep(args.inspect_after)
            log("=== STOPPED FOR INSPECTION; the disk is not published ===")
            log("  the recording is the evidence: look at the last frame it kept")
            return 3
    finally:
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
    if run_overlay is not None:
        run_overlay.flatten(out)
        # AND THEN RELEASED, WHICH IS ALSO THE CHECK: close() deletes the diff and compares the
        # base's size and mtime against what it recorded before qemu started.
        run_overlay.close()
    else:
        compress.compress(disk, out)
    log(f"BAKED macOS {args.release} desktop -> {out} ({os.path.getsize(out) >> 20} MiB)")
    log("  this disk reaches its desktop without any wizard; the ssh stage can use it")
    return 0


# `wait_for_desktop`, `_type_login_password` AND `_frame_age` WERE HERE, AND THEY ARE STEPS NOW.
#
# The loop they made up - look at the recording, recognise the login window, type into it, wait
# for the wallpaper test, keep a heartbeat - is the epilogue of macos/15/steps/sequoia.yaml:
#
#     click the password field / type the password / press return
#     dismiss the keyboard assistant
#     wait for the desktop                       (python: ssh_panes:wait_for_desktop)
#     stop the machine
#
# The reason it is worth moving rather than copying: `_type_login_password` and the ssh stage's
# `unlock()` were two opinions about how to log in to the same window, and the ssh stage's is the
# one with the click-first note (runs 42079736995 against 42083224228). One copy, in ssh_panes.py,
# beside the file that names it.
#
# `_frame_age` went with the heartbeat, which is now inside wait_for_desktop's own loop.


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
