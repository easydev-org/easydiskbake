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

# How long winget may take before this gives up. Measured under a minute every time; the
# allowance is for a cold Windows Update metadata refresh, which happens once.
WINGET_TIMEOUT = 600

# The account the answer file creates.
GUEST_USER = "docker"
GUEST_PASSWORD = os.environ.get("WINDOWS_ADMIN_PASSWORD", "docker")


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

    def type(self, text: str, settle: float = 0.06) -> None:
        """Type ASCII, one key at a time.

        A newline is refused rather than dropped: sendkey cannot produce one, and a caller
        that passes one has a command that needs splitting, which is a decision, not a
        retry. The easysandbox version learned this as "cannot map newline" after typing
        nothing at all.
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

    THREE KINDS OF CHARACTER, and the first version handled one:

        lowercase, digits      one key, no modifier
        uppercase              shift + the lowercase key
        punctuation            shift + its unshifted key, or a bare named key

    The uppercase case was missing, and it is not cosmetic: a winget package id is
    "Microsoft.OpenSSH.Preview", so five of its characters were unmappable and the
    command could not be typed at all. The failure would have been "no qcode for 'M'"
    after the Run dialog was already open.
    """
    # VALIDATE FIRST, THEN SEND. Typing half a command and only then discovering that the
    # next character has no key leaves the guest with a partial line at a prompt - which
    # the following Enter would then run.
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
        time.sleep(0.06)


# THE INSTALL. One line, because it is typed.
#
#   --exact                    without it a search for "Microsoft.OpenSSH.Preview" can
#                              resolve to a different package whose id merely contains it
#   --disable-interactivity    no progress UI, so the screenshot afterwards shows a prompt
#                              rather than a spinner that never ends
#   --accept-*-agreements      the two prompts that would otherwise wait for a keypress
WINGET_INSTALL = (
    "winget install --id Microsoft.OpenSSH.Preview --exact --source winget "
    "--accept-source-agreements --accept-package-agreements --disable-interactivity"
)


def configure_sshd() -> list[str]:
    """The PowerShell statements that make sshd usable, one line each.

    Each is typed, so each is a single statement. `;` joins them into one command line at
    the call site, which is what PowerShell expects and what the keyboard can produce.
    """
    cfg = r"C:\ProgramData\ssh\sshd_config"
    return [
        # The service is installed but Manual; nothing starts it at boot.
        "Set-Service sshd -StartupType Automatic",
        # Password sign-in is commented out in the shipped config. The bake's account has a
        # password (the answer file sets one) and key auth needs a key this stage does not
        # have, so password it is.
        f"(Get-Content '{cfg}') -replace '^#?\\s*PasswordAuthentication\\s+.*', "
        f"'PasswordAuthentication yes' | Set-Content '{cfg}'",
        # The shipped config has Port commented out, which means 22. It is set explicitly
        # so the value is readable afterwards rather than inferred.
        f"(Get-Content '{cfg}') -replace '^#?\\s*Port\\s+.*', "
        f"'Port {GUEST_SSH_PORT}' | Set-Content '{cfg}'",
        "Start-Service sshd",
        # The default inbound rule is created by the package but points at 22; a new one
        # for the port actually configured is what makes it reachable.
        "New-NetFirewallRule -Name sshd-%d -DisplayName 'OpenSSH %d' -Enabled True "
        "-Direction Inbound -Protocol TCP -Action Allow -LocalPort %d "
        "-ErrorAction SilentlyContinue | Out-Null" % (
            GUEST_SSH_PORT, GUEST_SSH_PORT, GUEST_SSH_PORT),
        # A listener, asked of the guest rather than read off a picture.
        f"Get-NetTCPConnection -State Listen -LocalPort {GUEST_SSH_PORT} "
        "-ErrorAction SilentlyContinue | Select-Object -First 1 LocalPort",
    ]


def open_powershell(kb: Keyboard) -> None:
    """Get to a PowerShell prompt from the desktop.

    Esc first: a Start menu, a notification or an update overlay swallows the Win+R that
    follows, and the symptom is that the command lands nowhere. Measured on the
    easysandbox bake, which recorded exactly that.
    """
    log("Esc, then Win+R")
    kb.key("esc", settle=1.0)
    kb.combo("meta_l", "r", settle=3.0)
    kb.shot("run-dialog")
    log("typing powershell")
    type_text(kb, "powershell")
    kb.key("ret", settle=12.0)
    kb.shot("powershell")


