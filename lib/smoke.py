"""Is the disk that came out of a bake actually a bootable system?

    from smoke import check_disk, boot_disk

    check_disk("disk.qcow2", root_fs="ext4")     # without starting a machine
    boot_disk("disk.qcow2", r"login:|lightdm", 600)   # with one

Two halves, and the order matters.

THE DISK, read without booting it. A bake that produced 8 GB of filesystem and no
bootloader looks exactly like a bake that produced nothing, from the outside. The
first 512 bytes and the filesystem superblock say which it is, in a second,
before any qemu starts.

THE BOOT, watched. A disk can pass every byte-level check and still not start - a
GRUB stage 1 that cannot find its stage 2 does exactly that - so the console is
read and the screen is photographed.

Nothing here knows what distribution it is testing. What a desktop looks like, and
which string proves it arrived, is the caller's business.

WHY THIS IS PYTHON NOW

The shell version read a disk by shelling out to `qemu-io -c "read -v 0 512"`,
captured the hexdump to a temp file, then shelled out again to `hexdump.py` to
parse it, then grepped the result for the string "55aa". Four processes and two
temporary files to ask whether two bytes equal each other.

Here it is `qemu_io_read(disk, 0, 512)[510:512] == b"\\x55\\xaa"`.

The superblock offset bug is also visible: the comment in the shell version
records reading at 0x1f0 instead of 0x1fe and spending three runs believing a
working disk was broken. In this version the offsets are named constants and the
slice is written out.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vm  # noqa: E402

# The MBR, by offset:
#   0x000..0x1bd   boot code (446 bytes)
#   0x1be..0x1fd   four partition entries, 16 bytes each
#   0x1fe..0x1ff   0x55 0xAA - the signature
MBR_SIGNATURE = (0x1FE, 0x200)
MBR_BOOT_CODE = (0x000, 0x1BE)
GPT_PROTECTIVE_TYPE = b"\xee"

# ext superblock magic 0xEF53, little-endian, at offset 0x38 of a superblock that
# is 1024 bytes into the partition.
EXT_MAGIC_OFFSET = 1024 + 0x38

# The APFS container superblock: the block begins with a 32-byte object header whose
# type is NXSB, so the magic sits at relative offset 32 of the container's first block.
APFS_MAGIC = b"NXSB"
APFS_MAGIC_OFFSET = 32

# APFS partition type GUID, in the byte order GPT stores it: the first three fields are
# little-endian and the last two keep the GUID's own byte order. macOS creates exactly
# one of these for its container, so reading the entry is a better answer than scanning
# for the magic - the entry states where the partition starts.
#
#   7C3457EF-0000-11AA-AA11-00306543ECAC  ->  7c3457ef 0000 aa11 aa11 00306543ecac
APFS_PARTITION_TYPE = bytes.fromhex("7c3457ef0000aa11aa1100306543ecac")

# The GPT partition entry array. LBA 1 is the header, which states where the array is
# (normally LBA 2); each entry is 128 bytes and its first 16 are the partition type GUID.
GPT_HEADER_LBA = 1
GPT_ENTRY_SIZE = 128
GPT_MAX_ENTRIES = 128


def log(msg: str) -> None:
    print(f"[disk] {msg}", flush=True)


class DiskReadError(RuntimeError):
    pass


def qemu_io_read(disk: str, offset: int, length: int) -> bytes:
    """Read raw bytes out of a qcow2 without mounting it.

    qemu-io rather than debugfs or a loopback mount: the image is qcow2, so the
    guest's logical bytes are not at the same file offset as the host's, and
    mounting needs privileges a container job may not have.
    """
    result = subprocess.run(
        ["qemu-io", "-f", "qcow2", "-c", f"read -v {offset} {length}", disk],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DiskReadError(f"qemu-io could not read {disk} at {offset}: {result.stderr.strip()}")

    # qemu-io -v prints one line per 16 bytes, in this exact shape:
    #
    #     00000000:  55 aa 00 00 ...  ascii
    #     00000010:  ...
    #     read 512/512 bytes at offset 0
    #     512 bytes, 1 ops; ...
    #
    # THE COLON IS PART OF THE FORMAT. The first version of this pattern was
    #
    #     ^([0-9a-f]{8,16})\s+...
    #
    # which needs whitespace right after the offset and therefore matched none of these
    # lines. Every read parsed to zero bytes, so every disk was reported as
    #
    #     [disk] short read: got 0 bytes, expected 512
    #     [disk] partition table truncated
    #
    # - and a Windows install that had just reached its desktop was thrown away as an
    # unbootable disk. The check exists to catch a bad disk; it was rejecting a good one.
    data = bytearray()
    for line in result.stdout.splitlines():
        # THE ASCII COLUMN IS PART OF THE OUTPUT AND MUST NOT BE PARSED.
        #
        # qemu-io -v prints
        #
        #     00000000:  55 aa 00 00 00 00 00 00 00 00 00 00 00 00 00 00  U...............
        #
        # and a greedy hex run at the end of the line swallows the ASCII when the bytes
        # happen to be hex digits. Reading 64 MiB of a partition rather than 512 bytes of
        # a header made that certain, and run 41492587109 failed the whole bake with
        #
        #     ValueError: non-hexadecimal number found in fromhex() arg at position 3
        #
        # The fix is to bound the hex run by the column width: 16 bytes per line, so at
        # most 16 tokens of two hex digits, and stop at the double space that separates
        # them from the ASCII.
        m = re.match(r"^([0-9a-f]{8,16}):\s+([0-9a-f ]+?)\s{2,}", line + "  ")
        if not m:
            continue
        tokens = [t for t in m.group(2).split() if re.fullmatch(r"[0-9a-f]{2,8}", t)]
        for token in tokens:
            if len(token) == 8:
                # -v may group into 4-byte words on some builds; split those out.
                data += bytes.fromhex(token)[::-1]
            else:
                data += bytes.fromhex(token)
    return bytes(data)


def check_mbr(disk: str) -> bool:
    """Is there boot code in the MBR?

    A disk with a signature and no code, and a disk with neither, are different
    failures: the first is a bootloader that was not installed, the second is a
    disk that was never partitioned.
    """
    try:
        head = qemu_io_read(disk, 0, 512)
    except DiskReadError as exc:
        log(f"THE MBR COULD NOT BE READ - this says nothing about the disk: {exc}")
        return False

    if len(head) < 512:
        log(f"short read: got {len(head)} bytes, expected 512")
        return False

    sig = head[slice(*MBR_SIGNATURE)]
    code = head[slice(*MBR_BOOT_CODE)]

    log(f"mbr signature: {sig.hex()}")
    log(f"mbr boot code: {code[:8].hex()}...")

    if sig != b"\x55\xaa":
        log("NO MBR SIGNATURE - SeaBIOS has nothing to run")
        return False

    if not any(code):
        log("signature present, boot code area is zero - no bootloader")
        return False

    log("boot code present")
    return True


def check_efi_bootloader(disk: str) -> bool:
    """Is there a Windows boot loader inside the EFI system partition?

    THE FAILURE THIS EXISTS FOR, MEASURED ON RUN 41483694400

    The ssh stage started the published gold disk and got, at the firmware:

        BdsDxe: failed to load Boot000A "Windows Boot Manager" from
          HD(1,GPT,...) /EFI/Microsoft/Boot/bootmgfw.efi: Not Found
        >>Start PXE over IPv4.
        PXE-E16: No valid offer received.
        ...
        UEFI Interactive Shell v2.2

    The bootstate did its job - Boot000A exists, and it was recovered from the archive -
    so the firmware looked exactly where the entry said. There was no bootmgfw.efi on the
    disk. The gold disk was not an installed Windows.

    WHY NOTHING CAUGHT IT. check_disk() verified a protective MBR and a GPT header, and
    an empty disk has both: `qemu-img create` plus a partition step is enough. The bake
    printed "BAKED windows11" and published a disk that could never start, and the next
    stage spent fifteen minutes in the firmware's own shell before timing out.

    Reading the file is the answer that cannot be faked. mtools (already installed for
    the boot image work) reads a FAT32 partition out of a raw file offset, which is what
    an ESP is - no mounting, no loop device, no root.

    Returns False when it cannot tell; the caller decides whether that is fatal.
    """
    import shutil as _shutil

    if not _shutil.which("mdir"):
        log("mtools is not installed; the EFI system partition cannot be checked")
        return False

    try:
        partitions = _gpt_partitions(disk)
    except DiskReadError as exc:
        log(f"could not read the GPT: {exc}")
        return False
    if not partitions:
        log("no partitions in the GPT")
        return False

    # THE ESP IS THE FAT PARTITION. Its type GUID is C12A7328-F81F-11D2-BA4B-00A0C93EC93B,
    # stored as the first three fields little-endian.
    esp = None
    for entry in partitions:
        if entry["type"] == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b":
            esp = entry
            break
    if esp is None:
        types = ", ".join(sorted(set(e["type"][:8] for e in partitions)))
        log(f"no EFI system partition in the GPT (types: {types})")
        return False

    offset = esp["first_lba"] * 512
    size = (esp["last_lba"] - esp["first_lba"] + 1) * 512

    # THE PARTITION IS COPIED OUT FIRST, because mtools' `@@offset` syntax wants a raw
    # image and every disk here is a qcow2: the guest's logical bytes are not at the same
    # host offset. Handing mtools `disk@@offset` produced
    #
    #     init :: could not read boot sector
    #
    # on a disk whose ESP was fine - measured while writing this check.
    tmpdir = tempfile.mkdtemp(prefix="esp-")
    espimg = os.path.join(tmpdir, "esp.img")
    try:
        with open(os.path.join(tmpdir, "esp.bin"), "wb") as fh:
            # 8 MiB IS ENOUGH FOR A FAT32 BOOT SECTOR AND ITS ROOT DIRECTORY, and
            # copying 64 MiB through qemu-io's text output for every Windows bake is
            # both slow and a much larger surface for the parser above.
            fh.write(qemu_io_read(disk, offset, min(size, 8 << 20)))
        shutil.copyfile(os.path.join(tmpdir, "esp.bin"), espimg)
        result = subprocess.run(
            ["mdir", "-i", espimg, "-/", "-b", "::/EFI/Microsoft/Boot"],
            capture_output=True, text=True, timeout=60,
        )
    except (DiskReadError, OSError, subprocess.SubprocessError) as exc:
        log(f"could not read the EFI partition: {exc}")
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if result.returncode != 0:
        log("the EFI system partition has no /EFI/Microsoft/Boot")
        log(f"  mdir said: {(result.stderr or result.stdout).strip()[:200]}")
        return False

    # `mdir -b` prints FULL PATHS, not bare names:
    #
    #     ::/EFI/Microsoft/Boot/bootmgfw.efi
    #
    # which is easy to read as a name and then never match. Compare basenames.
    names = [os.path.basename(n.strip().replace("\\", "/")).lower()
             for n in result.stdout.splitlines() if n.strip()]
    loader = "bootmgfw.efi"
    if loader not in names:
        log(f"/EFI/Microsoft/Boot exists but holds no {loader} "
            f"({', '.join(names[:8]) or 'nothing'})")
        return False

    log(f"the EFI system partition holds {loader} "
        f"({esp['first_lba']}..{esp['last_lba']} LBA, GUID {esp['unique']})")
    return True


def _gpt_partitions(disk: str) -> list[dict]:
    """The GPT entries, as {type, first_lba, last_lba}.

    Read with one qemu-io call over the entry array, which lives at LBA 2 and is sized by
    the header. Entry size and count come from the header rather than being assumed: 128
    x 128 is the usual pair and not the only one.
    """
    header = qemu_io_read(disk, 512, 512)
    if header[0:8] != b"EFI PART":
        return []
    entry_lba = int.from_bytes(header[72:80], "little")
    count = int.from_bytes(header[80:84], "little")
    size = int.from_bytes(header[84:88], "little")
    if not (entry_lba and count and 128 <= size <= 4096):
        return []
    count = min(count, 128)

    raw = qemu_io_read(disk, entry_lba * 512, count * size)
    out = []
    for i in range(count):
        entry = raw[i * size:(i + 1) * size]
        if len(entry) < 56:
            break
        if entry[0:16] == b"\x00" * 16:
            continue
        # The type GUID's first three fields are little-endian; printing it in the usual
        # order is what makes c12a7328... recognisable.
        a = int.from_bytes(entry[0:4], "little")
        b = int.from_bytes(entry[4:6], "little")
        c = int.from_bytes(entry[6:8], "little")
        d = entry[8:10].hex()
        e = entry[10:16].hex()
        guid = f"{a:08x}-{b:04x}-{c:04x}-{d}-{e}"
        # THE PARTITION'S OWN GUID, which is not the type GUID. UEFI boot entries
        # address a partition by THIS value, so a boot entry recovered from one disk and
        # applied to another finds nothing - measured on launch-windows run 41518782273,
        # where the firmware reported
        #
        #     failed to load Boot000A "Windows Boot Manager" from
        #     HD(1,GPT,D8AC67A6-BD6D-46A3-9AAD-57C0B50BC178,0x800,0x82000)
        #     /\EFI\Microsoft\Bootootmgfw.efi: Not Found
        #
        # for a disk whose ESP this module had just read successfully.
        ua = int.from_bytes(entry[16:20], "little")
        ub = int.from_bytes(entry[20:22], "little")
        uc = int.from_bytes(entry[22:24], "little")
        unique = (f"{ua:08x}-{ub:04x}-{uc:04x}-{entry[24:26].hex()}-"
                  f"{entry[26:32].hex()}")
        out.append({
            "type": guid,
            "unique": unique,
            "first_lba": int.from_bytes(entry[32:40], "little"),
            "last_lba": int.from_bytes(entry[40:48], "little"),
        })
    return out


def check_gpt(disk: str) -> bool:
    """A GPT disk has a protective MBR: one partition entry of type 0xEE."""
    try:
        head = qemu_io_read(disk, 0, 512)
    except DiskReadError as exc:
        log(f"could not read the protective MBR: {exc}")
        return False

    # First partition entry starts at 0x1BE; its type byte is the 5th byte.
    entry = head[0x1BE:0x1BE + 16]
    if len(entry) < 5:
        log("partition table truncated")
        return False

    if entry[4:5] == GPT_PROTECTIVE_TYPE:
        log("GPT protective MBR found")
        return True

    log(f"no GPT protective MBR (partition type {entry[4]:#04x})")
    return False


def check_ext_root(disk: str) -> bool:
    """ext superblock magic at 1 MiB.

    ext ONLY. The caller says which filesystem it expects, because a macOS or
    Windows disk has no ext superblock there and reporting "no ext magic" for a
    perfectly good APFS or NTFS disk is a check that only ever produces false
    alarms.
    """
    try:
        chunk = qemu_io_read(disk, EXT_MAGIC_OFFSET - (EXT_MAGIC_OFFSET % 512), 4096)
    except DiskReadError as exc:
        log(f"could not read the superblock area: {exc}")
        return False

    magic = chunk[0x38:0x3A]
    if magic == b"\x53\xef":
        log("ext superblock magic 53ef found at 1 MiB")
        return True

    log(f"no ext magic at 1 MiB (read {magic.hex()}) - the root is elsewhere or not ext")
    return False


def check_apfs(disk: str) -> bool:
    """Is there an APFS container on this disk, and is it where a partition says?

    WHY THIS IS THE macOS CHECK AND "GPT" ALONE IS NOT. A disk that reached this point
    has been through the installer, so it has a partition table either way. What the
    bake needs to know is whether the SYSTEM is on it, and for macOS that means an APFS
    container - macOS 15 installs to nothing else.

    WHY THE GPT IS READ RATHER THAN SEARCHED. The first version scanned the first 64 MiB
    of the disk for the NXSB magic. That is wrong twice over: an installed APFS
    partition starts after macOS's own 200-500 MiB EFI system partition, so the magic is
    past the window; and a search that happens to find the magic anywhere proves nothing
    about which partition it belongs to. The GPT entry states the start LBA, which makes
    this a read at a known place.

    It does not prove the APFS DRIVER works - only booting the disk does, and the
    recording from the run that produced it is the evidence for that. It proves the
    installation put a container there, which is what an artifact is being published on.
    """
    header_lba = GPT_HEADER_LBA * 512
    try:
        header = qemu_io_read(disk, header_lba, 512)
    except DiskReadError as exc:
        log(f"could not read the GPT header: {exc}")
        return False

    if header[:8] != b"EFI PART":
        log(f"no GPT header at {header_lba:#x} (found {header[:8]!r})")
        return False

    # entry_lba  offset 72, number_of_entries  offset 80, entry_size  offset 84
    entry_lba = int.from_bytes(header[72:80], "little")
    count = int.from_bytes(header[80:84], "little")
    size = int.from_bytes(header[84:88], "little")
    if not (entry_lba and count and size):
        log(f"the GPT header states no entry array ({entry_lba}, {count}, {size})")
        return False
    if size < 16 or count > GPT_MAX_ENTRIES:
        log(f"implausible GPT entry array: {count} entries of {size} bytes")
        return False

    try:
        entries = qemu_io_read(disk, entry_lba * 512, count * size)
    except DiskReadError as exc:
        log(f"could not read the partition entries: {exc}")
        return False

    for index in range(count):
        entry = entries[index * size:(index + 1) * size]
        if entry[:16] != APFS_PARTITION_TYPE:
            continue

        # first_lba is the third 8-byte field of a GPT entry.
        start = int.from_bytes(entry[32:40], "little") * 512
        log(f"APFS partition at {start:#x} (entry {index + 1})")
        try:
            block = qemu_io_read(disk, start, 4096)
        except DiskReadError as exc:
            log(f"could not read the container header: {exc}")
            return False
        if block[APFS_MAGIC_OFFSET:APFS_MAGIC_OFFSET + 4] == APFS_MAGIC:
            log("APFS container superblock: NXSB")
            return True
        log(f"the APFS partition has no NXSB at its start "
            f"(found {block[APFS_MAGIC_OFFSET:APFS_MAGIC_OFFSET + 4]!r})")
        return False

    log("no APFS partition in the GPT - macOS did not install")
    return False


def check_disk(disk: str, root_fs: str = "ext4") -> bool:
    """Everything that can be learned without starting the machine.

    root_fs is one of:
        "ext4"  (default) look for 53ef at 1 MiB
        "gpt"   only check that the partition table is GPT
        "apfs"  GPT, and an APFS container on the disk
        "windows" GPT, and bootmgfw.efi in the EFI system partition
        "none"  skip the filesystem check entirely

    A missing disk, or a host without qemu-io, returns False rather than raising:
    the caller prints the verdict and continues to the boot check, and a traceback
    here would read as "the bake crashed" instead of "the disk is bad".
    """
    if root_fs not in ("ext4", "gpt", "apfs", "windows", "none"):
        raise ValueError(
            f"unknown root_fs: {root_fs!r} (expected ext4, gpt, apfs, windows or none)"
        )

    problems = 0
    print("=== the disk ===", flush=True)
    if os.path.exists(disk):
        print(f"  {disk}: {os.path.getsize(disk)} bytes", flush=True)
    else:
        print(f"  {disk}: MISSING", flush=True)
        print("=== THE DISK WILL NOT BOOT ===", flush=True)
        return False
    # THE MBR IS NOT THE BOOT PATH ON A UEFI DISK.
    #
    # Windows on UEFI boots from bootmgfw.efi in the EFI system partition and never reads
    # the MBR's code area; the "protective MBR" is a table entry and one signature byte.
    # Requiring 440 bytes of boot code here made a perfectly good Windows disk fail with
    #
    #     [disk] signature present, boot code area is zero - no bootloader
    #     === THE DISK WILL NOT BOOT ===
    #
    # while the check that WOULD have answered the question never ran, because "windows"
    # was not in the branch below and fell through to "filesystem check skipped".
    #
    # So the MBR is required only where something actually boots from it: ext4 (a BIOS
    # bootloader) and gpt (a disk claimed to be bootable by its own table).
    mbr_required = root_fs in ("ext4", "gpt")
    if not check_mbr(disk):
        if mbr_required:
            problems += 1
        else:
            log("(no MBR boot code - expected for a UEFI disk)")

    if root_fs == "ext4":
        if not check_ext_root(disk):
            log("(not fatal here - a non-ext root is possible)")
    elif root_fs in ("gpt", "apfs", "windows"):
        if not check_gpt(disk):
            problems += 1
        # An APFS disk is GPT *and* has a container. The second is the one that says
        # macOS is on it; the first only says the disk was partitioned.
        if root_fs == "apfs" and not check_apfs(disk):
            problems += 1
        # A GPT with no boot loader on it is a partition table, not a system.
        if root_fs == "windows" and not check_efi_bootloader(disk):
            problems += 1
    else:
        log("filesystem check skipped")

    if problems:
        print("=== THE DISK WILL NOT BOOT ===", flush=True)
        return False
    return True


def wait_for(logfile: str, pattern: str, seconds: int = 600) -> bool:
    """Wait until the console matches `pattern`."""
    rx = re.compile(pattern, re.M)
    for i in range(1, seconds + 1):
        try:
            with open(logfile, "rb") as fh:
                if rx.search(fh.read().decode("utf-8", "replace")):
                    print(f"[boot] matched after {i}s: {pattern}", flush=True)
                    return True
        except OSError:
            pass
        time.sleep(1)
    print(f"[boot] never matched after {seconds}s: {pattern}", flush=True)
    return False


def boot_disk(disk: str, expect: str, seconds: int = 600,
              work: str = ".", frames: str | None = None) -> bool:
    """Boot the disk and see whether it arrives.

    EXPECT is what proves arrival for this distribution - "login:" for a server,
    a shell name for a desktop. The caller supplies it because only the caller
    knows.

    An empty EXPECT means "photograph it and let a human decide", which is what a
    Windows or macOS disk needs: neither has a serial console by default, so the
    only evidence is the screen.
    """
    work = os.path.abspath(work)
    frames = frames or os.path.join(work, "shots")
    disk = os.path.abspath(disk)
    os.makedirs(work, exist_ok=True)
    os.makedirs(frames, exist_ok=True)

    print(f"[boot] disk={disk}", flush=True)
    print(f"[boot] work={work}", flush=True)
    print(f"[boot] frames={frames}", flush=True)

    guest = vm.Guest(
        work,
        extra=["-drive", f"file={disk},if=virtio,format=qcow2", "-no-reboot"],
        stdout=os.path.join(work, "smoke.log"),
    ).start()

    time.sleep(5)
    if not guest.alive():
        print("[boot] qemu died at startup:", flush=True)
        try:
            with open(os.path.join(work, "smoke.log")) as fh:
                print("".join(fh.readlines()[-20:]), flush=True)
        except OSError:
            pass
        return False

    # Photograph the boot from the second qemu starts. The recording is the point
    # of the whole pipeline: it is what shows a lightdm greeter, a GRUB prompt or
    # a kernel panic when every log line looked the same.
    snap = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots.py"),
         guest.paths.record, frames, os.environ.get("SNAPSHOT_INTERVAL", "1"), "900"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    try:
        if expect:
            ok = wait_for(os.path.join(work, "smoke.log"), expect, seconds)
        else:
            log(f"no arrival pattern given; waiting {seconds}s and leaving it to the frames")
            time.sleep(seconds)
            ok = True
    finally:
        guest.stop()
        snap.terminate()
        try:
            snap.wait(timeout=5)
        except subprocess.TimeoutExpired:
            snap.kill()

    count = len([f for f in os.listdir(frames) if f.startswith("frame-") and f.endswith(".ppm")])
    print(f"=== frames: {count} ===", flush=True)
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: smoke.py <disk.qcow2> <expect-pattern|->")
    disk, pattern = sys.argv[1], sys.argv[2]
    root = os.environ.get("SMOKE_ROOT_FS", "ext4")
    ok_disk = check_disk(disk, root)
    ok_boot = boot_disk(disk, "" if pattern == "-" else pattern)
    raise SystemExit(0 if (ok_disk and ok_boot) else 1)
