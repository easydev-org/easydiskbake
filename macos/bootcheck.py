"""Boot a macOS disk that has already been installed and say what it reaches.

    python3 macos/bootcheck.py --disk out/macos26-setup.qcow2 --out out/bootcheck.mp4

WHAT THIS IS FOR, AND WHY IT IS NOT A STAGE OF THE PIPELINE

`macos/26/bake.py` writes the installer's output and then waits for the Setup Assistant. On
run 42031548356 the disk stopped growing at 39,355 MiB - the same number the one successful
macos26 setup ended on, 39,396 - and the Setup Assistant never appeared. The recording shows
three `HANDOFF TO XNU`, so the installer rebooted twice and then stopped drawing anything.

That leaves two possibilities that need different fixes, and no amount of reading the bake
log separates them:

    the disk is fine and the bake's wait is wrong      -> fix the wait
    the disk itself does not boot to the wizard        -> fix the install

So this boots a FINISHED disk - by default the `macos26-setup` tag, which is the one setup
that ever succeeded - and reports what the screen does for N minutes. It is not a stage; it
produces no artifact and publishes nothing. Its output is a recording and a list of how many
distinct screens were seen.

THE DIFFERENCE FROM THE BAKE'S WAIT is that this one reports rather than decides. The bake
stops at the first frame it cannot classify and calls it a failure; this prints a hash of
each minute's frame, so "nothing moved" and "it moved through six screens" look different
in the log.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
M15 = os.path.join(HERE, "15")
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, M15)


def log(message: str) -> None:
    print(f"[bootcheck {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", default="out/macos26-setup.qcow2")
    p.add_argument("--out", default="out/bootcheck.mp4")
    p.add_argument("--work", default="out/bootcheck-work")
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--minutes", type=int, default=20,
                   help="how long to watch the screen")
    p.add_argument("--recovery", default=None,
                   help="recovery image for the APFS driver; pulled if absent")
    args = p.parse_args(argv)

    if not os.path.isfile(args.disk):
        log(f"=== NO DISK AT {args.disk} ===")
        return 2
    os.makedirs(args.work, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)

    log(f"disk {args.disk} ({os.path.getsize(args.disk) >> 20} MiB)")
    log(f"work {args.work}")
    log(f"watching for {args.minutes} minutes")

    # THE BOOT ASSETS. opencore.build() extracts Apple's APFS driver from the recovery image,
    # so it is required even though nothing here installs from it.
    import machine
    import opencore
    import macos_recovery
    import vm
    import live as livelib

    recovery = args.recovery
    if not recovery:
        log("=== recovery image ===")
        recovery = macos_recovery.fetch_published("26", args.work)
        log(f"  {recovery} ({os.path.getsize(recovery) >> 20} MiB)")

    log("=== OpenCore, firmware, APFS driver ===")
    assets = opencore.build("26", args.work)
    log(f"  identity {assets['identity']['serial']} / {assets['identity']['mac']}")

    shape = machine.shape(assets, cpus=args.cpus, mem_mb=args.mem)
    guest = vm.Guest(
        args.work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            # THE PLAIN DRIVE. This disk is finished and read-mostly; discard would be a
            # change to it, and the point of the exercise is to observe what is already
            # there.
            "-drive", machine.TARGET_DRIVE_PLAIN.format(disk=os.path.abspath(args.disk)),
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
            "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
        ],
        shape=shape,
        stdout=os.path.join(args.work, "console.log"),
    ).start()

    frames = os.path.join(args.work, "frames")
    os.makedirs(frames, exist_ok=True)
    snap = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         guest.paths.record, frames, "2"],
        stdout=open(os.path.join(frames, "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )
    live = livelib.start(
        [os.path.join(frames, "shots"), frames],
        args.out,
        os.path.join(args.work, "last-frame.png"),
        every=20.0,
    )

    # A HASH PER SAMPLE, SO "NOTHING MOVED" IS A LINE AND NOT AN ABSENCE.
    #
    # Two frames that hash the same are the same picture. A run that shows fourteen identical
    # hashes and one that shows fourteen different ones are both "no Setup Assistant" to the
    # bake's wait, and they are completely different problems.
    seen: list[str] = []
    try:
        deadline = time.time() + args.minutes * 60
        last_report = 0
        while time.time() < deadline:
            if not guest.alive():
                log("qemu exited before the deadline")
                break
            elapsed = int(args.minutes * 60 - (deadline - time.time()))
            if elapsed - last_report >= 30:
                last_report = elapsed
                newest = None
                try:
                    names = sorted(
                        f for f in os.listdir(frames)
                        if f.startswith("frame-") and f.endswith((".ppm", ".jpg"))
                    )
                    if names:
                        newest = os.path.join(frames, names[-1])
                except OSError:
                    pass
                if newest:
                    with open(newest, "rb") as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()[:12]
                    if not seen or seen[-1] != digest:
                        seen.append(digest)
                        log(f"  {elapsed:4d}s  screen changed -> {digest}")
                    else:
                        log(f"  {elapsed:4d}s  unchanged ({digest})")
            time.sleep(5)
    finally:
        live.stop(final=True)
        guest.stop(acpi_first=True)
        snap.terminate()
        try:
            snap.wait(timeout=5)
        except subprocess.TimeoutExpired:
            snap.kill()

    log(f"=== {len(seen)} distinct screen(s) in {args.minutes} minutes ===")
    for index, digest in enumerate(seen, 1):
        log(f"  {index}: {digest}")
    log(f"recording: {args.out} ({os.path.getsize(args.out) >> 20} MiB)"
        if os.path.isfile(args.out) else "no recording was written")

    # THE ANSWER, IN THE ONLY FORM THIS SCRIPT CAN GIVE ONE.
    if len(seen) <= 1:
        log("=== THE SCREEN NEVER CHANGED ===")
        log("  the disk booted to one picture and stayed on it for the whole window")
    else:
        log(f"=== THE SCREEN CHANGED {len(seen) - 1} TIME(S) ===")
        log("  look at the recording to see what it changed to; the meaning of each")
        log("  change is not something a hash can decide")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
