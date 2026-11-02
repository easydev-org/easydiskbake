"""Boot a baked Android AVD and prove it is the disk the bake produced.

    python3 android/15/validate.py --avd out/android15.tar.zst

WHY THIS IS A SEPARATE STEP AND A SEPARATE PROCESS

The bake ends by packing an AVD and saying the guest reached sys.boot_completed. That
is a statement about the machine it ran on, made while the emulator was still holding
the data partition open. This is a different statement: the archive, unpacked fresh,
starts a guest, and that guest still has what the bake put in it.

WHY -wipe-data IS ABSENT HERE, WHICH IS THE WHOLE POINT

lib/emulator.build_args() takes wipe_data and defaults it to True, because a bake must
start from a known-clean device. A verify that used the same default would prove almost
nothing: it would create a fresh data partition, boot the system image - which is true
of the INPUT too - and pass whether or not the bake wrote anything.

Without it, the emulator boots the /data inside the archive. If that /data is the one
the bake marked, the marker is there. If the bake's shutdown lost it, or the pack
dropped the file, or the wrong archive was uploaded, it is not.

WHAT A PASS MEANS

    sys.boot_completed = 1      the framework reached its started state
    the gold marker is present   this is the disk the bake produced, not a fresh one
    the marker matches          it is THIS bake's disk, not another run's

WHAT IT DOES NOT MEAN

That the desktop is usable, that Google services work, or that the image is free of
bugs. It is the same claim the Windows and macOS bakes make when they see a desktop:
the system booted on its own and the artifact is the thing that was tested.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from lib import avd, emulator  # noqa: E402


def log(msg: str) -> None:
    print(f"[validate-android {__import__('time').strftime('%H:%M:%S')}] {msg}",
          flush=True)


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--avd", required=True, help="the archive the bake produced")
    p.add_argument("--expect", default="",
                   help="the token the bake wrote; empty means any non-empty marker")
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--memory-mb", type=int, default=4096)
    p.add_argument("--cores", type=int, default=4)
    p.add_argument("--gpu", default="swiftshader_indirect")
    p.add_argument("--keep", action="store_true", help="leave the unpacked AVD in place")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse(argv)

    if not os.path.isfile(args.avd):
        log(f"no archive at {args.avd}")
        return 1

    home = avd.avd_home()
    try:
        avd.unpack(args.avd, home)
    except avd.AvdError as exc:
        log(f"the archive could not be unpacked: {exc}")
        return 1

    names = avd.list_avds(home)
    if not names:
        log(f"no AVD in {home} after unpacking")
        return 1
    name = names[0]
    log(f"AVD: {name}")

    work = os.path.dirname(os.path.abspath(args.avd)) or "."
    emu = emulator.Emulator(
        name, os.path.join(work, "validate"),
        memory_mb=args.memory_mb, cores=args.cores, gpu=args.gpu,
        # THE LINE THIS WHOLE FILE IS ABOUT.
        wipe_data=False,
    )

    try:
        emu.start()
    except emulator.EmulatorError as exc:
        log(f"the emulator did not start: {exc}")
        return 1

    try:
        if not emu.wait_for_boot(args.boot_timeout):
            log("=== THE ARTIFACT DID NOT BOOT ===")
            log(f"emulator log: {emu.log_path}")
            return 1
        log("sys.boot_completed reached")

        marker = emu.read_mark()
        if not marker:
            log("=== THE GOLD MARKER IS NOT THERE ===")
            log("The archive booted, but its /data is not the one the bake wrote:")
            log("the bake's shutdown lost it, the pack dropped it, or this archive is")
            log("a copy of the input rather than a gold disk. The artifact is not")
            log("publishable on this result.")
            return 1

        if args.expect and marker != args.expect:
            log(f"=== THE MARKER BELONGS TO ANOTHER BAKE ===")
            log(f"  expected {args.expect}")
            log(f"  found    {marker}")
            return 1

        log(f"gold marker: {marker}")
        log("=== THE ARTIFACT BOOTED AND IS THE DISK THE BAKE PRODUCED ===")
        return 0
    finally:
        emu.stop()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
