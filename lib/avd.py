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
    "hw.lcd.width": "1024",
    "hw.lcd.height": "768",
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
    "skin.name": "1024x768",
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

    if os.path.isdir(target) and os.listdir(target):
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

    for required in ("system.img", "userdata.img"):
        if not os.path.exists(os.path.join(target, required)):
            # vendor.img is separate in newer releases and is not always present.
            raise AvdError(
                f"{required} is missing from {target}; the zip did not contain a "
                f"system image"
            )

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
    """Tar the AVD directory. zstd, because the images are mostly sparse zeros."""
    if not os.path.isdir(avd_home_dir):
        raise AvdError(f"no AVD directory at {avd_home_dir}")

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
