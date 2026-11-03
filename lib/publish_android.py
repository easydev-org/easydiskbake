"""Build an AVD from Google's system image and publish it.

    python3 lib/publish_android.py --image sysimg:android-15-aosp --repository o/r

WHAT IT PRODUCES

    ghcr.io/easydev-org/easydiskbake:sysimg-android-15-aosp

A tar.zst of an AVD directory: config.ini plus system.img, vendor.img, userdata.img
and the kernel/ramdisk the emulator boots them with.

WHY THIS IS A SEPARATE STEP FROM THE BAKE

Two reasons, and they are the same two that moved the Windows ISO out of the bake:

    the bake stops downloading 746 MB from Google on its critical path;
    when Google reissues a system image, this fails and says so instead of a bake
    failing later with a guest that does not boot.

There is a third reason specific to Android: creating an AVD needs `cmdline-tools` and
answers a licence prompt. Doing that once here means the bake image does not carry
cmdline-tools at all, and no workflow has to answer a prompt.

WHAT IS VERIFIED

    sha1        Google's own, from sys-img2-3.xml - the only hash they publish
    size        a floor, because a truncated zip unpacks to a half-written image
    contents    system.img and userdata.img must exist after unpacking

The sha1 is weaker than the sha256 used for Windows. It is what the publisher
provides, it still catches a replaced or truncated download, and inventing a second
hash that only this repository knows would not make the supply chain stronger.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import avd  # noqa: E402
import ci  # noqa: E402
import emulator  # noqa: E402
import images  # noqa: E402
import iso  # noqa: E402

MIN_PLAUSIBLE = 500 * 1024 * 1024   # the Android 15 zip is ~746 MB


def log(msg: str) -> None:
    print(f"[android] {msg}", flush=True)


def sha1_of(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(zip_path: str, pin: dict) -> str:
    """Download the zip and check it against the pin."""
    if os.path.exists(zip_path):
        log(f"already downloaded: {zip_path}")
    else:
        log(f"fetching {pin['url']}")
        iso.fetch_url(pin["url"], zip_path)

    size = os.path.getsize(zip_path)
    log(f"{size >> 20} MiB")
    if size < MIN_PLAUSIBLE:
        raise avd.AvdError(
            f"{zip_path} is {size} bytes; an Android system image is hundreds of "
            f"megabytes. The transfer almost certainly stopped early."
        )

    expected = pin.get("sha1")
    if expected:
        actual = sha1_of(zip_path)
        if actual != expected:
            os.unlink(zip_path)
            raise avd.AvdError(
                f"sha1 mismatch:\n  expected {expected}\n  actual   {actual}\n"
                f"Google has probably published a new r-number; update lib/images.py."
            )
        log(f"sha1 verified: {expected}")
    else:
        log("no sha1 pinned")

    return zip_path


def build(zip_path: str, pin: dict, workdir: str) -> str:
    """Unpack, create the AVD, START IT ONCE, then pack it. Returns the archive.

    WHY THE EMULATOR HAS TO RUN HERE, WHICH THE FIRST VERSION ASSERTED IT DID NOT

    android/15/Containerfile.builder carried this note:

        WHAT IT DOES NOT CONTAIN
        No emulator, no qemu, no KVM. Building an AVD does not boot anything.

    That is wrong twice over. `avdmanager create avd` writes config.ini and nothing
    else - the disk images are created by the emulator's first start - so packing after
    create produced a 1.6 KB archive of one config file and pushed it to ghcr as a
    system image. And avdmanager itself refuses to create an AVD at all unless the
    `emulator` package is present, which is how the second run failed:

        Error: "emulator" package must be installed!

    So this function boots the AVD to completion once, which is what makes
    system.img, userdata.img and cache.img exist. Those files are 4.5 GB; the archive
    before this change was 1642 bytes.

    -wipe-data is deliberate: the published AVD must be a clean device, and the boot
    here is preparation rather than use. The bake's own first boot wipes again.
    """
    avd.install_system_image(
        zip_path, api=pin["api"], flavour=pin["flavour"], abi=pin["abi"],
    )
    avd.create_avd("sandbox", api=pin["api"], flavour=pin["flavour"], abi=pin["abi"])

    home = avd.avd_home()
    created = avd.list_avds(home)
    if not created:
        raise avd.AvdError(f"no AVD in {home} after create_avd")

    # THE BOOT THAT CREATES THE DISKS. Without KVM this is slow and is the reason the
    # publish job needs /dev/kvm.
    name = created[0]
    log(f"starting {name} once so its disk images exist")
    emulator_run = emulator.Emulator(
        name, os.path.join(workdir, "emulator"),
        memory_mb=4096, cores=4, gpu="swiftshader_indirect",
        wipe_data=True,
    )
    try:
        emulator_run.start()
    except emulator.EmulatorError as exc:
        raise avd.AvdError(
            f"the emulator did not start, so the AVD has no disk images to pack: {exc}"
        ) from exc

    try:
        if not emulator_run.wait_for_boot(1800):
            raise avd.AvdError(
                "the AVD did not reach sys.boot_completed, so its disks were never "
                "created; the archive would be a config file"
            )
    finally:
        emulator_run.stop()

    archive = os.path.join(workdir, "avd.tar.zst")
    avd.pack(home, archive)
    return archive


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--image", default="sysimg:android-15-aosp",
                   help="a key in lib/images.py ANDROID (default sysimg:android-15-aosp)")
    p.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--keep", action="store_true", help="keep the downloaded zip")
    p.add_argument("--artifacts", default="",
                   help="directory to copy the recording and last frame into, so a "
                        "workflow can publish them; without it the work directory is "
                        "a tempdir and both are deleted with it")
    args = p.parse_args(argv)

    table = images.all_images()
    if args.image not in table:
        log(f"unknown image {args.image!r}; known: "
            f"{', '.join(sorted(k for k in table if k.startswith('sysimg')))}")
        return 2
    pin = table[args.image]

    # THE WORK DIRECTORY HAS TO OUTLIVE THE RUN WHEN SOMETHING IN IT MATTERS.
    #
    # It used to be an unconditional tempfile.mkdtemp(), and the bake inside it wrote
    # install.mp4 and last-frame.png beside the AVD archive. Both were then deleted by
    # the rmtree below, inside a container, and android-15_aosp-publish.yml uploaded no artifact
    # at all - so every Android run so far produced no picture of itself. The only way to
    # see what the emulator drew was to add a screendump step and run it again.
    #
    # With --artifacts the tempdir is created UNDER the caller's directory, so the rmtree
    # still happens and the recording has already been copied out.
    if args.artifacts:
        os.makedirs(args.artifacts, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix="android-", dir=args.artifacts)
    else:
        workdir = tempfile.mkdtemp(prefix="android-")
    zip_path = os.path.join(workdir, "sysimg.zip")

    try:
        fetch(zip_path, pin)
        archive = build(zip_path, pin, workdir)

        if not args.repository:
            log("no --repository (or $GITHUB_REPOSITORY); leaving the archive at "
                f"{archive}")
            return 0

        # The tag is the image key with the colon replaced: a colon is not legal in a
        # tag, so `sysimg:android-15-aosp` publishes as `sysimg-android-15-aosp`.
        tag = args.image.replace(":", "-")
        ci.install_oras()
        ci.login("ghcr.io")

        import publish_disk
        publish_disk.push(
            archive, tag, ci.lowercase_repository(),
            kind="android-sysimg",
            media_type="application/vnd.easydev.avd",
        )
        return 0
    except (avd.AvdError, iso.IsoError, ci.Failed) as exc:
        log(f"FAILED: {exc}")
        return 1
    finally:
        # COPY THE PICTURES OUT BEFORE ANYTHING DELETES THEM. See --artifacts above.
        if args.artifacts:
            for name in ("install.mp4", "last-frame.png", "emulator.log"):
                source = os.path.join(workdir, name)
                if os.path.isfile(source):
                    shutil.copyfile(source, os.path.join(args.artifacts, name))
                    log(f"kept {name} ({(os.path.getsize(source)) >> 10} KiB)")
        if args.keep:
            log(f"kept {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