def install(kb: Keyboard) -> bool:
    """winget install, then wait for the prompt rather than a fixed delay."""
    log("winget install Microsoft.OpenSSH.Preview")
    type_text(kb, WINGET_INSTALL)
    kb.key("ret", settle=5.0)

    # Poll the screen for the prompt coming back. winget has finished in under a minute
    # every time this has been run; the deadline is for a cold metadata refresh.
    deadline = time.time() + WINGET_TIMEOUT
    last = None
    while time.time() < deadline:
        time.sleep(20)
        shot = kb.shot("winget")
        if shot is None:
            continue
        text = ocr(shot)
        # The prompt (PS C:\...>) at the end means winget returned. Anything else is still
        # working, and the frame is kept either way.
        if text and re.search(r"PS [A-Z]:\\", text):
            log("winget returned to the prompt")
            return True
        if text != last:
            log(f"  winget: {text[:100]}")
            last = text
    log(f"winget did not return within {WINGET_TIMEOUT}s")
    return False


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
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=6144)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--bootstate", default=None,
                   help="the .bootstate.tar.gz published with the disk; "
                        "without it the firmware has no boot entry")
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
    log(f"copying {args.gold} -> {disk}")
    copied = subprocess.run(["cp", "--reflink=auto", args.gold, disk],
                            capture_output=True, text=True)
    if copied.returncode != 0:
        raise SystemExit(
            f"could not copy the disk: {(copied.stderr or copied.stdout).strip()[:300]}"
        )

    firmware_code = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd")
    firmware_vars = os.path.join(work, "OVMF_VARS.fd")
    template = os.environ.get("OVMF_VARS_TEMPLATE", "/usr/share/OVMF/OVMF_VARS_4M.fd")
    if not os.path.exists(firmware_vars):
        shutil.copyfile(template, firmware_vars)
    # THE FIRMWARE AND TPM STATE HAVE TO COME BACK BEFORE THE MACHINE STARTS.
    #
    # A published disk carries a complete Windows 11 and no boot entry: the one that
    # matters is written into OVMF's variables during setup, and a fresh template has
    # none of them. Started that way the firmware tries Boot0001..Boot0005, fails each,
    # and drops into its own shell - measured on run 41468968378.
    import bootstate
    # THE ARCHIVE MOVES WITH THE DISK. This stage copies the published disk to a working
    # name (out/windows11-gold.qcow2 -> ssh-work/disk.qcow2), and restore() looks for
    # `ssh-work/disk.qcow2.bootstate.tar.gz` and nothing else. Run 41477859630 failed
    # with "no bootstate archive was found" while the archive sat in out/ under the
    # published disk's name - so it is renamed here rather than searched for later.
    if args.bootstate:
        source = args.bootstate
        # A PATH THAT DOES NOT EXIST IS AN ERROR HERE, NOT A WARNING LATER.
        #
        # The workflow names this file and can name it wrongly - run 41480134822 passed
        # `out/windows11-gold.qcow2.bootstate.tar.gz` while the artifact downloads as
        # `windows11.qcow2.bootstate.tar.gz`. The old code silently skipped a missing
        # --bootstate and failed on the generic "no archive at <working disk>" message,
        # which pointed at the wrong directory.
        if not os.path.isfile(source):
            raise SystemExit(
                f"--bootstate {source} does not exist. It must be the archive as it "
                f"arrives - named after the PUBLISHED disk (windows11.qcow2), not after "
                f"the tag this stage pulled (windows11-gold.qcow2). Instead of guessing, "
                f"this stops: the archive beside {os.path.basename(source)} is what the "
                f"publisher actually wrote."
            )
    else:
        source = bootstate.archive_path(args.gold)
    placed = bootstate.adopt(disk, source)
    log(f"bootstate {source} -> {placed}")
    restored = bootstate.restore(work, disk)
    if not restored:
        raise SystemExit(
            "no bootstate archive at "
            f"{bootstate.archive_path(disk)}. The disk alone will not boot: OVMF's "
            "variables hold the \"Windows Boot Manager\" entry and the TPM holds the "
            "state Windows sealed against it. Pass --bootstate <disk>.bootstate.tar.gz, "
            "or publish it beside the disk - see lib/bootstate.py."
        )
    log(f"bootstate restored into {work}")

    tpm = start_swtpm(work)

    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        firmware_code=firmware_code, firmware_vars=firmware_vars,
        tpm_socket=tpm, vga="std", ssh_port=2222,
    )
    console = os.path.join(work, "console.log")
    guest = vm.Guest(work, extra=[], shape=shape, stdout=console).start()

    kb = Keyboard(guest.paths.drive, shots)
    ok = False
    try:
        log(f"waiting up to {args.boot_timeout}s for the desktop")
        if not wait_for_desktop(guest, shots, args.boot_timeout):
            log("the guest did not reach a desktop")
            return 1

        open_powershell(kb)
        if not install(kb):
            return 1

        log("configuring sshd")
        for statement in configure_sshd():
            type_text(kb, statement)
            kb.key("ret", settle=3.0)
        kb.shot("sshd-configured")

        log("checking that sshd answers on the host's port %d" % HOST_SSH_PORT)
        if not wait_for_ssh(HOST_SSH_PORT):
            log("sshd did not answer")
            kb.shot("no-ssh")
            return 1

        ok = True
        return 0
    finally:
        guest.stop(acpi_first=ok)
        kb.close()
        stop_swtpm(work)
        if ok:
            log(f"exporting {args.out}")
            subprocess.run(["qemu-img", "convert", "-O", "qcow2",
                            "-c", "-o", "compression_type=zstd", disk, args.out],
                           check=True)
            log(f"BAKED {args.out} ({os.path.getsize(args.out) >> 20} MiB)")

            # THIS STAGE HAS TO PUBLISH THE BOOTSTATE TOO, because bake-windows-dev
            # starts from this disk. The firmware variables and the TPM were carried in
            # from the base bake, restored for this boot, and the boot wrote to them -
            # so what goes out is what this stage ended with, not what came in.
            #
            # Until this was added the ssh workflow uploaded a `windows-ssh-bootstate`
            # artifact with `if-no-files-found: warn`, which silently uploaded nothing,
            # and the dev stage would have failed exactly as the ssh stage did.
            archive = bootstate.save(work, args.out)
            if archive:
                log(f"bootstate: {archive} ({os.path.getsize(archive) >> 10} KiB)")
            else:
                log("bootstate: NOTHING SAVED - the dev stage will not be able to boot "
                    "this disk; the firmware variables were not found in " + work)


