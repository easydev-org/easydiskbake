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
    recovery = macos_recovery.fetch_published("15", work)
    log(f"  {recovery} ({os.path.getsize(recovery) >> 20} MiB)")

    # THE BOOT ASSETS. opencore.build() needs the recovery image only to take Apple's
    # APFS driver out of it - the driver is installed into boot.img, which travels with
    # the machine, not with the disk.
    log("=== OpenCore, firmware, APFS driver ===")
    # THE RELEASE, NOT A CONSTANT. This said "15", so a macOS 26 desktop run built its boot
    # assets from SEQUOIA's recovery image - the stage's own log line, run 42085569136:
    #
    #     [recovery]   pulling macos-recovery-sequoia
    #
    # opencore.build() takes Apple's APFS driver out of that image and installs it into
    # boot.img. A 26 disk booted through a driver taken from 15 is a boot that can fail for a
    # reason no other line in the run would name.
    #
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

    # NO RECOVERY DISK. machine.drives() wants one, so this does not call it: a desktop
    # stage that booted the installer again would be asking a different question.
    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            "-drive", machine.TARGET_DRIVE.format(disk=disk),
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
            "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
        ],
        shape=machine.shape(assets, cpus=args.cpus, mem_mb=args.mem),
        stdout=os.path.join(work, "console.log"),
    ).start()

    # WHOEVER STARTED QEMU HAS TO STOP IT. This process did, so this process does.
    #
    # qemu is started with start_new_session=True (lib/vm.py), which puts it in a session of
    # its own. log-stream's cancel handler walks the process table for ppid descendants and
    # signals each group it finds - but that walk needs THIS process to still exist to connect
    # it to qemu. If this process is killed outright, qemu is reparented to PID 1 and the
    # walk can no longer reach it: the guest keeps booting, the runner keeps the KVM device
    # checked out, and `gh run cancel` looks like it did nothing.
    #
    # That is run 42280638394. It was cancelled three times - the request was accepted each
    # time - and it stayed in_progress with no log-stream line to say why, because the signal
    # never reached the streamer: the stage was waiting on opencore.build()'s 848 MiB recovery
    # download, before log-stream had a check run to report into. The handle below is what
    # makes a cancel at ANY point in this stage stop the guest.
    #
    # SIGTERM FIRST, NOT SIGKILL. A guest stopped mid-write produces a disk that repairs
    # itself on the next boot, which is a worse artifact than a slower cancel - the same rule
    # lib/vm.py's stop() and log_stream's _kill_tree_of() both state.
    def _stop(signum: int, _frame) -> None:
        log(f"signal {signum}: stopping the guest and exiting")
        try:
            guest.stop(acpi_first=False)
        except Exception as exc:                       # noqa: BLE001 - shutting down
            log(f"  the guest did not stop cleanly: {exc}")
        finally:
            # 143 IS SHELL CONVENTION FOR "TERMINATED BY SIGTERM", which is what a cancelled
            # step is. Exiting through the normal path would report a cancel as a build
            # failure and run the publish steps.
            os._exit(143)

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(_sig, _stop)

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
        # ONE DRIVER. THE READING ONE.
        #
        # drive.py used to run here and drive_read.py after it. That is two paths to the same
        # outcome, and it made the stage impossible to reason about: run 42280638394's
        # `drive the Setup Assistant` step sat for half an hour without one log-stream line,
        # and there was no way to tell which of the two drivers was responsible or which pane
        # it was on.
        #
        # drive.py is still in the tree - it is the sequence that was measured the longest and
        # its coordinate comments are where every number here came from - but it is not
        # called. A pane that this driver cannot answer fails loudly at that pane, which is
        # the whole reason for reading the screen instead of replaying a table.
        log("=== the Setup Assistant: drive_read.py ===")
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(HERE, "drive_read.py"),
                 "--qmp", guest.paths.drive, "--frames", frames,
                 "--user", args.user, "--password", args.password,
                 "--release", args.release,
                 "--boot-timeout", str(args.desktop_timeout)],
                check=False, timeout=args.assistant_timeout,
            )
            if proc.returncode != 0:
                log(f"the reading driver exited {proc.returncode}; see its log for the pane "
                    f"it could not answer")
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"the reading driver failed: {exc}")

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
        arrived = wait_for_desktop(guest, frames, args.desktop_timeout,
                                   user=args.user, password=args.password)
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
    if run_overlay is not None:
        run_overlay.flatten(out)
    else:
        compress.compress(disk, out)
    log(f"BAKED macOS {args.release} desktop -> {out} ({os.path.getsize(out) >> 20} MiB)")
    log("  this disk reaches its desktop without any wizard; the ssh stage can use it")
    return 0


