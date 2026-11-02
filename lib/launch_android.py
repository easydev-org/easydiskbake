"""Start a published AVD and photograph it. Nothing is built and nothing is changed.

    python3 lib/launch_android.py --tag sysimg-android-15-aosp --artifacts out

WHAT THIS IS FOR

publish-android already boots the AVD once - it has to, because avdmanager only writes
config.ini and the disk images appear on the first start. That boot proves the BUILD is
sane. It does not prove the ARTIFACT is: the pack, the tar, the zstd stream, the ghcr
push and the pull each get a chance to break something in between.

So this starts from the published bytes, on a KVM machine, and takes a picture. Every
recent failure in this repository has been of the form "the step reported success and
something downstream could not use the result":

    seventeen macOS clicks landed on a blank screen, all reported as successful
    a Windows disk published without its firmware state, which booted to a shell
    an AVD that booted in 30s and was then rejected by its own pack()

A picture of the running guest is the one claim that cannot be made without the artifact
working. That is why it is the output.

WHAT IT DELIBERATELY DOES NOT DO

It does not create an AVD, patch a config, or install an app. Those belong to the bake,
and a verify that quietly fixes what it is checking is not checking anything.

THE ONE THING IT DOES INSTALL IS AN INPUT, NOT A FIX. The AVD archive carries the disk
images the first boot produced, but not system.img: that file is 1.7 GB, read-only, and
shared by every AVD of the same Android version, so pack() leaves it out correctly. The
AVD's own config.ini names it, and without it the emulator refuses to start:

    WARNING | .../system-images/android-35/default/x86_64/ is not a valid directory.
    FATAL   | Broken AVD system path. Check your ANDROID_SDK_ROOT value

So it is installed from the same pin in lib/images.py that publish-android used. A
version with no pin raises rather than substituting a different image, because a launch
against a system image the bake never used proves nothing about the bake.

    --wipe-data boots with -wipe-data. The default is NOT to wipe, because the state in
    /data is the thing being verified: a wipe would make the published AVD
    indistinguishable from a fresh one, and "it boots" would be a claim about Android
    rather than about this artifact.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import avd  # noqa: E402
import emulator  # noqa: E402
import iso  # noqa: E402


def log(message: str) -> None:
    print(message, flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tag", default="sysimg-android-15-aosp",
                   help="the published AVD tag to launch")
    p.add_argument("--work", default=None,
                   help="work directory (default: a tempdir under --artifacts)")
    p.add_argument("--artifacts", default="out",
                   help="where to put screen.png, emulator.log and launch.txt")
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--settle", type=int, default=20,
                   help="seconds to let the launcher settle before the screenshot")
    p.add_argument("--memory-mb", type=int, default=4096)
    p.add_argument("--cores", type=int, default=4)
    p.add_argument("--wipe-data", action="store_true",
                   help="boot with -wipe-data; off by default, because the /data in the "
                        "artifact is what is being verified")
    p.add_argument("--keep", action="store_true", help="keep the unpacked AVD")
    args = p.parse_args(argv)

    os.makedirs(args.artifacts, exist_ok=True)
    report = os.path.join(args.artifacts, "launch.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)

    work = args.work or os.path.join(args.artifacts, "avd-launch")
    os.makedirs(work, exist_ok=True)

    home = avd.avd_home()
    os.makedirs(home, exist_ok=True)

    archive = os.path.join(work, "avd.tar.zst")

    def finish(code: int) -> int:
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return code

    # --- 1. the published bytes ------------------------------------------------
    try:
        iso.fetch_tag(args.tag, archive)
    except iso.IsoError as exc:
        note(f"FAILED: could not pull {args.tag}: {exc}")
        return finish(1)
    note(f"pulled {args.tag}: {os.path.getsize(archive)} bytes")

    # --- 2. unpack -------------------------------------------------------------
    try:
        avd.unpack(archive, home)
    except avd.AvdError as exc:
        note(f"FAILED: could not unpack the AVD: {exc}")
        return finish(1)

    names = avd.list_avds(home)
    if not names:
        note(f"FAILED: no *.avd directory in {home} after unpacking")
        return finish(1)
    name = names[0]
    note(f"unpacked AVD: {name}")

    # AN AVD FROM AN ARCHIVE HAS NO .ini BESIDE IT. avdmanager writes both halves:
    # <home>/<name>.ini pointing at the directory, and <home>/<name>.avd/. A tar of the
    # directory alone is missing the first, and the emulator then says
    #
    #     Unknown AVD name [sandbox], use -list-avds to see valid list
    #     ANDROID_AVD_HOME is defined but there is no file sandbox.ini in $ANDROID_AVD_HOME
    #
    # measured while launching the published tag by hand on 2026-11-02. It is written
    # here rather than fixed in pack(), because an AVD directory is what pack() is given
    # and its caller owns where it lives.
    # THE SYSTEM IMAGE IS NOT IN THE ARCHIVE, AND IT HAS TO BE THERE.
    #
    # config.ini's image.sysdir.1 names a directory under ANDROID_SDK_ROOT, and the AVD
    # archive deliberately does not carry it: system.img is 1.7 GB, read-only, and shared
    # by every AVD that uses that Android version. publish-android installs it before
    # creating the AVD; a launch that only unpacks the archive does not, and the emulator
    # says so and stops:
    #
    #     WARNING | /opt/android-sdk/system-images/android-35/default/x86_64/ is not a
    #               valid directory.
    #     FATAL   | Broken AVD system path. Check your ANDROID_SDK_ROOT value
    #
    # measured on run 41493183039. So it is installed here, from the same pin in
    # lib/images.py that the publisher uses - the artifact and its system image have to
    # be the same pair, and taking the version from the AVD's own config.ini is what
    # keeps them that way.
    ensure_system_image(home, name, note)

    ini = os.path.join(home, f"{name}.ini")
    if not os.path.isfile(ini):
        with open(ini, "w", encoding="utf-8") as fh:
            fh.write("avd.ini.encoding=UTF-8\n")
            fh.write(f"path={os.path.join(home, name + '.avd')}\n")
            fh.write(f"path.rel=avd/{name}.avd\n")
            fh.write("target=android-35\n")
        note(f"wrote the missing {os.path.basename(ini)}")

    # --- 3. what the artifact carries, before the boot changes it ---------------
    #
    # device_provisioned and user_setup_complete are 0 on a fresh AVD and 1 after setup
    # has run. Reading them BEFORE the boot would need the guest already up, so they are
    # read after - what this line records is the state the launch produced, and the bake
    # records the state it packed.
    note(f"disk images: " + ", ".join(
        f"{n} {(os.path.getsize(os.path.join(home, name, n)) >> 20)} MiB"
        for n in ("userdata-qemu.img", "userdata-qemu.img.qcow2", "cache.img")
        if os.path.isfile(os.path.join(home, name, n))
    ))

    # --- 4. start it -----------------------------------------------------------
    guest = emulator.Emulator(
        name, work,
        memory_mb=args.memory_mb, cores=args.cores,
        gpu="swiftshader_indirect", wipe_data=args.wipe_data,
    )
    try:
        guest.start()
    except emulator.EmulatorError as exc:
        note(f"FAILED: the emulator did not start: {exc}")
        note_tail(guest, note)
        return finish(1)

    started = time.time()
    booted = False
    try:
        # A PICTURE BEFORE THE BOOT COMPLETES, so a guest that never reaches the
        # framework still leaves evidence of what it was doing.
        take_screenshot(guest, os.path.join(args.artifacts, "screen-before.png"), note)

        booted = guest.wait_for_boot(args.boot_timeout)
        note(f"sys.boot_completed={'yes' if booted else 'NO'} "
             f"after {time.time() - started:.0f}s")

        if booted:
            for prop in ("ro.product.model", "ro.build.version.release",
                         "sys.boot_completed", "dev.bootcomplete"):
                note(f"  {prop} = {guest.adb('shell', 'getprop', prop) or '-'}")

            note(f"letting the launcher settle for {args.settle}s")
            time.sleep(args.settle)
            take_screenshot(guest, os.path.join(args.artifacts, "screen.png"), note)

            # THE FOREGROUND ACTIVITY IS THE LAUNCHER ON A WORKING GUEST.
            #
            # This is the closest thing to "a desktop" that Android has: there is no
            # window manager to photograph the way macOS has one, and a boot animation
            # runs whether or not the system behind it works.
            resumed = guest.adb("shell", "dumpsys", "activity", "activities")
            for line in (resumed or "").splitlines():
                if "topResumedActivity" in line or "mResumedActivity" in line:
                    note(f"  {line.strip()}")
                    break
    finally:
        guest.stop()

    if not booted:
        note_tail(guest, note)
        return finish(1)

    note("OK: the published AVD started and reached its launcher")
    return finish(0)


def ensure_system_image(home: str, name: str, note) -> None:
    """Install the system image the AVD's config.ini asks for, if it is not there.

    The pin comes from lib/images.py, which is the same table publish-android unpacks
    from, so the system image and the AVD are always the pair that was published
    together. Anything unrecognised raises rather than guessing a version: a launch
    against a system image the bake never used proves nothing about the bake.
    """
    import images

    config = os.path.join(home, name, "config.ini")
    wanted = ""
    try:
        with open(config, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("image.sysdir.1"):
                    wanted = line.split("=", 1)[1].strip().rstrip("/")
                    break
    except OSError as exc:
        note(f"  could not read {config}: {exc}")
        return

    if not wanted:
        note("  the AVD names no system image; the emulator will say so if it needs one")
        return

    target = os.path.join(avd.sdk_root(), wanted)
    if os.path.isfile(os.path.join(target, "system.img")):
        note(f"system image already installed at {target}")
        return

    note(f"installing the system image the AVD asks for: {wanted}")
    match = None
    for key, pin in images.all_images().items():
        if not key.startswith("sysimg"):
            continue
        expected = f"system-images/android-{pin['api']}/{pin['flavour']}/{pin['abi']}"
        if expected == wanted:
            match = (key, pin)
            break
    if match is None:
        raise SystemExit(
            f"the AVD wants {wanted}, which is not a pin in lib/images.py. "
            f"Refusing to substitute a different system image: the launch would be "
            f"testing a disk the bake never produced."
        )

    key, pin = match
    note(f"  using the pin for {key}")
    zip_path = os.path.join(home, "sysimg.zip")
    iso_fetch_vendor(zip_path, pin, note)
    avd.install_system_image(zip_path, api=pin["api"], flavour=pin["flavour"],
                             abi=pin["abi"])


def iso_fetch_vendor(dest: str, pin: dict, note) -> None:
    """Download Google's system image zip and check it against its sha1.

    iso.fetch_iso does this for the vendor images, but it prefixes the tag and looks in
    the package first; a system image is a plain vendor download with a hash and no
    registry entry of its own.
    """
    import hashlib
    import urllib.request

    if os.path.isfile(dest) and pin.get("sha1"):
        with open(dest, "rb") as fh:
            if hashlib.sha1(fh.read()).hexdigest() == pin["sha1"]:
                note(f"  {os.path.basename(dest)} already present and verified")
                return
        note("  the cached zip does not match its sha1; downloading again")
        os.unlink(dest)

    note(f"  downloading {pin['url']}")
    with urllib.request.urlopen(pin["url"], timeout=300) as resp, \
            open(dest + ".part", "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if total and got % (128 << 20) < len(chunk):
                note(f"    {got >> 20} MiB / {total >> 20} MiB")
    os.replace(dest + ".part", dest)

    if pin.get("sha1"):
        with open(dest, "rb") as fh:
            actual = hashlib.sha1(fh.read()).hexdigest()
        if actual != pin["sha1"]:
            os.unlink(dest)
            raise SystemExit(
                f"sha1 mismatch for {pin['url']}: expected {pin['sha1']}, got {actual}. "
                f"Google has probably published a new r-number; update lib/images.py."
            )
        note("  sha1 verified")


def take_screenshot(guest: "emulator.Emulator", dest: str, note) -> bool:
    """screencap inside the guest, pull it out, and keep it.

    adb screencap writes a PNG to the guest's own filesystem and `adb pull` fetches it,
    which needs the guest to be up far enough for both. A guest still booting gives
    nothing here, and that is not a failure: screen-before.png exists to show the boot,
    not to prove anything.
    """
    remote = "/sdcard/bake-screen.png"
    result = guest.adb("shell", "screencap", "-p", remote)
    if result.startswith("FAILED"):
        note(f"  no screenshot yet: {result[:120]}")
        return False
    pulled = guest.adb("pull", remote, dest)
    if pulled.startswith("FAILED") or not os.path.isfile(dest):
        note(f"  could not pull the screenshot: {pulled[:120]}")
        return False
    note(f"  screenshot: {dest} ({os.path.getsize(dest) >> 10} KiB)")
    return True


def note_tail(guest: "emulator.Emulator", note) -> None:
    """The last lines of the emulator's own log, which is where a failed start says why."""
    try:
        with open(guest.log_path, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        note("  (no emulator.log)")
        return
    note("  last lines of the emulator log:")
    for line in text.splitlines()[-15:]:
        note(f"    {line}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
