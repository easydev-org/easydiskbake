"""Boot a published NixOS desktop disk and prove its Wayland session is running.

    python3 linux/nixos/launch.py --tag nixos-kde
    python3 linux/nixos/launch.py --disk out/nixos-labwc.qcow2 --desktop labwc

WHAT THIS ANSWERS, AND WHY THE BAKE CANNOT ANSWER IT

linux/nixos/derive.py reads DERIVE_RC from the serial console, which says nixos-rebuild
built a generation and made it the default. It does not say the generation starts, and
the three ways a derived disk has failed are all silent at build time:

    a display manager whose assertion passed but which cannot start
    a compositor that aborts with "Failed to find any DRM render node"
    a session that starts and paints nothing

So this boots the published artifact by itself and asks the running system whether its
Wayland session exists.

WHY SSH, AND WHY NOT THE SCREEN

The first version of this file judged a desktop by its pixels: a frame with colour in it
counted as "drawn". Two of the six desktops broke that test for reasons that have nothing
to do with whether they work:

    labwc    mean  0.0  chroma 0.0     the whole screen stayed black
    miracle  mean 50.7  chroma 8.0     a flat backdrop, no colour to measure

Both had in fact booted - the console showed `nixos login: docker (automatic login)` - and
a pixel test can never tell "this compositor does not draw on the emulated VGA adapter"
from "this compositor failed to start". The screen is evidence about the adapter, not
about the session.

So the verdict is the SESSION, read from inside the guest over ssh:

    a Wayland session exists in loginctl
    its compositor process is running
    that session is a Wayland one, not an X11 one

The screenshot is still taken and still uploaded - it is the thing a person looks at -
but it is an artifact, not the verdict.

WHY WAYLAND AND NOT "ANY SESSION"

Five of these six desktops are Wayland-only by construction (labwc, hyprland, miracle-wm,
cosmic and GNOME's default), and KDE on this configuration runs a Wayland session too.
Accepting an X11 session would pass a disk that had silently fallen back to something the
configuration does not ask for, which is exactly the kind of "it started" that is not the
thing being tested. `loginctl` reports the type as `wayland` or `x11`, so the check is a
string comparison rather than an inference.

HOW SSH GETS IN

lib/vm.py forwards host:2222 to guest:22 on every guest it builds, and
configuration-minimal.nix enables sshd with password authentication for the account
docker/admin. Nothing here needs a key.

sshpass is used because the password is known and there is no terminal to type it at; it
is in the bake image for the Windows and macOS dev stages, and the workflow asserts it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import lastframe  # noqa: E402
import live as livelib  # noqa: E402
import serial  # noqa: E402
import session  # noqa: E402
import vm  # noqa: E402

# THE SIX DESKTOPS derive.py CAN BUILD, and the only names this accepts. A tag is
# nixos-<desktop>, so the name is also how a caller names the artifact.
DESKTOPS = ["labwc", "gnome", "kde", "cosmic", "hyprland", "miracle"]

# THE PROCESS NAME THAT PROVES EACH SESSION IS UP. These are not guesses: they are the
# binaries the modules in configuration-desktop.nix install and run.
#
#   labwc        programs.labwc / services.greetd's exec ${pkgs.labwc}/bin/labwc
#   gnome        gnome-shell, the compositor gdm starts for the GNOME session
#   kde          kwin_wayland (NOT plasmashell - kwin is what owns the session's display
#                server, and it is the process that fails when there is no DRM node)
#   cosmic       cosmic-comp, the COSMIC compositor
#   hyprland     Hyprland, whose binary is capitalised
#   miracle      miracle-wm
#
# pgrep -f, not pgrep -x: Hyprland's binary is `Hyprland`, kwin's is `kwin_wayland`, and a
# full command line match is what catches a session started by a wrapper script.
SESSION_PROCESS = {
    "labwc": "labwc",
    "gnome": "gnome-shell",
    "kde": "kwin_wayland",
    "cosmic": "cosmic-comp",
    "hyprland": "Hyprland",
    "miracle": "miracle-wm",
}

# WHAT PROVES THE SYSTEM BOOTED, independently of what it draws. NixOS prints this on
# ttyS0 once the kernel has mounted root and handed over to userspace. Every one of these
# disks carries boot.kernelParams = [ "console=ttyS0,115200n8" ] from
# configuration-minimal.nix, so the console is not a thing to detect - it is guaranteed by
# the configuration this bake wrote.
BOOTED_MARKER = "Welcome to NixOS"

# THE ACCOUNT AND THE SSH CONVERSATION LIVE IN lib/session.py, because Ubuntu and Kubuntu
# judge their second boot the same way. The password is the one every image here uses.


def log(message: str) -> None:
    print(f"[nixos-launch {time.strftime('%H:%M:%S')}] {message}", flush=True)


def read_ppm(path: str) -> tuple[int, int, bytes] | None:
    """The pixels of a binary PPM, or None if it cannot be read.

    Kept because the screenshot is still taken - it is just no longer the verdict. Same
    shape as lib/desktop.read_ppm: one read, one pass over the bytes, no image library.
    """
    try:
        with open(path, "rb") as fh:
            if fh.readline().strip() != b"P6":
                return None
            line = fh.readline()
            while line.startswith(b"#"):
                line = fh.readline()
            try:
                width, height = (int(v) for v in line.split())
            except ValueError:
                return None
            fh.readline()
            return width, height, fh.read(width * height * 3)
    except OSError:
        return None


def screen_mean(path: str) -> float | None:
    """Mean grey of a frame, for the record only.

    NOT A VERDICT. It is written into launch.txt so a reader can tell at a glance whether
    the screenshot is a desktop, a greeter or a black screen - but a black screen is a
    valid PASS if the session is running, because two of these six compositors do not draw
    on the emulated adapter at all.
    """
    shot = read_ppm(path)
    if shot is None:
        return None
    width, height, pixels = shot
    total = 0
    count = 0
    for index in range(0, len(pixels), 97 * 3):
        total += pixels[index] + pixels[index + 1] + pixels[index + 2]
        count += 1
    return total / (count * 3) if count else None


def wait_for_console(console: str, marker: str, seconds: int, note) -> bool:
    """Wait until `marker` appears in the guest's console log.

    Read from the file rather than from a stream, because the guest talks before this is
    called: qemu starts, GRUB counts down and the kernel prints while the caller is still
    setting up. A tail on a growing file is the way to see all of it, not a listener that
    missed the beginning.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with open(console, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            text = ""
        if marker in text:
            note(f"the console says {marker!r}")
            return True
        time.sleep(5)
    note(f"=== THE CONSOLE NEVER SAID {marker!r} IN {seconds}s ===")
    return False


def take_screenshot(guest, artifacts: str, note) -> str:
    """One full-size screendump, converted to PNG. Returns the path or "".

    ALWAYS TAKEN, PASS OR FAIL. It is the artifact a person looks at; the verdict is the
    session, but a picture is what turns "the session is running" into something reviewable
    and what explains a failure at a glance.

    SETTLES FIRST, BECAUSE A SESSION THAT EXISTS HAS NOT NECESSARILY DRAWN YET.

    The verdict is read over ssh the moment logind reports a session, and that happens
    before the compositor has painted anything - GNOME registers its session and then spends
    several seconds drawing. Run 41650668823's screenshot for gnome is the kernel's boot
    log, not a desktop: the check passed at 09:56 and the picture was taken in the same
    second.

    Five seconds is enough for every one of these six; the ones that need longer (COSMIC on
    a cold profile) already spend it starting, and this only moves the camera later. A
    screendump of a half-drawn desktop is worse than one taken a minute late, because the
    former looks like a failure.
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

    mean = screen_mean(shot)
    if mean is not None:
        note(f"  the screenshot's mean grey is {mean:.1f} (for the record, not the verdict)")
    try:
        lastframe.convert(shot, png)
    except (OSError, subprocess.SubprocessError) as exc:
        note(f"  could not convert the screenshot: {exc}")
        return ""
    return png


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tag", default=None,
                   help="a tag in this repository's package, e.g. nixos-kde")
    p.add_argument("--disk", default=None,
                   help="a local qcow2 instead of a tag (the tag is then only for logs)")
    p.add_argument("--desktop", default=None, choices=DESKTOPS,
                   help="which desktop this disk should start; inferred from --tag")
    p.add_argument("--artifacts", default="out",
                   help="where the recording, frames and verdict are written")
    p.add_argument("--work", default=None,
                   help="qemu's own directory (defaults to --artifacts)")
    p.add_argument("--boot-timeout", type=int, default=900,
                   help="seconds to wait for the Wayland session")
    p.add_argument("--ssh-port", type=int, default=2222,
                   help="host port forwarded to the guest's 22 (lib/vm.py's default)")
    p.add_argument("--mem", type=int, default=6144)
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

    desktop = args.desktop
    if not desktop and args.tag:
        candidate = args.tag.split("-", 1)[-1]
        if candidate in DESKTOPS:
            desktop = candidate
    if not desktop:
        print(f"=== CANNOT TELL WHICH DESKTOP THIS IS ===\n"
              f"  --tag is {args.tag!r} and no --desktop was given. It has to be one of "
              f"{', '.join(DESKTOPS)}.", flush=True)
        return 2

    width, height = (int(v) for v in args.resolution.lower().split("x", 1))
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
        note(f"VERDICT {'PASS' if code == 0 else 'FAIL'} desktop={desktop} "
             f"tag={args.tag or args.disk}")
        return code

    disk = args.disk
    if not disk:
        disk = os.path.join(work, f"nixos-{desktop}.qcow2")
        if not os.path.exists(disk):
            note(f"pulling {args.tag} from ghcr")
            import iso  # imported here: only the tag path needs it
            try:
                disk = iso.fetch_tag(args.tag, disk)
            except iso.IsoError as exc:
                note(f"=== {args.tag} IS NOT PUBLISHED: {exc}")
                note("  run linux-nixos-bake for this desktop first")
                return finish(2)
    if not os.path.exists(disk):
        note(f"=== NO DISK AT {disk} ===")
        return finish(2)
    note(f"disk {disk} ({os.path.getsize(disk) >> 20} MiB)")

    console_path = os.path.join(artifacts, "console.log")
    ser = serial.Serial(work, "serial", sink=console_path)
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        vga="std", serial_pipe=ser.stdin.removesuffix(".in"),
        resolution=(width, height),
        ssh_port=args.ssh_port,
    )
    drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)

    shots = os.path.join(work, "frames")
    os.makedirs(shots, exist_ok=True)
    snap = None
    live = None
    try:
        guest.start()
        ser.open(timeout=60)

        snap = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
             guest.paths.record, shots,
             os.environ.get("SNAPSHOT_INTERVAL", "5"), "2000"],
            stdout=open(os.path.join(shots, "snapshots.log"), "wb"),
            stderr=subprocess.STDOUT,
        )
        live = livelib.start(
            [shots], os.path.join(artifacts, "install.mp4"),
            os.path.join(artifacts, "last-frame.png"),
            every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "20")),
        )

        # THE SYSTEM BOOTED, THEN SSH, THEN THE SESSION. Each is a precondition of the
        # next, and stopping at the first that fails says which layer is broken:
        #   no BOOTED_MARKER   the disk does not start
        #   no ssh             the system started and its network or sshd did not
        #   no wayland session the system is reachable and its desktop did not come up
        boot_seconds = min(args.boot_timeout, 600)
        if not wait_for_console(console_path, BOOTED_MARKER, boot_seconds, note):
            note("  the disk did not reach userspace; the console log is uploaded")
            take_screenshot(guest, artifacts, note)
            return finish(1)

        if not session.wait_for_ssh(args.ssh_port, min(args.boot_timeout, 600), note):
            note("  the system booted but never answered on ssh")
            take_screenshot(guest, artifacts, note)
            return finish(1)

        deadline = time.time() + args.boot_timeout
        started = time.time()
        passed = False
        while time.time() < deadline:
            if not guest.alive():
                note("qemu exited while waiting for the session")
                break
            if session.check_wayland_session(args.ssh_port, SESSION_PROCESS[desktop], note):
                note(f"the {desktop} Wayland session is up after "
                     f"{time.time() - started:.0f}s")
                passed = True
                break
            note(f"  {time.time() - started:.0f}s: no session yet")
            time.sleep(20)

        # THE PICTURE IS AN ARTIFACT, NOT THE VERDICT. Taken either way.
        png = take_screenshot(guest, artifacts, note)
        if png:
            note(f"screenshot {png}")
        return finish(0 if passed else 1)
    finally:
        guest.stop()
        ser.close()
        if snap is not None:
            snap.terminate()
            try:
                snap.wait(timeout=5)
            except subprocess.TimeoutExpired:
                snap.kill()
        if live is not None:
            live.stop(final=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
