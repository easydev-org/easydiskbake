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


def _read_virtual(disk: str, offset: int, length: int) -> bytes:
    """The guest's bytes at `offset`, not the qcow2 file's.

    A qcow2 IS NOT THE DISK. Its bytes are a sparse store - a header, L1/L2 tables, and
    clusters written in the order the guest touched them - so `open(disk, "rb")` and
    scanning the result asks a question about the container, not about the filesystem
    inside it.

    THIS WAS THE BUG, AND IT LOOKED LIKE LUCK FOR A LONG TIME. Both checks in this file
    used to read the file directly. UFS passed anyway, because a fresh bsdinstall writes
    a large contiguous run early in the guest's life and some of that text - including
    the superblock's magic - lands in the qcow2 file's first 8 MiB nearly verbatim. That
    is a coincidence of the allocation pattern, not a property of the format.

    ZFS broke the coincidence. Run 41991139008 installed all 394 packages in 135
    seconds, `bsdinstall` reached its `umount` step, the disk held 3705 MiB of files -
    and this check reported

        === THE DISK HOLDS NO FILESYSTEM ===
        neither a ZFS label nor a UFS superblock is on it

    a disk that was completely fine. The ZFS labels sit at the start of the POOL, which
    zfsboot places on a partition - so even in a raw image they are not at file offset
    0; in a qcow2 file they are wherever the cluster allocator put them.

    lib/smoke.qemu_io_read already had the right answer for the Linux and Windows
    checks: `qemu-io -f qcow2 -c "read -v <offset> <length>"` reads what the guest would
    see. This is the same call, for the same reason, and it takes the file format as a
    parameter rather than assuming it - the argument is quoted as qcow2 because every
    disk this repository publishes is one.
    """
    try:
        import smoke
    except ImportError:  # called outside the lib/ path layout
        import os
        import sys

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import smoke
    try:
        return smoke.qemu_io_read(disk, offset, length)
    except smoke.DiskReadError as exc:
        log(f"  could not read {offset}+{length} from {disk}: {exc}")
        return b""


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

    # READ THE GUEST'S BYTES, NOT THE qcow2 FILE'S. See the note on _read_virtual: a
    # qcow2 store is not the disk, and scanning it only ever worked by luck.
    head = _read_virtual(disk, 0, 8 * 1024 * 1024)
    if not head:
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

    WHAT IT LOOKS FOR. Every vdev label begins with the eight bytes
    `\\x00\\x00\\x00\\x00\\x0c\\xb1\\xba\\x00` - UBERBLOCK_MAGIC - and there are four
    labels per vdev: two at the start (offset 0 and 256 KiB) and two at the end (the last
    512 KiB, twice).

    WHY A MAGIC SCAN AND NOT `zpool import`. The bake has no zpool on the host, and
    importing a pool to ask whether it exists would mount it, take the device exclusively,
    and make the next stage's qemu start fail with "pool is in use". A magic value at a
    known place answers the same question without touching the pool.

    THE POOL IS ON A PARTITION, NOT ON THE DISK - and that is the part that was wrong.
    `zfsboot` creates a GPT with an EFI system partition at 260 MiB and the pool after
    it, so L0 is not at disk offset 0. Scanning a megabyte from the start of the disk
    finds nothing on a perfectly good ZFS volume.

    THAT IS WHY THIS READS THE GPT FIRST, exactly as lib/smoke.check_apfs does: the
    entry states the start LBA, and reading there makes the question "is there a label
    in the pool partition" instead of "does the magic happen to be near the front".

    WHAT IT COST TO LEARN. Run 41991139008 installed every one of its 394 packages in
    135 seconds and the disk held 3705 MiB - and this returned False, so the bake threw
    the disk away as "no filesystem". Two separate faults lined up: this read the qcow2
    FILE rather than the guest's disk (see _read_virtual) and it looked at the wrong
    place even when reading correctly.

    A FAILED INSTALL LEAVES A PARTITION TABLE AND NO POOL, which is the same distinction
    check_disk_ufs() draws for UFS - see its docstring.
    """
    magic = b"\x00\x00\x00\x00\x0c\xb1\xba\x00"

    # THE WHOLE-DISK CASE IS STILL CHECKED FIRST, because a pool on a bare vdev (no
    # partition table) is a layout ZFSBOOT_DISKS would produce if ZFSBOOT_PARTITION_SCHEME
    # were changed, and the check should not silently stop working if it is.
    head = _read_virtual(disk, 0, 1024 * 1024)
    if head.find(magic) >= 0:
        log(f"  ZFS label magic at disk offset {head.find(magic)}")
        return True

    # THE PARTITIONED CASE: ask the GPT where the pool starts.
    try:
        import os
        import sys

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import smoke
    except ImportError:
        return False

    header_lba = smoke.GPT_HEADER_LBA * 512
    header = _read_virtual(disk, header_lba, 512)
    if header[:8] != b"EFI PART":
        log(f"  no GPT header at {header_lba:#x} (found {header[:8]!r})")
        return False

    entry_lba = int.from_bytes(header[72:80], "little")
    count = int.from_bytes(header[80:84], "little")
    size = int.from_bytes(header[84:88], "little")
    if not (entry_lba and count and size) or size < 16 or count > smoke.GPT_MAX_ENTRIES:
        log(f"  implausible GPT entry array ({entry_lba}, {count}, {size})")
        return False

    entries = _read_virtual(disk, entry_lba * 512, count * size)
    if not entries:
        return False

    # EVERY ENTRY IS NAMED ON THE WAY PAST, so "no ZFS partition" and "the entries were
    # read from the wrong place" look different in the log - the same reason check_apfs
    # lists its types.
    found_types: list[str] = []
    for index in range(count):
        entry = entries[index * size:(index + 1) * size]
        if entry[:16] == b"\x00" * 16:
            break
        found_types.append(entry[:16].hex())
        # A ZFS partition is freebsd-zfs (516e7cba-6ecf-11d6-8ff8-00022d09712b). The pool
        # label is at the partition's first byte, not 64 KiB into it.
        if entry[:16] != smoke.ZFS_PARTITION_TYPE:
            continue
        start = int.from_bytes(entry[32:40], "little") * 512
        block = _read_virtual(disk, start, 256 * 1024)
        where = block.find(magic)
        if where >= 0:
            log(f"  ZFS label magic in the freebsd-zfs partition at {start:#x} "
                f"(+{where})")
            return True
        log(f"  the freebsd-zfs partition at {start:#x} holds no label magic")
        return False

    log(f"  no freebsd-zfs partition; the GPT holds {len(found_types)} entries:")
    for index, kind in enumerate(found_types, 1):
        mark = " <- this is freebsd-zfs" if kind == smoke.ZFS_PARTITION_TYPE.hex() else ""
        log(f"    {index}: {kind}{mark}")
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
