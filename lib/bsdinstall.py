"""Get an answer file onto a BSD installation medium, and check the result.

    from lib import bsdinstall
    bsdinstall.fat_seed(work, {"install.conf": text})
    bsdinstall.remaster_iso(src, dst, {"/etc/installerconfig": text})

THE TWO BSDs ANSWER DIFFERENTLY AND THE TRANSPORT IS THE DIFFERENCE

    FreeBSD   bsdinstall reads /etc/installerconfig from the MEDIUM. The file has a
              preamble of environment variables and, after a #! line, a shell script that
              runs chrooted in the new system. Nothing prompts.


FreeBSD does not fetch an answer file from the network by default, and has no answer
file mechanism that can be handed over on the kernel command line the way Ubuntu's can.
So the file has to be ON the medium, and this module is the two documented ways of
getting it there:

    FAT SEED      a second disk image, FAT formatted, holding the answer file. bsdinstall
                  looks at removable media; this is the cheapest transport and it does
                  not touch the installer image.

    REMASTER      unpack the ISO, add the file, repack. Slower and it rewrites a
                  published artifact, but it is the only transport that works regardless
                  of what the installer scans.

WHY mtools AND NOT mkfs.vfat PLUS A MOUNT

Mounting needs root and a loop device, and neither exists inside every job that runs
this. mtools writes a FAT filesystem into a plain file with no privileges at all:
mformat creates it, mcopy puts a file in it. Both are in the bake image.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def log(message: str) -> None:
    print(f"[bsd {message}]", flush=True)


def _run(args: list[str], what: str) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"{what} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )
    return result


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def fat_seed(work: str, files: dict[str, str], name: str = "seed.img",
             size_mb: int = 8) -> str:
    """Write `files` into a FAT image, and return its path.

    THE NAMES ARE FLAT. mtools addresses files inside the image by name, not by path, so
    a caller wanting /etc/install.conf puts "install.conf" here and the installer finds it
    at whatever path it looks in. Both BSDs look at the ROOT of a removable medium for
    their answer file, which is why a flat image is enough.

    THE IMAGE IS CREATED, NOT REUSED. A leftover seed.img from a previous run in the same
    work directory would still be a valid filesystem with a valid - and possibly stale -
    answer file, and the difference between "the new file was written" and "the old one
    was found" is invisible from the log.
    """
    for tool in ("mformat", "mcopy"):
        if not have(tool):
            raise RuntimeError(
                f"{tool} is not installed. It is what writes a FAT image without a loop "
                f"device; the bake image carries mtools."
            )

    path = os.path.join(work, name)
    if os.path.exists(path):
        os.unlink(path)

    # mformat wants a file that exists and has a size; it does not truncate.
    with open(path, "wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)

    _run(["mformat", "-i", path, "-F", "::"], f"mformat {path}")

    for leaf, body in files.items():
        tmp = os.path.join(work, f".{leaf}.src")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        try:
            _run(["mcopy", "-i", path, "-o", tmp, f"::{leaf}"], f"mcopy {leaf}")
        finally:
            os.unlink(tmp)

    listing = _run(["mdir", "-i", path, "-/", "::"], f"mdir {path}") if have("mdir") else None
    if listing is not None and listing.stdout:
        for line in listing.stdout.splitlines():
            if line.strip():
                log(f"  seed: {line.strip()}")
    return path


def remaster_iso(src: str, dst: str, files: dict[str, str],
                 work: str | None = None) -> str:
    """Repack `src` with `files` added, into `dst`.

    ONLY FOR A FILE THE INSTALLER READS FROM INSIDE THE IMAGE'S ROOT. FreeBSD's
    /etc/installerconfig lives in the memory filesystem the medium boots, not on the ISO's
    own directory tree, so adding a path to the ISO is not enough on every release - which
    is why the caller decides between this and fat_seed rather than this deciding.

    THE TOOL IS WHATEVER EXISTS. xorriso keeps the El Torito boot record intact and is
    the right tool; genisoimage is the fallback with the same flags for this use. Asking
    for a specific one by name would fail in a job that had the other.
    """
    if work is None:
        work = os.path.dirname(os.path.abspath(dst)) or "."
    tree = os.path.join(work, "iso-tree")
    if os.path.exists(tree):
        shutil.rmtree(tree)
    os.makedirs(tree)

    _run(["7z", "x", "-y", f"-o{tree}", src], f"unpack {src}")

    for path, body in files.items():
        target = os.path.join(tree, path.lstrip("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        log(f"  added {path}")

    if have("xorriso"):
        _run([
            "xorriso", "-as", "mkisofs", "-o", dst,
            "-b", "boot/cdboot", "-no-emul-boot", "-c", "boot/boot.catalog",
            tree,
        ], "xorriso")
    elif have("genisoimage"):
        _run([
            "genisoimage", "-o", dst, "-b", "boot/cdboot", "-no-emul-boot",
            "-c", "boot/boot.catalog", tree,
        ], "genisoimage")
    else:
        raise RuntimeError(
            "neither xorriso nor genisoimage is installed, so the ISO cannot be repacked. "
            "Use fat_seed() instead if the installer reads its answer file from "
            "removable media, which is the case this module prefers."
        )
    return dst


def check_disk_ufs(disk: str) -> bool:
    """Does this qcow2 hold a UFS/FFS filesystem?

    WHAT IT LOOKS FOR. A UFS2 superblock lives at 65536 bytes into its partition, with
    the magic 0x19540119 in fs_magic. lib/smoke.py checks for an ext superblock at 1 MiB
    and says so; BSD is not ext, so a BSD disk passes that call's MBR half and skips the
    filesystem half - which is honest and is also why this exists.

    A FAILED INSTALL LOAVES A PARTITION TABLE AND NO FILESYSTEM, so this is the check
    that separates "the installer ran" from "the installer worked".
    """
    import struct

    # Read the first 8 MiB: the boot blocks, the partition table and the superblock of a
    # partition that starts at sector 0 or just after it.
    with open(disk, "rb") as fh:
        head = fh.read(8 * 1024 * 1024)

    if len(head) < 1024 * 1024:
        return False

    # UFS2's magic, big or little endian, anywhere in the first 8 MiB. A byte scan is
    # used rather than a fixed offset because the partition's start depends on the
    # layout the installer chose, and the magic is distinctive enough (0x19540119).
    for magic in (b"\x19\x01\x54\x19", b"\x19\x54\x01\x19"):
        index = head.find(magic)
        if index >= 0:
            log(f"  UFS magic at offset {index}: {magic.hex()}")
            return True
    return False


def check_disk_zfs(disk: str) -> bool:
    """Does this qcow2 hold a ZFS pool?

    WHAT IT LOOKS FOR. Every vdev label begins with the eight bytes `\x00\x00\x00\x00\
    \x0c\xb1\xba\x00` - UBERBLOCK_MAGIC - and the four labels sit at the start and the end
    of each device (offset 0, 256 KiB, and the last 512 KiB, twice). A pool created by
    `bsdinstall zfsboot` writes all four.

    WHY A MAGIC SCAN AND NOT `zpool import`. The bake has no zpool on the host, and
    importing a pool to ask whether it exists would mount it, take the device exclusively,
    and make the next stage's qemu start fail with "pool is in use". Four magic values in
    the first megabyte answer the same question without touching the pool.

    A FAILED INSTALL LEAVES A PARTITION TABLE AND NO POOL, which is the same distinction
    check_disk_ufs() draws for UFS - see its docstring.
    """
    import struct

    magic = b"\x00\x00\x00\x00\x0c\xb1\xba\x00"

    # The first label is at offset 0 of the device; zfsboot puts the pool on the whole
    # vtbd0 with a partition table, so the label is at the start of the block device and
    # not at a partition offset. 1 MiB is enough to cover L0 and L1's first copy.
    try:
        with open(disk, "rb") as fh:
            head = fh.read(1024 * 1024)
    except OSError:
        return False
    if len(head) < 1024:
        return False

    index = head.find(magic)
    if index >= 0:
        # The pool name is in the label's nvlist, and finding it is not worth a parser
        # here - the next stage names the pool itself. What this reports is the offset,
        # so a "found nothing" and a "found it in the wrong place" look different.
        log(f"  ZFS label magic at offset {index}")
        return True
    return False


def iso_boot_files(iso_path: str) -> dict[str, str]:
    """Which boot-image path inside an ISO a repack has to name.

    THE EL TORITO RECORD DIFFERS BETWEEN THE TWO BSDs and guessing it wrong produces an
    ISO that looks right and does not boot. FreeBSD's live media boots boot/cdboot;
    Rebuilding it means naming the boot record, and this reports what it found rather than
    assumed, so a caller can say what it found when a boot fails.
    """
    found: dict[str, str] = {}
    result = subprocess.run(["xorriso", "-indev", iso_path, "-report_el_torito", "plain"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return found
    for line in (result.stdout or "").splitlines():
        if "El Torito boot img" in line:
            found.setdefault("eltorito", line.strip())
    return found
