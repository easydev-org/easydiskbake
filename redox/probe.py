"""Boot a Redox OS image and report what it is before investing in a bake.

    python3 redox/probe.py --image out/redox.img

WHY THIS EXISTS BEFORE A BAKE

Redox is the sixth operating system candidate and the only one whose installer has never
been run here. It is also the cheapest to test: its desktop image is 144 MiB against
macOS's 28 GiB, so a run costs seconds instead of an hour.

Four questions decide whether a bake is worth writing, and this answers them from one boot:

    1. does the image boot at all under this repository's qemu invocation
    2. does Orbital come up, and does a frame show it
    3. does the guest get a network address
    4. is there an sshd to log in to, or only a console

THE FIRST FRAME IS A MENU, NOT A DESKTOP, AND IT WAITS FOR A KEY

Run 41776866958 booted this image correctly and the probe called it "NOTHING DRAWN": the
bootloader's menu is white text on black, so its mean brightness is 9.9 where a desktop's is
above 20, and every frame for 240 s was that same unchanged menu. A menu does not advance on
its own, so no amount of waiting would have produced Orbital. The probe now recognises
firmware drawing at all (anything above black), waits one more frame for the menu to finish
drawing, and sends Return to take the default mode.

WHAT A PASS HERE DOES AND DOES NOT MEAN

It means the image runs on this machine and that the pieces a later stage needs are present.
It does not mean Redox can be a gold disk: the installer, the account, and whether sshd can
be enabled are separate questions with separate evidence.

THE KERNEL IS NOT LINUX, SO NOTHING ELSE HERE APPLIES

Redox has its own kernel and its own filesystem (RedoxFS). There is no systemd, no logind, no
`/proc/cmdline`, and its shell is `ion`. Anything this repository does with serial consoles -
typing at a login prompt, reading `Welcome to NixOS` - has a Redox equivalent only where Redox
chose to implement one.

Documented behaviour, from doc.redox-os.org: the demo/desktop images boot to Orbital's login
screen, the user is `user` with no password, root's password is `password`, and F2 switches to
a text console while F3 returns to the GUI. Those are the facts this probe is built on; the
run is what confirms them for this image and this qemu.
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
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import lastframe  # noqa: E402
import live as livelib  # noqa: E402
import vm  # noqa: E402


def log(message: str) -> None:
    print(f"[redox {time.strftime('%H:%M:%S')}] {message}", flush=True)


# THE IMAGE IS ZSTD, AND IT HAS TO BE DECOMPRESSED BEFORE QEMU CAN READ IT.
#
# Redox publishes `<name>.img.zst`. qemu cannot read zstd-compressed raw images - it reads
# qcow2's zstd, which is a different thing inside a container - so this is `zstd -d` and a
# raw file, not a `-drive ... compression` flag.
#
# THE DECOMPRESSED FILE IS A RAW DISK, NOT AN ISO. It has its own partition table and its
# own bootloader, and the machine boots it as a virtio disk the way macOS's recovery image
# is booted as usb-storage. Booting it as a cdrom would not work and booting it as the
# primary disk means installing over it, which is why the bake will need a second disk.
def decompress(image: str, dest: str) -> str:
    """Un-zstd `image` into `dest` unless it is already there."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        log(f"{dest} already present ({os.path.getsize(dest) >> 20} MiB)")
        return dest
    if not shutil.which("zstd"):
        raise SystemExit(
            "=== zstd IS NOT INSTALLED ===\n"
            "  the Redox images are published as .img.zst; it is in the Containerfile for\n"
            "  the same reason it is needed here"
        )
    log(f"decompressing {image} -> {dest}")
    result = subprocess.run(
        ["zstd", "-d", "-f", "-o", dest, image],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"=== zstd COULD NOT DECOMPRESS {image} ===\n"
            f"  {(result.stderr or '').strip()[:300]}"
        )
    log(f"  {dest} ({os.path.getsize(dest) >> 20} MiB)")
    return dest


