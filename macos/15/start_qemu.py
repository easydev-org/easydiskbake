"""Start qemu on an EXISTING disk and stay out of the way.

    python3 start_qemu.py --release 26 --disk /work/t26/disk.qcow2 --work /work/t26

WHY THIS IS NOT boot_setup.py

boot_setup.py creates the overlay itself, and its first act is `os.unlink(overlay)` so a
second run always starts from the pristine backing file. That is right for "walk the wizard
from the beginning" and wrong for "boot this exact disk state again", which is what a test
of a hard kill needs: the whole question is what survived on the disk.

So this stages the recovery image if it is missing, builds the OpenCore assets (idempotent),
and starts qemu on the disk it is given - nothing else. It never deletes anything.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

REPO = os.environ.get("REPO", "/repo")
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(REPO, "macos", "15"))

import opencore    # noqa: E402
import vm          # noqa: E402
import machine     # noqa: E402


def log(msg: str) -> None:
    print(f"[start-qemu {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--release", default="26")
    p.add_argument("--disk", required=True, help="the qcow2 to boot, used as-is")
    p.add_argument("--work", required=True)
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    a = p.parse_args(argv)

    if not os.path.isfile(a.disk):
        raise SystemExit(f"no disk at {a.disk}")

    scratch = os.path.join(a.work, "scratch")
    os.makedirs(scratch, exist_ok=True)

    # The recovery image is an input even though this does not boot from it: opencore.build
    # extracts Apple's APFS driver from it, and a driver from the wrong release is a boot that
    # fails for a reason nothing else in the log names.
    src = "/assets/run/recovery.dmg"
    if a.release != "15":
        alt = "/assets/run/recovery-tahoe.dmg"
        if os.path.exists(alt):
            src = alt
    recovery = os.path.join(scratch, "recovery.dmg")
    if not os.path.exists(recovery):
        log(f"staging {src}")
        try:
            os.link(src, recovery)
        except OSError:
            shutil.copyfile(src, recovery)

    log(f"disk {a.disk} ({os.path.getsize(a.disk) >> 20} MiB)")
    assets = opencore.build(a.release, scratch)
    log(f"identity {assets['identity']['serial']}")

    guest = vm.Guest(
        a.work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            "-drive", machine.TARGET_DRIVE.format(disk=a.disk),
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
            "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
        ],
        shape=machine.shape(assets, cpus=a.cpus, mem_mb=a.mem),
        stdout=os.path.join(scratch, "console.log"),
    )
    guest.shape.vnc_bind = "0.0.0.0"
    guest.start()
    log(f"qemu pid {guest.pid}")
    log(f"qmp {guest.paths.drive}")
    log("RUNNING")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
