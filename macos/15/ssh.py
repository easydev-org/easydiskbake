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

# THE SEQUENCE IS A FILE NOW: boot, log in, System Settings, the ssh assertions, the shutdown.
# Everything this stage keeps is what a step cannot be - the disk, the overlay, the OpenCore
# assets and the recording.
WALK = os.path.join(HERE, "steps", "ssh.yaml")
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


# The password the desktop stage was given. Remote Login is turned on from a root shell
# started with sudo, so this is the account's password, not root's.
GUEST_USER = os.environ.get("MACOS_USER", "vmuser")
GUEST_PASSWORD = os.environ.get("MACOS_PASSWORD", "admin")


def _sha256(path: str) -> str:
    """The file's digest, streamed so a 28 GiB disk never has to fit in memory."""
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


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
    p.add_argument("--steps", default="",
                   help="turn Remote Login on from this YAML walkthrough instead of ssh.py's "
                        "own System Settings sequence. The login, the wait and the checks "
                        "around it are unchanged.")
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--overlay", action="store_true",
                   help="treat --disk as a READ-ONLY base and write into a throwaway qcow2 "
                        "overlay: the base is never modified and the overlay is deleted at "
                        "the end, so every run starts from the same bytes. See lib/overlay.py")
    args = p.parse_args(argv)

    out = args.out or args.disk.replace(".qcow2", "-ssh.qcow2")
    work = args.work or os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"disk={args.disk} out={out}")
    if not os.path.isfile(args.disk):
        raise SystemExit(f"no disk at {args.disk}")

    # THE DISK THIS RUN WRITES TO.
    #
    # --overlay IS THE ONE THAT KEEPS THE BASE INTACT. It makes a qcow2 whose backing file is
    # the published disk, so qemu reads through to the base for everything the run does not
    # change and writes only its own clusters into the overlay. The base is opened read-only
    # while a backing file is in use, and close() deletes the overlay - which restores the disk
    # to exactly what was published, not approximately. lib/overlay.py also records the base's
    # size and mtime before qemu starts and checks them after, because "I did not write to it"
    # is an assumption and this is a stage that has been wrong about assumptions.
    #
    # WITHOUT IT the old behaviour stands: `working_copy` moves the disk (the default, since
    # the disk normally came from ghcr and the registry still holds it) or copies it when
    # --copy is given.
    # THE INPUT'S DIGEST, TAKEN BEFORE ANYTHING MOVES IT. See macos/15/desktop.py for why:
    # `working_copy` MOVES the disk, this stage is supposed to change it, and the publication
    # has to be able to prove it did. The push runs after this process has exited, so the number
    # goes to a file.
    input_digest = _sha256(args.disk)
    log(f"input  sha256 {input_digest[:16]}  ({args.disk})")
    with open(os.path.join(work, "input.sha256"), "w", encoding="utf-8") as fh:
        fh.write(input_digest + "\n")

    run_overlay = None
    if args.overlay:
        import overlay as overlaymod

        run_overlay = overlaymod.Overlay(args.disk, work, name="ssh-overlay.qcow2")
        disk = run_overlay.open()
    else:
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

    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)

    # THE RECORDING SOCKET IS COMPUTED, NOT ASKED FOR. This stage no longer starts qemu - the
    # file's `boot:` step does - so there is no guest to read `paths.record` from yet, and the
    # sampler has to be running BEFORE the machine is, or the login window it exists to explain
    # would be the one boot nobody photographed. `vm.Paths` is where that name comes from, and
    # lib/snapshots.py waits for the socket to appear (60 x 2s) rather than requiring it.
    record_socket = vm.Paths(work).record
    snap = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         record_socket, frames,
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
    session = None
    try:
        # THE WALKTHROUGH OWNS THE WHOLE MACHINE: the boot, the login window, the System Settings
        # sequence, the ssh assertions and the shutdown. What this stage keeps is everything a
        # step cannot be - the disk, the overlay, the OpenCore assets and the recording.
        #
        # THE TOGGLE PROBLEM IS IN THE FILE, WHERE IT CAN BE READ. `turn remote login on` carries
        # an `unless: [log, in, to, this, computer]`, so a disk that already has the daemon on
        # does not get the switch pressed - and pressing it turns it OFF, which is what this stage
        # used to do on a re-run.
        import steps as stepslib

        # THE SHUTDOWN METHOD IS A FACT ABOUT THIS RUN, NOT ABOUT THE SCREEN, so it arrives as a
        # var: an overlay can be killed because nothing it wrote survives, and a disk this stage
        # owns has to be closed with `shutdown -h now`. See the two comments in ssh.yaml.
        shutdown_method = "kill" if run_overlay is not None else "ssh"

        session = stepslib.MachineSession(
            "macos", disk, work,
            # NO HOST SHARE, AND THIS STAGE IS THE ONE THAT PROVES WHY THE DEFAULT CANNOT BE
            # "ALWAYS SERVE ONE". The disk it is given is the DESKTOP disk - vm-configure has not
            # run yet, so there is no launchd job that mounts anything. Booting with a share the
            # guest never asked for made this stage fail before its first step with
            #
            #     === THE HOST COULD NOT SERVE /github/home/shared ===
            #
            # (run 42813552966), which is an smbd problem for a disk that shows no sign of wanting
            # one. `""` is vm_session's documented "do not serve", and the stage that needs the
            # share is macos15-vm-configure, which asks for it by name.
            share="",
            overlay=run_overlay, ssh_port=args.ssh_port,
            cpus=args.cpus, mem_mb=args.mem,
            # `copy` IS "WRAP THIS DISK IN A PROTECTIVE OVERLAY", AND IT IS ALWAYS FALSE HERE.
            #
            # The distinction this got wrong: `run_overlay` is a diff the STAGE made and owns,
            # and `copy` is a second one that vm_session would make on top. They are mutually
            # exclusive by construction -
            #
            #     --overlay given   `disk` is already the overlay; the base is read-only and
            #                       the stage flattens this diff into the artifact
            #     not given         `disk` is a working copy the stage MOVED into work; it is
            #                       the file the artifact is made from
            #
            # so wrapping it again sends every write into a session overlay that is deleted on
            # the way out. Run 42831281414 did exactly that on the desktop stage: 2639 MiB
            # written, "released the overlay (2639 MiB written this run)", and the published
            # disk was the untouched base - which is why `macos15-desktop` had the same content
            # digest as `macos15-setup`.
            boot_options={"assets": assets, "copy": False})
        ok, ran = stepslib.run_file(
            WALK, "", work=work,
            user=GUEST_USER, password=os.environ.get("MACOS_PASSWORD", "admin"),
            ssh_port=args.ssh_port, release="15", frames=frames,
            extra_vars={"args.shutdown": shutdown_method},
            session=session,
        )
        log(f"{len(ran)} step(s) answered" if ok
            else f"the walkthrough failed after {len(ran)} step(s)")
        if not ok:
            return 1

        ok = True
        log("Remote Login is answering")

        # WHAT THIS STAGE OWES THE NEXT ONE IS A DISK THAT ANSWERS SSH.
        #
        # The check that a later stage can reach it is macos15-dev's: that job pulls the
        # published tag and connects. This stage's job is to turn Remote Login on, prove the
        # account is admitted, and hand the disk back.
        #
        # THERE IS NO CLEAN ANY MORE, BY INSTRUCTION - AND IT WAS NEVER DOING ANYTHING HERE.
        # macclean called `diskutil apfs trim`, which is not a macOS verb; measured on a real
        # 15.8.2 guest, `rm` of 2 GB left the qcow2 at 8,899,136 blocks before and after,
        # because APFS does not forward a discard to virtio-blk. So the step could not shrink
        # the image even when it ran, and it had been failing the whole stage instead:
        #
        #     === cleaning the guest, over ssh ===
        #     sudo: a password is required
        #     === diskutil apfs trim WAS REFUSED (exit 1) ===
        #     === THE CLEAN DID NOT FINISH ===
        #     nothing is published: the disk is not what this stage claims to produce
        #
        # lib/macclean.py is kept: it still deletes what the guest can delete, and macos15-dev
        # may want it. Nothing in this stage calls it now.
        return 0
    finally:
        # THE WALKTHROUGH TURNED THE MACHINE OFF, AND THAT IS THE ONLY STOP HERE.
        #
        # `shutdown:` in macos/15/steps/ssh.yaml is the last step and it names its method:
        # `kill` when this stage runs on an overlay (nothing written is kept; the flatten is the
        # artifact), `ssh` when it owns the real disk. `run_file` stops the guest itself if the
        # file forgets, so a failure in the middle does not leave a qemu holding /dev/kvm.
        #
        # WHAT THIS STAGE STILL OWNS at the end is the recording and the artifact. The snapshot
        # sampler and the encoder are not steps: they run for the whole stage, which is not
        # something a step can be.
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
            if run_overlay is not None:
                # AN OVERLAY IS A DIFF, NOT A DISK. It only means anything next to the base
                # it was made from, so the published artifact is produced by reading the chain
                # and writing one standalone file - which is what flatten() does.
                run_overlay.flatten(out)
                # AND THEN IT IS RELEASED, WHICH IS ALSO THE CHECK. `close()` deletes the diff and
                # compares the base's size and mtime against what it recorded before qemu started:
                # "I did not write to it" is an assumption, and this stage has been wrong about
                # assumptions. It happens AFTER the flatten because the flatten reads it.
                run_overlay.close()
                log(f"BAKED macos15 ssh -> {out} ({os.path.getsize(out) >> 20} MiB)")
            else:
                compress.compress(disk, out)
                log(f"BAKED macos15 ssh -> {out} ({os.path.getsize(out) >> 20} MiB)")


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


