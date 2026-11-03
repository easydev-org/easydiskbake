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
import time

# android/15/bake.py -> the repository root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib import avd, ci, emulator, iso, live as livelib, vm  # noqa: E402

IMAGE = "sysimg:android-15-aosp"

# The marker a gold disk carries, and the file it lives in. A token rather than a fixed
# string so two bakes cannot be confused for one another.
GOLD_MARK = "/data/local/tmp/bake-gold"


def gold_token() -> str:
    """The identity this bake writes into the gold disk's /data.

    A run id, a timestamp and four random hex digits. All three, because each one
    covers a way two tokens could collide: the run id repeats across re-runs of the
    same workflow, a second-resolution timestamp repeats within one run, and the random
    tail covers a local bake where there is no run id at all.
    """
    return (f"gold-{os.environ.get('GITHUB_RUN_ID', 'local')}-"
            f"{int(time.time())}-{os.urandom(2).hex()}")


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


def system_image_path(avd_dir: str) -> str:
    """Where the AVD's system image actually is, from its own config.ini.

    Returns "" when the config names none, which is a broken AVD rather than a missing
    file - the two want different sentences.
    """
    config = os.path.join(avd_dir, "config.ini")
    try:
        with open(config, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("image.sysdir.1"):
                    wanted = line.split("=", 1)[1].strip().rstrip("/")
                    return os.path.join(avd.sdk_root(), wanted, "system.img")
    except OSError:
        return ""
    return ""


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
            # android-15_aosp-publish.yml was not run, and that is worth seeing at second ten.
            log(f"pulling {tag} from ghcr")
            try:
                # fetch_tag, NOT fetch_iso: the tag is already exact.
                #
                # fetch_iso prepends "iso-", which is right for the installer images
                # (iso-ubuntu, iso-nixos) and wrong here - publish_android.py takes
                # "sysimg:android-15-aosp" and publishes it as "sysimg-android-15-aosp"
                # with no "iso-" anywhere. Run 41599434278 asked for
                # "iso-sysimg-android-15-aosp" and got "no manifest for tag".
                iso.fetch_tag(tag, archive)
            except iso.IsoError as exc:
                raise SystemExit(
                    f"{tag} is not in this repository's ghcr package: {exc}\n\n"
                    f"Run android-15_aosp-publish.yml first:\n"
                    f"    gh workflow run android-15_aosp-publish.yml\n"
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

    # THE SYSTEM IMAGE IS A SECOND ARTIFACT, AND THIS BAKE FORGOT IT.
    #
    # A published AVD holds config.ini, userdata, cache and an initrd - not the system
    # partition. That comes from $ANDROID_SDK_ROOT/system-images/<api>/<tag>/<abi>/, the
    # path the AVD's own config.ini names as image.sysdir.1, and run 41605230808 started
    # an emulator without it:
    #
    #     WARNING | /opt/android-sdk/system-images/android-35/default/x86_64/ is not a
    #               valid directory
    #     FATAL   | Broken AVD system path. Check your ANDROID_SDK_ROOT value
    #
    # lib/launch_android.py HAS ALWAYS DONE THIS - that is why launch-android succeeded
    # on 41494640366 while this bake failed: the launch installed the system image from
    # its pin before starting the emulator, and the bake never did. ensure_system_image
    # is that same function, moved to be callable from here rather than copied.
    from lib import launch_android

    launch_android.ensure_system_image(home, name, log)

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

    # THE RECORDING IS KEPT CURRENT, so a cancelled run still has an mp4 and a last
    # frame - see lib/live.py. The emulator's boot takes minutes and its failure mode is
    # a black screen, which is a thing to look at rather than to read.
    live = livelib.start(
        [shots], os.path.join(work, "install.mp4"),
        os.path.join(work, "last-frame.png"),
        every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "20")),
        fps=int(os.environ.get("RECORD_FPS", "1")),
    )

    try:
        booted = emu.wait_for_boot(args.boot_timeout)

        # The gold marker goes in AFTER the boot completed, so it proves the guest
        # reached the framework and not merely the kernel. See Emulator.mark().
        marked = emu.mark(gold_token()) if booted else False
    finally:
        # acpi_first is not available here - the emulator has no ACPI power button - so
        # stop() asks it to shut down through adb and only then signals. That is what
        # flushes userdata-qemu.img, which is the file this bake exists to publish.
        emu.stop()
        snap.terminate()
        try:
            snap.wait(timeout=10)
        except subprocess.TimeoutExpired:
            snap.kill()
        live.stop(final=True)

    # Written whether or not it booted: a failed boot is exactly when the frames are
    # worth watching, and the emulator's own log is kept beside them.
    log("=== finalising the recording ===")
    mp4 = os.path.join(work, "install.mp4")
    if os.path.exists(mp4):
        log(f"recording: {mp4} ({os.path.getsize(mp4) >> 20} MiB)")
    else:
        log("recording: nothing was encoded")

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
    if not marked:
        # Not fatal to the disk, but fatal to the claim: without the marker nothing can
        # prove a later boot read the same /data, so the artifact would be a copy of the
        # input and the verify step would be testing the wrong thing.
        log("=== THE GOLD MARKER COULD NOT BE WRITTEN ===")
        return 1
    log("the gold marker is on /data")

    # --- what the AVD contains, before publishing it -----------------------
    #
    # smoke.check_disk() reads an MBR and an ext superblock, which an AVD does not have:
    # it is a directory of Android images, not a partitioned disk. So the check here is
    # the one that applies - the files the emulator needs are present and not empty.
    #
    # THE NAMES ARE THE ONES THE EMULATOR ACTUALLY WRITES, and the first version had
    # them wrong in two ways:
    #
    #     config.ini       lives in the AVD directory, but it is created by avdmanager
    #                      from the system image's own config - so checking it proves the
    #                      AVD exists, not that anything booted
    #     userdata.img     does not exist. The emulator creates userdata-qemu.img on the
    #                      first boot; userdata.img is an input that is not shipped
    #
    # A zero-length userdata-qemu.img is what a failed first boot leaves behind, and a
    # device without it starts and then reports a corrupt data partition. That is the
    # failure worth catching, so that is the file checked.
    avd_dir = os.path.join(home, f"{name}.avd")
    ok = True
    required = {
        "config.ini": 1,            # bytes; any non-empty config
        "userdata-qemu.img": 1,     # the data partition, created by the first boot
    }
    for filename, minimum in required.items():
        path = os.path.join(avd_dir, filename)
        size = os.path.getsize(path) if os.path.exists(path) else -1
        log(f"  {filename}: {size} bytes")
        if size < minimum:
            log(f"  MISSING or EMPTY: {filename}")
            ok = False

    # system.img IS NOT EXPECTED IN THE .avd DIRECTORY, AND CHECKING FOR IT THERE WAS
    # WRONG FOR THE SECOND TIME.
    #
    # An AVD holds config.ini, userdata, cache and an initrd. The system partition comes
    # from $ANDROID_SDK_ROOT/system-images/<api>/<tag>/<abi>/, named by the config's
    # image.sysdir.1 - so a file in the .avd directory is one layout, not the layout. The
    # comment above `required` said "a hard link or a copy", which is true of AVDs created
    # by a tool that copies; this one does not.
    #
    # Run 41607953974 reported
    #
    #     system.img: -1 bytes
    #     MISSING or EMPTY: system.img
    #
    # about an AVD whose system image had been installed and used successfully: the
    # emulator had booted to sys.boot_completed and the gold marker was already written.
    # The check was refusing a working artifact.
    #
    # So the file that matters is asked for where it actually lives, in the SDK path
    # config.ini names.
    wanted = system_image_path(avd_dir)
    if wanted:
        size = os.path.getsize(wanted) if os.path.exists(wanted) else -1
        log(f"  system image: {size} bytes at {wanted}")
        if size < 1:
            log(f"  MISSING or EMPTY: {wanted}")
            ok = False
    else:
        log("  config.ini names no image.sysdir.1, so no system image can be located")
        ok = False

    if not ok:
        log("=== THE AVD FAILED ITS CHECKS; refusing to produce an artifact ===")
        return 1

    # --- publish ------------------------------------------------------------
    log("repacking the AVD after its first boot")
    out = args.out if os.path.isabs(args.out) else os.path.join(work, "android15.tar.zst")
    # pack() MAY RENAME: the archive cannot be a .tar.zst made by 7z, so it returns
    # whatever path it actually wrote. Using `out` here ignored that and reported a file
    # that did not exist.
    out = avd.pack(home, out)
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
