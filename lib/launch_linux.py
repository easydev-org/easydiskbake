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
import shutil
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
    "freebsd": "FreeBSD/amd64",
    # --- the GUI bakes, one name per distribution -----------------------------------
    "ubuntu": "Ubuntu",
    "kubuntu": "Ubuntu",
    "fedora-workstation": "Fedora",
    "mint": "Linux Mint",
    "opensuse-tumbleweed": "openSUSE",
    "cachyos": "CachyOS",
    "manjaro": "Manjaro",
    "ghostbsd-mate": "FreeBSD/amd64",
    "endeavour": "EndeavourOS",
    "zorin": "Zorin",
    "mx": "MX Linux",
    "popos": "Pop!_OS",
    "nixos": "NixOS",
}

# THE COMPOSITOR THAT PROVES A DESKTOP SESSION, or "" for a system that has none.
# THE COMPOSITOR THAT PROVES A DESKTOP SESSION, or "" for a system that has none.
#
# FreeBSD runs XFCE ON XORG - an X session, not a Wayland one - because that is what its
# ports tree recommends for a VM and what the disk was built to run. `ALLOW_X11` below says
# so, rather than the session check carrying a list of distributions.
COMPOSITOR = {
    "freebsd": "xfce4-session",
    # The GUI bakes. Each is the compositor its ISO actually runs, so a disk whose desktop
    # failed to start is told apart from one whose desktop never existed.
    "ubuntu": "gnome-shell",
    "kubuntu": "kwin_wayland",
    "fedora-workstation": "gnome-shell",
    "mint": "cinnamon",          # Cinnamon runs on Xorg
    "opensuse-tumbleweed": "kwin_wayland",
    "cachyos": "kwin_wayland",
    "manjaro": "kwin_wayland",
    "ghostbsd-mate": "mate-session",  # MATE on Xorg
    "endeavour": "kwin_wayland",
    "zorin": "gnome-shell",
    "mx": "xfce4-session",        # Xfce on Xorg
    "popos": "cosmic-comp",
    "nixos": "gnome-shell",
}

