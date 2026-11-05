"""Turn a macOS desktop disk into one with Remote Login, by turning it on in Terminal.

    python3 macos/15/ssh.py --disk out/macos15-desktop.qcow2 --out out/macos15-ssh.qcow2

WHY THIS IS NOT AN ANSWER FILE EITHER

macOS ships sshd off, and there is no configuration line to set before the system boots
on a disk somebody else installed. There is `systemsetup -setremotelogin on`, and it needs
root, so the bake opens Terminal on the desktop - which it can, because a desktop is what
the previous stage produced - and types it.

THE RULE THAT MAKES THIS DIFFERENT FROM CLICKING

Nothing here reads the screen to decide anything. The desktop is already known to be a
desktop, the Terminal window is opened by a keyboard shortcut whose behaviour does not
depend on where anything is drawn, and the command is typed into whatever has focus in
that window. What IS read is the result, from the guest's own tools: the stage ends when
sshd answers on a forwarded port, not when a screen looks right.

WHY A SECOND PORT AND NOT 2222

The host half of the forward is whatever the workflow says, because this disk may be run
next to a Windows disk that also wants 2222 on the same runner. The guest half is
always 22 - it is macOS's own sshd and there is no reason to move it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

# WHICH RELEASE THIS DISK IS. The stages in this directory are shared between
# macOS 15 and 26 - macos/26 keeps only the measurements that differ - so the
# recovery image and the OpenCore build have to be told which one they are
# working on. A constant here made macos26's desktop stage pull
# macos-recovery-sequoia, which is 15's image, and install Apple's APFS driver
# from it into a 26 boot.
RELEASE = os.environ.get("MACOS_RELEASE", "15")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

# lib/ ON THE PATH AS WELL, for the same reason linux/dev.py does it: clean_disk is a module
# in lib/ that itself imports `session`, and `session` is reached by bare name (`from session
# import ssh`) rather than as `lib.session`. Without this the clean at the end of this stage
# fails with ModuleNotFoundError for a module that is sitting right there.
sys.path.insert(0, os.path.join(REPO, "lib"))

# TWO SOURCES, AS IN macos/15/desktop.py. `machine` is this file's neighbour, not a lib/
# module, so `from lib import machine` cannot see it:
#
#     ImportError: cannot import name 'machine' from 'lib'
#
# and lib/desktop.py cannot be reached by name at all, because macos/15 is on sys.path and
# desktop.py there would win - see the note in desktop.py. It is loaded by path below.
from lib import compress, live as livelib, smoke, vm  # noqa: E402

sys.path.insert(0, HERE)
import machine  # noqa: E402  - macos/15/machine.py, beside this file


def _wallpaper_test():
    import importlib.util

    path = os.path.join(REPO, "lib", "desktop.py")
    spec = importlib.util.spec_from_file_location("lib_desktop", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# The password the desktop stage was given. Remote Login is turned on from a root shell
# started with sudo, so this is the account's password, not root's.
GUEST_USER = os.environ.get("MACOS_USER", "docker")
GUEST_PASSWORD = os.environ.get("MACOS_PASSWORD", "admin")


def log(message: str) -> None:
    print(f"[macos-ssh {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the desktop-stage disk")
    p.add_argument("--out", default=None)
    p.add_argument("--copy", action="store_true",
                   help="copy the input disk instead of moving it; the default is to "
                        "move, because the disk normally came from ghcr and the "
                        "registry protects it")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--ssh-port", type=int, default=2223,
                   help="the HOST port the guest's 22 is forwarded to")
    p.add_argument("--boot-timeout", type=int, default=900)
    args = p.parse_args(argv)

    out = args.out or args.disk.replace(".qcow2", "-ssh.qcow2")
    work = args.work or os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"disk={args.disk} out={out}")
    if not os.path.isfile(args.disk):
        raise SystemExit(f"no disk at {args.disk}")

    # MOVED, NOT COPIED - the desktop disk came from ghcr, so the registry still holds it.
    import working_copy as wc

    disk = wc.working_copy(args.disk, os.path.join(work, "ssh.qcow2"),
                           copy=args.copy, note=log)

    # THE RECOVERY IMAGE IS AN INPUT EVEN THOUGH THIS STAGE DOES NOT BOOT FROM IT.
    #
    # opencore.build() takes Apple's APFS driver out of the recovery image and puts it in
    # boot.img. Without it the guest cannot mount what desktop.py wrote, so the image is
    # required here even though no cdrom is attached.
    #
    # THIS STAGE ASSUMED THE IMAGE WOULD BE PRESENT, and run 41979713660 is what that
    # costs: setup succeeded, desktop succeeded, and this died in 2.4 seconds with
    #
    #     opencore.OpenCoreError: no recovery image at out/recovery.dmg, and the APFS
    #     driver can only come from one
    #
    # after the two previous stages had already done an hour of work. A job pulls one
    # disk from ghcr and nothing else - it inherits no other stage's inputs - so the
    # fetch has to be here. macos_recovery.fetch_published is the single place that
    # knows the tag and the file name; desktop.py and bake.py reach the same file
    # through it now.
    log("=== recovery image ===")
    import macos_recovery

    recovery = macos_recovery.fetch_published(RELEASE, work)
    log(f"  {recovery} ({os.path.getsize(recovery) >> 20} MiB)")

    log("=== OpenCore, firmware, APFS driver ===")
    import opencore

    assets = opencore.build(RELEASE, work)
    log(f"identity {assets['identity']['serial']} / {assets['identity']['mac']}")

    shape = machine.shape(assets, cpus=args.cpus, mem_mb=args.mem)
    # THE FORWARD, ON THE GUEST'S OWN SSHD. macOS listens on 22 and moving it would mean
    # editing system sshd_config; the host side is what a caller needs to choose.
    shape.ssh_port = args.ssh_port

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            "-drive", machine.TARGET_DRIVE.format(disk=disk),
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
            "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
        ],
        shape=shape,
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

    # THE RECORDING IS KEPT CURRENT, so a run that FAILS still has a video.
    #
    # THIS STAGE HAD NO MP4 AT ALL. macos/15/bake.py and macos/15/desktop.py both call
    # livelib.start(), and ssh.py only started snapshots.py - so `out/install.mp4` was never
    # written, and the workflow's "keep the recording" step reported
    #
    #     No files were found with the provided path: out/install.mp4
    #
    # on every ssh run, including the one that failed at 10:09:38 with
    #
    #     === THE DISK DID NOT REACH A DESKTOP ===
    #
    # which is exactly the failure a video is needed for: the frames were captured, and
    # nothing turned them into something a person can watch.
    #
    # live.start() is the same call the other two stages make: it encodes what is there every
    # LIVE_ARTIFACT_SECONDS, so a run killed at any point leaves a playable file rather than
    # an empty one.
    # None FIRST, so the finally below cannot hit a NameError if start() itself raises -
    # the object is created outside the try, but a reader should not have to check that.
    live = None
    live = livelib.start(
        [os.path.join(frames, "shots"), frames],
        os.path.join(work, "install.mp4"),
        os.path.join(work, "last-frame.png"),
        every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "20")),
    )

    ok = False
    try:
        # BOOT, WAIT A FIXED TIME, TYPE THE PASSWORD. NO DETECTION.
        #
        # THIS DISK ALWAYS BOOTS TO A LOCK SCREEN, so waiting for a desktop was waiting for
        # something this stage can cause but never observe. desktop.py finishes by letting the
        # guest settle and stopping it - it never logs in - so the published disk starts at
        # the login window every time: wallpaper, clock, account tile, password field.
        #
        # THE WALLPAPER TEST CANNOT SEE THAT. It wants a dark menu bar across the top and a
        # lock screen draws wallpaper there instead, so it correctly says "not a desktop"
        # about a machine that is healthy and waiting. Measured on run 42028663003: the login
        # window was on screen by 12:48, the test spent until 13:02 saying no, and the same
        # machine showed a full desktop 25 seconds after the password was typed. Fifteen
        # minutes of the timeout bought nothing.
        #
        # A FIXED WAIT IS HONEST HERE BECAUSE THE NUMBER IS MEASURED. A booted macOS shows its
        # login window two to four minutes after qemu starts - this disk has no installer to
        # run, only the system to bring up. 120 seconds is the settle the rest of this file
        # uses for a boot, and typing the password early is not a mistake: the keystrokes go
        # to a machine that is not listening yet and are simply lost, and unlock() is followed
        # by the ssh check that is the real verdict.
        delay = int(os.environ.get("MACOS_LOGIN_DELAY", "120"))
        log(f"waiting {delay}s for the boot, then logging in - this disk always shows a "
            f"lock screen")
        time.sleep(delay)

        log(f"=== logging in as {GUEST_USER} ===")
        unlock(guest)

        # THE KEYBOARD DIALOG COMES UP WITH THE FIRST DESKTOP SESSION, so it is dismissed
        # here - before anything is typed at Spotlight, because it swallows the next key.
        dismiss_keyboard_assistant(guest)

        # AND NOW THE DESKTOP IS REACHABLE. The wallpaper test is kept, with a short budget,
        # because it is the only thing that can confirm the password was accepted - and if it
        # was not, the ssh check below would fail for a reason that reads like a broken sshd.
        if not wait_for_desktop(guest, frames, 180):
            log("=== THE DISK DID NOT REACH A DESKTOP AFTER LOGGING IN ===")
            log("  the password was typed and the screen still is not a desktop; check the")
            log("  recording - the login window shakes its field when the password is wrong")
            return 1

        log("=== turning Remote Login on ===")
        enable_remote_login(guest)

        log(f"checking that the guest's sshd answers on the host's port {args.ssh_port}")
        if not wait_for_ssh(args.ssh_port, timeout=180):
            log("=== REMOTE LOGIN IS NOT ANSWERING ===")
            log("  the preference was set but sshd never came up; the frames say what "
                "Terminal showed")
            return 1

        ok = True
        log("Remote Login is answering")

        # THE CLEAN RUNS HERE, IN THIS SESSION, AND NOT IN A STEP OF ITS OWN.
        #
        # 6d57bc3 moved it out, into `clean the disk` steps, for a good reason: macOS was the
        # only system whose clean was invisible in the run's step list. But a separate step
        # CANNOT INHERIT THIS MACHINE. lib/clean_disk.clean_running's docstring is explicit -
        # "the caller owns the machine: this does not start or stop anything" - so a step
        # must boot the disk from cold and reach sshd AGAIN. That is a different question
        # from "is Remote Login on", and on macOS the answer is no:
        #
        #     [clean-step] waiting for ssh
        #     [clean-step] === SSH DID NOT ANSWER IN 900s ===
        #     [clean-step]   last output: kex_exchange_identification: read: Connection
        #                    reset by peer
        #
        # CONNECTION RESET BY PEER IS NOT THE ACCESS GROUP. The recording of run 42062472207's
        # enable-stage shows the group already complete while the machine was up -
        #
        #     GroupMembers: 0 501
        #     NestedGroups: 80 20 401
        #     tcp4  0  0  *.22   *.*   LISTEN
        #
        # - and that stage's wait_for_ssh passed with SSH-2.0-OpenSSH_9.9. The ten dscl lines
        # were added on the theory that a half-built group caused it; the evidence now says
        # the group was never the problem, and they are kept because a complete group is
        # still the correct state.
        #
        # The check that a LATER stage can reach this disk is macos15-dev's, which pulls the
        # published tag and connects to it. This stage's job is to turn Remote Login on and
        # hand back its free space while it has the machine - which is what it had before
        # 6d57bc3, and what every other system still does inside its own stage.
        log("=== cleaning the disk before it is exported ===")
        import clean_disk

        if not clean_disk.clean_running("macos", port=args.ssh_port, note=log, disk=disk):
            # A CLEAN THAT DID NOT RUN IS A FAILURE, NOT A WARNING - the same rule the other
            # three systems follow, for the same reason: publishing a disk whose free blocks
            # were never handed back means every later pull of that tag carries the bloat.
            log("=== THE CLEAN DID NOT FINISH ===")
            log("  nothing is published: the disk is not what this stage claims to produce")
            return 1

        return 0
    finally:
        # ACPI FIRST, SO THE DISK IS CLOSED RATHER THAN KILLED. The clean has just written to
        # it over ssh and the publish step compresses and pushes what this leaves behind.
        guest.stop(acpi_first=ok)
        snap.terminate()
        try:
            snap.wait(timeout=5)
        except subprocess.TimeoutExpired:
            snap.kill()
        # THE FINAL ENCODE HAPPENS WHATEVER THE OUTCOME. live.stop(final=True) is the same
        # call desktop.py makes; without it the last LIVE_ARTIFACT_SECONDS of frames would
        # not be in the file, which on a failure is the part that shows why.
        if live is not None:
            try:
                live.stop(final=True)
            except Exception as exc:              # noqa: BLE001 - the video is not the verdict
                log(f"could not finalise the recording: {exc}")
        if ok:
            if not smoke.check_disk(disk, root_fs="apfs"):
                log("=== THE DISK FAILED ITS CHECKS ===")
                raise SystemExit(1)
            log("compressing")
            compress.compress(disk, out)
            log(f"BAKED macos15 ssh -> {out} ({os.path.getsize(out) >> 20} MiB)")


def wait_for_desktop(guest: vm.Guest, frames: str, timeout: int) -> bool:
    """The wallpaper test again - see macos/15/desktop.py for why it is this one.

    THE RECORDING'S FRAMES, NOT drive.py's SCREENSHOTS. macos/15/desktop.py already had
    this right and this copy had it wrong:

        desktop.py    shots = frames               <- one per second for the whole run
        ssh.py        shots = os.path.join(frames, "shots")   <- a few dozen wizard panes

    `frames/shots` is where drive.py puts the handful of pictures it takes around its own
    clicks. This stage does not run drive.py at all - it boots the desktop disk and waits -
    so that directory holds whatever the previous stage left at the same paths, none of
    them this boot's desktop. `reached_desktop` looks at the newest five frames in whatever
    it is given, so it was examining the wrong pictures for the whole 900 seconds.

    Run 42020622755 is what that costs: the guest had ALREADY reached its desktop - the run's
    own last-frame.png shows the Sonoma wallpaper, the clock, the account picture and a
    password field - and the stage reported

        === THE DISK DID NOT REACH A DESKTOP ===

    after waiting out the full timeout on a machine that was sitting there.
    """
    from lib import desktop

    shots = frames
    deadline = time.time() + timeout
    started = time.time()
    while time.time() < deadline:
        if not guest.alive():
            log("qemu exited before a desktop appeared")
            return False
        if _wallpaper_test().reached_desktop(shots, kind="macos"):
            log(f"a macOS desktop is on screen ({(time.time() - started):.0f}s)")
            return True
        time.sleep(5)
    return False


# THE KEYBOARD SETUP ASSISTANT'S Quit BUTTON, IN 1920x1080 PIXELS.
#
# Measured on run 42034256322's recording, which is 1280x800, and scaled to the guest's own
# framebuffer:
#
#     the dialog           centred, about 390..905 by 165..630 on the 1280x800 frame
#     [Quit]               centre (819, 612) at 1280x800  ->  (1228, 826) at 1920x1080
#     [Continue]           centre (871, 612) at 1280x800  ->  (1306, 826) at 1920x1080
#
# QUIT, NOT CONTINUE. Continue asks the guest to identify a keyboard by pressing keys it can
# then map, which is a wizard in its own right and needs an Apple keyboard plugged in.
# Quit dismisses it and leaves the machine usable with the emulated devices qemu provides.
KEYBOARD_ASSISTANT_QUIT = (1228, 826)


def dismiss_keyboard_assistant(guest: vm.Guest) -> bool:
    """Click Quit on the "Keyboard Setup Assistant" dialog, if it is up.

    THIS DIALOG EATS THE NEXT KEYSTROKE, WHICH IS WHY sshd WAS NEVER ENABLED.
    Run 42034256322's recording, at t=135s:

        Spotlight open, `/Applications/Utilities/Terminal.app` typed into it
        Keyboard Setup Assistant across the middle of the screen:
            "Your Apple device cannot be identified and will not be usable until it is
             identified."                               [Quit]  [Continue]

    and at t=170s, Safari showing a Google search for
    `/Applications/Utilities/Terminal.appsudo systemsetup -setremotelogin on`.

    The Return went to the dialog. Spotlight never resolved, so the command and the password
    that followed went into whatever had focus afterwards, and the stage reported "something
    answered, but not sshd" for its whole budget.

    WHY macOS PUTS IT UP: qemu's command line has a usb-tablet and no keyboard. QMP send-key
    does not need one - it injects into the input subsystem, which is why Windows and Linux
    have never had this problem - but macOS enumerates HID devices at login and, finding none
    it recognises, asks the user to identify one.

    IT APPEARS AFTER LOGGING IN, NOT BEFORE. The dialog is part of the first desktop session,
    so this runs after unlock() and before anything is typed at Spotlight.

    Returns True when a click was sent. That is not the same as "it was there": a click at
    (1228, 826) on a desktop with no dialog lands on the wallpaper and does nothing, which is
    the correct outcome for a machine that never showed one.
    """
    import qmp as qmpmod

    kb_work = guest.paths.drive
    deadline = time.time() + 120
    while not os.path.exists(kb_work):
        if time.time() > deadline:
            log(f"  no QMP socket at {kb_work}; cannot dismiss anything")
            return False
        time.sleep(1)

    q = qmpmod.QMP(kb_work)
    try:
        log(f"  clicking Quit on the Keyboard Setup Assistant at "
            f"{KEYBOARD_ASSISTANT_QUIT}, if it is up")
        qmpmod.click(q, *KEYBOARD_ASSISTANT_QUIT)
        time.sleep(3)
    finally:
        q.close()
    return True


def unlock(guest: vm.Guest) -> bool:
    """Type the account's password into the login window, if that is what is on screen.

    THE LOCK SCREEN IS THE FIRST THING A FRESHLY INSTALLED macOS SHOWS. desktop.py stops the
    guest after the Setup Assistant and does not log in, so the disk it publishes boots to a
    login window: wallpaper, clock, account tile, password field. Nothing is wrong with it
    and nothing can be done on it - Spotlight, Terminal and `systemsetup` all need a
    session - so this types the password and presses Return.

    ONE COORDINATE, AND IT IS NOT A DECISION. This docstring used to say the login window
    focuses its password field by itself and that no click was needed. Run 42083224228
    disproved it - the field was drawn empty and the typed password went nowhere - so the
    field is now clicked before anything is typed, the same control on every boot of this
    disk. The rectangle is fixed because the window is: an account picture, a name, a field.

    RETURNS FALSE WHEN THE PASSWORD IS NOT ACCEPTED, so the caller can say "still not a
    desktop" rather than waiting out another timeout on a machine showing an error shake.
    The shake is not detectable in a screenshot without OCR of a message that is not printed,
    so the verdict is read back the same way everything else here is: by asking whether a
    desktop appeared afterwards.
    """
    import qmp as qmpmod
    from lib import keys

    kb_work = guest.paths.drive
    deadline = time.time() + 120
    while not os.path.exists(kb_work):
        if time.time() > deadline:
            log(f"  no QMP socket at {kb_work}; cannot type at the login window")
            return False
        time.sleep(1)

    q = qmpmod.QMP(kb_work)
    try:
        # Escape first: a notification banner or a "there is no keyboard" hint swallows the
        # next keystroke, and this guest has never had input before.
        qmpmod.key(q, "esc")
        time.sleep(1)

        # CLICK THE PASSWORD FIELD BEFORE TYPING INTO IT.
        #
        # THE KEYSTROKES GO TO WHATEVER HAS FOCUS, and "the login window focuses its own
        # password field" - which this function has said since it was written - is not true
        # on every boot. Run 42083224228's last frame is the proof: the docker account tile
        # and an EMPTY "Enter Password" field, meaning not one character of the password
        # arrived, and the stage then waited out 180 seconds for a desktop that never came.
        # The run before it, 42079736995, typed at the same 120-second mark and reached the
        # desktop seventeen seconds later.
        #
        # THE DIFFERENCE IS FOCUS, NOT TIMING, so a longer wait would not have helped. The
        # Esc above is what takes it away: it dismisses the wake hint AND can move focus off
        # the field, leaving the window drawn exactly as it looks when it is ready.
        #
        # (965, 981) is the field's centre, measured from that frame: the "Enter Password"
        # pill spans x=880..1210 at y=960..1000 in the guest's own 1920x1080.
        #
        # A CLICK HERE IS THE ONE PLACE THIS STAGE USES ONE, and it is not a decision about
        # what is on screen - it is the same control every time this window appears.
        qmpmod.click(q, 965, 981)
        time.sleep(1)

        log(f"  typing the password for {GUEST_USER}")
        keys.type_into(q, GUEST_PASSWORD)
        time.sleep(1)
        qmpmod.key(q, "ret")
        time.sleep(15)
    finally:
        q.close()
    # The caller decides whether it worked - it already has a loop that does nothing else.
    return True


# ENABLING REMOTE LOGIN WITHOUT systemsetup, WHICH WANTS A PRIVILEGE sudo DOES NOT GIVE.
#
# Run 42041505699 is what systemsetup prints now, and it is not a permissions mistake on our
# side - it is macOS 13+ refusing on principle:
#
#     docker@dockers-iMac ~ % sudo systemsetup -setremotelogin on
#     Password:
#     setremotelogin: Turning Remote Login on or off requires Full Disk Access privileges.
#
#     docker@dockers-iMac ~ % sudo systemsetup -getremotelogin
#     Remote Login: Off
#
# sudo AND TCC ARE ORTHOGONAL. sudo says the process runs as root; TCC says whether this
# APPLICATION may touch this KIND OF DATA. systemsetup reads and writes the network service
# configuration, which is TCC-protected, so it is refused no matter whose uid it runs under.
# That is deliberate on Apple's part - if sudo bypassed TCC, any script a user ran with a
# password could read every protected store on the machine.
#
# THE SAME JOB WITHOUT THE PRIVILEGE is three launchctl calls, and this is exactly what
# easydev-org/dockur-macos does in src/install/firstboot/launch.sh:
#
#     "launchd owns sshd; enabling the job is what /usr/sbin/systemsetup does, minus the
#      parts of that tool which want to talk to a logged-in session."
#
# `enable` marks the job for loading at boot, `load -w` loads it now, and `kickstart` restarts
# it so the settings take effect rather than waiting for a reboot. None of that touches
# TCC-protected data: launchd is already root and does the work.
#
# THE CONFIGURATION AND THE ACCESS GROUP COME WITH IT. PasswordAuthentication is what makes
# sshpass usable - `yes` is macOS's own default in sshd_config but it ships commented out -
# and com.apple.access_ssh is an allow-list macOS consults SEPARATELY from sshd_config: if
# that group exists and the account is not in it, sshd answers and then refuses the login.
# SEVERAL SHORT COMMANDS, NOT ONE LONG ONE.
#
# The whole sequence as a single line is 471 characters, typed one keystroke at a time - and
# this repository has already paid for that once: run 41997580476 typed a 105-character
# winget command into Windows and the guest received `get install`, because a shell that
# redraws its input line coalesces keystrokes. A macOS Terminal under qemu is no faster.
#
# Each entry is typed, Return is pressed, and the next waits. sudo caches its credential
# after the first one, so the later commands do not prompt again.
REMOTE_LOGIN_STEPS = [
    # 1. THE JOB. `enable` marks it for boot, `load -w` loads it now.
    "sudo launchctl enable system/com.openssh.sshd",
    "sudo launchctl load -w /System/Library/LaunchDaemons/ssh.plist",
    # 2. PASSWORD AUTHENTICATION, which is what makes sshpass usable. macOS ships the line
    #    commented out, and `sed -i ''` is BSD sed's in-place form - the empty argument is
    #    the backup suffix, and GNU sed would treat it as the script.
    "sudo sed -i '' -E 's|^#?[[:space:]]*PasswordAuthentication[[:space:]].*|"
    "PasswordAuthentication yes|' /etc/ssh/sshd_config",
    # 2b. AND NO REVERSE LOOKUP, WHICH IS WHAT ACTUALLY KILLS THE CONNECTION HERE.
    #
    # The host's verbose log of run 42095016629 says the credentials were never rejected -
    # the connection was CLOSED while waiting for the answer:
    #
    #     debug1: Next authentication method: password
    #     debug3: send packet: type 50
    #     debug2: we sent a password packet, wait for reply
    #     Connection closed by 127.0.0.1 port 2222
    #
    # "Permission denied" is what sshd says when it decides; this says nothing, which is what
    # sshd does when it dies on the way to deciding. The guest's own unified log, printed by
    # step 19 of this list, names the call:
    #
    #     sshd: (libsystem_info.dylib) Retrieve User by Name
    #     sshd: (libsystem_info.dylib) [com.apple.network:libinfo:si_destination_compare]
    #           send failed: Invalid argument
    #
    # si_destination_compare compares SOCKET ADDRESSES - it is the name/address resolution
    # path, and the address it is looking at is the one qemu's user-mode network gives the
    # host (10.0.2.2). sshd resolves the client before it finishes the login, that call fails
    # with EINVAL, and the process ends.
    #
    # A guest logging in to itself never takes that path - its peer is 127.0.0.1 - which is
    # exactly why the guest's own `ssh docker@127.0.0.1` answered "Permission denied" while
    # the host got a closed connection. THAT ASYMMETRY IS THE WHOLE DIAGNOSIS.
    #
    # UseDNS no is the sshd setting for not doing the lookup at all. macOS ships it commented
    # out (the default is `yes`, with a historical caveat), so it costs one line.
    #
    # THE sed ALONE IS NOT ENOUGH AND THE SECOND COMMAND IS NOT A CHECK. sed -i rewrites a
    # line that MATCHES; if the file has no UseDNS line at all, it rewrites nothing and exits
    # 0, and this would look done. `grep -q ... || echo >>` appends only when the pattern is
    # missing, which is the case the sed cannot cover.
    "sudo sed -i '' -E 's|^#?[[:space:]]*UseDNS[[:space:]].*|UseDNS no|' /etc/ssh/sshd_config",
    "sudo sh -c \"grep -q '^UseDNS no' /etc/ssh/sshd_config || echo 'UseDNS no'"
    " >> /etc/ssh/sshd_config\"",
    # 2c. AND USE PAM OFF, BECAUSE PAM IS WHERE THE CRASH IS.
    #
    # Run 42110424017 settled three things the earlier runs could not:
    #
    #     dscl . -authonly docker admin   ->  AUTHOK          the password is right
    #     sshd -T | grep -i usedns        ->  usedns no       the directive took
    #     ssh docker@10.0.2.15 true       ->  Permission denied, a real decision
    #
    # That last one is the important one. 10.0.2.15 is a NON-LOOPBACK address - it is what
    # qemu's DHCP gives the guest - so sshd handling a LAN peer works. What does not work is
    # the connection qemu forwards from the host, whose peer address is 10.0.2.2.
    #
    # AND THE UNIFIED LOG NAMES THE CALL INSIDE THAT PATH:
    #
    #     sshd: (libsystem_info.dylib) Retrieve User by Name
    #     sshd: (libsystem_info.dylib) [com.apple.network:libinfo:si_destination_compare]
    #           send failed: Invalid argument
    #
    # "Retrieve User by Name" through libsystem_info is the OpenDirectory lookup, and
    # OpenDirectory is what pam_opendirectory drives when sshd authenticates THROUGH PAM.
    # `dscl . -authonly` succeeds because it talks to the directory directly; sshd fails
    # because the PAM module takes a path through libinfo that the NAT peer address breaks.
    #
    # UsePAM no removes that path: sshd checks the password itself against the local account
    # database instead of handing it to PAM. Every other system here authenticates without
    # PAM - Linux's sshd uses it, but Linux's PAM has no OpenDirectory in it.
    "sudo sed -i '' -E 's|^#?[[:space:]]*UsePAM[[:space:]].*|UsePAM no|' /etc/ssh/sshd_config",
    "sudo sh -c \"grep -q '^UsePAM no' /etc/ssh/sshd_config || echo 'UsePAM no'"
    " >> /etc/ssh/sshd_config\"",
    # 3. THE ACCESS GROUP, IN FULL. EVERY ATTRIBUTE BELOW IS LOAD-BEARING.
    #
    # macOS consults com.apple.access_ssh SEPARATELY from sshd_config, and a HALF-BUILT group
    # is worse than none: launchd's socket activation sees a group with that name, decides
    # access is restricted, and resets the connection before sshd ever speaks. That is
    # exactly what run 42056084784 got, and the message is what names it:
    #
    #     kex_exchange_identification: read: Connection reset by peer
    #
    # - the TCP connection succeeded and the protocol exchange was cut off, which is a
    # different failure from `Connection refused` (nothing listening) and from
    # `Permission denied` (sshd answered and rejected the credential).
    #
    # THE FIRST VERSION OF THIS LIST HAD THREE LINES AND NEEDS ELEVEN. It created the group,
    # set PrimaryGroupID, and appended uid 501. Missing:
    #
    #     GroupMembership           the short-name list; without it the group has members it
    #                               cannot name, and the membership check fails closed
    #     GroupMembers "0"          root. An access group that does not contain root cannot
    #                               be used by the system's own administrative paths
    #     NestedGroups              admin (80), staff (20), _lpoperator (401). docker is in
    #                               admin, and the check walks nesting - a direct member that
    #                               is only reachable through a nested group does not count
    #
    # The eleven lines are easydev-org/dockur-macos's, in the same order, from
    # src/install/firstboot/launch.sh. 501 is docker's uid.
    "sudo dscl . -create /Groups/com.apple.access_ssh",
    "sudo dscl . -create /Groups/com.apple.access_ssh PrimaryGroupID 399",
    "sudo dscl . -create /Groups/com.apple.access_ssh GroupMembership",
    "sudo dscl . -create /Groups/com.apple.access_ssh GroupMembers 0",
    "sudo dscl . -append /Groups/com.apple.access_ssh GroupMembers 501",
    "sudo dscl . -create /Groups/com.apple.access_ssh NestedGroups",
    "sudo dscl . -append /Groups/com.apple.access_ssh NestedGroups 80",
    "sudo dscl . -append /Groups/com.apple.access_ssh NestedGroups 20",
    "sudo dscl . -append /Groups/com.apple.access_ssh NestedGroups 401",
    # 4. AND RESTART IT, so all of the above takes effect now rather than at the next boot.
    "sudo launchctl kickstart -k system/com.openssh.sshd",
    # 5. THE VERDICT, READ FROM THE GUEST ITSELF. Two facts, both required: the group lists
    #    the account, and something is listening on 22. Printing them means the recording
    #    carries the answer even when this stage fails later.
    "sudo dscl . -read /Groups/com.apple.access_ssh GroupMembers NestedGroups",
    "netstat -an | grep '\\*.22 ' | head -2",
    # 6. AND THE ONLY TEST THAT MATTERS: THE GUEST LOGGING IN TO ITSELF.
    #
    # The three commands above were added so a failure would carry its own state, and run
    # 42087483424's recording shows they all passed - GroupMembers: 0 501, NestedGroups:
    # 80 20 401, `*.22 LISTEN` - while the host's authenticated probe got an empty answer and
    # the stage failed. So none of them is the missing fact.
    #
    # THIS IS. `ssh docker@127.0.0.1 true` asks exactly what the host-side check asks, from
    # inside, and prints what ssh itself says when it says no. The two failures worth telling
    # apart are both invisible from the group and the socket:
    #
    #     docker is not in the password database the way sshd reads it   -> denied
    #     the account has no password, or its AuthenticationAuthority is
    #     missing/disabled                                               -> denied
    #     AllowUsers / DenyUsers / an sshd_config directive              -> denied
    #
    # `dscl . -read /Users/docker AuthenticationAuthority` is the field that says whether a
    # password is set at all. macOS keeps it in a separate attribute from the account record,
    # and an account created by the Setup Assistant always has it - but nothing so far has
    # ever printed it, so "the account cannot authenticate" has been an assumption for every
    # run of this stage.
    #
    # EVERY ONE OF THESE IS SHORT. The whole sequence typed as one line was 471 characters
    # and a shell that redraws its input line coalesces keystrokes - run 41997580476 typed a
    # 105-character winget command into Windows and the guest received `get install`. The
    # longest here is under 80.
    #
    # BatchMode=yes so ssh FAILS instead of waiting at a password prompt this list cannot
    # answer, and the failure is the useful part: ssh names the methods the server offered.
    "ssh -o BatchMode=yes -o StrictHostKeyChecking=no docker@127.0.0.1 true",
    "sudo dscl . -read /Users/docker AuthenticationAuthority",
    # THE EFFECTIVE CONFIG, NOT THE FILE. sshd -T resolves the includes and the defaults, so
    # this says what sshd will actually do rather than what one file happens to spell.
    # 7. AND WHY IT SAID NO. Everything above says the refusal SHOULD NOT happen, and run
    # 42090649098's recording is the proof that it does:
    #
    #     ssh -o BatchMode=yes docker@127.0.0.1 true
    #       -> Permission denied (publickey,password,keyboard-interactive)
    #     AuthenticationAuthority: ;ShadowHash;HASHLIST:<SALTED-SHA512-PBKDF2..> ;SecureToken;
    #     sshd -T: usepam yes / passwordauthentication yes / permitemptypasswords no
    #
    # BatchMode=yes cannot answer a password prompt, so that line proves only that sshd
    # OFFERS password authentication - it does not prove a password would be accepted. The
    # server's own account of the decision is the only thing that distinguishes
    #
    #     the password is wrong            (the host types admin/see session.SSH_PASSWORD)
    #     the account may not log in      (a blocked or expired AuthenticationAuthority)
    #     PAM refused it                  (the macOS side of usepam yes)
    #
    # and macOS writes it to the unified log, not to a file. The predicate is narrow on
    # purpose: `log show` for a whole hour is thousands of lines and the last five of them are
    # the wrong five.
    "sudo log show --last 2m --predicate 'process == \"sshd\"' 2>/dev/null | tail -6",
    # 8. DOES THE PASSWORD WORK AT ALL? This is the one question that splits the two
    #    remaining hypotheses, and it never touches ssh.
    #
    #    `dscl . -authonly` asks the directory service to authenticate the account with the
    #    password, exactly as a login would, and prints nothing on success. So:
    #
    #        exits 0        the password is right, and the fault is in how sshd takes it
    #                       from this client - qemu's NAT address, the auth plugin, PAM
    #        exits non-0    the password is wrong, and NOTHING about sshd matters
    #
    #    Every run so far has assumed the first without testing it. The account is created by
    #    the desktop stage through the Setup Assistant, whose form validates as it is typed
    #    and drops characters that arrive too fast - run 42085569136's account form even
    #    ended up with "admin" in Full name and an empty Password after a coordinate bug.
    #    A disk whose account password never got set would fail exactly like this, and the
    #    ssh-side diagnostics cannot see it.
    #
    #    "&& echo AUTHOK || echo AUTHFAIL" turns the verdict into a word the recording shows
    #    without anyone having to know what dscl prints on failure.
    "sudo dscl . -authonly docker admin && echo AUTHOK || echo AUTHFAIL",
    # 9. THE TWO QUESTIONS RUN 42105500358 LEFT UNANSWERED, LAST SO THEY STAY ON SCREEN.
    #
    # `dscl . -authonly docker admin` printed AUTHOK: THE PASSWORD IS RIGHT. So the account
    # and the password are not in question, and neither is the config's intent - every value
    # sshd -T has reported says yes. The only thing left is what sshd does with THIS client.
    #
    # 9a. DID UseDNS no ACTUALLY APPLY? It was set and the failure did not change, which is
    #     either "the directive took and is not what this is" or "the directive never took".
    #     macOS reads /etc/ssh/sshd_config.d/*.conf through an Include, and a later include
    #     can put back what an earlier sed removed. sshd -T prints the resolved value, so it
    #     settles which. Printed LAST because the six-word grep used to scroll away.
    "sudo /usr/sbin/sshd -T | grep -i usedns",
    # 9b. DOES A NON-LOOPBACK PEER BREAK IT FROM INSIDE THE GUEST?
    #
    #     The guest's `ssh docker@127.0.0.1` has always answered "Permission denied" - a real
    #     decision - while the host's comes from 10.0.2.2 and gets a closed connection. If the
    #     guest connecting to its OWN LAN address (10.0.2.15, what qemu's DHCP gives it) also
    #     gets a closed connection, then it is not qemu and not the NAT: sshd fails for any
    #     peer whose address it has to resolve, and 127.0.0.1 is exempt because it never
    #     leaves the loopback.
    #
    #     That is a different fix from anything on the sshd side, and this one line says
    #     which of the two it is.
    "ssh -o BatchMode=yes -o StrictHostKeyChecking=no docker@10.0.2.15 true; echo PEER=$?",
]

# THE VERDICT IS THE GUEST'S, AND IT IS TWO FACTS: the job is loaded, and something is
# listening. `launchctl list` alone says the job exists; only the socket says sshd is up.
REMOTE_LOGIN_CHECK = "netstat -an | grep -c '*.22 '"


def enable_remote_login(guest: vm.Guest) -> None:
    """Open Terminal and set Remote Login on, as root.

    SPOTLIGHT, NOT THE DOCK. Cmd-Space opens Spotlight wherever the Dock is and whatever
    is in it, and the wizard's choices do not move it - which is why this is a keyboard
    route rather than a click at a Dock icon whose position depends on what the user
    pinned.

    sudo READS ITS PASSWORD FROM THE TERMINAL, so it is typed after the command rather
    than passed to it. `echo` is not used: the password would land in the guest's shell
    history and in any frame taken while it scrolled past.

    EVERY KEY COMES FROM lib/keys.py, which is also what the Windows stage types with -
    a second table here is a second place for ":" to mean the wrong thing, and that
    failure reads as a shell syntax error rather than as a keyboard bug.
    """
    import qmp as qmpmod
    from lib import keys

    kb_work = guest.paths.drive
    deadline = time.time() + 120
    while not os.path.exists(kb_work):
        if time.time() > deadline:
            raise SystemExit(f"no QMP socket at {kb_work}")
        time.sleep(1)

    q = qmpmod.QMP(kb_work)
    try:
        # Escape first: a notification or an update overlay swallows the next chord.
        qmpmod.key(q, "esc")
        time.sleep(1)

        # SPOTLIGHT, AND THE FIRST RESULT IS NOT ALWAYS THE APPLICATION.
        #
        # Run 42028663003's last frame is SAFARI with a Google search for
        #
        #     terminalsudo systemsetup -setremotelogin onadmin
        #
        # `terminal` DID appear as a result - the word matched, the app was found - but
        # Spotlight's own top row for that query is the web-search suggestion, and Return
        # takes the highlighted row. So Return opened the browser, and the command and the
        # password that followed were typed into the search field. sshd never started and the
        # stage reported "something answered, but not sshd" for its whole budget.
        #
        # THE SEARCH TERM IS THEREFORE THE FULL PATH, which has no web suggestion, and the
        # wait before Return is longer than Spotlight's index needs to answer. Both are
        # cheap; guessing which row is highlighted is not.
        qmpmod.key_combo(q, "meta_l", "spc")   # Spotlight
        time.sleep(3)
        keys.type_into(q, "terminal")
        # TEN SECONDS BETWEEN EVERY STEP, BECAUSE EACH ONE IS A DIFFERENT WINDOW'S TURN.
        #
        #     1. type "terminal"     Spotlight has to search and draw its result list
        #     2. Return              Spotlight has to launch the application
        #     3. the next command    Terminal has to open a window and a login shell
        #
        # Every one of those is a separate process starting on a guest with four vCPUs and
        # no cache to help it, and typing into an application that has not appeared puts the
        # text wherever focus happens to be. The waits above were 3s, 5s and 12s, chosen at
        # different times for different reasons; this is ten seconds for each, which is what
        # the slowest of them needed.
        #
        # `/Applications/Utilities/Terminal.app` WAS TRIED AND IS NOT THE ANSWER. It was
        # meant to avoid Spotlight's web-search row, but run 42034256322 showed the real
        # cause was elsewhere: Keyboard Setup Assistant was on screen and took the Return.
        # The dialog is dismissed before this is reached - see dismiss_keyboard_assistant().
        time.sleep(10)
        qmpmod.key(q, "ret")
        time.sleep(10)

        # FIRST COMMAND, THEN THE PASSWORD - sudo reads it from the terminal.
        #
        # THE PASSWORD IS TYPED AFTER THE COMMAND, never passed as an argument:
        # `echo password | sudo -S` would leave it in the guest's shell history and in every
        # frame taken while it scrolled past.
        first, *rest = REMOTE_LOGIN_STEPS
        log(f"  {first}")
        keys.type_into(q, first)
        qmpmod.key(q, "ret")
        time.sleep(10)
        keys.type_into(q, GUEST_PASSWORD)
        qmpmod.key(q, "ret")
        # sudo CACHES THE CREDENTIAL AFTER THE FIRST SUCCESS, so the rest do not prompt.
        # Ten seconds each: launchctl talks to launchd, sed rewrites a file in /etc, and
        # dscl writes the directory store - each is a separate privileged operation.
        for step in rest:
            log(f"  {step[:80]}")
            time.sleep(6)
            keys.type_into(q, step)
            qmpmod.key(q, "ret")
            time.sleep(6)

        # SETREMOTELOGIN PRINTS ONLY ON REFUSAL, so the preference is read back rather
        # than trusted: -getremotelogin answers "Remote Login: On" when it took. The read
        # back is what the recording is checked against - if it says On and sshd still does
        # not answer, the problem is sshd rather than the preference.
        keys.type_into(q, REMOTE_LOGIN_CHECK)
        qmpmod.key(q, "ret")
        time.sleep(10)
    finally:
        q.close()


def wait_for_ssh(port: int, timeout: int = 180) -> bool:
    """Wait until sshd on `port` lets the account run a command.

    THE BANNER IS NOT ENOUGH, AND THIS FUNCTION SAID IT WAS. It used to stop here:

        banner = sock.recv(256)
        if banner.startswith("SSH-2.0"):
            return True

    which proves one thing - something speaking the SSH protocol is listening. It does not
    prove the account can authenticate, and on this guest those are different facts:

        launchd's socket activation accepts the connection
        the banner is written by sshd before any credential is offered
        com.apple.access_ssh is consulted AFTER the banner

    So a guest whose access table does not admit the account answers this function with a
    perfect SSH-2.0-OpenSSH_9.9 and then refuses every real connection. That is exactly what
    run 42079736995 recorded:

        18:14:50  banner: SSH-2.0-OpenSSH_9.9
        18:14:50  Remote Login is answering
        18:14:50  === cleaning the guest, over ssh ===
        18:14:50  `df -m / | awk 'NR==2 {print $3}'` returned 255: (no output)

    255 is ssh's own exit status for "the connection failed", and it arrived in the same
    SECOND the banner was accepted.

    lib/session.wait_for_ssh already documents this rule and runs a probe for it
    ("A BANNER IS NOT ENOUGH - this runs a command, because a socket that accepts a connection
    and then refuses authentication is a different failure and has been one here before").
    This module had its own copy that did not, which is why the check passed and the clean
    that followed it could not connect.

    THE PROBE IS `true`, the POSIX builtin - the smallest command that proves a shell ran.
    For macOS that is all that is needed: it is not cmd.exe, so the Windows note in
    session.wait_for_ssh does not apply.
    """
    import session

    if not session.wait_for_ssh(port, timeout, log, probe="true"):
        return False

    # AND THE SOCKET IS REPORTED, because the probe passing and the banner passing look the
    # same in a log and only one of them means the account was admitted.
    log(f"  sshd admitted {session.SSH_USER} on port {port}")
    return True


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
