"""Build the three disks an Android system image needs to boot without an AVD.

    python3 android/15/mkdisks.py --out /a/android-test

WHY THIS EXISTS

Google's SDK ships `system.img` and `vendor.img` as GPT DISKS, not as partitions. The
emulator's normal path unpacks them into an AVD and hands paths to `-system`/`-vendor`.
That works only when the emulator owns the AVD. Here the disks are assembled explicitly,
because every piece has to be inspectable:

    cache.img     system, metadata and super - the three first-stage init needs by-name
    userdata.img  an ext4 /data
    ramdisk.img   the SDK ramdisk plus an fstab naming every partition by-name

Everything below was measured against sysimg android-15 x86_64 (build AE3A.240806.019)
and is written down where the number is not obvious.

WHY cache.img HOLDS TWO PARTITIONS AND NOT FIVE

The first working layout wrote five - system, metadata, super, vbmeta, boot - because it
did not trust Google's own GPT and copied `super` and `vbmeta` out of it. It does trust it.
The guest mounts both from the system disk, and the duplication showed up as a conflict in
the guest's own log:

    ueventd: Failed to symlink /dev/block/vdb4 to /dev/block/by-name/vbmeta,
             which already links to: /dev/block/vda1: File exists
    ueventd: Failed to symlink /dev/block/vda2 to /dev/block/by-name/super,
             which already links to: /dev/block/vdb3: File exists

`boot` was 32 MiB of zeros that nothing reads: no fstab entry, and `by-name/boot` appears
zero times in a fully booted guest's log. Dropping all three took the disk from 2465 MiB
to 887 MiB.

`system` is the one partition that STILL has to be copied out of `super`, because AVB will
not verify a logical partition and dm-0 has no by-name path:

    init: [libfs_avb] Device path not found: /dev/block/by-name/system

so the same bytes are given a partition of their own.

WHY NOT ONE DISK

Tried and measured, not assumed. A single GPT holding all seven partitions is enumerated
correctly by the kernel:

    vda: vda1 vda2 vda3 vda4 vda5 vda6 vda7

and first-stage init still cannot see them:

    init: partition(s) not found in /sys, waiting for their uevent(s): metadata, system
    init: partition(s) not found after polling timeout: metadata, system

The emulator does not publish GPT partition uevents for the disk it mounts at `-system`;
`-cache` is a plain virtio-blk and does. `system` and `metadata` therefore have to live on
the cache disk, which is why this is three files and not one.

Could stock qemu do it instead? It was checked, and the answer is not in this file's
control: `kernel-ranchu` is a qemu `Image`, not a Linux bzImage. Its first 497 bytes are
0xff padding and the `HdrS` header starts at 498:

    0xff * 497, then 1f 01 00 40 3c 12 00 00 ...

`qemu-system-x86_64 -kernel` looks for the boot protocol header at 0x1f1 of the file, finds
padding, and falls through to SeaBIOS - which then boots from ROM and exits with nothing on
the serial port. Booting this image under stock qemu means either converting the kernel to
a bzImage or extracting it and supplying the emulator's own load addresses, and neither is
a change to the disk layout. So the three-disk shape stands until that is done, and the
`-system` restriction above is unchanged whether qemu or the emulator launches it, because
it comes from Android's first-stage init rather than from the VMM.

WHERE THE OFFSET COMES FROM, AND THE BUG THAT COST THE MOST TIME

liblp stores partitions and extents in two tables that follow the metadata header
back to back, with NO offset header in front of them. The first 36 bytes of the tables
region are the literal word "system":

    tables first 32: 73797374656d0000...   = "system\\0\\0"

so reading three uint32s as offsets yields `partitions_off=1953724787`, which is the
ASCII of "stem". The tables are walked in order instead.

Each extent is 24 bytes:

    u64 num_sectors, u32 target_type, u64 target_data, u32 target_source

`<QIQI` is wrong: uint32 target_type is followed by a UINT64 target_data. Read that way
every extent reports `target_data=0` and `system` gets copied from sector 0 of `super`,
i.e. 871 MiB of zeros. For a LINEAR extent the offset is in `target_data`;
`target_source` is 0 for all of them on this image.
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import subprocess
import sys
import uuid

SECTOR = 512
LINUX_FS_BYTES = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4").bytes_le

# THE SIZE THAT IS ACTUALLY NEEDED, READ FROM GOOGLE'S OWN IMAGE.
#
# These used to be constants copied out of the SDK's GPT headers by hand -
# `SUPER_FIRST_LBA = 4096`, `SUPER_SECTORS = 3166207 - 4096 + 1` - which is a copy that
# goes stale the moment Google ships a different system image, and the failure mode would
# be a `super` partition with the wrong length. super_first()/super_sectors() below read
# them from the disk that is about to be used.
METADATA_SECTORS = 32768

# 64 GiB, BECAUSE /data IS WHERE INSTALLED APPLICATIONS LIVE.
#
# It was 8 GiB, which holds the system's own first-boot state and very little else - a few
# large applications and it is full, and a full /data does not fail at install time in a
# way that is easy to read. The disk is sparse: it costs 64 GiB of VIRTUAL size and only
# what is written (a few hundred MiB after a first boot) in real bytes, so the number on
# disk does not grow by 56 GiB when this changes.
USERDATA_BYTES = 64 * 2 ** 30

LZ4_LEGACY_MAGIC = b"\x02\x21\x4c\x18"


def log(msg: str) -> None:
    print(f"[mkdisks] {msg}", flush=True)


def crc32(data: bytes) -> int:
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


# --- liblp -----------------------------------------------------------------


def lp_partitions(super_head: bytes) -> dict[str, tuple[int, int]]:
    """{name: (sector_offset_in_super, num_sectors)} from the LP metadata.

    See the module docstring for the two parsing traps. Both were silent: the offsets
    read as ASCII, and the extent offset read as zero.
    """
    magic = struct.pack("<I", 0x414C5030)
    meta_off = None
    for m in re.finditer(re.escape(magic), super_head[:4 * 2 ** 20]):
        hdr = super_head[m.start():m.start() + 16]
        if len(hdr) >= 12 and struct.unpack("<H", hdr[4:6])[0] in (10, 11):
            meta_off = m.start()
            break
    if meta_off is None:
        raise SystemExit("=== NO LP METADATA in super ===")

    hdr = super_head[meta_off:meta_off + 128]
    hdr_size = struct.unpack("<I", hdr[8:12])[0]
    tables_size = struct.unpack("<I", hdr[44:48])[0]
    tables = super_head[meta_off + hdr_size:meta_off + hdr_size + tables_size]

    partitions: list[tuple[str, bytes]] = []
    p = 0
    while p + 52 <= len(tables):
        name = tables[p:p + 36].split(b"\x00")[0]
        if not name or not all(32 <= c < 127 for c in name):
            break
        partitions.append((name.decode(), tables[p:p + 52]))
        p += 52

    extents: list[bytes] = []
    while p + 24 <= len(tables):
        entry = tables[p:p + 24]
        if not any(entry):
            break
        extents.append(entry)
        p += 24

    out: dict[str, tuple[int, int]] = {}
    for name, entry in partitions:
        _attrs, first_extent, num_extents = struct.unpack("<III", entry[36:48])
        if num_extents == 0 or first_extent >= len(extents):
            continue
        extent = extents[first_extent]
        num_sectors = struct.unpack("<Q", extent[0:8])[0]
        target_data = struct.unpack("<Q", extent[12:20])[0]
        out[name] = (target_data, num_sectors)
    return out


# --- GPT -------------------------------------------------------------------


def read_gpt(path: str) -> dict[str, tuple[int, int]]:
    """{name: (first_lba, last_lba)} from a disk image's own GPT.

    THE LAYOUT OF GOOGLE'S IMAGE IS AN INPUT, NOT A CONSTANT. `super` is a GPT partition
    of system.img with a length that changes when Google ships a new image, and the first
    version of this file hardcoded both its LBA and its size. Reading them here means a
    new upstream image either works or fails with a sentence, rather than producing a
    `super` partition of exactly the wrong length.
    """
    out: dict[str, tuple[int, int]] = {}
    with open(path, "rb") as fh:
        fh.seek(SECTOR)
        header = fh.read(92)
        if header[0:8] != b"EFI PART":
            return out
        entries_lba = int.from_bytes(header[72:80], "little")
        count = int.from_bytes(header[80:84], "little")
        size = int.from_bytes(header[84:88], "little")
        fh.seek(entries_lba * SECTOR)
        array = fh.read(count * size)
    for i in range(count):
        entry = array[i * size:(i + 1) * size]
        if not any(entry):
            continue
        name = entry[56:128].decode("utf-16-le", "ignore").rstrip("\x00")
        out[name] = (int.from_bytes(entry[32:40], "little"),
                     int.from_bytes(entry[40:48], "little"))
    return out


def gpt(entries: list[tuple[str, int, int]], disk_sectors: int):
    """(primary header + array, the array, backup header) for the given layout."""
    mbr = bytearray(SECTOR)
    struct.pack_into("<H", mbr, 510, 0xAA55)
    struct.pack_into("<B", mbr, 446 + 4, 0xEE)
    struct.pack_into("<I", mbr, 446 + 8, 1)
    struct.pack_into("<I", mbr, 446 + 12, 0xFFFFFFFF)

    array = bytearray(128 * 128)
    for i, (name, first, last) in enumerate(entries):
        entry = bytearray(128)
        entry[0:16] = LINUX_FS_BYTES
        entry[16:32] = uuid.uuid4().bytes_le
        struct.pack_into("<Q", entry, 32, first)
        struct.pack_into("<Q", entry, 40, last)
        encoded = name.encode("utf-16-le")[:72]
        entry[56:56 + len(encoded)] = encoded
        array[i * 128:(i + 1) * 128] = entry

    header = bytearray(SECTOR)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 32, disk_sectors - 1)
    struct.pack_into("<Q", header, 40, 34)
    struct.pack_into("<Q", header, 48, disk_sectors - 34)
    header[56:72] = uuid.uuid4().bytes_le
    struct.pack_into("<Q", header, 72, 2)
    struct.pack_into("<I", header, 80, 128)
    struct.pack_into("<I", header, 84, 128)
    struct.pack_into("<I", header, 88, crc32(array))
    struct.pack_into("<I", header, 16, 0)
    struct.pack_into("<I", header, 16, crc32(bytes(header[:92])))

    backup = bytearray(header)
    struct.pack_into("<Q", backup, 24, disk_sectors - 1)
    struct.pack_into("<Q", backup, 32, 1)
    struct.pack_into("<Q", backup, 72, disk_sectors - 33)
    struct.pack_into("<I", backup, 16, 0)
    struct.pack_into("<I", backup, 16, crc32(bytes(backup[:92])))

    return bytes(mbr) + bytes(header) + bytes(array), bytes(array), bytes(backup)


# --- metadata --------------------------------------------------------------


def metadata_filesystem() -> bytes:
    """A 16 MiB ext4 for the `metadata` partition.

    Without it init says, before anything else can happen:

        init: [libfs_mgr] Invalid ext4 superblock on '/dev/block/by-name/metadata'

    and later apexd aborts on the missing /metadata/apex/sessions and reboots the
    machine, because apexd has `reboot_on_failure`.
    """
    path = "/tmp/metadata-fs.img"
    with open(path, "wb") as fh:
        fh.truncate(METADATA_SECTORS * SECTOR)
    subprocess.run(
        ["/sbin/mkfs.ext4", "-q", "-F",
         "-O", "has_journal,extent,huge_file,flex_bg,metadata_csum,64bit,"
               "dir_nlink,extra_isize",
         "-L", "metadata", path],
        check=True)
    with open(path, "rb") as fh:
        return fh.read()


def userdata_filesystem(dest: str, force: bool = False) -> bool:
    """A 64 GiB ext4 /data, without `orphan_file`. Returns True if it was created.

    THE GUEST'S OWN e2fsck PREDATES ONE FEATURE:

        e2fsck: /dev/block/mapper/userdata has unsupported feature(s): FEATURE_C12
        e2fsck: ... WARNING: Filesystem still has errors

    WRITTEN IN PLACE, NOT BUILT IN /tmp AND MOVED. /tmp and the mounted volume are
    different filesystems, and os.replace across them is EXDEV, not a copy.

    AN EXISTING /data IS KEPT, AND THAT IS THE POINT OF THIS CHECK.

    /data is where installed applications live. Rebuilding it every run makes "keep what I
    installed" impossible however the disk is packaged, and it is what made every earlier
    run start from an empty device. `--force-userdata` asks for a blank one.

    WHY THE TEST IS THE SIZE AND NOT THE EXT4 MAGIC

    An earlier version checked for `0x53EF` at 0x438 and that was WRONG - measured:

        freshly mkfs'd, never booted   0x53EF   <- looks like ext4
        after one boot                 0xfcbf   <- does not

    /data uses metadata encryption, so once the guest has booted, the whole partition is
    ciphertext including the superblock. The magic check therefore rebuilt a good, encrypted
    /data on every run after the first, throwing away its applications AND the key that
    matched it. Size is the property that survives encryption and distinguishes a real image
    from a truncated one, and it is what this asks.
    """
    if not force and has_userdata(dest):
        log(f"userdata {dest}: KEPT (an existing /data, applications included)")
        return False
    with open(dest, "wb") as fh:
        fh.truncate(USERDATA_BYTES)
    subprocess.run(["/sbin/mkfs.ext4", "-q", "-F", "-O", "^orphan_file",
                    "-L", "userdata", dest], check=True)
    return True


def has_userdata(path: str) -> bool:
    """True if the file is a /data image of the size this builder makes.

    SIZE, NOT CONTENT - see the docstring above: after the first boot every byte of /data is
    encrypted, so no filesystem-level test works. A file of the wrong size is a leftover from
    a different version of this builder or an interrupted write, and either is worth
    replacing; a file of the right size is the guest's own data and is kept.
    """
    try:
        return os.path.getsize(path) == USERDATA_BYTES
    except OSError:
        return False


# --- ramdisk ---------------------------------------------------------------

# THE FSTAB MUST CONTAIN ONLY ENTRIES. libfstab rejects the whole file for one bad line,
# and a comment that documents a flag is not an entry. The explanation lives here.
#
#   /dev/block/by-name/system   physical, copied out of super
#   system_ext / product / vendor / system_dlkm
#                               logical, inside super, hence `logical`
#   /dev/block/vdc              /data, the third virtio-blk device
#   by-name/metadata            on the same disk, named in its GPT
#
# system_dlkm IS EROFS - the only one that is. Declaring it ext4 produced
#   init: [libfs_mgr] Invalid ext4 superblock on '/dev/block/dm-1'
#   init: Failed to mount /system_dlkm: File exists
# and the whole first stage then failed.
FSTAB = b"""\
/dev/block/by-name/system  /system  ext4  ro,barrier=1  wait,first_stage_mount
system_ext  /system_ext  ext4  ro,barrier=1  wait,logical,first_stage_mount
product     /product     ext4  ro,barrier=1  wait,logical,first_stage_mount
vendor      /vendor      ext4  ro,barrier=1  wait,logical,first_stage_mount
system_dlkm /system_dlkm erofs  ro            wait,logical,first_stage_mount
/dev/block/by-name/metadata  /metadata  ext4  noatime,nosuid,nodev  wait,formattable,first_stage_mount
/dev/block/vdc  /data  ext4  noatime,nosuid,nodev  wait,check,quota,latemount
/dev/block/zram0  none  swap  defaults  zramsize=75%
"""

# THE MOUNT POINTS HAVE TO EXIST. /system, /system_ext, /product and /vendor do; this
# ramdisk predates /system_dlkm.
DIRS = [
    "first_stage_ramdisk",
    "first_stage_ramdisk/system",
    "first_stage_ramdisk/system/etc",
    "first_stage_ramdisk/system_dlkm",
    "system/etc",
    "etc",
]

TARGETS = [
    "first_stage_ramdisk/system/etc/fstab",
    "first_stage_ramdisk/system/etc/fstab.ranchu",
    "first_stage_ramdisk/fstab",
    "system/etc/fstab",
    "etc/fstab",
]


def cpio(name: str, body: bytes, mode: int) -> bytes:
    name_b = name.encode() + b"\x00"
    header = b"070701" + b"".join(
        f"{v:08x}".encode() for v in (
            0, mode, 0, 0, 1, 0, len(body), 0, 0, 0, 0, len(name_b), 0,
        )
    )
    out = header + name_b
    out += b"\x00" * ((-len(out)) % 4)
    out += body
    out += b"\x00" * ((-len(out)) % 4)
    return out


def patch_ramdisk(src: str, dest: str) -> None:
    """Append one more legacy-LZ4 stream carrying the fstab.

    THE SDK RAMDISK IS A CHAIN OF TWO LZ4 LEGACY STREAMS, not one. Android's initrd
    reader consumes streams until the input ends, so appending a complete stream is
    exactly what the format already does. Rebuilding the cpio and recompressing it as a
    SINGLE stream makes init never run at all: 340 kernel restarts, no `init:` line.
    """
    data = open(src, "rb").read()
    if data[:4] != LZ4_LEGACY_MAGIC:
        raise SystemExit("=== SOURCE IS NOT A LEGACY LZ4 INITRD ===")

    for line in FSTAB.decode().splitlines():
        if line.strip().startswith("#"):
            raise SystemExit(f"=== FSTAB CONTAINS A COMMENT: {line!r} ===")

    tail = bytearray()
    for d in DIRS:
        tail += cpio(d, b"", 0o040755)
    for t in TARGETS:
        tail += cpio(t, FSTAB, 0o100644)
    tail += cpio("TRAILER!!!", b"", 0)

    packed = subprocess.run(["lz4", "-l", "-c", "-"], input=bytes(tail),
                            capture_output=True)
    if packed.returncode != 0:
        raise SystemExit(f"=== lz4 FAILED: {packed.stderr[:200]!r} ===")

    with open(dest, "wb") as fh:
        fh.write(data + packed.stdout)

    # VERIFY THE CONTENT SURVIVED, in the file that was just written.
    check = subprocess.run(["lz4", "-d", "-c", dest], capture_output=True).stdout
    if b"/dev/block/by-name/system" not in check:
        raise SystemExit("=== the fstab did not survive the round trip ===")
    log(f"ramdisk {dest}: {len(data)} + {len(packed.stdout)} B, fstab present")


# --- cache -----------------------------------------------------------------


def build_cache(src: str, dest: str) -> None:
    """Two partitions, and nothing that Google's own image already provides.

    THE FIRST VERSION OF THIS FUNCTION WROTE FIVE, THREE OF WHICH WERE REDUNDANT:

        system     carved out of super - genuinely needed, see below
        metadata   16 MiB ext4 - genuinely needed
        super      a byte-for-byte copy of GPT partition 2 of system.img
        vbmeta     a byte-for-byte copy of GPT partition 1 of system.img
        boot       32 MiB of ZEROS, read by nothing

    `super` and `vbmeta` were duplicated on purpose at the time, because the first working
    layout put everything on this one disk and did not trust the SDK image's own GPT. It
    does trust it - the guest mounts both from vda - and the duplication was visible in the
    guest's own log:

        ueventd: Failed to symlink /dev/block/vdb4 to /dev/block/by-name/vbmeta,
                 which already links to: /dev/block/vda1: File exists
        ueventd: Failed to symlink /dev/block/vda2 to /dev/block/by-name/super,
                 which already links to: /dev/block/vdb3: File exists

    The `boot` partition was 65536 sectors of zeros: `grep -c by-name/boot` over a fully
    booted guest's log is 0, and the fstab has no entry for it.

    Together that is 1577 MiB of a 2465 MiB disk. What remains is 887 MiB, and every byte
    of it is something the boot asks for by name.

    WHY `system` STILL HAS TO BE COPIED OUT OF super

    It is also inside super, and AVB will not verify a logical partition:

        init: [libfs_avb] Device path not found: /dev/block/by-name/system

    dm-0 has no by-name path, so the same bytes are given a partition of their own.
    """
    # SYSTEM AND METADATA ARE JOINED BY `super` AND `vbmeta`, AND THE EVIDENCE SAYS WHICH
    # COPY THE GUEST ACTUALLY READS.
    #
    # In a fully booted guest both disks carry a partition by each of those names, and
    # ueventd reports which one won:
    #
    #     Failed to symlink /dev/block/vda2 to /dev/block/by-name/super,
    #              which already links to: /dev/block/vdb3: File exists
    #     Failed to symlink /dev/block/vdb4 to /dev/block/by-name/vbmeta,
    #              which already links to: /dev/block/vda1: File exists
    #
    # vdb3 is this disk. So `super` IS needed here - removing it stopped the boot with
    # `realpath failed: /dev/block/by-name/super`, because the system disk's copy at vda2
    # does not get a by-name link in time for first-stage init.
    #
    # `vbmeta` is the other way round: vda1 is Google's copy in system.img and it wins.
    # That one is genuinely redundant here and is not written.
    #
    # `boot` is not written at all. It was 65536 sectors of ZEROS, the fstab has no entry
    # for it, and `by-name/boot` appears zero times in a fully booted guest's log.
    gpt_in_system = read_gpt(src)
    if "super" not in gpt_in_system:
        raise SystemExit(f"=== {src} has no `super` GPT partition ===")
    super_first, super_last = gpt_in_system["super"]
    super_sectors = super_last - super_first + 1

    with open(src, "rb") as fh:
        fh.seek(super_first * SECTOR)
        super_head = fh.read(4 * 2 ** 20)
    parts = lp_partitions(super_head)
    if "system" not in parts:
        raise SystemExit("=== NO `system` IN THE LP TABLE ===")
    sys_sector, sys_sectors = parts["system"]
    log(f"super: {super_sectors * SECTOR / 2 ** 20:.0f} MiB at sector {super_first} "
        f"(Google's own GPT, NOT copied)")
    log(f"system: sector {sys_sector} inside super, {sys_sectors * SECTOR / 2 ** 20:.0f} MiB")

    # SYSTEM FIRST. by-name published only the early partitions on this disk when the
    # layout was metadata-first, and `system` (then vdb5) was the one that did not
    # resolve. Ordering was the only difference.
    layout: list[tuple[str, int, int]] = []
    cursor = 2048
    for name, sectors in (("system", sys_sectors),
                          ("metadata", METADATA_SECTORS),
                          ("super", super_sectors)):
        layout.append((name, cursor, cursor + sectors - 1))
        cursor += sectors
    disk_sectors = cursor + 34 + 1

    head, array, backup = gpt(layout, disk_sectors)
    raw = dest + ".raw"
    positions = {name: first for name, first, _ in layout}
    with open(raw, "wb") as out:
        out.truncate(disk_sectors * SECTOR)
        out.write(head)
        out.seek((disk_sectors - 33) * SECTOR)
        out.write(array)
        out.seek((disk_sectors - 1) * SECTOR)
        out.write(backup)

        meta = metadata_filesystem()
        out.seek(positions["metadata"] * SECTOR)
        out.write(meta)
        log(f"metadata: ext4, {len(meta) / 2 ** 20:.0f} MiB")

        # system, EXTRACTED FROM super AT THE OFFSET THE LP TABLE NAMES.
        with open(src, "rb") as s:
            s.seek((super_first + sys_sector) * SECTOR)
            out.seek(positions["system"] * SECTOR)
            copy(s, out, sys_sectors * SECTOR, "system")

            s.seek(super_first * SECTOR)
            out.seek(positions["super"] * SECTOR)
            copy(s, out, super_sectors * SECTOR, "super")

    # A PARTITION OF ZEROS READS AS SUCCESS. Check the filesystem magic the guest will
    # look for, in the file that was just written.
    with open(raw, "rb") as fh:
        fh.seek(positions["system"] * SECTOR + 1024)
        probe = fh.read(64)
    if probe[56:58] != b"\x53\xef":  # ext4 magic at 1080 absolute
        raise SystemExit("=== the copied `system` is not ext4 - the offset is wrong ===")

    os.replace(raw, dest)
    log(f"cache {dest}: {disk_sectors * SECTOR / 2 ** 20:.0f} MiB")
    for name, first, last in layout:
        log(f"  {name:10} {first:>9}..{last:<9} "
            f"{(last - first + 1) * SECTOR / 2 ** 20:>7.0f} MiB")


def copy(src, dst, total: int, name: str) -> None:
    remaining = total
    while remaining > 0:
        chunk = src.read(min(64 * 2 ** 20, remaining))
        if not chunk:
            break
        dst.write(chunk)
        remaining -= len(chunk)
    log(f"{name}: copied {(total - remaining) / 2 ** 20:.0f} MiB")


def stage(work: str, sdk: str) -> None:
    """Put the system image's own files where run_qemu.py expects them.

    THE OUTPUT DIRECTORY HAS TO BE SELF-CONTAINED, and this is not tidiness. run_qemu.py
    passes `-sysdir <dir>` plus explicit `-system`/`-vendor`/`-kernel`/`-ramdisk` paths,
    and an earlier layout kept the kernel and vendor elsewhere - the emulator answered

        ERROR | Invalid or missing kernel image file: /a/android-test/kernel

    then, once the kernel was there but vendor was not,

        exit code: -11

    which is a SIGSEGV with no message at all. A directory that boots is a directory that
    holds every file the boot names.

    HARDLINKS, NOT COPIES, WHERE THE FILESYSTEM ALLOWS IT. system.img is 1.6 GB and the
    SDK's copy is read-only and never modified here; a hardlink costs nothing and a copy
    costs a minute and 1.6 GB on every run. `os.link` fails with EXDEV across filesystems,
    which is the fallback's whole purpose.
    """
    pairs = [
        ("kernel-ranchu", "kernel"),
        ("system.img", "system.img"),
        ("vendor.img", "vendor-patched.img"),
        # AND A SECOND COPY UNDER ITS OWN NAME, WHICH IS NOT REDUNDANT.
        #
        # `-vendor vendor-patched.img` says which vendor image to BOOT. It does not stop
        # the emulator looking for `vendor.img` in `-sysdir`, and when that file is
        # missing it dies with
        #
        #     Segmentation fault
        #     timeout: the monitored command dumped core
        #
        # before writing a single line to its log - no message, no exit code, nothing to
        # read. Two runs were lost to this and both looked like a broken disk set rather
        # than a missing file. The bytes are identical, so the copy costs one link.
        ("vendor.img", "vendor.img"),
    ]
    for source_name, dest_name in pairs:
        source = os.path.join(sdk, source_name)
        dest = os.path.join(work, dest_name)
        if not os.path.isfile(source):
            raise SystemExit(f"=== {source} is not in the system image ===")
        if os.path.exists(dest) and os.path.getsize(dest) == os.path.getsize(source):
            continue
        try:
            os.link(source, dest)
            log(f"{dest_name}: hardlink to {source_name}")
        except OSError:
            import shutil
            shutil.copyfile(source, dest)
            log(f"{dest_name}: copy of {source_name} "
                f"({os.path.getsize(dest) >> 20} MiB)")


def sdk_from_environment(api: int = 35, flavour: str = "default",
                         abi: str = "x86_64") -> str:
    """Where the SDK keeps the unpacked system image.

    This is the same path lib/avd.py's install_system_image() writes and the AVD's
    config.ini names in image.sysdir.1, so there is one answer to "where is the system
    image" across the whole repository.
    """
    root = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or ""
    if not root:
        return ""
    return os.path.join(root, "system-images", f"android-{api}", flavour, abi)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sdk", default="",
                   help="directory holding system.img, vendor.img and ramdisk.img; "
                        "defaults to $ANDROID_SDK_ROOT/system-images/android-35/default/x86_64")
    p.add_argument("--out", default="/a/android", help="where the disks are written")
    p.add_argument("--force-userdata", action="store_true",
                   help="throw away an existing /data instead of keeping its applications")
    a = p.parse_args(argv)

    sdk = a.sdk or sdk_from_environment()
    if not sdk:
        raise SystemExit(
            "=== NO SYSTEM IMAGE ===\n"
            "Pass --sdk, or set ANDROID_SDK_ROOT to an SDK that has\n"
            "system-images/android-35/default/x86_64/ unpacked into it."
        )
    if not os.path.isfile(os.path.join(sdk, "system.img")):
        raise SystemExit(f"=== {sdk} has no system.img ===")

    work = os.path.abspath(a.out)
    os.makedirs(work, exist_ok=True)
    log(f"system image: {sdk}")

    stage(work, sdk)
    build_cache(os.path.join(sdk, "system.img"), os.path.join(work, "cache.img"))

    userdata = os.path.join(work, "userdata.img")
    if userdata_filesystem(userdata, force=a.force_userdata):
        log(f"userdata {userdata}: {USERDATA_BYTES >> 30} GiB ext4, freshly made")
        log("  (an existing /data is KEPT by default - its applications are the point)")
    else:
        log("  to start from a blank device: --force-userdata")
        log("  to carry this /data to another machine: python3 android/15/persist.py "
            "--save")

    patch_ramdisk(os.path.join(sdk, "ramdisk.img"),
                  os.path.join(work, "ramdisk.img"))
    log(f"\nDISKS_READY {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
