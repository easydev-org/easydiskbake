"""Boot a published NixOS desktop disk and photograph what it draws.

    python3 linux/nixos/launch.py --tag nixos-kde --out out/nixos-kde-launch
    python3 linux/nixos/launch.py --disk out/nixos-kde.qcow2 --desktop kde

WHAT THIS ANSWERS, AND WHY THE BAKE CANNOT ANSWER IT

linux/nixos/derive.py rebuilds a disk and reads DERIVE_RC from the serial console. That
says nixos-rebuild succeeded: a generation was built and made the default. It does NOT
say the generation can start - a display manager whose assertion passes at build time can
still fail to come up, a compositor can abort on a missing DRM node, and a session can
start and paint nothing. Those are the three ways a derived nixos disk has actually
failed in easysandbox-vm, and all three exit nixos-rebuild cleanly.

So this boots the artifact by itself, from the same published tag a deployment would
pull, and records what appears.

THE TWO EVIDENCE STREAMS, AND WHY BOTH

    the screen   every desktop here ends on a framebuffer, and the recording is what a
                 person looks at to decide whether "it started" is true
    the serial   NixOS prints to console=ttyS0 on every one of these disks. A guest whose
                 display manager died still reaches a login prompt there, which is the
                 difference between "booted but no session" and "did not boot"

Neither alone is enough. A frame of a black screen is also what a guest looks like while
gdm is still starting, and a login prompt is also what a desktop that failed to start
leaves behind. The verdict below requires the screen to have LIT UP - not to match any
particular wallpaper, because six desktops do not share one.

WHY NOT lib/desktop.py

That module's test is Windows-shaped: a light upper half, a dark lower half with a
taskbar and icons, measured on Windows 11's wallpaper. One of these six is GNOME, one is
Plasma, one is COSMIC, and three are bare compositors whose whole screen is a wallpaper
and nothing else. A gradient-and-taskbar test would call a working labwc session a
failure. What IS common to all six is that something drew: the frame is not black and not
the firmware's text console.

WHAT A LAUNCH IS EXPECTED TO LEAVE BEHIND

    out/nixos-<desktop>-launch/install.mp4     the recording, kept current while it runs
    out/nixos-<desktop>-launch/last-frame.png  the newest frame, for a cancelled run
    out/nixos-<desktop>-launch/screen-*.png    samples, named by the second
    out/nixos-<desktop>-launch/launch.txt      the verdict, one line
    out/nixos-<desktop>-launch/console.log     the guest's serial console
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
import vm  # noqa: E402

# THE SIX DESKTOPS derive.py CAN BUILD, and the only names this accepts. A tag is
# nixos-<desktop>, so the name is also how a caller names the artifact.
DESKTOPS = ["labwc", "gnome", "kde", "cosmic", "hyprland", "miracle"]

# WHAT PROVES THE SYSTEM BOOTED, independently of what it draws. NixOS prints this on
# ttyS0 once the kernel has mounted root and handed over to userspace. Every one of these
# disks carries boot.kernelParams = [ "console=ttyS0,115200n8" ] from
# configuration-minimal.nix, so the console is not a thing to detect - it is guaranteed by
# the configuration this bake wrote.
BOOTED_MARKER = "Welcome to NixOS"

# A DESKTOP THAT DREW IS A FRAME WITH COLOUR IN IT. The floor is deliberately the same
# value lib/install.py uses for "nothing is drawn there" (NOTHING_DRAWN = 8) but a little
# higher: the firmware's own text console is readable at a mean grey of 10-15 and that is
# exactly the state this must NOT accept as a desktop. A wallpaper, a greeter or a
# compositor all measure far above this; the darkest of the six is a labwc session with
# the default background, which is a grey-blue that measures in the thirties.
DRAWN_MEAN = 24

# AND IT HAS TO BE COLOUR, NOT JUST LIGHT. A console full of text is white-on-black: all
# three channels move together. A desktop has a wallpaper, and the mean spread between the
# channels is what separates the two without knowing which desktop it is.
#
# THE NUMBERS ARE MEASURED, NOT CHOSEN. Runs of frames from this repository, plus flat
# colours standing in for each of the six wallpapers:
#
#     a real Windows 11 desktop       mean 152.3  chroma 106.9   <- drawn
#     a KDE blue wallpaper            mean  80.3  chroma 103.0   <- drawn
#     a GNOME purple wallpaper        mean  83.0  chroma  48.0   <- drawn
#     labwc's default grey-blue       mean  58.3  chroma  22.0   <- drawn
#     macOS's dark installer panes    mean  26.5  chroma   1.6   <- NOT drawn
#     Disk Utility                    mean  26.1  chroma   0.6   <- NOT drawn
#     a plain grey greeter            mean  60.0  chroma   0.0   <- NOT drawn
#     a white-on-black text console   mean 230.0  chroma   0.0   <- NOT drawn
#
# The two groups do not overlap on chroma: every drawn frame is above 20 and every
# not-drawn one below 3. Twelve is the middle of that gap, and it is the test that
# matters - a bright console scores a MEAN of 230 and would pass a brightness test alone.
#
# WHY A GREY GREETER IS A FAILURE AND NOT A DESKTOP. It means the session did not start,
# and that is the thing this stage exists to catch. The old measure in easysandbox-vm was
# a screenshot the smoke test rejected if it was blank; this is that idea with the
# brightness test made explicit.
DRAWN_CHROMA = 12


def log(message: str) -> None:
    print(f"[nixos-launch {time.strftime('%H:%M:%S')}] {message}", flush=True)


def read_ppm(path: str) -> tuple[int, int, bytes] | None:
    """The pixels of a binary PPM, or None if it cannot be read.

    Same shape as lib/desktop.read_ppm and lib/install.py's capture(), and for the same
    reason: a screendump goes through a file the emulator wrote, so this is one read and
    one pass over the bytes - no image library, and no interpretation of what is drawn.
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