def dismiss_keyboard_assistant(guest: vm.Guest, q=None) -> bool:
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

    # THREE CLICKS SPACED OUT, NOT ONE. The dialog appears WITH the first desktop session and
    # the session takes a while to finish drawing itself, so a single click can arrive before
    # the dialog exists - which lands on the wallpaper, does nothing, and looks identical in
    # the log to a click that dismissed it.
    #
    # Run 42162859846 is that exact case: the log says "clicking Quit ... 04:46:32", the
    # desktop test passed at 04:46:51, and the recording at t=130s still shows the dialog
    # covering the screen while Spotlight is open behind it. Every keystroke after it went to
    # the dialog, and the switch sequence ran against a screen it could not see.
    #
    # A click on a machine with no dialog is harmless - that point is wallpaper - so three is
    # free when the dialog is absent and enough when it arrives late.
    # `q=` IS THE SAME SHARING RULE AS enable_remote_login()'S, AND THIS IS THE CALL THAT MADE
    # IT NECESSARY. qemu's -qmp unix:... accepts ONE client, so a walkthrough that holds the
    # socket and calls enable_remote_login(q=...) still hung here: this function opened a
    # SECOND connection for the three Quit clicks, which got no greeting and blocked. The
    # caller that passes one owns its lifetime; otherwise open one and close it.
    owned = q is None
    q = q or qmpmod.QMP(kb_work)
    try:
        for attempt in range(3):
            log(f"  clicking Quit on the Keyboard Setup Assistant at "
                f"{KEYBOARD_ASSISTANT_QUIT} (attempt {attempt + 1})")
            qmpmod.click(q, *KEYBOARD_ASSISTANT_QUIT)
            time.sleep(4)
    finally:
        # ONLY THE CONNECTION THIS FUNCTION OPENED. Closing a caller's socket takes the
        # walkthrough's own screen reader down with it, and the next qmpmod.click then fails
        # with 'OSError: [Errno 9] Bad file descriptor' on a connection that looked fine.
        if owned:
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
REMOTE_LOGIN_STEPS: list[str] = []  # replaced by the System Settings sequence

