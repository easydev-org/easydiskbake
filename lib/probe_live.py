"""Start a Linux live ISO in qemu and photograph it. Stop at the installer, decide nothing.

    python3 probe-live.py --iso /work/isos/mx.iso --name mx --out /work/probe/mx

WHAT THIS IS FOR

Every one of the Linux bakes in easysandbox-vm drives a different installer:

    Ubuntu, Debian, Fedora   told what to do on the kernel command line, no typing
    CachyOS, NixOS           a program inside the live image, driven over the console
    MX                       OCR and the pointer, plus GRUB by hand in a chroot

Before deciding which of those to port, the question is smaller: does the ISO reach
an installer at all, and what does that installer look like on screen. That is what
this answers, and it stops there - no three-way branch, no QC, no bootloader.

WHY THE ISO IS BOOTED DIRECTLY RATHER THAN ITS KERNEL EXTRACTED

easysandbox-vm's scripts pull casper/vmlinuz or antiX/vmlinuz out of the image and
pass -kernel/-initrd, because that lets them put arguments on the command line. It is
also the step that failed first for MX: "-kernel and only console= drops you into the
initramfs rescue shell - the prompt reads '[rootfs ~]#', there is no pacman and no
curl".

Booting the ISO as a CD lets its own bootloader pick the right kernel and command
line, which is what a person installing from a USB stick gets. If the screen shows an
installer this way, the harder road is still open; if it does not, nothing else was
going to work.

WHAT IS KEPT

A screenshot every SAMPLE seconds, named by the second, plus the serial console. The
pictures are the result: "it reached a desktop" and "it stopped at 'Scanning disk
drives'" look the same in a log and different in a photograph.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import vm  # noqa: E402


def log(message: str) -> None:
    print(f"[probe {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--iso", required=True)
    p.add_argument("--name", required=True, help="a label for the files")
    p.add_argument("--out", required=True, help="directory for pictures and the log")
    p.add_argument("--minutes", type=float, default=12)
    p.add_argument("--memory-mb", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--resolution", default="1280x800")
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"no ISO at {args.iso}")

    shots = os.path.join(args.out, "frames")
    os.makedirs(shots, exist_ok=True)
    work = os.path.join(args.out, "work")
    os.makedirs(work, exist_ok=True)

    width, height = (int(v) for v in args.resolution.lower().split("x"))

    # NO DISK. This is a probe of the live medium, and a disk would only add a second
    # thing the installer can write to.
    drives = [
        "-drive", f"file={args.iso},media=cdrom,readonly=on",
    ]

    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.memory_mb, machine="q35",
        firmware_code="",          # SeaBIOS: these ISOs are hybrid BIOS/UEFI and
        firmware_vars="",          # their own bootloader knows which to use
        vga="std",
        resolution=(width, height),
    )
    guest = vm.Guest(work, extra=drives, shape=shape,
                     stdout=os.path.join(args.out, "console.log")).start()

    sampler = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         guest.paths.record, shots, "30"],
        stdout=open(os.path.join(shots, "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )

    deadline = time.time() + args.minutes * 60
    started = time.time()
    last_keep = 0.0
    try:
        while time.time() < deadline:
            if not guest.alive():
                log("qemu exited")
                break
            now = time.time()
            if now - last_keep >= 30:
                last_keep = now
                shot = newest(shots)
                if shot:
                    elapsed = int(now - started)
                    keep(shot, os.path.join(shots, f"{args.name}-{elapsed:04d}s.png"))
                    log(f"  {elapsed:4d}s  {describe(shot)}")
            time.sleep(5)
    finally:
        guest.stop()
        sampler.terminate()
        try:
            sampler.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sampler.kill()

    log(f"done; {len(os.listdir(shots))} files in {shots}")
    return 0


def newest(directory: str) -> str | None:
    """The highest-numbered frame from snapshots.py, which writes frame-NNNN.ppm."""
    try:
        names = [n for n in os.listdir(directory)
                 if n.startswith("frame-") and n.endswith(".ppm")]
    except OSError:
        return None
    if not names:
        return None
    return os.path.join(directory, max(names, key=lambda n: int(n[6:10])))


def keep(source: str, dest: str) -> None:
    try:
        import lastframe
        lastframe.convert(source, dest)
    except Exception:  # noqa: BLE001 - a picture is not worth the run
        pass


def describe(path: str) -> str:
    """Mean grey of a frame, so the log says whether anything is drawn."""
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
        return "empty"
    total = sum(pixels[i] + pixels[i + 1] + pixels[i + 2]
                for i in range(0, len(pixels), 97 * 3))
    count = len(range(0, len(pixels), 97 * 3)) * 3
    return f"{width}x{height} mean grey {total // count}"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
