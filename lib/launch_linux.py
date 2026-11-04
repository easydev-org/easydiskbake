"""Boot a baked Linux or BSD disk by itself and prove it starts, over ssh.

    python3 lib/launch_linux.py --tag linux-ubuntu --kind ubuntu
    python3 lib/launch_linux.py --disk out/ubuntu.qcow2 --kind ubuntu
    python3 lib/launch_linux.py --tag bsd-freebsd --kind freebsd --compositor ""

WHAT THIS ANSWERS, AND WHY THE BAKE DOES NOT

The four bakes prove their own install: subiquity's exit, bsdinstall's exit, the disk
holding a filesystem. None of them proves the disk STARTS - a disk whose bootloader was
never written, or whose root filesystem label is wrong, installs perfectly and then does
nothing. That is a different failure from an install that did not finish, and the only way
to see it is to boot the artifact with no installer attached.

So this pulls the published tag, starts it alone, and asks the running system:

    1. does the serial console reach userspace        (the kernel and init ran)
    2. does sshd answer and accept the account        (the system is up and networked)
    3. is the desktop's Wayland session running       (only where a desktop is expected)

The third is skipped for the BSDs with --compositor "". Their installs have no display
manager - sshd and the account are the whole of what a runner needs from them - so
demanding a session would fail a disk that is exactly right.

WHY SSH AND NOT A SCREENSHOT

The bakes stopped recording video because there was nothing in it. A subiquity install
draws to ttyS0 at 80x25 and the framebuffer only mirrors it; bsdinstall does not draw at
all. The picture was black or unreadable, and the same argument applies to a boot: what
proves a Linux or BSD system started is that it answers, not that something was drawn.

lib/session.py holds the ssh conversation and the Wayland check, shared with
linux/nixos/launch.py and with the Ubuntu/Kubuntu second boot.

WHAT IS LEFT BEHIND

    <artifacts>/launch.txt      the verdict and the reasoning, one line at a time
    <artifacts>/console.log     the guest's serial console
    <artifacts>/screen.png      one screendump, for the record - not the verdict
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))

import lastframe  # noqa: E402
import serial  # noqa: E402
import session  # noqa: E402
import vm  # noqa: E402

# WHAT PROVES THE KERNEL HANDED OVER, per distribution. Each of these is a string its own
# init prints once root is mounted and userspace starts:
#
#     ubuntu, kubuntu   systemd's own banner, which both print on ttyS0
#     freebsd           the multiuser transition
#
# An empty marker means "do not wait for one" - used by nothing today, kept because a
# distribution whose console is silent would otherwise fail before ssh is even tried.
BOOTED_MARKER = {
    "ubuntu": "Ubuntu",
    "kubuntu": "Ubuntu",
    "freebsd": "FreeBSD/amd64",
}

# THE COMPOSITOR THAT PROVES A DESKTOP SESSION, or "" for a system that has none.
# See configuration-desktop.nix and the two Ubuntu answer files.
# THE COMPOSITOR THAT PROVES A DESKTOP SESSION, or "" for a system that has none.
#
# FreeBSD runs XFCE ON XORG - an X session, not a Wayland one - because that is what its
# ports tree recommends for a VM and what the disk was built to run. `ALLOW_X11` below says
# so, rather than the session check carrying a list of distributions.
COMPOSITOR = {
    "ubuntu": "gnome-shell",
    "kubuntu": "kwin_wayland",
    "freebsd": "xfce4-session",
}

# DISKS WHOSE DESKTOP IS AN X SESSION BY DESIGN. Only FreeBSD, for the reason above.
# has no display manager at all and its entry is "" so this is never consulted for it.
ALLOW_X11 = {"freebsd"}


def log(message: str) -> None:
    print(f"[launch {time.strftime('%H:%M:%S')}] {message}", flush=True)


def wait_for_console(console: str, marker: str, seconds: int, note) -> bool:
    """Wait until `marker` appears in the guest's console log.

    Read from the file, not from a stream: the guest talks before this is called. qemu
    starts, the bootloader counts down and the kernel prints while the caller is still
    setting up, so a tail on a growing file is the only way to see all of it.
    """
    deadline = time.time() + seconds
    started = time.time()
    while time.time() < deadline:
        try:
            with open(console, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            text = ""
        if marker in text:
            note(f"the console says {marker!r} after {time.time() - started:.0f}s")
            return True
        time.sleep(5)
    note(f"=== THE CONSOLE NEVER SAID {marker!r} IN {seconds}s ===")
    return False


def take_screenshot(guest, artifacts: str, note) -> str:
    """One screendump as PNG. Always attempted; never a verdict.

    SETTLES FIRST. The verdict is read over ssh the moment logind reports a session, which
    is before the compositor has painted - GNOME registers its session and then spends
    seconds drawing. The NixOS launch stage produced a screenshot of the kernel boot log
    that way; the same sequence is used here.
    """
    import qmp as qmpmod

    settle = float(os.environ.get("LAUNCH_SCREENSHOT_SETTLE", "5"))
    if settle > 0:
        note(f"  letting the session paint for {settle:g}s before the screenshot")
        time.sleep(settle)

    shot = os.path.join(artifacts, "screen.ppm")
    png = os.path.join(artifacts, "screen.png")
    client = qmpmod.QMP(guest.paths.drive)
    try:
        client.execute("screendump", filename=shot)
    except (OSError, RuntimeError) as exc:
        note(f"  the screendump failed: {exc}")
        return ""
    finally:
        client.close()
    try:
        lastframe.convert(shot, png)
    except (OSError, subprocess.SubprocessError) as exc:
        note(f"  could not convert the screenshot: {exc}")
        return ""
    return png


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tag", default=None,
                   help="a tag in this repository's package, e.g. linux-ubuntu")
    p.add_argument("--disk", default=None,
                   help="a local qcow2 instead of a tag")
    p.add_argument("--kind", required=True, choices=sorted(BOOTED_MARKER),
                   help="which distribution this disk is")
    p.add_argument("--compositor", default=None,
                   help="override the compositor that proves a session ('' to skip)")
    p.add_argument("--artifacts", default="out")
    p.add_argument("--work", default=None)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--resolution", default="1920x1080")
    args = p.parse_args(argv)

    if not args.disk and not args.tag:
        print("=== NEITHER --tag NOR --disk ===\n"
              "  nothing to start; pass a published tag or a local qcow2", flush=True)
        return 2

    work = os.path.abspath(args.work or args.artifacts)
    artifacts = os.path.abspath(args.artifacts)
    os.makedirs(work, exist_ok=True)
    os.makedirs(artifacts, exist_ok=True)

    kind = args.kind
    compositor = args.compositor if args.compositor is not None else COMPOSITOR[kind]
    marker = BOOTED_MARKER[kind]
    verdict_path = os.path.join(artifacts, "launch.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)
        try:
            with open(verdict_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def finish(code: int) -> int:
        note(f"VERDICT {'PASS' if code == 0 else 'FAIL'} kind={kind} "
             f"tag={args.tag or args.disk}")
        return code

    disk = args.disk
    if not disk:
        disk = os.path.join(work, f"{kind}.qcow2")
        if not os.path.exists(disk):
            note(f"pulling {args.tag} from ghcr")
            import iso
            try:
                disk = iso.fetch_tag(args.tag, disk)
            except iso.IsoError as exc:
                note(f"=== {args.tag} IS NOT PUBLISHED: {exc}")
                note("  run its bake first")
                return finish(2)
    if not os.path.exists(disk):
        note(f"=== NO DISK AT {disk} ===")
        return finish(2)
    note(f"disk {disk} ({os.path.getsize(disk) >> 20} MiB)")

    console_path = os.path.join(artifacts, "console.log")
    ser = serial.Serial(work, "serial", sink=console_path)
    width, height = (int(v) for v in args.resolution.lower().split("x", 1))
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        vga="std", serial_pipe=ser.stdin.removesuffix(".in"),
        resolution=(width, height),
        ssh_port=args.ssh_port,
    )
    # NO CDROM. That is what makes this a test of the disk rather than of an installer.
    drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)

    try:
        guest.start()
        ser.open(timeout=60)

        # 1. THE SYSTEM BOOTED.
        if not wait_for_console(console_path, marker, min(args.boot_timeout, 600), note):
            note("  the disk did not reach userspace; the console is uploaded")
            take_screenshot(guest, artifacts, note)
            return finish(1)

        # 2. SSH ANSWERS. This is the real proof for a BSD, which has no desktop.
        if not session.wait_for_ssh(args.ssh_port, args.boot_timeout, note):
            note("  the system booted but never answered on ssh")
            note("  sshd is on in every one of these disks, so this is the ssh config,")
            note("  the account, or the network - not the boot")
            take_screenshot(guest, artifacts, note)
            return finish(1)

        # 3. THE DESKTOP SESSION, where one is expected.
        passed = True
        if compositor:
            passed = session.wait_for_wayland(
                args.ssh_port, compositor, args.boot_timeout, note,
                ssh_seconds=60,      # already answered above
                allow_x11=kind in ALLOW_X11)
            if not passed:
                note("  the system is reachable and its desktop session is not running")
        else:
            note(f"no compositor expected for {kind}; ssh is the whole test")

        png = take_screenshot(guest, artifacts, note)
        if png:
            note(f"screenshot {png} (for the record, not the verdict)")
        return finish(0 if passed else 1)
    finally:
        guest.stop()
        # AFTER guest.stop(), so the guest keeps its console through shutdown.
        ser.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