# THE VERDICT IS THE GUEST'S, AND IT IS TWO FACTS: the job is loaded, and something is
# listening. `launchctl list` alone says the job exists; only the socket says sshd is up.
REMOTE_LOGIN_CHECK = "netstat -an | grep -c '*.22 '"


def _remote_login_listening(guest: vm.Guest, port: int = 2222) -> bool:
    """Does anything answer on the guest's ssh port already?

    THE SWITCH IN SYSTEM SETTINGS IS A TOGGLE, NOT AN "ON" BUTTON, so pressing it on a machine
    where Remote Login is already on turns it OFF. Measured on a live guest in the local pod:
    three runs of this stage left sshd closed, and the `wait for sshd` step after it waited out
    its whole 600-second budget for a daemon the step before had just switched off.

    The check is the socket, not the switch. QMP can read the pane and a person could tell the
    two states apart, but the honest question is "does sshd answer", and the answer costs one
    TCP connect through the forward lib/net.py builds.

    Returns False - meaning "go ahead and press it" - when the port cannot be reached at all,
    which is the fresh-disk case this stage exists for.

    `port` IS THE CALLER'S, NOT A CONSTANT. The first version hard-coded 2222, which is the
    default but not the only value a stage may pass - so on a run using another port this probe
    would always fail, always answer "not listening", and always press a switch that might have
    been on already.
    """
    import socket

    import net

    try:
        with socket.create_connection((net.FORWARD_HOST, port), timeout=4) as sock:
            sock.settimeout(4)
            banner = sock.recv(64)
    except OSError:
        return False
    # A CONNECTED PORT IS NOT AN SSHD: qemu's forward could be answered by anything. The banner
    # is what says it is ssh, and it is the same test wait_for_ssh() makes.
    return banner.startswith(b"SSH-")


