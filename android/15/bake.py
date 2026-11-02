"""Bake an Android 15 (AOSP) sandbox disk.

    python3 android/15/bake.py --out out/android15.tar.zst

WHAT "BAKE" MEANS HERE, AND WHY IT IS SHORT

The other four platforms run an installer for twenty to ninety minutes: partition a
disk, write a system, wait for it to reboot twice, then prove it starts. Android does
not have that step. Google ships a system image whose contents are the disks, and the
emulator boots them directly.

So this file is not a long pipeline. It is:

    pull the AVD from ghcr          lib/iso.py
    start the emulator              lib/emulator.py
    record every frame              lib/snapshots.py over QMP
    wait for sys.boot_completed     lib/emulator.py (adb)
    repack the data partition       (the AVD after first boot)
    encode the recording            lib/vm.py
    publish                         lib/publish_disk.py

The output is an AVD archive rather than a qcow2, because that is what the runtime
consumes: `emulator -avd sandbox`. A qcow2 of an ext4 system image is not something
the emulator can boot.

WHY THE FIRST BOOT IS STILL A STEP

A fresh AVD boots to a setup screen and an unformatted /data. Running it once here
means the published artifact boots straight to a launcher, which is what a sandbox
needs - the same reason the Windows bake runs OOBE and the macOS bake writes the
Setup Assistant marker.

WHY QMP AND NOT VNC

The emulator speaks QMP on a socket, and lib/qmp.py already drives QMP. It also means
the recording path is the same code as the other platforms: lib/snapshots.py takes one
frame every N seconds, and lib/vm.py encodes them. No new capture mechanism.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# android/15/bake.py -> the repository root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib import avd, ci, emulator, iso, vm  # noqa: E402

IMAGE = "sysimg:android-15-aosp"


def log(msg: str) -> None:
    print(f"[bake-android {__import__('time').strftime('%H:%M:%S')}] {msg}", flush=True)


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/android15.tar.zst")
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--memory-mb", type=int, default=4096)
    p.add_argument("--cores", type=int, default=4)
    p.add_argument("--gpu", default="swiftshader_indirect")
    p.add_argument("--snapshot-interval", default=os.environ.get("SNAPSHOT_INTERVAL", "5"))
    p.add_argument("--avd", default="", help="an AVD archive already on disk")
    p.add_argument("--publish", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse(argv)

    work = os.path.dirname(os.path.abspath(args.out)) or "."
    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    os.makedirs(work, exist_ok=True)

    # --- the AVD ------------------------------------------------------------
    home = avd.avd_home()
    if args.avd:
        log(f"using the AVD archive on disk: {args.avd}")
        avd.unpack(args.avd, home)
    else:
        archive = os.path.join(work, "avd-source.tar.zst")
        tag = IMAGE.replace(":", "-")

        if os.path.exists(archive):
            log(f"{archive} already present")
        else:
            # ONE SOURCE: this repository's package. No fallback to Google, for the
            # same reason as every other platform - a missing artifact here means
            # publish-android.yml was not run, and that is worth seeing at second ten.
            log(f"pulling {tag} from ghcr")
            try:
                iso.fetch_iso(tag, archive)
            except iso.IsoError as exc:
                raise SystemExit(
                    f"{tag} is not in this repository's ghcr package: {exc}\n\n"
                    f"Run publish-android.yml first:\n"
                    f"    gh workflow run publish-android.yml\n"
                    f"This bake does not fetch from Google: a system image built for\n"
                    f"the emulator is not something to re-derive on every run."
                ) from exc

        avd.unpack(archive, home)

    names = avd.list_avds(home)
    if not names:
        log(f"no AVD in {home}")
        return 1
    name = names[0]
    log(f"AVD: {name}")

    # --- run it -------------------------------------------------------------
    shots = os.path.join(frames, "shots")
    os.makedirs(shots, exist_ok=True)

    emu = emulator.Emulator(
        name, work,
        memory_mb=args.memory_mb, cores=args.cores, gpu=args.gpu,
    )
    try:
        emu.start()
    except emulator.EmulatorError as exc:
        log(f"the emulator did not start: {exc}")
        return 1

    snap = subprocess.Popen(
        [sys.executable,
         os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
             os.path.abspath(__file__)))), "lib", "snapshots.py"),
         emu.qmp, shots, str(args.snapshot_interval), "2000"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        booted = emu.wait_for_boot(args.boot_timeout)
    finally:
        emu.stop()
        snap.terminate()
        try:
            snap.wait(timeout=10)
        except subprocess.TimeoutExpired:
            snap.kill()

    # Encoded whether or not it booted: a failed boot is exactly when the frames are
    # worth watching, and the emulator's own log is kept beside them.
    log("=== encoding the recording ===")
    mp4 = os.path.join(work, "install.mp4")
    if vm.record_many(mp4, shots, fps=int(os.environ.get("RECORD_FPS", "1"))):
        log(f"recording: {mp4} ({os.path.getsize(mp4) >> 20} MiB)")
    else:
        log("recording: nothing to encode")

    if not booted:
        log("=== THE GUEST DID NOT REACH sys.boot_completed ===")
        log(f"emulator log: {emu.log_path}")
        try:
            with open(emu.log_path, "rb") as fh:
                for line in fh.read().decode("utf-8", "replace").splitlines()[-25:]:
                    log(f"  {line}")
        except OSError:
            pass
        return 1

    log("the guest booted")

    # --- what the disk says, before publishing it ---------------------------
    #
    # smoke.check_disk() reads an MBR and an ext superblock, which an AVD does not
    # have: it is a directory of Android images, not a partitioned disk. So the check
    # here is the one that applies - the files the emulator needs are present and not
    # empty. A zero-length userdata.img is what a failed first boot leaves behind, and
    # it produces an AVD that starts and then reports a corrupt data partition.
    ok = True
    for required in ("config.ini", "userdata.img", "system.img"):
        path = os.path.join(home, f"{name}.avd", required)
        size = os.path.getsize(path) if os.path.exists(path) else -1
        log(f"  {required}: {size} bytes")
        if size <= 0:
            log(f"  MISSING or EMPTY: {required}")
            ok = False

    if not ok:
        log("=== THE AVD FAILED ITS CHECKS; refusing to produce an artifact ===")
        return 1

    # --- publish ------------------------------------------------------------
    log("repacking the AVD after its first boot")
    out = args.out if os.path.isabs(args.out) else os.path.join(work, "android15.tar.zst")
    avd.pack(home, out)
    log(f"BAKED android15 -> {out} ({os.path.getsize(out) >> 20} MiB)")

    if args.publish:
        ci.install_oras()
        ci.login("ghcr.io")
        import publish_disk
        try:
            publish_disk.push(
                out, "android-15-aosp", ci.lowercase_repository(),
                kind="android-avd", media_type="application/vnd.easydev.avd",
            )
        except ci.Failed as exc:
            log(f"publish failed: {exc}")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
