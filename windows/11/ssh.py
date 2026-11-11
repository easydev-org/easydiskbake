"""Give a baked Windows disk an SSH server, and prove it answers.

    python3 windows/11/ssh.py --gold out/windows11.qcow2 --out out/windows11-ssh.qcow2

WHY THIS IS A SECOND STAGE AND NOT PART OF THE BAKE

The bake produces a disk that boots to a desktop. This takes that disk, starts it,
installs OpenSSH, and proves a client can log in. The two are separate because they fail
in different ways and are worth reporting separately:

    the bake      a wrong answer file, a driver, an ISO - all visible in a recording
    this          a winget id that moved, a firewall rule, an sshd_config line

A single 90-minute job that does both reports "failed" for either, and the person reading
it has to work out which. A stage that starts from an already-good disk also re-runs in
minutes when only the second half changed.

HOW A COMMAND REACHES THE GUEST, BEFORE SSH EXISTS

Chicken and egg: SSH is what makes everything afterwards easy, and it is not there yet.
The disk boots to a desktop with a "Docker" account and no password prompt, so the
channel is the keyboard:

    Esc           dismiss whatever is in front (the Start menu, an update overlay)
    Win+R         Run, which accepts typing immediately - unlike Start search
    powershell    a shell
    <command>     typed one character at a time through QMP

That is the same mechanism macos/15/drive.py uses for its wizard, and lib/qmp.py already
sends the key events. What this adds is the ordering: a fixed sequence with a settle after
each step, and a screenshot at each one so a failure shows what was on screen.

WHY winget AND NOT A DOWNLOAD

winget installs Microsoft.OpenSSH.Preview, which brings the service, the firewall rule and
the sshd_config the rest of this file edits. A hand-downloaded MSI would need all of that
done here, and the id would still be the thing that moves.

The command is typed, so it is one line. That is a constraint rather than a preference:
sendkey cannot type a newline, and pretending otherwise is how the easysandbox version
died once ("cannot map newline" after sending nothing).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import net  # noqa: E402
import swtpm as swtpmmod  # noqa: E402
import qmp as qmpmod  # noqa: E402
import vm  # noqa: E402

# THE GUEST LISTENS ON 22, AND THE HOST SEES IT ON 2222.
#
# lib/vm.build_args() already forwards host 2222 -> guest 22:
#
#     hostfwd=tcp::{ssh_port}-:22
#
# so sshd has to be on 22 or the forward points at nothing. The two numbers being
# different is deliberate and is the usual convention: the guest keeps the standard port
# so anything that assumes 22 works inside, and the host uses a high one because a job
# container is not root on a port that matters.
GUEST_SSH_PORT = 22
HOST_SSH_PORT = 2222

# HOW LONG TO WAIT AFTER winget install BEFORE TOUCHING sshd.
#
# A FIXED WAIT, NOT A POLL FOR THE PROMPT, AND RUN 41587909137 IS WHY.
#
# The previous version watched the screen for "PS C:\...>" to come back and called that
# success. winget prints that prompt whether it installed the package or not, so the stage
# went on to configure a service that did not exist:
#
#     Get-Content : Cannot find path 'C:\ProgramData\ssh\sshd_config'
#     Start-Service : Cannot find any service with service name 'sshd'
#
# and then waited two minutes for an sshd that was never going to answer. The run before
# it installed in 51 seconds and worked; this one took 54 and did not, so there is nothing
# in the prompt to distinguish them.
#
# 120 seconds, because that is how long the install takes when it takes its time: the
# measured ones are 51 and 54 seconds, and winget's own first-run metadata refresh on this
# image has been seen to add most of a minute. Waiting is free here; the guest is a KVM VM
# with nothing else to do, and the alternative was a check that cannot tell success from
# failure. Whether it worked is asked of the machine afterwards, not of its screen.
WINGET_SETTLE_SECONDS = 120

# The id in the winget index. Not .Beta and not .Server: neither exists.
WINGET_PACKAGE = "Microsoft.OpenSSH.Preview"

# The account the answer file creates.
# THE WALKTHROUGH THIS STAGE RUNS. Beside the stage, because the numbers in it were measured
# on this guest - see the file's own header for why each one is what it is.
WALK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steps", "ssh.yaml")

GUEST_USER = os.environ.get("WINDOWS_ADMIN_USER", "vmuser")
GUEST_PASSWORD = os.environ.get("WINDOWS_ADMIN_PASSWORD", "admin")


def log(msg: str) -> None:
    print(f"[ssh {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Keyboard:
    """Typing into a guest that has no other channel yet.

    Every method here is one QMP call, and every call is followed by a settle: the guest
    is a Windows desktop, and the gap between "the key arrived" and "the shell is ready for
    the next character" is the thing that makes this reliable or not.
    """

    def __init__(self, qmp_path: str, shots: str):
        # THE QMP SOCKET IS CREATED BY QEMU, ASYNCHRONOUSLY, AFTER IT IS STARTED.
        #
        # vm.Guest.start() returns as soon as the process exists; qemu then takes a moment
        # to bind the sockets. Connecting immediately raises
        #
        #     FileNotFoundError: [Errno 2] No such file or directory
        #     self.sock.connect(path)
        #
        # which is what the first CI run of this stage did. lib/snapshots.py has the same
        # wait for the same reason; this is that wait, with the message a bake needs.
        deadline = time.time() + 120
        while not os.path.exists(qmp_path):
            if time.time() > deadline:
                raise SystemExit(
                    f"no QMP socket at {qmp_path} after 120s; qemu either failed to start "
                    f"or was given a different work directory"
                )
            time.sleep(1)
        self.q = qmpmod.QMP(qmp_path)
        self.shots = shots
        self.n = 0

    def shot(self, label: str) -> str | None:
        self.n += 1
        path = os.path.join(self.shots, f"ssh-{self.n:03d}-{label}.ppm")
        try:
            self.q.execute("screendump", filename=path)
        except (OSError, RuntimeError) as exc:
            log(f"screendump failed: {exc}")
            return None
        return path if os.path.exists(path) else None

    def key(self, name: str, settle: float = 0.4) -> None:
        qmpmod.key(self.q, name)
        time.sleep(settle)

    def combo(self, *keys: str, settle: float = 1.0) -> None:
        qmpmod.key_combo(self.q, *keys)
        time.sleep(settle)

    def type(self, text: str, settle: float = 0.15) -> None:
        """Type ASCII, one key at a time.

        A newline is refused rather than dropped: sendkey cannot produce one, and a caller
        that passes one has a command that needs splitting, which is a decision, not a
        retry. The easysandbox version learned this as "cannot map newline" after typing
        nothing at all.

        THE DEFAULT IS 150 ms FOR THE REASON type_text() GIVES: PSReadLine redraws on every
        keystroke, and 60 ms let it drop characters - run 41997580476 typed `winget install`
        and the guest received `get install`. A default that loses input is a trap for the
        next caller, so the slow value is the default and a caller that wants speed asks.
        """
        for ch in text:
            if ch in "\r\n\t":
                raise ValueError(
                    f"cannot type {ch!r}; split the command and join with ';'"
                )
            qmpmod.key(self.q, qcode_for(ch))
            time.sleep(settle)

    def close(self) -> None:
        try:
            self.q.close()
        except OSError:
            pass


# QEMU's qcode names are the PS/2 set: letters are themselves, and everything else has a
# name of its own. The characters that appear in a winget command line are a small set,
# which is why this table is short rather than a full keyboard.
SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "minus", "+": "equal", "{": "bracket_left",
    "}": "bracket_right", "|": "backslash", ":": "semicolon", '"': "apostrophe",
    "<": "comma", ">": "dot", "?": "slash", "~": "grave_accent",
}
NAMED = {
    " ": "spc", "-": "minus", "=": "equal", "[": "bracket_left",
    "]": "bracket_right", "\\": "backslash", ";": "semicolon", "'": "apostrophe",
    ",": "comma", ".": "dot", "/": "slash", "`": "grave_accent",
}


def qcode_for(ch: str) -> str:
    """The qcode for one character, with shift where the character needs it.

    lib/qmp.key() sends a single down/up pair; a shifted character is two keys held at
    once, which is key_combo's job. Returning a name from here and letting the caller
    decide would put that decision in three places, so this returns a name that key()
    understands and the shifted case goes through type() below.
    """
    if ch.isdigit() or (ch.islower() and ch.isalpha()):
        return ch
    if ch.isupper() and ch.isalpha():
        raise ValueError(
            f"{ch!r} needs shift; type_text() handles that, qcode_for() does not"
        )
    if ch in NAMED:
        return NAMED[ch]
    raise ValueError(f"no qcode for {ch!r}")


def type_text(kb: Keyboard, text: str) -> None:
    """Type `text`, using shift for the characters that need it.

    STILL NEEDED BY THE LOCK SCREEN, which is the one thing this stage still types at a
    keyboard: the walkthrough below handles the desktop, but a guest that boots to a password
    field has no ssh and no walkthrough to run. The other callers - the winget line and the
    sshd statements - are steps in windows/11/steps/ssh.yaml now.

    THREE KINDS OF CHARACTER, and the first version handled one:

        lowercase, digits      one key, no modifier
        uppercase              shift + the lowercase key
        punctuation            shift + its unshifted key, or a bare named key

    The uppercase case was missing, and it is not cosmetic: a winget package id is
    "Microsoft.OpenSSH.Preview", so five of its characters were unmappable and the
    command could not be typed at all.

    150 MILLISECONDS PER KEY, AND 60 WAS DROPPING THEM. PSReadLine redraws the line it is
    editing on every keystroke and coalesces them below about that; run 41997580476 typed
    `winget install` and the guest received `get install`.
    """
    control = [ch for ch in text if ch in "\r\n\t"]
    if control:
        raise ValueError(
            f"cannot type {control[0]!r}; sendkey has no such key, so split the command "
            f"and join with ';'"
        )
    for ch in text:
        if ch.isupper() and ch.isalpha():
            qmpmod.key_combo(kb.q, "shift", ch.lower())
        elif ch in SHIFTED:
            qmpmod.key_combo(kb.q, "shift", SHIFTED[ch])
        else:
            qmpmod.key(kb.q, qcode_for(ch))
        time.sleep(0.15)


def ocr(path: str) -> str:
    """Read a frame, if tesseract is here. Absent OCR is not an error."""
    if not shutil.which("tesseract"):
        return ""
    try:
        result = subprocess.run(
            ["tesseract", path, "-", "--psm", "6", "-l", "eng"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(l.strip() for l in result.stdout.splitlines() if l.strip())


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--gold", required=True, help="the disk the bake produced")
    p.add_argument("--out", default="out/windows11-ssh.qcow2")
    p.add_argument("--copy", action="store_true",
                   help="copy the input disk instead of moving it; the default is to "
                        "move, because the disk normally came from ghcr and the "
                        "registry protects it")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=6144)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--tpm", action="store_true",
                   help="provide a TPM 2.0; off by default, because the answer file "
                        "bypasses the check and the disk boots without one")
    p.add_argument("--no-clean", dest="clean", action="store_false",
                   help="leave the disk as it is; the workflow runs lib/clean_step.py next")
    p.add_argument("--bootstate", default=None,
                   help="IGNORED. The disk carries \\EFI\\BOOT\\BOOTX64.EFI and this "
                        "machine uses a fresh variable store; kept so an existing command "
                        "line still parses")
    args = p.parse_args(argv)

    if not os.path.isfile(args.gold):
        log(f"no gold disk at {args.gold}")
        return 1

    work = args.work or os.path.join(os.path.dirname(os.path.abspath(args.out)), "ssh-work")
    shots = os.path.join(work, "shots")
    os.makedirs(shots, exist_ok=True)

    # A REAL COPY, NOT A QCOW2 BACKING FILE.
    #
    # The obvious design is `qemu-img create -b <gold> -F qcow2`, which costs nothing until
    # something writes. It does not work against a disk this repository publishes, and the
    # error says why:
    #
    #     qemu-img: Unsupported qcow2 feature(s): compression type
    #     Could not open backing image to determine size.
    #
    # qcow2 refuses a compressed image as a backing file, and every disk here is published
    # with -o compression_type=zstd for size. So the reuse is a copy.
    #
    # `cp --reflink=auto` asks the filesystem for copy-on-write (btrfs, xfs with reflink)
    # and falls back to a byte copy where it cannot. On a CI overlay filesystem that is the
    # latter, which is why the free-space check in the workflow asks for the disk twice.
    disk = os.path.join(work, "disk.qcow2")
    # MOVED, NOT COPIED. The disk this stage starts from came from ghcr, so the
    # registry still holds it - a copy protects nothing and costs minutes on ext4.
    # --copy is here for the case where it IS the only copy.
    import working_copy as wc
    disk = wc.working_copy(args.gold, os.path.join(work, "disk.qcow2"), copy=args.copy, note=log)

    firmware_code = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd")
    firmware_vars = os.path.join(work, "OVMF_VARS.fd")
    template = os.environ.get("OVMF_VARS_TEMPLATE", "/usr/share/OVMF/OVMF_VARS_4M.fd")
    # ALWAYS A FRESH COPY OF THE TEMPLATE, NEVER A RESTORED BOOTSTATE.
    #
    # The disk is self-booting: bake.py writes the Windows loader to the UEFI fallback path
    # \EFI\BOOT\BOOTX64.EFI, which the firmware tries when no boot entry resolves. So this
    # machine is given an EMPTY variable store on purpose - it is the machine a person who
    # downloads the artifact gets, and the boot is the same boot.
    #
    # THIS USED TO REQUIRE A BOOTSTATE ARCHIVE AND ABORT WITHOUT ONE. Every stage carried
    # `<job>_bootstate`, the archive held `windows.vars` with the "Windows Boot Manager"
    # entry, and a missing artifact failed the job before the disk was ever started:
    #
    #     Unable to download artifact(s): Artifact not found for name: windows11-bootstate
    #
    # which is run 41835601248 - the disk was fine, and it was pulled and never booted.
    # Restoring an old bootstate is not merely unnecessary now, it is worse than useless:
    # its entries name the partition GUID of whichever disk wrote them, so an archive from
    # a different boot points at partitions this disk may not have.
    shutil.copyfile(template, firmware_vars)
    if args.bootstate:
        log(f"--bootstate {args.bootstate} IGNORED: the disk carries its own loader at "
            f"\\EFI\\BOOT\\BOOTX64.EFI and this machine uses a fresh variable store")

    # NO TPM BY DEFAULT - see the note in bake.py's swtpm(). A published Windows disk
    # from this repository is installed with the answer file's BypassTPMCheck keys, so it
    # boots without one, and a TPM makes the disk and its state an inseparable pair.
    tpm = swtpmmod.start(work) if args.tpm else ""

    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        firmware_code=firmware_code, firmware_vars=firmware_vars,
        tpm_socket=tpm, vga="std", ssh_port=2222,
    )
    console = os.path.join(work, "console.log")
    # THE DISK HAS TO BE ATTACHED, AND FOR SIX RUNS IT WAS NOT.
    #
    # This stage copies the published disk, restores its firmware state, and then started
    # a machine with `extra=[]` - no drive at all. Every symptom that followed was read
    # as something else:
    #
    #     Boot000A "Windows Boot Manager" ... bootmgfw.efi: Not Found
    #     map -r  ->  BLK0 only, no FS0:
    #
    # The firmware could not find a boot loader because there was no Windows disk to
    # find it on: BLK0 was the firmware's own variable store. bake.py passes its drives
    # through `extra`, and this stage never did.
    drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=console).start()

    kb = Keyboard(guest.paths.drive, shots)
    ok = False
    try:
        log(f"waiting up to {args.boot_timeout}s for the desktop")
        if not wait_for_desktop(guest, shots, args.boot_timeout, kb=kb):
            log("the guest did not reach a desktop")
            return 1

        # THE SEQUENCE IS A FILE NOW: windows/11/steps/ssh.yaml.
        #
        # What was here - open PowerShell, type the winget line, type six sshd statements,
        # wait for the port, push 55 guest settings - was 300 lines of measurements of THIS
        # guest and not one decision. The measurement belongs somewhere a reader can see it
        # beside the number it produced; the decision ("does this stage pass?") stays here.
        #
        # THE WALKTHROUGH CANNOT MAKE THE KEYBOARD. `Keyboard` owns the QMP socket and the
        # screenshots, and a second client cannot open one (qemu accepts ONE), so the steps
        # that type are given this stage's own driver rather than opening their own. That is
        # the `driver=` argument below, and it is why this is not a `subprocess` call to
        # lib/steps.py's CLI.
        log(f"=== the walkthrough: {os.path.relpath(WALK, REPO)} ===")
        import steps as stepslib
        parsed = stepslib.load(WALK)
        # `q=kb.q` IS NOT AN OPTIMISATION. qemu accepts ONE client on a QMP socket, and the
        # Keyboard above already holds it - it had to, to wake the lock screen before ssh
        # existed. A Driver that opened its own would block on the greeting and read as a hung
        # guest, which is the same single-client rule the macOS ssh extensions had to learn.
        #
        # The Driver does not close a socket it did not open (`_do_action`'s python: steps are
        # the same rule), so `stage.close()` is a no-op here and `kb.close()` still owns it.
        stage = stepslib.driver_for(
            guest.paths.drive, work=work,
            ssh_port=HOST_SSH_PORT, width=1920, height=1080, q=kb.q)
        try:
            ok_walk, ran = stepslib.walk_with(
                parsed, stage, ssh_port=HOST_SSH_PORT,
                user=GUEST_USER, password=GUEST_PASSWORD,
            )
        finally:
            stage.close()
        log(f"{len(ran)} step(s) answered" if ok_walk
            else f"the walkthrough failed after {len(ran)} step(s)")
        if not ok_walk:
            kb.shot("walkthrough-failed")
            return 1

        # CLEANING IS ITS OWN WORKFLOW STEP, so this stage can be asked to skip it.
        #
        # --no-clean is what the workflow passes: the clean runs as the step after this one,
        # using lib/clean_step.py, which boots the disk again and cleans it there. That costs
        # a boot, and it buys a step in the run graph that says "clean the disk: failed"
        # rather than a line buried in the middle of this stage's log.
        #
        # THE DEFAULT IS TO CLEAN, because a disk that is not cleaned should not leave this
        # stage: everything after this one starts from it, and an uncleaned ssh disk carries
        # its winget caches into the dev disk and then into every artifact after that.
        if args.clean:
            # It has to be after the check above, because clean.ps1 is 190 lines and cannot be
            # typed at a keyboard: ssh is not merely convenient here, it is the only channel
            # that can carry the script.
            from lib.clean_disk import clean_running

            if not clean_running("windows", port=HOST_SSH_PORT, note=log):
                log("=== THE GUEST WAS NOT CLEANED; THE DISK IS NOT PUBLISHED ===")
                log("  an unclaimed disk carries this run's installers into every later stage")
                return 1
        else:
            log("not cleaning here; the next step does it (lib/clean_step.py)")

        ok = True
        return 0
    finally:
        guest.stop(acpi_first=ok)
        kb.close()

        if args.tpm:
            swtpmmod.stop(work)
        if ok:
            log(f"exporting {args.out}")
            # compress.compress(), NOT A HAND-WRITTEN qemu-img CONVERT.
            #
            # This line used to spell out the conversion itself, and on run 41586561730 it
            # failed after a completely successful installation - sshd was answering when
            # it ran:
            #
            #     qemu-img: Could not open '.../disk.qcow2':
            #     Unsupported qcow2 feature(s): compression type
            #
            # lib/compress.py already had the answer: the bare name `qemu-img` resolves
            # to the Android SDK's copy on this image's PATH, and that build cannot read
            # the compressed disk it is handed. compress.py uses the absolute
            # /usr/bin/qemu-img, probes whether it can write zstd before claiming it did,
            # and converts to a .part name so a failure cannot leave a truncated file
            # under the published one. Three properties this call site was re-deriving
            # wrongly.
            import compress

            compress.compress(disk, args.out)
            log(f"BAKED {args.out} ({os.path.getsize(args.out) >> 20} MiB)")

            # NO BOOTSTATE IS SAVED, BECAUSE NOTHING CONSUMES ONE.
            #
            # This stage used to pack the firmware variables and TPM state so the dev stage
            # could restore them. The disk now carries \EFI\BOOT\BOOTX64.EFI and starts on a
            # fresh template, so there is no second artifact - and a stage that published one
            # anyway would be maintaining a dependency the bake just removed.
            log("no bootstate: the disk boots by its own fallback loader")


# HOW BRIGHT A FRAME HAS TO BE TO BE WORTH A KEYPRESS.
#
# A lock screen measured 77; the firmware shell measured 1 and a blank screen 0. The
# number only has to separate "something is painted" from "nothing is", so it sits well
# below the lock screen and well above a text console on black.
LIT_SCREEN = 20

# THE GAP THE SEQUENCE ASKS FOR, in seconds. Long enough that the page has repainted
# before the next keystroke: the password field is drawn by an animation, and typing into
# it mid-fade drops characters - which reads afterwards as a wrong password.
WAKE_SETTLE = 5.0

# HOW LONG A LIT SCREEN HAS TO STAY LIT, AND STILL NOT BE A DESKTOP, BEFORE IT IS TAKEN
# FOR A LOCK PAGE. Run 41575708286 held its lock screen for 257 seconds, so a minute is
# not a guess about how long Windows takes - it is the answer to "has this stopped
# changing", and it leaves the ordinary case (a desktop at 50 seconds) untouched.
LOCK_SECONDS = 60.0


def wake_from_lock_screen(kb: "Keyboard") -> bool:
    """Enter, five seconds, the password, five seconds, Enter. Called once per launch.

    THE LOCK SCREEN IS THE COMMON CASE, NOT A FAILURE.

    Run 41575708286's recording is unambiguous once the frames are looked at instead of
    the log. At 49 seconds the screen shows

        4:06
        Tuesday, November 3

    over the Windows 11 bloom wallpaper - the lock screen - and it held that single
    still picture for 257 seconds before Windows turned the display off. Every log line
    said mean grey 77 and then 0, and desktop.is_windows_desktop() said no, because its test is
    "a light band on top and a busier one below", which is a taskbar and a desktop, not
    a full-bleed wallpaper with a clock.

    bake.py already records this exact lesson:

        What had happened was a lock screen. Windows blanks the display after a minute
        of no input, and this guest gets no input - so the common case, not the failure
        case, was being read as a crash.

    WHY THIS IS ONE UNCONDITIONAL SEQUENCE. The first version pressed a key, looked at
    the frame, and only typed the password if that had not produced a desktop. Two
    branches to get right, and the second one was still a guess about what the frame
    meant. The sequence below is not a guess: Enter dismisses a lock page and focuses the
    password field, so both steps belong to the same action. It runs once, and the frames
    it leaves behind say what happened.

    THE CALLER ONLY RUNS THIS ON A LIT SCREEN THAT HAS STAYED LIT. Enter typed into the
    firmware shell is input to a different program; see LIT_SCREEN and LOCK_SECONDS.

    The password is not printed; only the account name is.
    """
    log(f"lock page: Enter, {WAKE_SETTLE}s, the password for {GUEST_USER}, "
        f"{WAKE_SETTLE}s, Enter")
    kb.key("ret", settle=WAKE_SETTLE)
    shot = kb.shot("after-enter")
    if shot:
        log(f"  after Enter: {describe_shot(shot)}")
    type_text(kb, GUEST_PASSWORD)
    time.sleep(WAKE_SETTLE)
    shot = kb.shot("after-password")
    if shot:
        log(f"  after the password: {describe_shot(shot)}")
    kb.key("ret", settle=WAKE_SETTLE)
    shot = kb.shot("after-sign-in")
    if shot:
        log(f"  after the second Enter: {describe_shot(shot)}")
    return True


def wait_for_desktop(guest: vm.Guest, shots: str, timeout: int,
                     kb: "Keyboard | None" = None, on_sample=None,
                     say=None) -> bool:
    """Wait for frames to stop changing after boot, then for a desktop by colour.

    lib/desktop.py's test is the same one the bake uses: a light band at the top and a
    darker, busier one at the bottom. It is reused rather than reimplemented, because a
    second opinion about what a desktop looks like is a second thing to keep in step.

    `kb` makes a stuck lock page recoverable: without it this can only wait and report,
    which is all it did on run 41575708286, where the guest was at a lock screen from 49
    seconds and the display went dark at 342.

    `on_sample(shot, elapsed)` is called once per frame, before any judgement, for a
    caller that has to keep the pictures - launch_windows writes one PNG per sample so a
    cancelled run still has its whole timeline. `say` is where the narrative goes, for a
    caller whose report is not this module's log.
    """
    import desktop

    speak = say or log
    deadline = time.time() + timeout
    booted = time.time()
    last_shot: str | None = None
    lit_since: float | None = None
    woken = False
    while time.time() < deadline:
        if not guest.alive():
            speak("qemu exited during boot")
            return False
        shot = take_shot(guest, shots)
        if shot:
            last_shot = shot
        elapsed = time.time() - booted
        if on_sample is not None:
            on_sample(shot, elapsed)
        if shot and desktop.is_windows_desktop(shot):
            speak(f"a desktop is on screen ({elapsed:.0f}s)")
            return True

        # A LIT, STILL SCREEN THAT IS NOT A DESKTOP IS A LOCK PAGE.
        #
        # Run 41575708286 showed "4:06 / Tuesday, November 3" over the bloom wallpaper
        # from 49 seconds, held it for 257 seconds, and then Windows turned the display
        # off. desktop.is_windows_desktop() said no throughout, because its test asks for a
        # taskbar and a lock page is one full-bleed picture.
        #
        # THE TRIGGER IS SIXTY SECONDS OF BEING LIT, and it is brightness rather than a
        # timer alone: a guest in the firmware shell is also not a desktop, and keystrokes
        # there go to the shell - run 41562919683 spent nine minutes in that state. This
        # lock page read mean grey 77; the shell read 1 and a blank screen reads 0.
        #
        # ONCE PER LAUNCH. The sequence presses Enter, waits, types the password, waits,
        # presses Enter - after that the screen may legitimately be an OOBE page, and
        # repeating it would type the password into whatever came up.
        grey = frame_brightness(last_shot)
        if grey is not None and grey >= LIT_SCREEN:
            if lit_since is None:
                lit_since = elapsed
        else:
            lit_since = None

        if (kb is not None and not woken and lit_since is not None
                and elapsed - lit_since >= LOCK_SECONDS):
            woken = True
            speak(f"{elapsed - lit_since:.0f}s of a lit screen (mean grey {grey}) that "
                  f"is not a desktop; assuming a lock page")
            wake_from_lock_screen(kb)
            # The sequence leaves its own frames, and the loop judges whatever comes
            # next: a desktop, an OOBE page, or nothing. There is no second guess here.
            continue

        if not woken and int(elapsed) % 60 < 12:
            # SAY WHAT THE SCREEN IS, not only how long it has been watched. A run that
            # times out here currently reports nothing about what it was looking at, and
            # "waited 900s" cannot distinguish a guest stuck at the firmware shell from
            # one sitting on a login screen that the desktop test does not recognise.
            speak(f"waiting {elapsed:.0f}s for a desktop"
                  + (f" (last frame {os.path.basename(last_shot)}, "
                     f"{describe_shot(last_shot)})" if last_shot else " (no frames yet)"))
        time.sleep(12)

    log("=== NO DESKTOP WITHIN THE BOOT TIMEOUT ===")
    if last_shot:
        log(f"  the last frame is {last_shot} ({describe_shot(last_shot)})")
        log("  it is kept in the recording artifact; it says what the screen was")
    else:
        log("  not one frame could be captured - the recorder socket never answered,")
        log("  which means qemu was not drawing at all")
    return False


def frame_brightness(path: str | None) -> int | None:
    """Mean grey of a PPM, or None if it cannot be read.

    Only for "is anything drawn at all". What IS drawn is desktop.is_windows_desktop()'
    question, and answering it twice would be two things to keep in step.
    """
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            if fh.readline().strip() != b"P6":
                return None
            line = fh.readline()
            while line.startswith(b"#"):
                line = fh.readline()
            width, height = (int(v) for v in line.split())
            fh.readline()
            pixels = fh.read(width * height * 3)
    except (OSError, ValueError):
        return None
    if not pixels:
        return None
    total = 0
    count = 0
    for i in range(0, len(pixels), 97 * 3):
        total += pixels[i] + pixels[i + 1] + pixels[i + 2]
        count += 3
    return total // count if count else None


def describe_shot(path: str | None) -> str:
    """One line about a frame: size and mean grey, or why it could not be read.

    Deliberately not a description of what is DRAWN. That is what desktop.is_windows_desktop()
    is for, and a second opinion about the screen would be a second thing to keep in
    step - this only reports whether anything was drawn at all.
    """
    if not path:
        return "none"
    try:
        with open(path, "rb") as fh:
            if fh.readline().strip() != b"P6":
                return "not a PPM"
            line = fh.readline()
            while line.startswith(b"#"):
                line = fh.readline()
            width, height = (int(v) for v in line.split())
    except (OSError, ValueError):
        return "unreadable"
    grey = frame_brightness(path)
    if grey is None:
        return f"{width}x{height}, empty"
    return f"{width}x{height}, mean grey {grey}"


def take_shot(guest: vm.Guest, shots: str, label: str = "boot") -> str | None:
    """One frame from the recorder socket, or None.

    The socket may not exist yet on the first call - qemu binds it a moment after the
    process starts - and a screendump that fails is not fatal here: the caller is polling
    for a desktop and will ask again. Returning None rather than raising keeps a slow
    start from ending a ten-minute stage.
    """
    path = os.path.join(shots, f"{label}-{int(time.time())}.ppm")
    if not os.path.exists(guest.paths.record):
        return None
    try:
        q = qmpmod.QMP(guest.paths.record)
        q.execute("screendump", filename=path)
        q.close()
    except (OSError, RuntimeError):
        return None
    return path if os.path.exists(path) else None


def wait_for_ssh(port: int = 2222, timeout: int = 120) -> bool:
    """Does the host's forward answer? A TCP connect is the whole test."""
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((net.FORWARD_HOST, port), timeout=5) as sock:
                # sshd speaks first. Reading a byte proves it is sshd and not something
                # else listening after a port collision.
                sock.settimeout(5)
                banner = sock.recv(64)
                if banner.startswith(b"SSH-"):
                    log(f"sshd banner: {banner.decode('ascii', 'replace').strip()}")
                    return True
        except OSError:
            pass
        time.sleep(5)
    return False

if __name__ == "__main__":
    # THIS STAGE IS BEING MIGRATED INTO ITS WORKFLOW FILE. Until every caller says `easyvm`, the
    # old form still runs - but it says so, and EASYVM_STRICT=1 makes it a refusal. See
    # lib/entrypoint.py for why the seam exists rather than a deletion.
    import entrypoint  # noqa: E402

    entrypoint.note_stage("windows/11/ssh.py", "windows/11/steps/ssh.yaml",
                          extra="--boot-disk out/windows11-gold.qcow2")
    raise SystemExit(main(sys.argv[1:]))
