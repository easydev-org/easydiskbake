"""Turn Google's system-image zip into an AVD, and back into a publishable tar.

    from avd import install_system_image, create_avd, pack, unpack

    install_system_image("x86_64-35_r02.zip")   # into $ANDROID_SDK_ROOT
    create_avd("sandbox", api=35)
    pack("/opt/android-avd", "avd.tar.zst")     # to publish
    unpack("avd.tar.zst", "/opt/android-avd")   # on the bake side

WHY AN AVD AND NOT THE RAW IMAGES

`easysandbox-android` reached the same conclusion at build time: it runs `avdmanager
create avd` once while building the image, then ships `/opt/android-avd`. An AVD is a
directory holding `config.ini` and the disk images, and the emulator needs the
`config.ini` - screen size, memory, GPU mode, whether the data partition is 4G - to
boot the images in a shape the sandbox expects.

Packing that directory means the bake pulls one artifact and starts the emulator, with
no SDK manager, no licence prompt, and no `avdmanager` call at bake time.

WHAT THE CONFIG FIXES, AND WHY

The defaults an AVD gets from `-d pixel_6` are a phone in portrait. The sandbox is a
desktop: 1024x768 landscape, matching the Windows, macOS and Linux sandboxes, so a
viewer sees the same shape whichever platform they are driving. That is a deliberate
deviation from the device profile, and it is the reason the config is written out
explicitly rather than left to the default.

The GPU mode is in the config as well as on the command line because the emulator
reads it from both places and the config wins in some versions.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile


def log(msg: str) -> None:
    print(f"[avd] {msg}", flush=True)


class AvdError(RuntimeError):
    pass


# The shape every sandbox uses. 1024x768 is what easysandbox-android chose so the
# streamed desktop matches the other platforms; the rest is what makes a phone AVD
# behave like a desktop with a mouse.
CONFIG = {
    "hw.lcd.width": "1920",
    "hw.lcd.height": "1080",
    "hw.lcd.density": "240",
    "hw.initialOrientation": "landscape",
    "hw.keyboard": "yes",
    "hw.mouse": "yes",
    "hw.dPad": "no",
    "hw.mainKeys": "no",
    "hw.camera.back": "none",
    "hw.camera.front": "none",
    "hw.gpu.enabled": "yes",
    "hw.gpu.mode": "swiftshader_indirect",
    "hw.audioInput": "no",
    "hw.audioOutput": "no",
    "hw.sdCard": "no",
    "hw.trackBall": "no",
    "hw.gyroscope": "no",
    "hw.sensors.orientation": "no",
    "hw.sensors.proximity": "no",
    "disk.dataPartition.size": "4G",
    "skin.name": "1920x1080",
    "skin.path": "_no_skin",
    "showDeviceFrame": "no",
}


def sdk_root() -> str:
    root = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if not root:
        raise AvdError(
            "ANDROID_SDK_ROOT is not set. The bake image sets it to /opt/android-sdk."
        )
    return root


def avd_home() -> str:
    home = os.environ.get("ANDROID_AVD_HOME", "/opt/android-avd")
    # avdmanager only honours ANDROID_AVD_HOME when the directory already exists.
    os.makedirs(home, exist_ok=True)
    return home


def install_system_image(zip_path: str, *, api: int = 35, flavour: str = "default",
                         abi: str = "x86_64") -> str:
    """Unpack Google's zip into $ANDROID_SDK_ROOT/system-images/android-<api>/<flavour>/<abi>.

    The zip's own layout is exactly that path, so there is nothing to rearrange: it
    contains `x86_64/` at its root and the SDK wants it one level down. sdkmanager
    would do this and also fetch a licence prompt, which is not something a CI job
    should have to answer.
    """
    root = sdk_root()
    target = os.path.join(root, "system-images", f"android-{api}", flavour, abi)

    # "exists and is not empty" is not the same as "is installed". The first version
    # returned early on the former, so a directory left behind by an interrupted unpack
    # - or by an earlier version of this function that wrote the wrong layout - was
    # treated as a finished system image and every later step failed somewhere else.
    # The file that decides it is system.img, the one thing the emulator cannot start
    # without.
    if os.path.isfile(os.path.join(target, "system.img")):
        log(f"system image already installed at {target}")
        return target

    log(f"unpacking {os.path.basename(zip_path)} -> {target}")
    os.makedirs(target, exist_ok=True)

    result = subprocess.run(
        ["unzip", "-q", "-o", zip_path, "-d", target],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AvdError(f"unzip failed: {result.stderr[:400]}")

    # The zip contains a single directory named after the abi. Flatten it, because
    # that is the layout avdmanager looks for.
    nested = os.path.join(target, abi)
    if os.path.isdir(nested):
        for entry in os.listdir(nested):
            shutil.move(os.path.join(nested, entry), os.path.join(target, entry))
        os.rmdir(nested)

    for required in ("system.img", "vendor.img"):
        if not os.path.exists(os.path.join(target, required)):
            raise AvdError(
                f"{required} is missing from {target}; the zip did not contain a "
                f"system image"
            )

    # userdata.img IS NOT IN THE ZIP AND MUST NOT BE REQUIRED.
    #
    # The first version listed it and every publish failed on a zip that had downloaded
    # and sha1-verified correctly:
    #
    #     FAILED: userdata.img is missing from .../android-35/default/x86_64
    #
    # Google ships an empty /data template instead (data/empty_data_disk), and the
    # emulator creates userdata-qemu.img from it on first boot. Requiring the file here
    # made a correct zip look corrupt.
    log(f"installed: {', '.join(sorted(os.listdir(target))[:6])}...")
    return target


def create_avd(name: str, *, api: int = 35, flavour: str = "default",
               abi: str = "x86_64") -> str:
    """Create the AVD and write the config the sandbox needs. Returns its directory."""
    root = sdk_root()
    home = avd_home()
    package = f"system-images;android-{api};{flavour};{abi}"
    avd_dir = os.path.join(home, f"{name}.avd")

    if os.path.isdir(avd_dir):
        log(f"AVD already exists at {avd_dir}")
        return avd_dir

    avdmanager = os.path.join(root, "cmdline-tools", "latest", "bin", "avdmanager")
    if not os.path.exists(avdmanager):
        raise AvdError(
            f"avdmanager not found at {avdmanager}. The build image needs "
            f"cmdline-tools; the bake image does not, because the AVD is created once "
            f"and published."
        )

    log(f"creating AVD {name} from {package}")
    result = subprocess.run(
        [avdmanager, "create", "avd", "-n", name, "-k", package,
         "-d", "pixel_6", "--force"],
        input="no\n", capture_output=True, text=True,
        env={**os.environ, "ANDROID_AVD_HOME": home},
    )
    if result.returncode != 0:
        raise AvdError(f"avdmanager failed: {result.stderr[:600]}")

    config_path = os.path.join(avd_dir, "config.ini")
    with open(config_path, "a", encoding="utf-8") as fh:
        fh.write("\n# the sandbox shape - see lib/avd.py for why these are explicit\n")
        for key, value in CONFIG.items():
            fh.write(f"{key}={value}\n")

    log(f"wrote {len(CONFIG)} config entries to {config_path}")
    return avd_dir


def pack(avd_home_dir: str, out: str) -> str:
    """Tar the AVD directory. zstd, because the images are mostly sparse zeros.

    REFUSES AN AVD THAT HOLDS NO DISK. `avdmanager create avd` writes config.ini and
    nothing else - the disk images (system.img, userdata.img, cache.img) are made by the
    emulator when it first starts the AVD. Packing straight after create therefore
    produces a 1.6 KB archive of one config file, and that is exactly what the first
    publish-android run pushed to ghcr:

        [avd] packing /opt/android-avd -> avd.tar.zst
        [avd] 0 MiB

    Nothing downstream noticed: lib/publish_disk.py pushes whatever it is given and the
    tag listing showed a manifest. So the check lives here, where the missing files are
    still visible.
    """
    if not os.path.isdir(avd_home_dir):
        raise AvdError(f"no AVD directory at {avd_home_dir}")

    # Find the .avd directory and require a system image in it.
    avds = [d for d in os.listdir(avd_home_dir) if d.endswith(".avd")]
    if not avds:
        raise AvdError(
            f"no *.avd directory in {avd_home_dir}; avdmanager did not create one, or "
            f"--avd-home points somewhere else"
        )
    # WHAT THE FIRST BOOT ACTUALLY WRITES, AND WHY IT IS NOT system.img
    #
    # This check used to require system.img and userdata.img in the AVD directory, and
    # run 41480233705 failed on both even though the emulator had booted:
    #
    #     [emulator] sys.boot_completed after 30s
    #     [emulator] stopped cleanly (adb emu kill)
    #     FAILED: the AVD holds no disk: sandbox.avd/system.img, sandbox.avd/userdata.img
    #
    # Neither file belongs there:
    #
    #     system.img    lives in the SYSTEM IMAGE directory
    #                   ($ANDROID_SDK_ROOT/system-images/android-35/default/x86_64/),
    #                   is read-only, and is shared by every AVD that uses that image.
    #                   It is an input to the boot, not something the boot produces.
    #
    #     userdata.img  is the read-only template Google ships as
    #                   data/empty_data_disk. The emulator COPIES it.
    #
    # What the first boot produces is the writable pair, both named after emulator's
    # own defaults for -data and -cache:
    #
    #     userdata-qemu.img   the /data partition. This is the disk the bake is about:
    #                         it is where the gold marker written by Emulator.mark()
    #                         survives, and what a verify run reads back.
    #     cache.img           the /cache partition.
    #
    # The module already said as much eleven lines above create_sysimg()'s return -
    # "userdata.img IS NOT IN THE ZIP AND MUST NOT BE REQUIRED ... the emulator creates
    # userdata-qemu.img from it on first boot" - and pack() then required it anyway.
    missing = []
    for name in avds:
        for required in ("userdata-qemu.img", "cache.img"):
            path = os.path.join(avd_home_dir, name, required)
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                missing.append(f"{name}/{required}")
    if missing:
        raise AvdError(
            f"the AVD holds no disk: {', '.join(missing)}.\n"
            f"avdmanager only writes config.ini; userdata-qemu.img and cache.img are "
            f"created when the emulator first starts the AVD. Boot it once (with "
            f"-wipe-data) before packing, or this archive is a config file and nothing "
            f"else."
        )

    if os.path.exists(out):
        os.unlink(out)

    log(f"packing {avd_home_dir} -> {out}")
    # -S keeps the sparse holes; the data partition is 4G of them and a plain tar
    # would write every zero.
    result = subprocess.run(
        ["tar", "--sparse", "-C", avd_home_dir, "-I", "zstd -3 -T0",
         "-cf", out, "."],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AvdError(f"tar failed: {result.stderr[:400]}")

    log(f"{os.path.getsize(out) >> 20} MiB")
    return out


def unpack(archive: str, dest: str) -> str:
    """Restore a packed AVD. zstd again, and --sparse so the holes stay holes."""
    os.makedirs(dest, exist_ok=True)

    log(f"unpacking {archive} -> {dest}")
    result = subprocess.run(
        ["tar", "--sparse", "-C", dest, "-I", "zstd -d -T0", "-xf", archive],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AvdError(f"tar failed: {result.stderr[:400]}")

    avds = [d for d in os.listdir(dest) if d.endswith(".avd")]
    if not avds:
        raise AvdError(f"no *.avd directory in {dest} after unpacking")
    log(f"restored: {', '.join(avds)}")
    return os.path.join(dest, avds[0])


def list_avds(home: str | None = None) -> list[str]:
    home = home or avd_home()
    if not os.path.isdir(home):
        return []
    return sorted(d[:-4] for d in os.listdir(home) if d.endswith(".avd"))
