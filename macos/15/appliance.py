"""Stage everything the macOS appliance image carries, at image build time.

    python3 macos/15/appliance.py fetch --out /assets
    python3 macos/15/appliance.py check --assets /assets

WHY THIS RUNS AT BUILD TIME AND NOT AT CONTAINER START

The appliance is a container you start and it boots macOS. That means every input the boot
needs has to be inside the image, because the first start is not allowed to depend on ghcr
being reachable or on a 16 GiB download finishing before anything appears on screen:

    recovery  :macos-recovery-sequoia     847 MiB   the APFS driver is extracted from it
    opencore  :macos-asset-opencore        10 MiB   the bootloader
    template  :macos-asset-opencore-...    15 MiB   kexts, drivers, the config that boots
    firmware  :macos-asset-ovmf-code        2 MiB   OVMF_CODE.fd
    vars      :macos-asset-ovmf-vars-...   smallest   the GOP resolution is baked in here
    the disk  :macos15-ssh               15.9 GiB   a finished Sequoia install with sshd on

FIVE OF THOSE ARE SMALL AND ONE IS NOT. The disk is split into 4 GiB parts by
lib/disk_parts.py. The reason is narrower than "the registry forbids it": this repository does
publish 27.57 GiB in one oras layer today and it works. What has never been measured is a
`COPY` layer above 10 GB, which is the path an image build takes, and the disk is 15.9 GiB -
so the parts keep every layer under the documented ceiling. lib/disk_parts.py carries the
measurements. The parts are joined on first start onto the volume the container was given.

NO FALLBACK, IN THE SAME SENSE THE REST OF THIS REPOSITORY MEANS IT

Every fetch here goes through lib/iso.py, which reads this repository's own ghcr package and
nothing else. There is no vendor URL, no mirror, and no "download it from Apple if the tag is
missing" - a build with a missing tag fails here, with the tag named, instead of producing an
image whose first start lands on a Setup Assistant because a file was quietly different.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

REPO = os.environ.get("REPO") or os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import disk_parts     # noqa: E402
import macos_recovery  # noqa: E402
import opencore       # noqa: E402
import pull_disk      # noqa: E402

# THE DISK THE APPLIANCE BOOTS. macos15-ssh, not macos15-desktop: the ssh stage is the one
# that turns Remote Login on and is the last stage that produces a disk, so it is the only
# tag that boots to a login screen you can actually log in to.
DISK_TAG = "macos15-ssh"

# The four pinned OpenCore/firmware assets, under the names opencore.build() looks for in a
# work directory. The mapping is the contract: build() calls fetch_pinned(name, work/<this>),
# and a file already at that path with the right sha256 is used without a network call.
ASSET_FILES = {
    "opencore": "opencore.zip",
    "opencore-template": "opencore-template.iso",
    "ovmf-code": "macos.rom",
    "ovmf-vars-1920x1080": "OVMF_VARS-1920x1080.fd",
}


def log(message: str) -> None:
    print(f"[appliance {__import__('time').strftime('%H:%M:%S')}] {message}", flush=True)


def _place(source: str, dest: str, *, move: bool = True) -> None:
    """Put `source` at `dest`.

    `move=False` COPIES, and that is what staging needs: /assets is a read-only mount in the
    running container, so a link across filesystems cannot be made and a move would either
    fail on the read-only mount or delete an asset out of the image. The build-time fetches
    use the default, because there the source is a temporary download and holding both copies
    of 16 GiB is the thing to avoid.
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


def fetch(out: str) -> int:
    os.makedirs(out, exist_ok=True)
    tmp = os.path.join(out, ".tmp")
    os.makedirs(tmp, exist_ok=True)

    # --- the recovery image, which is an input and not an installer here ---------------
    #
    # opencore.build() extracts Apple's APFS driver out of this image, and there is no other
    # source for it. A disk booted without that driver stops at a prohibited sign.
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
        # fetch_pinned verifies the sha256 against lib/images.MACOS_ASSETS either way, so a
        # tag that was overwritten in the registry fails here rather than at first boot.
        opencore.fetch_pinned(name, dest)

    # --- the disk, split ---------------------------------------------------------------
    log(f"disk {DISK_TAG}")
    pulled = pull_disk.pull(DISK_TAG, tmp)
    log(f"  {pulled}: {os.path.getsize(pulled) >> 20} MiB")
    manifest = disk_parts.split(pulled, os.path.join(out, "disk"))
    os.unlink(pulled)

    # THE PARTS HAVE TO GO, they are most of the image and the disk has been re-read once
    # to write the manifest. Keeping the source would double what the builder holds.
    shutil.rmtree(tmp, ignore_errors=True)
    log(f"manifest {manifest}")
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
    need(os.path.join(assets, "disk", disk_parts.MANIFEST), 64)

    manifest_path = os.path.join(assets, "disk", disk_parts.MANIFEST)
    if os.path.isfile(manifest_path):
        manifest = disk_parts.read_manifest(os.path.join(assets, "disk"))
        for entry in manifest["parts"]:
            need(os.path.join(assets, "disk", entry["name"]), entry["size"])
        log(f"disk: {manifest['size'] >> 20} MiB in {len(manifest['parts'])} part(s), "
            f"sha256 {manifest['sha256'][:16]}...")

    if problems:
        for problem in problems:
            log(f"  ! {problem}")
        return 1
    log("every asset the appliance boots from is present")
    return 0


def stage(assets: str, work: str) -> str:
    """Copy the assets into a work directory, so opencore.build() needs no network.

    THIS IS CALLED AT CONTAINER START, once per volume. opencore.build() asks for four files
    by name in `work`, and its fetch_pinned() returns a file whose sha256 matches the pin
    without touching the registry. Staging them here is what makes the first boot offline.

    COPIES, NOT LINKS. /assets is mounted read-only in the running container, so a hard link
    across filesystems is impossible and a move would delete an asset out of the image. The
    four files are 16 MiB together, which is the other reason copying is the right answer
    here and moving was right at build time.
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


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="pull every asset into --out (build time)")
    f.add_argument("--out", default="/assets")

    c = sub.add_parser("check", help="verify the assets are all there (build time)")
    c.add_argument("--assets", default="/assets")

    a = p.parse_args(argv)
    if a.cmd == "fetch":
        return fetch(a.out)
    return check(a.assets)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