def enable_remote_login(guest: vm.Guest, q=None) -> None:
    """Turn Remote Login on THROUGH SYSTEM SETTINGS, which is the only way that works.

    THIS REPLACES THIRTY SHELL COMMANDS, AND THE THIRTY WERE THE BUG.

    The old version opened Terminal and typed launchctl enable / launchctl load / a sed on
    sshd_config / ten dscl calls for the access group / launchctl kickstart. Every one of
    those is the right command, they all ran, and the result was still unusable: the host
    connected, sent its password packet, and sshd CLOSED the connection - six runs of
    diagnostics (UseDNS, UsePAM, the subnet, the NIC's MAC, the access group, the crash
    reports) never explained it.

    The difference is `Allow full disk access for remote users`, which exists only on this
    pane. Without it a remote session runs without Full Disk Access, and the account
    lookups sshd performs fail on data TCC protects - the log said

        sshd: (libsystem_info.dylib) Retrieve User by Name
        sshd: (libsystem_info.dylib) [...si_destination_compare] send failed: Invalid argument

    and closed the connection at exactly that point. There is no launchctl command for
    this, no sshd_config directive, and `systemsetup -setremotelogin on` refuses outright:

        setremotelogin: Turning Remote Login on or off requires Full Disk Access privileges.

    So the switch is pressed in the application that HAS the privilege, which is System
    Settings. Measured on this machine: after this sequence, `sshpass -p admin ssh -p 2222
    vmuser@172.30.0.1` returns SSH_OK, `sw_vers` and `id` run, and a second connection later
    still works. THE ADDRESS IS THE BRIDGE'S, not loopback: lib/net.py's OUTPUT rule explains
    why a forward cannot be reached at 127.0.0.1.

    THE COORDINATES ARE 1920x1080, THE SAME UNIT AS A SCREENDUMP. screendump writes the
    guest's own framebuffer at its own size, so a pixel measured in a saved frame IS the
    coordinate to send - multiplying it by 1.5 (which a 1280x800 recording would need) moves
    the click into the middle of the window and does something else entirely. That mistake
    cost three attempts: every click at the "search box" opened Safari, because the point it
    really hit was a link on the Software Update pane.
    """
    import qmp as qmpmod
    from lib import keys

    kb_work = guest.paths.drive
    deadline = time.time() + 120
    while not os.path.exists(kb_work):
        if time.time() > deadline:
            raise SystemExit(f"no QMP socket at {kb_work}")
        time.sleep(1)

    # THE DIALOG OWNS THE KEYBOARD, so it is dismissed again here rather than trusting that
    # the earlier attempt was early enough. The earlier one runs right after unlock(); this
    # one runs right before the first Spotlight chord, which is the moment that matters.
    dismiss_keyboard_assistant(guest, q=q)

    # `q=` IS FOR A CALLER THAT ALREADY HOLDS THE SOCKET, AND PASSING IT IS NOT OPTIONAL THERE.
    #
    # qemu's `-qmp unix:...,server=on,wait=off` accepts ONE client. A walkthrough driver that
    # owns the connection cannot hand this function a path and have it open a second - the
    # second connect gets no greeting and dies with
    #
    #     TimeoutError: timed out
    #       lib/qmp.py, in _read_message: chunk = self.sock.recv(65536)
    #
    # which is the same failure macos/15/install.py's Recovery(q=...) exists to avoid, in the
    # other stage. The caller that passes one owns its lifetime.
    q_owned = q is None
    q = q or qmpmod.QMP(kb_work)

    def click(x: int, y: int, settle: float = 2.0) -> None:
        qmpmod.click(q, x, y)
        time.sleep(settle)

    def shot(name: str) -> None:
        """One frame per step, so a failure says which pane it stopped on."""
        try:
            import lastframe
            work = os.path.dirname(kb_work)
            ppm = os.path.join(work, f"remote-login-{name}.ppm")
            q.execute("screendump", filename=ppm)
            lastframe.convert(ppm, os.path.join(work, f"remote-login-{name}.png"))
        except (OSError, RuntimeError) as exc:
            log(f"  screendump {name} failed: {exc}")

    try:
        # IS IT ALREADY ON? THE SWITCH IS A TOGGLE, NOT A "TURN ON".
        #
        # Running this stage twice on the same disk turns Remote Login OFF the second time -
        # measured on a live guest: three runs left sshd closed, and the stage that followed
        # waited out its whole budget for a daemon the step before it had just switched off.
        # The switch is at (1145, 353) and it reads "Off" until it is pressed, so a driver that
        # cannot tell the two states must not press it.
        #
        # The cheapest honest state check is the daemon itself: if the port answers, there is
        # nothing to do. It also makes a resumed run cheap - the GUI sequence is most of a
        # minute of clicking.
        # THE PORT LIVES ON THE MACHINE'S SHAPE, not on the socket paths - `Guest.start()`
        # forwards `shape.ssh_port`, so that is the number the host side is listening on.
        if _remote_login_listening(guest, getattr(getattr(guest, "shape", None),
                                                  "ssh_port", 2222)):
            log("=== Remote Login is ALREADY on; nothing to press ===")
            return

        # 1. SYSTEM SETTINGS, THROUGH SPOTLIGHT. The full name is used rather than "settings"
        #    because Spotlight's first row for a short query can be a web suggestion, and
        #    Return takes the highlighted row.
        log("=== opening System Settings ===")
        qmpmod.key(q, "esc")
        time.sleep(1)
        qmpmod.key_combo(q, "meta_l", "spc")
        time.sleep(3)
        keys.type_into(q, "system settings")
        time.sleep(7)
        qmpmod.key(q, "ret")
        time.sleep(14)
        shot("1-opened")

        # 2. THE SEARCH FIELD, AT ITS MEASURED PIXEL. (715, 267) is the centre of the box
        #    whose interior spans x 630..800, y 250..284 in a 1920x1080 frame.
        log("=== searching for remote login ===")
        click(715, 267, settle=2.0)
        keys.type_into(q, "remote login")
        # THE RESULT LIST FILTERS AS YOU TYPE, and on a loaded runner it lags the keystrokes.
        # Four seconds was measured on a machine with nothing else running; run 42162859846
        # is what happens when the click arrives first: the search field holds "remote login"
        # and the pane behind it is still the Sharing OVERVIEW, so the next click - aimed at
        # a row that has not been drawn yet - lands on that overview and every step after it
        # is off by one screen. Ten seconds costs six and removes the race.
        time.sleep(10)
        shot("2-searched")

        # 3. THE RESULT. The list puts "Sharing" as a header and "Remote Login" under it;
        #    (703, 336) is that row.
        log("=== opening Remote Login ===")
        # THE PANE HAS TO FINISH SWAPPING BEFORE THE SWITCHES ARE CLICKED, and that swap is
        # a whole page of layout. Five seconds was enough locally; the recording of run
        # 42162859846 shows a click at this row that selected the sidebar item while the right
        # side still showed the Sharing OVERVIEW, so the next two clicks - meant for the
        # Remote Login switch and the full-disk-access switch - hit list rows instead.
        click(703, 336, settle=12.0)
        shot("3-pane")

        # 4. THE SWITCH. (1145, 353) is the Remote Login toggle, which reads "Off" until it
        #    is pressed.
        log("=== turning Remote Login on ===")
        click(1145, 353, settle=4.0)
        shot("4-remote-login-on")

        # 5. FULL DISK ACCESS IS ALREADY ON, AND CLICKING IT AGAIN TURNS IT OFF.
        #
        # The switch above is not independent: turning Remote Login ON turns
        # `Allow full disk access for remote users` ON with it, on this release. The frames of
        # run 42167875539 show it exactly -
        #
        #     remote-login-4-remote-login-on.png    both switches blue
        #     remote-login-5-full-disk-access.png   Remote Login blue, full disk access GREY
        #
        # and the second frame is one click after the first. This stage had been pressing it
        # "to be sure", which is the one thing that un-does it. The cost was the clean:
        #
        #     === diskutil apfs trim WAS REFUSED (exit 1) ===
        #
        # because a remote session without Full Disk Access cannot trim the volume it is
        # running from.
        #
        # SO IT IS READ, NOT PRESSED. One frame is taken so the recording says what the state
        # was; if a release ever ships with the two switches independent, that frame is where
        # the next reader will see it.
        log("=== full disk access (on with Remote Login; not touched) ===")
        shot("5-full-disk-access")

        # 6. DONE, so the pane closes and the daemon settles rather than staying in a
        #    settings sheet that may hold the change until dismissal.
        log("=== Done ===")
        click(1145, 679, settle=5.0)
        shot("6-done")

        # AND THE PREFERENCE IS READ BACK ON THE CONSOLE, because a switch that looks on is
        # not the same fact as sshd listening. The caller waits for the port afterwards.
        keys.type_into(q, "sudo systemsetup -getremotelogin")
        qmpmod.key(q, "ret")
        time.sleep(4)
        keys.type_into(q, GUEST_PASSWORD)
        qmpmod.key(q, "ret")
        time.sleep(6)
        shot("7-verdict")
    finally:
        # THE SAME RULE ONE LEVEL UP: a connection the caller handed in is the CALLER'S to
        # close. Closing it here is what produced
        #
        #     OSError: [Errno 9] Bad file descriptor
        #
        # on the next qmpmod.click, one line after '=== opening System Settings ===' - a socket
        # that had been working a moment earlier.
        if q_owned:
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
    # THIS STAGE IS BEING MIGRATED INTO ITS WORKFLOW FILE. Until every caller says `easyvm`, the
    # old form still runs - but it says so, and EASYVM_STRICT=1 makes it a refusal. See
    # lib/entrypoint.py for why the seam exists rather than a deletion.
    import entrypoint  # noqa: E402

    entrypoint.note_stage("macos/15/ssh.py", "macos/15/steps/ssh.yaml", extra="--boot-disk out/macos15-desktop.qcow2")
    raise SystemExit(main(sys.argv[1:]))
