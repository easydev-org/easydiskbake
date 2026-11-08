"""Stage the assets the macOS appliance carries, and pull the disk it boots.

    python3 macos/15/appliance.py fetch --out /assets      # build time, no disk
    python3 macos/15/appliance.py check --assets /assets   # build time
    python3 macos/15/appliance.py pull  --data /data --disk ssh   # run time, once per volume

WHY THE IMAGE NO LONGER CARRIES A DISK

The first version baked macos15-ssh into the image. That made a 16 GiB image that could boot
exactly one thing, and it answered a question nobody had asked: "give me a macOS disk that is
already at the ssh stage". The questions people actually have are:

    boot the installer again       macos15-setup, to walk Recovery from the start
    look at the wizard             macos15-desktop, a disk at the Setup Assistant
    get a working machine          macos15-ssh, remote login already on

Those are three different disks and they are all already published. So the image carries the
BOOT ENVIRONMENT - recovery image, OpenCore, firmware - and the disk arrives when the container
is asked for one:

    docker run ... easydiskbake:macos15-appliance                 # Recovery, no disk
    docker run ... easydiskbake:macos15-appliance --disk setup    # the installer
    docker run ... easydiskbake:macos15-appliance --disk desktop  # the wizard
    docker run ... easydiskbake:macos15-appliance --disk ssh      # Remote Login on

WHAT STAYS IN THE IMAGE, AND WHY IT IS NOT NOTHING

    recovery.dmg      opencore.build() extracts Apple's APFS driver out of it, and there is no
                      other source. A disk booted without that driver stops at a prohibited
                      sign, so this is not an optional download.

    opencore/*        the four pinned assets: the bootloader, the LongQT template, OVMF_CODE and
                      OVMF_VARS. 27 MiB, and together with the recovery image they are what makes
                      a first start possible with no network at all.

    16 MiB + 847 MiB is an image that builds in seconds. 16 GiB was a disk that took fifteen
    minutes to pull and could only ever be one release at one stage.

NO FALLBACK, IN THE SAME SENSE THE REST OF THIS REPOSITORY MEANS IT

Every fetch goes through lib/iso.py, which reads this repository's own ghcr package and nothing
else. There is no vendor URL and no mirror: a missing tag fails with the tag named, instead of
producing an image whose first start lands on a Setup Assistant because a file was quietly
different.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

REPO = os.environ.get("REPO") or os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import disk_parts     # noqa: E402
import macos_recovery  # noqa: E402
import opencore       # noqa: E402
import pull_disk      # noqa: E402

# THE DISKS A CONTAINER MAY BE ASKED TO BOOT, AND WHERE THEY COME FROM.
#
# The names are the short ones a person types. The tags are what this repository publishes, and
# they are NOT interchangeable with a release: `macos15-setup` is a 15 disk stopped at the
# Setup Assistant, `macos15-ssh` is a 15 disk with Remote Login on. A `--disk 15` would be a
# guess about which of those was meant.
DISKS = {
    # the installer: Recovery, Disk Utility, the whole walk from the beginning
    "setup": "macos15-setup",
    # the wizard: an install that has finished and stopped at the Setup Assistant
    "desktop": "macos15-desktop",
    # the finished article: Remote Login on, the disk the other stages were built from
    "ssh": "macos15-ssh",
}

# NO DISK IS A FIRST-CLASS CHOICE, NOT AN ABSENCE. Booting with nothing attached is how a
# person gets to Recovery, and the firmware reaches OpenCore either way - see appliance_run.py.
NO_DISK = "none"

# The four pinned OpenCore/firmware assets, under the names opencore.build() looks for in a work
# directory. The mapping is the contract: build() calls fetch_pinned(name, work/<this>), and a
# file already at that path with the right sha256 is used without a network call.
ASSET_FILES = {
    "opencore": "opencore.zip",
    "opencore-template": "opencore-template.iso",
    "ovmf-code": "macos.rom",
    "ovmf-vars-1920x1080": "OVMF_VARS-1920x1080.fd",
}


def log(message: str) -> None:
    print(f"[appliance {time.strftime('%H:%M:%S')}] {message}", flush=True)


def _place(source: str, dest: str, *, move: bool = True) -> None:
    """Put `source` at `dest`.

    `move=False` COPIES, and that is what staging needs: /assets is a read-only mount in the
    running container, so a link across filesystems cannot be made and a move would either fail
    on the read-only mount or delete an asset out of the image. The build-time fetches use the
    default, because there the source is a temporary download and holding both copies is the
    thing to avoid.
    """
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest):
        os.unlink(dest)
    if not move:
        shutil.copyfile(source, dest)
        return
    try:
        os.link(source, dest)
    except OSError:
        shutil.move(source, dest)


# --- build time -------------------------------------------------------------------------


def fetch(out: str) -> int:
    """The recovery image and the four pinned assets. NO DISK - see the module docstring."""
    os.makedirs(out, exist_ok=True)
    tmp = os.path.join(out, ".tmp")
    os.makedirs(tmp, exist_ok=True)

    # --- the recovery image, which is an input and not an installer here ---------------
    log("recovery image")
    recovery = macos_recovery.fetch_published("15", tmp)
    _place(recovery, os.path.join(out, "recovery.dmg"))
    log(f"  recovery.dmg: {os.path.getsize(os.path.join(out, 'recovery.dmg')) >> 20} MiB")

    # --- the four pinned boot assets ---------------------------------------------------
    pin_dir = os.path.join(out, "opencore")
    os.makedirs(pin_dir, exist_ok=True)
    for name, filename in ASSET_FILES.items():
        dest = os.path.join(pin_dir, filename)
        log(f"{name} -> {filename}")
        # fetch_pinned verifies the sha256 against lib/images.MACOS_ASSETS either way, so a tag
        # that was overwritten in the registry fails here rather than at first boot.
        opencore.fetch_pinned(name, dest)

    shutil.rmtree(tmp, ignore_errors=True)
    log("no disk in this image: one is pulled on first start, or none at all")
    return 0


def check(assets: str) -> int:
    """What the image must contain, asked of the bytes rather than of the Dockerfile."""
    problems = []

    def need(path: str, minimum: int) -> int:
        if not os.path.isfile(path):
            problems.append(f"{path} is missing")
            return 0
        size = os.path.getsize(path)
        if size < minimum:
            problems.append(f"{path} is {size} bytes, expected at least {minimum}")
        return size

    need(os.path.join(assets, "recovery.dmg"), 500 << 20)
    for name, filename in ASSET_FILES.items():
        need(os.path.join(assets, "opencore", filename), 1024)

    # A DISK IN THE IMAGE IS NOT AN ERROR, IT IS A DIFFERENT IMAGE.
    #
    # The first Containerfile baked one in. This check does not fail if a directory is there -
    # it says what it found, so a reader of the build log knows which kind of image they built
    # - but the appliance does not need one and the default build produces none.
    if os.path.isdir(os.path.join(assets, "disk")):
        try:
            manifest = disk_parts.read_manifest(os.path.join(assets, "disk"))
            log(f"a disk is baked in: {manifest['size'] >> 20} MiB in "
                f"{len(manifest['parts'])} part(s) - a --disk choice will still override it")
        except disk_parts.PartsError as exc:
            problems.append(str(exc))

    if problems:
        for problem in problems:
            log(f"  ! {problem}")
        return 1
    log("the boot environment is complete (recovery + OpenCore + firmware)")
    return 0


def stage(assets: str, work: str) -> str:
    """Copy the assets into a work directory, so opencore.build() needs no network.

    opencore.build() asks for four files by name in `work`, and its fetch_pinned() returns a
    file whose sha256 matches the pin without touching the registry. Staging them here is what
    makes the first boot offline.

    COPIES, NOT LINKS. /assets is mounted read-only in the running container, so a hard link
    across filesystems is impossible and a move would delete an asset out of the image. The
    files are 863 MiB together, which is the other reason copying is the right answer here and
    moving was right at build time.
    """
    os.makedirs(work, exist_ok=True)

    recovery = os.path.join(work, "recovery.dmg")
    if not os.path.exists(recovery):
        _place(os.path.join(assets, "recovery.dmg"), recovery, move=False)

    for filename in ASSET_FILES.values():
        dest = os.path.join(work, filename)
        if os.path.exists(dest):
            continue
        _place(os.path.join(assets, "opencore", filename), dest, move=False)

    return recovery


# --- run time ---------------------------------------------------------------------------


def resolve(name: str) -> str:
    """`ssh` -> `macos15-ssh`; an already-valid tag is passed through.

    A TAG IS ACCEPTED AS WELL AS A SHORT NAME because the published names carry a date-less tag
    but a reader may have a `macos15-base-20261104-010259` from an older run, and refusing it
    would mean editing this file to boot it. Anything that is not a known short name and not a
    plausible tag is refused with the list, rather than being turned into a 404 halfway through
    a pull.
    """
    key = (name or "").strip()
    if not key:
        raise KeyError("no disk named")
    if key in DISKS:
        return DISKS[key]
    if key.startswith("macos") or "-" in key:
        return key
    raise KeyError(
        f"unknown disk {name!r}. Known: {', '.join(k for k in DISKS)}, "
        f"or {NO_DISK} for no disk at all, or a published tag such as macos15-ssh")


def pull_disk_named(name: str, data: str) -> str:
    """Pull `name` onto the volume and split it into parts. Idempotent.

    WHY THE DISK IS STILL SPLIT WHEN IT IS PULLED RATHER THAN BAKED

    lib/disk_parts.py exists because a `COPY` layer over 10 GB has never been measured here and
    an image build is the one path that would do it. At run time there is no COPY - the disk is
    a single file pulled by lib/iso.py, which this repository has measured at 27 GiB. So the
    parts are here for a different reason: they are the manifest that says the bytes on the
    volume are the bytes that were published, checked on every start rather than trusted once.

    Returns the joined path, or "" for NO_DISK.
    """
    if name == NO_DISK:
        log("no disk: this start goes to Recovery")
        return ""

    tag = resolve(name)
    parts = os.path.join(data, "parts", tag)
    joined = os.path.join(data, f"{tag}.qcow2")

    # THE JOINED DISK IS THE CACHE. A second start with the same --disk finds it and does
    # nothing; a start with a different one leaves it alone, because a volume can hold several
    # and the user picked one.
    if os.path.exists(joined):
        log(f"{tag}: already on the volume ({os.path.getsize(joined) >> 20} MiB)")
        return joined

    log(f"=== pulling {tag} ===")
    os.makedirs(parts, exist_ok=True)
    pulled = pull_disk.pull(tag, parts)
    log(f"  {pulled}: {os.path.getsize(pulled) >> 20} MiB")

    # SPLIT THEN JOIN, WHICH LOOKS CIRCULAR AND IS NOT.
    #
    # pull_disk writes one file; disk_parts.split records a sha256 per part and for the whole
    # disk. The join below verifies those hashes as it writes, so a truncated download is caught
    # here - once - instead of twenty minutes into a boot. The parts are kept because they are
    # cheap next to the joined disk and they are what makes the check repeatable on a later
    # start rather than a fact about one afternoon.
    manifest = disk_parts.split(pulled, parts, parts=disk_parts.PARTS)
    os.unlink(pulled)
    log(f"  manifest {manifest}")

    disk_parts.join(parts, joined)
    return joined


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="pull the boot environment into --out (build time)")
    f.add_argument("--out", default="/assets")

    c = sub.add_parser("check", help="verify the boot environment is all there (build time)")
    c.add_argument("--assets", default="/assets")

    d = sub.add_parser("pull", help="pull one disk onto a volume (run time)")
    d.add_argument("--data", default="/data")
    d.add_argument("--disk", default=NO_DISK,
                   help=f"one of {', '.join(DISKS)}, {NO_DISK}, or a published tag")
    d.add_argument("--list", action="store_true", help="print the choices and exit")

    a = p.parse_args(argv)
    if a.cmd == "fetch":
        return fetch(a.out)
    if a.cmd == "check":
        return check(a.assets)

    if a.list:
        for short, tag in DISKS.items():
            print(f"{short:10} {tag}")
        print(f"{NO_DISK:10} (boot Recovery with no disk attached)")
        return 0

    try:
        print(pull_disk_named(a.disk, a.data))
    except (KeyError, disk_parts.PartsError) as exc:
        log(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
