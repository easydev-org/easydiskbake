"""Capture real frames from a guest, for the encoding benchmark.

    python3 capture.py out/frames

Forty frames at one per second: the first half is a kernel scrolling, the second
half is a shell sitting still. That mix is what an install looks like - busy then
quiet - and it is what makes the size estimate honest. Forty frames of a static
screen would understate the cost by an order of magnitude.

WHY THIS IS SEPARATE FROM selftest.py

selftest asks "do the channels work". This asks "how much does a recording cost",
and it needs a different frame cadence and a different guest state. Sharing a script
would mean a flag that changes both the mechanism and the purpose.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import iso  # noqa: E402
import vm  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def main(argv: list[str]) -> int:
    outdir = argv[0] if argv else "frames"
    os.makedirs("x", exist_ok=True)

    log("=== a guest, booting and then idling ===")
    iso.fetch_iso("alpine", "alpine.iso")

    subprocess.run(
        ["7z", "x", "-y", "-ox", "alpine.iso", "boot/vmlinuz-virt", "boot/initramfs-virt"],
        capture_output=True, check=False,
    )
    kernel = os.path.abspath("x/boot/vmlinuz-virt")
    initrd = os.path.abspath("x/boot/initramfs-virt")
    for path in (kernel, initrd):
        if not os.path.exists(path):
            log(f"missing {path}")
            return 2

    subprocess.run(["qemu-img", "create", "-f", "qcow2", "disk.qcow2", "1G"], check=True)

    guest = vm.Guest(
        os.getcwd(),
        extra=[
            "-kernel", kernel,
            "-initrd", initrd,
            "-append", "console=ttyS0,115200n8",
            "-drive", f"file={os.path.abspath('disk.qcow2')},if=virtio,format=qcow2",
            "-no-reboot",
        ],
        stdout="console.log",
    ).start()

    try:
        log("=== 40 frames, one per second ===")
        os.makedirs(outdir, exist_ok=True)
        snap = subprocess.Popen(
            [sys.executable, "snapshots.py", guest.paths.record, os.path.abspath(outdir), "1", "40"],
        )
        # snapshots.py exits on its own after 40 frames; a timeout guards a hung QMP.
        deadline = time.time() + 120
        while snap.poll() is None and time.time() < deadline:
            time.sleep(2)
        if snap.poll() is None:
            snap.terminate()
            log("snapshots.py did not finish in 120s")
    finally:
        guest.stop()

    frames = sorted(f for f in os.listdir(outdir) if f.endswith(".ppm"))
    if not frames:
        log("no frames captured")
        return 1

    first = os.path.join(outdir, frames[0])
    total = sum(os.path.getsize(os.path.join(outdir, f)) for f in frames)
    log(f"frames captured: {len(frames)}")
    log(f"raw size: {total >> 20} MiB")
    log(f"SIZE_{os.path.getsize(first)}_BYTES_PER_FRAME")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
