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
import socket
import subprocess
import sys
import time

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

    recovery = macos_recovery.fetch_published("15", work)
    log(f"  {recovery} ({os.path.getsize(recovery) >> 20} MiB)")

    log("=== OpenCore, firmware, APFS driver ===")
    import opencore

    assets = opencore.build("15", work)
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

    ok = False
    try:
        log(f"waiting up to {args.boot_timeout}s for the desktop")
        if not wait_for_desktop(guest, frames, args.boot_timeout):
            log("=== THE DISK DID NOT REACH A DESKTOP ===")
            log("  this stage needs the desktop-stage disk, not the setup-stage one")
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

        # THE CLEAN IS A STEP IN THIS STAGE, NOT A JOB OF ITS OWN.
        #
        # That is the same rule the other three systems follow: lib/clean_disk.py's docstring
        # is explicit that a separate job would have to upload 12-16 GiB, download it again,
        # boot it and clean it - work that is already done here, because this stage has the
        # guest RUNNING, ssh CONNECTED and the disk on local storage.
        #
        # IT RUNS LAST, AFTER sshd IS PROVEN TO ANSWER, which is the only point at which
        # every step of the cleaner can be reached. lib/macclean.py, and the reason it exists
        # at all: the four macOS stages up to here each only ADD to the disk, so this is the
        # first and only place it can shrink before being published.
        log("=== cleaning the disk before it is exported ===")
        import clean_disk
        if not clean_disk.clean_running("macos", port=args.ssh_port, note=log,
                                        disk=disk):
            # A CLEAN THAT DID NOT RUN IS A FAILURE, NOT A WARNING.
            #
            # The other three systems' stages treat it this way, and the reason is the same
            # here: publishing a disk whose free blocks were never handed back means every
            # later pull of that tag carries the bloat, and the tag is what people use. A
            # run that says "success" over an unclean disk makes the size look like the
            # best this system can do.
            log("=== THE CLEAN DID NOT FINISH ===")
            log("  nothing is published: the disk is not what this stage claims to produce")
            return 1
        return 0
    finally:
        guest.stop(acpi_first=ok)
        snap.terminate()
        try:
            snap.wait(timeout=5)
        except subprocess.TimeoutExpired:
            snap.kill()
        if ok:
            if not smoke.check_disk(disk, root_fs="apfs"):
                log("=== THE DISK FAILED ITS CHECKS ===")
                raise SystemExit(1)
            log("compressing")
            compress.compress(disk, out)
            log(f"BAKED macos15 ssh -> {out} ({os.path.getsize(out) >> 20} MiB)")


def wait_for_desktop(guest: vm.Guest, frames: str, timeout: int) -> bool:
    """The wallpaper test again - see macos/15/desktop.py for why it is this one."""
    from lib import desktop

    shots = os.path.join(frames, "shots")
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
        qmpmod.key_combo(q, "meta_l", "spc")   # Spotlight
        time.sleep(3)
        keys.type_into(q, "terminal")
        qmpmod.key(q, "ret")
        time.sleep(8)

        keys.type_into(q, "sudo systemsetup -setremotelogin on")
        qmpmod.key(q, "ret")
        # sudo's prompt. Typed after the command, not as an argument to it.
        time.sleep(4)
        keys.type_into(q, GUEST_PASSWORD)
        qmpmod.key(q, "ret")
        time.sleep(6)

        # SETREMOTELOGIN PRINTS ONLY ON REFUSAL, so the preference is read back rather
        # than trusted: -getremotelogin answers "Remote Login: On" when it took.
        keys.type_into(q, "sudo systemsetup -getremotelogin")
        qmpmod.key(q, "ret")
        time.sleep(5)
    finally:
        q.close()


def wait_for_ssh(port: int, timeout: int = 180) -> bool:
    """Poll the host port until the SSH banner arrives.

    THE BANNER, NOT A CONNECT. A TCP connect succeeds against anything listening; the
    first bytes say whether it is sshd.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                sock.settimeout(10)
                banner = sock.recv(256).decode("utf-8", "replace").strip()
            if banner.startswith("SSH-2.0"):
                log(f"  banner: {banner}")
                return True
            log(f"  something answered, but not sshd: {banner[:60]}")
        except OSError:
            pass
        time.sleep(10)
    return False


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
