"""Build the three disks an Android system image needs to boot without an AVD.

    python3 android/15/mkdisks.py --out /a/android-test

WHY THIS EXISTS

Google's SDK ships `system.img` and `vendor.img` as GPT DISKS, not as partitions. The
emulator's normal path unpacks them into an AVD and hands paths to `-system`/`-vendor`.
That works only when the emulator owns the AVD. Here the disks are assembled explicitly,
because every piece has to be inspectable:

    cache.img     system, metadata, super, vbmeta, boot - the five names init looks for
    userdata.img  an ext4 /data
    ramdisk.img   the SDK ramdisk plus an fstab that names those five

Everything below was measured against sysimg android-15 x86_64 (build AE3A.240806.019)
and is written down where the number is not obvious.

THE FIVE PARTITIONS ON cache.img ARE THE POINT

init's first stage reads an fstab and then asks for each `/dev/block/by-name/<name>`.
`metadata`, `super`, `vbmeta` and `boot` are all things AOSP's own images carry, and
`system` is the one that has to exist PHYSICALLY even though it also lives inside
`super`:

    init: [libfs_avb] Device path not found: /dev/block/by-name/system

AVB will not verify a logical partition, and dm-0 has no by-name path, so the same bytes
are copied out of `super` at the offset the LP table names and given a partition of
their own.

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
SUPER_FIRST_LBA = 4096
LINUX_FS_BYTES = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4").bytes_le

# Sizes taken from the GPT headers of the SDK images themselves.
SUPER_SECTORS = 3166207 - SUPER_FIRST_LBA + 1
METADATA_SECTORS = 32768
VBMETA_SECTORS = 2048
BOOT_SECTORS = 65536

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


def userdata_filesystem(dest: str) -> None:
    """An 8 GiB ext4 /data, without `orphan_file`.

    The guest's own e2fsck predates FEATURE_C12:

        e2fsck: /dev/block/mapper/userdata has unsupported feature(s): FEATURE_C12
        e2fsck: ... WARNING: Filesystem still has errors

    WRITTEN IN PLACE, NOT BUILT IN /tmp AND MOVED. /tmp and the mounted volume are
    different filesystems, and os.replace across them is EXDEV, not a copy.
    """
    with open(dest, "wb") as fh:
        fh.truncate(8 * 2 ** 30)
    subprocess.run(["/sbin/mkfs.ext4", "-q", "-F", "-O", "^orphan_file",
                    "-L", "userdata", dest], check=True)


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
    with open(src, "rb") as fh:
        fh.seek(SUPER_FIRST_LBA * SECTOR)
        super_head = fh.read(4 * 2 ** 20)
    parts = lp_partitions(super_head)
    if "system" not in parts:
        raise SystemExit("=== NO `system` IN THE LP TABLE ===")
    sys_sector, sys_sectors = parts["system"]
    log(f"system: sector {sys_sector} in super, {sys_sectors} sectors "
        f"({sys_sectors * SECTOR / 2 ** 20:.0f} MiB)")

    # SYSTEM FIRST. by-name published only the early partitions on this disk when the
    # layout was metadata-first, and `system` (then vdb5) was the one that did not
    # resolve. Ordering was the only difference.
    layout: list[tuple[str, int, int]] = []
    cursor = 2048
    for name, sectors in (("system", sys_sectors),
                          ("metadata", METADATA_SECTORS),
                          ("super", SUPER_SECTORS),
                          ("vbmeta", VBMETA_SECTORS),
                          ("boot", BOOT_SECTORS)):
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

        with open(src, "rb") as s:
            # system, EXTRACTED FROM super AT THE OFFSET THE LP TABLE NAMES.
            s.seek((SUPER_FIRST_LBA + sys_sector) * SECTOR)
            out.seek(positions["system"] * SECTOR)
            copy(s, out, sys_sectors * SECTOR, "system")

            s.seek(SUPER_FIRST_LBA * SECTOR)
            out.seek(positions["super"] * SECTOR)
            copy(s, out, SUPER_SECTORS * SECTOR, "super")

            s.seek(2048 * SECTOR)
            out.seek(positions["vbmeta"] * SECTOR)
            out.write(s.read(VBMETA_SECTORS * SECTOR))
            log("vbmeta: 1.0 MiB")

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


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sdk", default="/a/sysimg/x86_64")
    p.add_argument("--out", default="/a/android-test")
    a = p.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)

    build_cache(os.path.join(a.sdk, "system.img"), os.path.join(a.out, "cache.img"))

    userdata_filesystem(os.path.join(a.out, "userdata.img"))
    log(f"userdata {os.path.join(a.out, 'userdata.img')}: 8 GiB ext4")

    patch_ramdisk(os.path.join(a.sdk, "ramdisk.img"),
                  os.path.join(a.out, "ramdisk.img"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
