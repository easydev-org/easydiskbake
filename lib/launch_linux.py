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
# A GUI BAKE'S DISK IS THE SAME KIND OF ARTEFACT AND ANSWERED BY THE SAME THREE QUESTIONS,
# so the eleven desktop distributions join the table rather than getting a second launch
# script. What differs per entry is only what its own init prints on the serial console and
# which compositor proves a session - both of which are facts about that distribution.
BOOTED_MARKER = {
    "ubuntu": "Ubuntu",
    "kubuntu": "Ubuntu",
    "freebsd": "FreeBSD/amd64",
    # --- the GUI bakes, in the order .github/workflows lists them -------------------
    "ubuntu-gui": "Ubuntu",
    "kubuntu-gui": "Ubuntu",
    "fedora-gui": "Fedora",
    "mint-gui": "Linux Mint",
    "opensuse-gui": "openSUSE",
    "cachyos-gui": "CachyOS",
    "manjaro-gui": "Manjaro",
    "ghostbsd-mate": "FreeBSD/amd64",
    "endeavour-gui": "EndeavourOS",
    "zorin-gui": "Zorin",
    "mx-gui": "MX Linux",
    "popos-gui": "Pop!_OS",
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
    # The GUI bakes. Each is the compositor its ISO actually runs, so a disk whose desktop
    # failed to start is told apart from one whose desktop never existed.
    "ubuntu-gui": "gnome-shell",
    "kubuntu-gui": "kwin_wayland",
    "fedora-gui": "gnome-shell",
    "mint-gui": "cinnamon",          # Cinnamon runs on Xorg
    "opensuse-gui": "kwin_wayland",
    "cachyos-gui": "kwin_wayland",
    "manjaro-gui": "kwin_wayland",
    "ghostbsd-mate": "mate-session",  # MATE on Xorg
    "endeavour-gui": "kwin_wayland",
    "zorin-gui": "gnome-shell",
    "mx-gui": "xfce4-session",        # Xfce on Xorg
    "popos-gui": "cosmic-comp",
}

# DISKS WHOSE DESKTOP IS AN X SESSION BY DESIGN. Only FreeBSD, for the reason above.
# has no display manager at all and its entry is "" so this is never consulted for it.
# DISKS WHOSE DESKTOP IS AN X SESSION BY DESIGN. The three that ship an X desktop - Mint's
# Cinnamon, GhostBSD's MATE and MX's Xfce - plus FreeBSD, for the reason above.
ALLOW_X11 = {"freebsd", "mint-gui", "ghostbsd-mate", "mx-gui"}


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