def screen_stats(path: str) -> dict | None:
    """Mean grey and mean channel spread of a frame.

    SAMPLED, NOT COMPLETE. Every 97th pixel, which is the stride lib/install.py already
    uses for the same question, and it is far finer than "did a desktop appear" needs.
    """
    shot = read_ppm(path)
    if shot is None:
        return None
    width, height, pixels = shot
    total = 0
    spread = 0
    count = 0
    for index in range(0, len(pixels), 97 * 3):
        r, g, b = pixels[index], pixels[index + 1], pixels[index + 2]
        total += r + g + b
        spread += max(r, g, b) - min(r, g, b)
        count += 1
    if not count:
        return None
    return {
        "width": width,
        "height": height,
        "mean": total / (count * 3),
        "chroma": spread / count,
    }


def is_desktop(path: str) -> tuple[bool, str]:
    """Whether this frame shows something drawn, and the sentence why.

    A tuple rather than a bool because the log is read to decide what to do next, and
    "mean 12.4, chroma 1.8" says which of the two tests failed.
    """
    stats = screen_stats(path)
    if stats is None:
        return False, "the frame could not be read"
    drawn = stats["mean"] >= DRAWN_MEAN and stats["chroma"] >= DRAWN_CHROMA
    return drawn, (f"mean {stats['mean']:.1f} chroma {stats['chroma']:.1f} "
                   f"at {stats['width']}x{stats['height']}")


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


def sample_until(guest, work: str, timeout: int, note, seen: list) -> bool:
    """Photograph the screen every few seconds until one of them is a desktop.

    PNG, not PPM, because these are uploaded and a PPM is 6 MiB each. lib/lastframe.py
    converts a screendump the same way for the same reason.

    The recording is kept current by lib/live.py independently of this loop, so a run
    that is cancelled mid-wait still has an mp4 and a last frame.

    ONE QMP CONNECTION, OPENED HERE AND CLOSED WITH THE GUEST. lib/vm.py does not hand
    out a client - it only uses one internally for the ACPI power button - so the
    screendump goes through a client of this function's own, exactly as
    macos/15/install.py's Recovery class does.
    """
    import qmp as qmpmod

    deadline = time.time() + timeout
    started = time.time()
    interval = 10
    shot = os.path.join(work, "probe.ppm")
    client = qmpmod.QMP(guest.paths.drive)
    try:
        while time.time() < deadline:
            if not guest.alive():
                note("qemu exited while waiting for a desktop")
                return False
            try:
                client.execute("screendump", filename=shot)
            except (OSError, RuntimeError) as exc:
                note(f"the screendump failed: {exc}")
                time.sleep(interval)
                continue

            elapsed = int(time.time() - started)
            png = os.path.join(work, f"screen-{elapsed:04d}.png")
            try:
                lastframe.convert(shot, png)
            except (OSError, subprocess.SubprocessError):
                png = ""

            drawn, why = is_desktop(shot)
            if drawn:
                note(f"a desktop is on screen after {elapsed}s ({why})")
                seen.append((elapsed, png, why))
                return True
            if elapsed % 60 < interval:
                note(f"  {elapsed}s: nothing drawn yet ({why})")
            if png and elapsed % 30 < interval:
                seen.append((elapsed, png, why))
            time.sleep(interval)

        note(f"=== NO DESKTOP APPEARED IN {timeout}s ===")
        return False
    finally:
        client.close()


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
                   help="seconds to wait for a desktop")
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
              f"  -f tag is {args.tag!r} and no --desktop was given. It has to be one of "
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

    # A LOCAL DISK IS THE CALLER'S; a tag is pulled here so the artifact under test is the
    # published one rather than a file left on a runner.
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

    # THE CONSOLE IS A CHARDDEV ON A FIFO, as in derive.py, because NixOS talks on ttyS0
    # and that is the only evidence a guest which draws nothing still gives off.
    console_path = os.path.join(artifacts, "console.log")
    ser = serial.Serial(work, "serial", sink=console_path)
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        vga="std", serial_pipe=ser.stdin.removesuffix(".in"),
        resolution=(width, height),
    )
    drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)

    shots = os.path.join(work, "frames")
    os.makedirs(shots, exist_ok=True)
    seen: list = []
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

        # THE SYSTEM BOOTED, THEN THE DESKTOP DREW. Both, in that order, because a
        # desktop cannot be on screen before the kernel has handed over - and if the
        # marker never arrives, waiting the full boot budget for a picture that cannot
        # come is nine minutes of nothing.
        boot_seconds = min(args.boot_timeout, 600)
        if not wait_for_console(console_path, BOOTED_MARKER, boot_seconds, note):
            note("  the disk did not reach userspace; the console log is uploaded")
            return finish(1)

        if not sample_until(guest, artifacts, args.boot_timeout, note, seen):
            # SAY WHICH FAILURE THIS IS. A guest that booted and drew nothing is a
            # display-manager problem; one that never booted is a disk problem, and the
            # console marker above has already told the two apart.
            note("  the system booted but no desktop was drawn")
            note("  the console log shows what the display manager did")
            return finish(1)

        note(f"desktop {desktop} started")
        return finish(0)
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