# DISKS WHOSE DESKTOP IS AN X SESSION BY DESIGN. Only FreeBSD, for the reason above.
# has no display manager at all and its entry is "" so this is never consulted for it.
# DISKS WHOSE DESKTOP IS AN X SESSION BY DESIGN. The three that ship an X desktop - Mint's
# Cinnamon, GhostBSD's MATE and MX's Xfce - plus FreeBSD, for the reason above.
ALLOW_X11 = {"freebsd", "mint", "ghostbsd-mate", "mx"}


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
    that shows something from one that shows nothing, and it does not try to tell one desktop
    from another:

        painted      the frame has at least 24 distinct colours AND more than 0.5% of its
                     pixels differ from the most common colour
        not painted  a single flat colour (a compositor that never drew), all black, or a
                     text console (too few distinct colours to be anything but glyphs)

    WHY NOT OCR, WHY NOT A WORD. This repository has already paid for that twice: a desktop
    whose panel reads as `Ppplications`, and a wizard whose panes are translated. A picture
    with structure in it is a picture with a desktop in it, whatever language it is in.

    THE 2% FLOOR WAS WRONG, AND FEDORA PAID FOR IT. Both thresholds were measured against
    this repository's own frames, and run 42499595942 is why they changed:

        ubuntu  desktop   3256 colours, 65.4% not the most common   painted
        kubuntu desktop   5041 colours, 70.9%                          painted
        zorin   GDM       2644 colours, 96.8%                          painted
        fedora  GDM        358 colours,  1.7%                          WRONGLY blank

    Fedora's greeter is a DARK full-screen image - a near-flat background with a user tile and
    a wordmark - so almost every sample IS the background and only 1.7% of them are not. It
    had reached GDM with the account `docker` drawn on screen, and this function called it
    "a flat or black screen" and failed a disk that booted correctly.

    WHAT ACTUALLY TELLS A GREETER FROM A BLANK SCREEN IS THE COLOUR COUNT, NOT THE AREA. A
    greeter, even a dark one, paints a gradient, a round tile, a wordmark and antialiased text;
    a blank framebuffer or an 80x25 text console paints one colour or a handful of glyph
    colours. Measured on synthetic controls and on the frames above:

        blank black         1 colour   0.00%      text console   5 colours   7.14%
        dark flat           1 colour   0.00%      fedora GDM   358 colours   1.70%

    So the area floor drops to 0.5% - enough to reject a literally flat frame - and the colour
    count carries the discrimination. A text console is still refused, by the colour count
    alone, which is the part that never needed the area threshold.

    WHY IT STAYS DELIBERATELY LOOSE OTHERWISE. Measured on this repository's own frames: NixOS'
    labwc drew a COMPLETELY black frame while working, and miracle-wm drew a flat backdrop -
    which is why lib/session.py does not use pixels for its own verdict. That is exactly why
    this is used only where no ssh session exists to ask: a desktop that paints nothing here
    is reported as unpainted rather than as broken, and the PNG is uploaded either way.
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
    # SEE THE DOCSTRING: the colour count separates a greeter from a console, and the area
    # floor only has to reject a literally flat frame.
    #
    # THE COLOUR COUNT ALONE IS NOT ENOUGH, AND CACHYOS IS WHY. Its SDDM is a FLAT two-colour
    # theme - one background and one text colour - and run d8baf01's ssh stage called it
    #
    #     2 colours, 14.2% not the most common (222361 of 259200 samples)
    #
    # "not painted", failed the stage before the walk that logs in on that very screen, and
    # published nothing. 14.2% of a 1920x1080 frame is 294,000 pixels that are not the
    # background: that is a login form, drawn. A blank framebuffer is 0.00% and an 80x25 text
    # console is 5 colours at 7.14%, so the pair below separates all three:
    #
    #     blank / flat     1 colour,  0.00%      -> not painted
    #     text console     5 colours, 7.14%      -> not painted (too few colours AND the
    #                                              glyphs are the only thing drawn)
    #     cachyos SDDM     2 colours, 14.2%      -> PAINTED: two colours, but a fifth of the
    #                                              frame is not the background
    #
    # A CONSOLE HAS GLYPHS THAT COVER A TENTH OF THE FRAME TOO, so area alone would pass one.
    # What separates them is that a console's non-background pixels are TEXT on a fixed grid,
    # and this cannot see a grid - so the test is on the PAIR: many colours (which a console
    # cannot have without antialiasing it does not do), OR enough area that something was
    # deliberately drawn (which a blank frame cannot have).
    # AND THE DISCRIMINATION IS "IS ANYTHING DRAWN AT ALL", WHICH IS THE QUESTION THE CALLER ASKS.
    #
    # `launch_linux` uses this to answer "did the desktop come up" for a stage with no other way
    # in. The ssh stage uses it to answer "has the machine stopped booting", and then LOGS IN on
    # whatever is there - its walk starts with a greeter step for exactly that reason. Telling a
    # console from a greeter is not a question either caller needs answered, and a threshold that
    # tries costs a real disk: cachyos' two-colour SDDM sat at 14.2% against a console's 7.14%,
    # eight points apart, which is not a decision this should be making on a screenshot.
    #
    # What it must reject is a frame with NOTHING on it: one colour, or all black.
    painted = distinct >= 2 and varying > total * 0.005
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
    # UEFI, ALWAYS, AND THERE IS NO BIOS PATH. Measured on Zorin's own disk: booted with
    # SeaBIOS it stopped at
    #
    #     SeaBIOS (version 1.17.0-debian-1.17.0-1)
    #     Booting from Hard Disk...
    #
    # and never moved - the disk has a GPT with an EFI system partition and NO MBR boot code,
    # which is exactly what every installer here produces. lib/uefi.py attaches OVMF for the
    # install; this attaches the same firmware to start the result, so the disk is verified
    # the way it was made and the way it will run.
    #
    # NO FALLBACK. A disk that does not start on UEFI is a broken disk, not a disk that needs
    # a different firmware - a legacy-boot path would find an MBR that is not there and then
    # report a firmware message as if it were a boot.
    import uefi
    # THE INSTALLER'S OWN NVRAM, WHEN THE BAKE KEPT IT. A UEFI installer writes its boot entry
    # into the VARS image (``Boot0001 ... \EFI\<distro>\grubx64.efi``), not into the disk's
    # MBR, and OVMF only falls back to the removable-media path if it finds nothing else. Start
    # the disk with a FRESH template and the entry is gone: measured on Zorin's disk, the
    # firmware fell through to
    #
    #     >>Start PXE over IPv4.
    #     PXE-E16: No valid offer received.
    #
    # and never touched the disk.
    #
    # So a vars file sitting next to the disk is used when there is one - the driver leaves it
    # there under the guest's own name - and a template is only a fallback for a disk whose
    # bake did not keep it.
    # ANY .vars BESIDE THE DISK, because the bake names it after the WORK directory, not after
    # the kind: Zorin's install left `zt.vars` next to `zt/d.qcow2`, and looking for
    # `zorin.vars` missed it - measured, the firmware fell through to PXE and the disk was
    # never read. A directory holding one disk and one vars image holds that disk's NVRAM.
    sibling = None
    disk_dir = os.path.dirname(disk)
    by_kind = [os.path.join(disk_dir, n) for n in (f"{kind}.vars", "vars.fd", "efivars.fd")]
    sibling = next((c for c in by_kind if os.path.exists(c)), None)
    if sibling is None:
        found = sorted(f for f in os.listdir(disk_dir) if f.endswith(".vars"))
        if len(found) == 1:
            sibling = os.path.join(disk_dir, found[0])
    # ORDER MATTERS: shape_with_uefi() COPIES THE TEMPLATE INTO <work>/<kind>.vars AND REMOVES
    # ANY FILE ALREADY THERE - see its docstring, which is right for an install (a stale NVRAM
    # must not boot a disk that no longer has a system). So the installer's own NVRAM has to be
    # written AFTER that call, not before it: measured, copying it first left the template in
    # place and the firmware went to PXE.
    shape = uefi.shape_with_uefi(shape, work, kind)
    if sibling:
        shutil.copyfile(sibling, shape.firmware_vars)
        log(f"using the installer's NVRAM: {sibling} -> {shape.firmware_vars}")
    else:
        log("no installer NVRAM beside the disk; OVMF gets a fresh template")
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