def frame_is_painted(ppm: str) -> tuple:
    """Whether a framebuffer frame shows something rather than a blank screen.

    Returns (painted, description). THE TEST IS NARROW ON PURPOSE - it separates a screen
    that shows a desktop from one that shows nothing, and it does not try to tell one desktop
    from another:

        painted      the frame has more than 8 distinct colours AND at least 2% of its pixels
                     differ from the most common colour
        not painted  a single flat colour (a compositor that never drew), all black (the
                     guest is at a text console), or fewer than 2000 coloured pixels

    WHY NOT OCR, WHY NOT A WORD. This repository has already paid for that twice: a desktop
    whose panel reads as `Ppplications`, and a wizard whose panes are translated. A picture
    with structure in it is a picture with a desktop in it, whatever language it is in.

    WHY THE THRESHOLDS ARE LOOSE. Measured on this repository's own frames: NixOS' labwc drew
    a COMPLETELY black frame while working, and miracle-wm drew a flat backdrop - which is why
    lib/session.py does not use pixels for its own verdict. That is exactly why this is used
    only where no ssh session exists to ask: a desktop that paints nothing here is reported
    as unpainted rather than as broken, and the PNG is uploaded either way.
    """
    try:
        with open(ppm, "rb") as fh:
            data = fh.read()
    except OSError:
        return False, "no frame"
    if not data.startswith(b"P6"):
        return False, "not a P6 frame"
    fields, i = [], 2
    while len(fields) < 3:
        while i < len(data) and data[i:i + 1].isspace():
            i += 1
        start = i
        while i < len(data) and not data[i:i + 1].isspace():
            i += 1
        fields.append(int(data[start:i]))
    i += 1
    w, h, _ = fields
    px = data[i:]
    if len(px) < w * h * 3:
        return False, "truncated frame"

    # SAMPLED, NOT EVERY PIXEL. A 1920x1080 frame is 6 MB and this runs once per launch; a
    # grid of every 8th pixel is 32k samples, which is plenty to catch a flat colour.
    step = 8 * 3
    counts: dict = {}
    total = 0
    for o in range(0, w * h * 3 - 3, step):
        key = (px[o], px[o + 1], px[o + 2])
        counts[key] = counts.get(key, 0) + 1
        total += 1
    if not total:
        return False, "no samples"
    common = max(counts.values())
    distinct = len(counts)
    varying = total - common
    painted = distinct > 8 and varying > total * 0.02
    return painted, (f"{distinct} colours, {100 * varying / total:.1f}% not the most common"
                     f" ({common} of {total} samples)")


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
    p.add_argument("--desktop-settle", type=int, default=240,
                   help="how long a desktop image gets to paint before the frame is taken; "
                        "only used where there is no ssh session to ask")
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--ssh", default="required", choices=("required", "optional", "skip"),
                   help="what an unanswered ssh port means: `required` (a bake that enables "
                        "sshd), `optional` (try it, do not fail on it) or `skip` (do not ask - "
                        "the GUI images do not ship sshd yet, which is a later stage)")
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
        #
        # THE CONSOLE IS A SIGNAL, NOT A GATE, ON A GUI IMAGE. A desktop distribution's
        # bootloader hides its menu (`quiet splash`), its init prints nothing to ttyS0, and
        # its console log stays at zero bytes for the whole boot - measured on Zorin's disk,
        # which reached a working desktop with an empty console.log. Waiting for a marker
        # there spends the entire budget and then reports a failure on a correct disk.
        #
        # So on `--ssh skip` the console is polled for a SHORT while and then let go: if it
        # says something, that is logged and believed; if it says nothing, the desktop check
        # below is what decides. The answer-file bakes keep the strict wait, because their
        # installers do print to the console and a silent one there really is a broken boot.
        console_seconds = min(args.boot_timeout, 600) if args.ssh != "skip" else 60
        saw_console = wait_for_console(console_path, marker, console_seconds, note)
        if not saw_console:
            if args.ssh != "skip":
                note("  the disk did not reach userspace; the console is uploaded")
                take_screenshot(guest, artifacts, note)
                return finish(1)
            note(f"  the console said nothing in {console_seconds}s - expected for a desktop "
                 "image; the frame decides")

        # 2. SSH ANSWERS, WHERE THE DISK IS EXPECTED TO ANSWER.
        #
        # `--ssh skip` IS FOR THE GUI IMAGES, AND IT IS NOT A WEAKENING. A desktop installed
        # by clicking through an installer does not carry sshd - measured, no GUI driver here
        # installs `openssh-server`, and none of their images has it. Adding it after the fact
        # would change the disk being verified. So this stage asks the one question these
        # disks can answer - does the desktop come up - and a later stage installs sshd and
        # asks this one too.
        ssh_ok = False
        if args.ssh == "skip":
            note(f"ssh not attempted (--ssh skip): these images do not carry sshd")
        else:
            ssh_ok = session.wait_for_ssh(args.ssh_port, args.boot_timeout, note)
            if not ssh_ok:
                if args.ssh == "required":
                    note("  the system booted but never answered on ssh")
                    note("  sshd is on in every one of these disks, so this is the ssh config,")
                    note("  the account, or the network - not the boot")
                    take_screenshot(guest, artifacts, note)
                    return finish(1)
                note("  ssh did not answer (optional); continuing to the desktop check")

        # 3. THE DESKTOP SESSION, where one is expected.
        passed = True
        if compositor and not ssh_ok:
            # THE SESSION CHECK IS AN SSH CONVERSATION - `loginctl` run inside the guest - so
            # without a session there is nothing to read. The screenshot below is then the
            # evidence, which is what a desktop image without sshd leaves.
            note(f"no ssh session, so the {compositor!r} check cannot run; "
                 "the screenshot is the record")
            if args.ssh == "required":
                passed = False
        elif compositor:
            passed = session.wait_for_wayland(
                args.ssh_port, compositor, args.boot_timeout, note,
                ssh_seconds=60,      # already answered above
                allow_x11=kind in ALLOW_X11)
            if not passed:
                note("  the system is reachable and its desktop session is not running")
        else:
            note(f"no compositor expected for {kind}; ssh is the whole test")

        # GIVE THE DESKTOP TIME TO DRAW BEFORE THE FRAME IS TAKEN. `--desktop-settle` is how
        # long, and it exists because the alternative is a frame of the boot splash reported
        # as "no desktop". The answer-file bakes do not need it (they have ssh to ask), so the
        # default keeps their timing.
        settle = args.desktop_settle if (compositor and not ssh_ok) else 0
        if settle:
            log(f"waiting {settle}s for the desktop to draw")
            deadline = time.time() + settle
            while time.time() < deadline:
                try:
                    import qmp as qmpmod
                    qmpmod.move(guest.paths.drive, 400, 300)
                except Exception:                  # noqa: BLE001
                    pass
                time.sleep(20)

        png = take_screenshot(guest, artifacts, note)
        if png:
            if compositor and not ssh_ok:
                # NO SSH SESSION, SO THE FRAME IS THE VERDICT - the one place this script
                # judges pixels, and only where nothing else can be asked.
                painted, why = frame_is_painted(os.path.join(artifacts, "screen.ppm"))
                note(f"screenshot {png}: {why}")
                if painted:
                    note("  the frame shows a desktop")
                else:
                    note("  the frame shows no desktop: a flat or black screen")
                    passed = False
            else:
                note(f"screenshot {png} (for the record, not the verdict)")
        return finish(0 if passed else 1)
    finally:
        guest.stop()
        # AFTER guest.stop(), so the guest keeps its console through shutdown.
        ser.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