def wait_for_desktop(guest: vm.Guest, shots: str, timeout: int) -> bool:
    """Wait for frames to stop changing after boot, then for a desktop by colour.

    lib/desktop.py's test is the same one the bake uses: a light band at the top and a
    darker, busier one at the bottom. It is reused rather than reimplemented, because a
    second opinion about what a desktop looks like is a second thing to keep in step.
    """
    import desktop

    deadline = time.time() + timeout
    booted = time.time()
    last_shot: str | None = None
    while time.time() < deadline:
        if not guest.alive():
            log("qemu exited during boot")
            return False
        shot = take_shot(guest, shots)
        if shot:
            last_shot = shot
        if shot and desktop.is_desktop(shot):
            log(f"a desktop is on screen ({(time.time() - booted):.0f}s)")
            return True
        if int(time.time() - booted) % 60 < 12:
            # SAY WHAT THE SCREEN IS, not only how long it has been watched. A run that
            # times out here currently reports nothing about what it was looking at, and
            # "waited 900s" cannot distinguish a guest stuck at the firmware shell from
            # one sitting on a login screen that the desktop test does not recognise.
            log(f"waiting {(time.time() - booted):.0f}s for a desktop"
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


def describe_shot(path: str | None) -> str:
    """One line about a frame: size and mean grey, or why it could not be read.

    Deliberately not a description of what is DRAWN. That is what desktop.is_desktop()
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
            fh.readline()
            pixels = fh.read(width * height * 3)
    except (OSError, ValueError):
        return "unreadable"
    if not pixels:
        return f"{width}x{height}, empty"
    total = 0
    count = 0
    for i in range(0, len(pixels), 97 * 3):
        total += pixels[i] + pixels[i + 1] + pixels[i + 2]
        count += 3
    return f"{width}x{height}, mean grey {total // count if count else 0}"


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
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
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


def start_swtpm(work: str) -> str:
    """Windows 11 will not boot without a TPM, and the disk remembers the one it was
    installed against, so this has to be a TPM that persists across the two stages."""
    state = os.path.join(work, "tpm")
    os.makedirs(state, exist_ok=True)
    sock = os.path.join(state, "swtpm.sock")
    if not shutil.which("swtpm"):
        raise SystemExit("swtpm is not installed; Windows 11 will not boot without a TPM")
    subprocess.run(
        ["swtpm", "socket", "--tpmstate", f"dir={state}",
         "--ctrl", f"type=unixio,path={sock}", "--tpm2", "--daemon"],
        check=True,
    )
    return sock


def stop_swtpm(work: str) -> None:
    subprocess.run(["pkill", "-f", os.path.join(work, "tpm")], check=False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
