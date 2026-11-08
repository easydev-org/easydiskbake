"""Install the pinned AOSP system image into $ANDROID_SDK_ROOT, and nothing else.

    python3 android/15/fetch_sysimg.py

WHAT THIS IS NOT

It is not lib/publish_android.py. That script creates an AVD, boots it once so the AVD's
disks exist, packs them and pushes the archive to ghcr - because an AVD is what the
emulator's `-avd` path consumes.

This flow does not use an AVD. It boots the system image as a plain qemu VM with explicit
partitions, which is what android/15/mkdisks.py builds the disks for. So the only thing
needed from Google is the unpacked system image directory:

    $ANDROID_SDK_ROOT/system-images/android-35/default/x86_64/
        system.img  vendor.img  ramdisk.img  kernel-ranchu  ...

WHY THE DOWNLOAD IS NOT DONE AT BAKE TIME

The same reason publish-android does it separately: 746 MB from dl.google.com on the
critical path of a boot is a dependency that can fail for reasons the boot cannot explain.
The pin - URL, sha1, size - lives in lib/images.py and is the same one publish-android
verifies, so both paths are guaranteed to be looking at the same upstream file.

The sha1 is Google's own, from sys-img2-3.xml. It is weaker than the sha256 used for
Windows, it is what the publisher provides, and it still catches a truncated or replaced
download, which is what the check is for.
"""

from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "lib"))

import avd  # noqa: E402
import images  # noqa: E402
import iso  # noqa: E402
import publish_android as nearby  # noqa: E402

KEY = "sysimg:android-15-aosp"


def log(msg: str) -> None:
    print(f"[sysimg {__import__('time').strftime('%H:%M:%S')}] {msg}", flush=True)


def main(argv: list[str]) -> int:
    pin = images.ANDROID.get(KEY)
    if pin is None:
        print(f"{KEY} is not in lib/images.py", file=sys.stderr)
        return 1

    target = os.path.join(
        avd.sdk_root(), "system-images", f"android-{pin['api']}",
        pin["flavour"], pin["abi"],
    )
    # ALREADY INSTALLED IS NOT "ALREADY USABLE". The test is system.img, the one file the
    # emulator cannot start without - the same rule install_system_image() applies, asked
    # here before the 746 MB download so a warm runner pays nothing.
    if os.path.isfile(os.path.join(target, "system.img")):
        log(f"already installed at {target}")
        print(f"SYSTEM_IMAGE_DIR={target}")
        return 0

    zip_path = os.path.join("/tmp", "sysimg.zip")
    log(f"pinned: api {pin['api']} {pin['abi']}/{pin['flavour']}, "
        f"{pin['size'] >> 20} MiB, sha1 {pin['sha1']}")
    try:
        nearby.fetch(zip_path, pin)
        installed = avd.install_system_image(
            zip_path, api=pin["api"], flavour=pin["flavour"], abi=pin["abi"],
        )
    except (avd.AvdError, iso.IsoError) as exc:
        log(f"FAILED: {exc}")
        return 1

    for required in ("system.img", "vendor.img", "ramdisk.img", "kernel-ranchu"):
        path = os.path.join(installed, required)
        if not os.path.isfile(path):
            log(f"FAILED: {required} is missing from {installed}; "
                f"mkdisks.py cannot build a bootable disk set without it")
            return 1

    log(f"installed at {installed}")
    print(f"SYSTEM_IMAGE_DIR={installed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
