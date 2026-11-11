"""Fetch the pinned BlissOS ISO, verified, and unpack the three files qemu needs.

    python3 android/blissos16/fetch_iso.py --out out/bliss

WHY BLISSOS AND NOT AOSP

This is the only Android path in this repository, and it is the only one there can be. An
earlier route booted Google's AOSP system image through the SDK's `emulator` binary, which
means goldfish/ranchu: `kernel-ranchu` is not a bzImage (its first 497 bytes are 0xff and
the boot flag is missing), `-system` does not publish GPT partition uevents so the disks
have to be split into three that must stay paired, /data comes up metadata-encrypted on
nosuid dm-42, and `su` refuses every uid but root and shell. That was 1400 lines of
workarounds, and the one thing it could not do was produce a disk that starts itself.

BlissOS is Android-x86: a standard bzImage, an initrd that assembles the root from a
ramdisk, and /data as a plain ext4 file. Stock qemu boots it with -kernel and -initrd,
and measured on 16.9.7:

    kernel      bzImage, HdrS at 0x1F1, boot flag 55aa, protocol 2.15
    system.efs  EROFS
    /data       /dev/block/vdb on /data type ext4 (rw,seclabel,noatime), 16 GiB
    adb root    uid=0(root) context=u:r:su:s0

WHAT THIS SCRIPT PRODUCES

The three files the launch needs, taken out of the ISO once:

    kernel      the bzImage qemu loads with -kernel
    initrd.img  Android-x86's init, which finds SRC= and mounts DATA=
    system.efs  read-only EROFS, attached as the first virtio disk

WHY THE ISO IS KEPT AND NOT THE EXTRACTED FILES ALONE

The ISO is the pinned artifact - one sha256 names the whole thing. Extracting kernel and
initrd from it is cheap, and re-extracting from a verified ISO is the only way to be sure
the kernel belongs to the system.efs beside it.

THE PIN IS A SHA256 BECAUSE SOURCEFORGE PUBLISHES NO CHECKSUM FOR THIS FILE. The hash
below was measured from a complete download, not copied from a web page: the repository
has no upstream number to compare against, so the number has to come from the bytes.
That still catches a truncated, replaced or partially-mirrored download, which is what
the check is for, but it is a weaker claim than a vendor-published one - and it is
recorded here as such rather than presented as stronger than it is.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "lib"))

import images  # noqa: E402
import iso  # noqa: E402

KEY = "iso:blissos-16.9.7"
# THE THREE FILES QEMU IS HANDED, and the pin already names them (`members`). One statement, so a
# fifth member cannot be added to one list and forgotten in the other.
MEMBERS = images.BLISSOS[KEY]["members"]



def log(msg: str) -> None:
    print(f"[bliss {__import__('time').strftime('%H:%M:%S')}] {msg}", flush=True)


def seven_zip() -> str:
    """The binary that can read this ISO, or a sentence saying which it looked for.

    `7z` is in the repository's base image because lib/opencore.py needed the Joliet layer
    of an ISO9660 image. The same tool reads this one, which is why nothing new has to be
    installed to unpack it.
    """
    for name in ("7z", "7za", "7zr"):
        found = subprocess.run(["sh", "-c", f"command -v {name}"], capture_output=True,
                               text=True).stdout.strip()
        if found:
            return found
    raise SystemExit(
        "=== no 7z ON THIS MACHINE ===\n"
        "  the repository's image carries it (Containerfile, p7zip-full); outside that\n"
        "  image install p7zip-full. `xorriso` alone cannot read the Joliet tree the way\n"
        "  this needs, and python has no ISO9660 reader that keeps the long names.")


def extract(archive: str, out: str) -> None:
    """Put the three members at <out>/ with the names qemu expects."""
    sevenzip = seven_zip()
    os.makedirs(out, exist_ok=True)
    missing = [m for m in MEMBERS if not os.path.exists(os.path.join(out, m))]
    if not missing:
        log(f"all three members already extracted in {out}")
        return
    log(f"extracting {', '.join(missing)} from {os.path.basename(archive)}")
    result = subprocess.run(
        [sevenzip, "x", "-y", f"-o{out}", archive, *MEMBERS],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"=== 7z COULD NOT READ {archive} ===\n"
            f"{result.stdout[-800:]}"
            f"{result.stderr[-800:]}")
    for member in MEMBERS:
        path = os.path.join(out, member)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise SystemExit(f"=== {member} IS MISSING FROM {archive} ===")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/bliss",
                   help="where the ISO and the extracted members go")
    p.add_argument("--keep-iso", action="store_true", default=True,
                   help="keep the ISO after extracting (default; it is the pinned artifact)")
    p.add_argument("--drop-iso", dest="keep_iso", action="store_false",
                   help="delete the ISO after a verified extraction, to save 2.2 GiB")
    a = p.parse_args(argv)

    pin = images.BLISSOS.get(KEY)
    if pin is None:
        print(f"=== {KEY} IS NOT IN lib/images.py ===", file=sys.stderr)
        return 1

    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    # THE FILE IS NAMED, BECAUSE THE URL'S LAST SEGMENT IS `download`.
    #
    # SourceForge serves every file through `/download`, so `os.path.basename(pin["url"])` is the
    # word `download` - and the ISO landed at out/bliss/download. The first CI run of the
    # YAML-only install then handed qemu `out/bliss/bliss16.iso` and qemu exited before the first
    # QMP message:
    #
    #     qemu-system-x86_64: -drive file=.../out/bliss/bliss16.iso,media=cdrom,readonly=on:
    #     Could not open '.../out/bliss/bliss16.iso': No such file or directory
    #
    # `name` is the pin's own statement of what this file is, and it is what the workflow and
    # `--boot-iso` both name.
    name = pin.get("name") or os.path.basename(pin["url"]).split("?")[0]
    archive = os.path.join(out, name)

    iso.fetch_url(pin["url"], archive, sha256=pin["sha256"], size=pin["size"])
    extract(archive, out)

    for member in MEMBERS:
        path = os.path.join(out, member)
        log(f"{member:12} {os.path.getsize(path):>13,} B")

    if not a.keep_iso:
        os.unlink(archive)
        log(f"dropped {os.path.basename(archive)} (it was verified before extraction)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