def _frame_age(directory: str) -> str:
    """How old the newest frame is, as a phrase for the heartbeat.

    WHY IT IS PART OF THE HEARTBEAT AND NOT A SEPARATE LINE. A desktop that has not arrived
    has two very different causes, and the frame's age separates them:

        frames arriving, test not matching     the wallpaper test is wrong
        frames stopped days... minutes ago     the guest stopped drawing, which is a pane
                                               waiting for an answer

    The second is the one that used to cost a whole run. A wizard stopped on a modal sheet
    does not change the screen, so every later frame is identical and the recorder keeps
    writing them; the newest frame's mtime is what tells the two apart.
    """
    try:
        newest = max(
            (os.path.join(directory, f) for f in os.listdir(directory)),
            key=os.path.getmtime,
            default=None,
        )
        if newest is None:
            return "none"
        age = time.time() - os.path.getmtime(newest)
        return f"{age:.0f}s old"
    except OSError:
        return "unreadable"


def _lock_screen_test():
    """The same module as _wallpaper_test - one loader, two decisions from it."""
    return _wallpaper_test()


def _type_login_password(guest: vm.Guest, user: str, password: str) -> None:
    """Type into a login window and submit.

    NO CLICK FIRST. A macOS login window puts the caret in the password field when it
    appears, and clicking it is not only unnecessary but risky: the field is inside a
    translucent pill whose exact position varies with the account tile's width, and a click
    a few pixels high lands on the wallpaper and dismisses the field.

    ENTER, NOT A BUTTON. The field submits on Return. There is no visible button until a
    wrong password is typed.
    """
    import qmp as qmpmod
    try:
        q = qmpmod.QMP(guest.paths.drive)
    except Exception as exc:                           # noqa: BLE001 - best effort
        log(f"  could not open QMP to type the password: {exc}")
        return
    try:
        time.sleep(2.0)
        for ch in password:
            qmpmod.key(q, "spc" if ch == " " else ch)
            time.sleep(0.25)
        time.sleep(1.0)
        # TWO RETURNS. The first wakes the window if the display had dimmed; the second
        # submits. A password already typed is not consumed by a wake.
        qmpmod.key(q, "ret")
        time.sleep(2.0)
        qmpmod.key(q, "ret")
    finally:
        try:
            q.close()
        except Exception:                              # noqa: BLE001
            pass


def wait_for_desktop(guest: vm.Guest, frames: str, timeout: int, *,
                     user: str = "docker", password: str = "admin") -> bool:
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
    # A HEARTBEAT, BECAUSE THIS LOOP CAN RUN FOR HALF AN HOUR IN SILENCE.
    #
    # The loop below checks the wallpaper every five seconds and says nothing until it
    # succeeds or the budget runs out. At the default 1800s that is thirty minutes in which
    # the Actions log and the check run show one line - "waiting for a desktop" - and the
    # stage is indistinguishable from a hang. The UI cannot tell them apart either, which is
    # why a cancelled run and a slow one looked the same.
    #
    # Every minute it now says how long it has been, whether the guest is alive, and how old
    # the newest frame is. The frame age is the part that separates "the guest is drawing and
    # the test has not matched yet" from "the screen has not changed since the pane that
    # stopped us" - the second is a wizard that never finished, and reading it here is much
    # cheaper than reading it out of a video afterwards.
    beat = int(os.environ.get("DESKTOP_HEARTBEAT_SECONDS", "60"))
    last_beat = 0.0
    unlocked = False
    while time.time() < deadline:
        if not guest.alive():
            log("qemu exited before a desktop appeared")
            return False

        # A LOGIN WINDOW IS A FINISHED SYSTEM, NOT A HANG.
        #
        # macOS 26's Setup Assistant ends by putting the machine at its login window, and the
        # screen then never changes on its own. Waiting for a desktop in that state waits out
        # the whole budget and reports "the wizard did not reach a desktop" about a disk that
        # is complete - run 42280638394 sat like that for 1800 seconds after answering
        # nineteen panes correctly.
        #
        # The password is typed once, when the window is first recognised. The field is
        # already focused on a login window, so no click is needed; Enter submits it.
        if not unlocked and _lock_screen_test().reached_lock_screen(shots):
            log(f"a macOS login window is on screen ({(time.time() - started):.0f}s); "
                f"typing the password")
            _type_login_password(guest, user, password)
            unlocked = True

        if _wallpaper_test().reached_desktop(shots, kind="macos"):
            log(f"a macOS desktop is on screen ({(time.time() - started):.0f}s)")
            settle = int(os.environ.get("DESKTOP_SETTLE_SECONDS", "30"))
            log(f"letting the guest settle for {settle}s before stopping it")
            time.sleep(settle)
            if not guest.alive():
                log("the guest exited while settling - not keeping the disk")
                return False
            return True
        elapsed = time.time() - started
        if elapsed - last_beat >= beat:
            last_beat = elapsed
            log(f"  still waiting: {elapsed:.0f}s of {timeout}s, "
                f"newest frame {_frame_age(shots)}, login window seen: {unlocked}")
        time.sleep(5)
    log(f"timed out after {timeout}s")
    return False


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