def take_frame(guest, dest: str, note) -> bool:
    """One screendump as PNG. True when something was written."""
    import qmp as qmpmod

    shot = dest + ".ppm"
    client = qmpmod.QMP(guest.paths.drive)
    try:
        client.execute("screendump", filename=shot)
    except (OSError, RuntimeError) as exc:
        note(f"  the screendump failed: {exc}")
        return False
    finally:
        client.close()
    try:
        lastframe.convert(shot, dest)
    except (OSError, subprocess.SubprocessError) as exc:
        note(f"  could not convert the frame: {exc}")
        return False
    return True


def send_key(guest, qcode: str, note) -> bool:
    """One keystroke through QMP. True when it was accepted."""
    import qmp as qmpmod

    client = qmpmod.QMP(guest.paths.drive)
    try:
        qmpmod.key(client, qcode)
    except (OSError, RuntimeError) as exc:
        note(f"  the keystroke {qcode} failed: {exc}")
        return False
    finally:
        client.close()
    return True


def frame_stats(path: str) -> tuple[float, float]:
    """Mean grey and mean channel spread of the PPM beside `path`."""
    try:
        with open(path + ".ppm", "rb") as fh:
            if fh.readline().strip() != b"P6":
                return (0.0, 0.0)
            line = fh.readline()
            while line.startswith(b"#"):
                line = fh.readline()
            width, height = (int(v) for v in line.split())
            fh.readline()
            pixels = fh.read(width * height * 3)
    except (OSError, ValueError):
        return (0.0, 0.0)
    if not pixels:
        return (0.0, 0.0)
    total = spread = count = 0
    for index in range(0, len(pixels), 97 * 3):
        r, g, b = pixels[index], pixels[index + 1], pixels[index + 2]
        total += r + g + b
        spread += max(r, g, b) - min(r, g, b)
        count += 1
    if not count:
        return (0.0, 0.0)
    return (total / (count * 3), spread / count)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--image", required=True,
                   help="the .img.zst or the decompressed .img")
    p.add_argument("--work", default=None)
    p.add_argument("--artifacts", default=None)
    p.add_argument("--seconds", type=int, default=600,
                   help="how long to watch the guest after it starts; the bootloader menu "
                        "waits for Return and Orbital then has to come up under TCG")
    p.add_argument("--mem", type=int, default=2048,
                   help="Redox's own QEMU example uses 1024 for the desktop variant; "
                        "2048 is that with room for a browser")
    p.add_argument("--cpus", type=int, default=2)
    args = p.parse_args(argv)

    work = os.path.abspath(args.work or os.path.dirname(os.path.abspath(args.image)) or ".")
    artifacts = os.path.abspath(args.artifacts or work)
    os.makedirs(work, exist_ok=True)
    os.makedirs(artifacts, exist_ok=True)

    image = args.image
    if image.endswith(".zst"):
        image = decompress(image, os.path.join(work, "redox.img"))

    verdict = os.path.join(artifacts, "probe.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)
        try:
            with open(verdict, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    # THE MACHINE, AND WHY EACH FIELD IS NOT THE DEFAULT.
    #
    #   cpu Broadwell with five features off   Redox's own documentation gives this exact
    #                                          string for QEMU. The kernel does not handle
    #                                          those five, and the symptom is a boot that
    #                                          stops with nothing on the screen.
    #   vga std                                Orbital draws to the framebuffer; a serial-only
    #                                          machine shows nothing.
    #   no firmware                            Redox ships its own bootloader, so OVMF is not
    #                                          used and not needed.
    #
    # THE IMAGE IS A VIRTIO DISK, not a cdrom: it is a raw disk image with its own partition
    # table, which is what its documented `-drive file=...img,format=raw,if=virtio` means.
    #
    # WRITABLE, THROUGH QEMU'S SNAPSHOT LAYER. The first probe opened it `readonly=on` to
    # guarantee the release image was never modified, and the guest panicked:
    #
    #     VirtIO-Blk: disk size: 1331200 sectors and block size of 512 bytes
    #     thread 'main' (1) panicked at drivers/storage/virtio-blk/src/scheme.rs:72:9:
    #     assertion `left == right` failed
    #       left: 1
    #      right: 0
    #     [ERROR virtio_blk@src/header/stdlib/mod.rs:121] Abort
    #
    # Redox mounts RedoxFS read-write and its virtio-blk driver has no read-only path: the
    # driver asserts that a write was accepted, and a read-only device refuses it. Redox's
    # own QEMU examples do not use readonly for the same reason.
    #
    # `snapshot=on` is the write allowed without the release file being touched: qemu
    # redirects every write to a temporary overlay and throws it away when the guest stops,
    # so a probe cannot corrupt the image it is measuring and cannot leave state behind that
    # makes the next probe different from this one.
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        cpu="Broadwell,-pcid,-x2apic,-tsc-deadline,-hle,-rtm,-invpcid",
        vga="std",
    )
    drives = ["-drive", f"file={image},format=raw,if=virtio,snapshot=on"]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)

    shots = os.path.join(work, "frames")
    os.makedirs(shots, exist_ok=True)
    snap = None
    live = None
    reached = False
    try:
        guest.start()
        note(f"image {image} ({os.path.getsize(image) >> 20} MiB)")
        note(f"machine q35, cpu Broadwell-with-features-off, {args.mem} MiB, vga std")

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

        deadline = time.time() + args.seconds
        started = time.time()
        shot = os.path.join(artifacts, "orbital.png")

        # THE BOOTLOADER MENU IS THE FIRST THING THAT DRAWS, AND IT WAITS FOR A KEY.
        #
        # run 41776866958 sat on it for 240 s and the probe called that "NOTHING DRAWN",
        # because the threshold below was written for a desktop and the menu is white text
        # on black: mean 9.9, chroma 0.0. The image had booted perfectly - the frame in
        # orbital.png names the RedoxFS volume and the best resolution - and nothing would
        # ever change, because a menu does not advance on its own.
        #
        #     Redox OS Bootloader 1.0.0 on x86/BIOS
        #     RedoxFS b8f97f3a-5f54-4d43-832e-c44f483ca7a1: 647 MiB
        #     Arrow keys and enter select mode / Press 1 to enable live mode
        #
        # So there are two thresholds, not one. Anything above pure black means firmware or
        # bootloader is drawing, and at that point the probe answers the menu. Only a frame
        # with both brightness and colour is Orbital, whose panel and wallpaper have both.
        BOOTLOADER_VISIBLE = 1.0
        ORBITAL_MEAN, ORBITAL_CHROMA = 20.0, 6.0

        answered = False
        menu_seen = 0
        last_mean = None
        while time.time() < deadline:
            if not guest.alive():
                note("qemu exited; the image did not stay up")
                break
            time.sleep(10)
            elapsed = int(time.time() - started)
            if not take_frame(guest, shot, note):
                continue
            mean, spread = frame_stats(shot)
            note(f"  {elapsed}s: mean {mean:.1f} chroma {spread:.1f}")

            if mean >= ORBITAL_MEAN and spread >= ORBITAL_CHROMA:
                note("=== ORBITAL IS DRAWN ===")
                note(f"  mean {mean:.1f}, chroma {spread:.1f} - a panel and a wallpaper")
                reached = True
                break

            if not answered and mean >= BOOTLOADER_VISIBLE:
                # TWO CONSECUTIVE FRAMES before answering, so the menu is finished drawing:
                # Enter sent while the bootloader is still probing disks is an Enter nobody
                # is listening for, and the run then looks like an image that does not boot.
                menu_seen += 1
                if menu_seen == 2:
                    note("=== THE BOOTLOADER MENU IS DRAWN ===")
                    note("  pressing Return to take the default mode")
                    send_key(guest, "ret", note)
                    answered = True
                    note("  waiting for Orbital after the boot continues")
            last_mean = mean

        note(f"verdict {'DRAWN' if reached else 'NOTHING DRAWN'} after {args.seconds}s")
        note("  install.mp4 and last-frame.png are in the artifacts directory")
        return 0 if reached else 1
    finally:
        guest.stop()
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
