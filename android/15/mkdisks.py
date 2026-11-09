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
import shutil
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


def has_cache(path: str) -> bool:
    """True if the file is a cache.img this builder made - a GPT with `system` in it.

    CHECKED BY STRUCTURE, NOT BY SIZE. cache.img's size follows the super partition inside
    it, which changes when Google ships a different system image; a size test would rebuild
    a perfectly good cache the first time that happened and take the /data key with it. The
    partition table is the property that says "this is one of ours".
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(SECTOR)
            header = fh.read(92)
            if header[0:8] != b"EFI PART":
                return False
            entries_lba = int.from_bytes(header[72:80], "little")
            count = int.from_bytes(header[80:84], "little")
            size = int.from_bytes(header[84:88], "little")
            fh.seek(entries_lba * SECTOR)
            array = fh.read(count * size)
    except OSError:
        return False
    names = {
        array[i * size:(i + 1) * size][56:128].decode("utf-16-le", "ignore").rstrip("\x00")
        for i in range(count) if any(array[i * size:(i + 1) * size])
    }
    return {"system", "metadata"} <= names


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


CPIO_MAGIC = b"070701"

# THE FLAGS THAT TURN /data ENCRYPTION ON.
#
# fs_mgr decides with two fstab fields, not a build option:
#
#     static bool should_use_metadata_encryption(const FstabEntry& entry) {
#         return !entry.metadata_key_dir.empty() && entry.fs_mgr_flags.file_encryption;
#     }
#
# so `fileencryption=` AND `keydirectory=` both have to be gone; `reservedsize=` and
# `fsverity` belong to the encryption setup and go with them.
ENCRYPT_FLAGS = ("fileencryption=", "keydirectory=", "reservedsize=", "fsverity")


def cpio_entries(raw: bytes) -> list[dict]:
    """Every entry in a newc cpio: header offset, name, size, body offset."""
    out = []
    off = 0
    while True:
        at = raw.find(CPIO_MAGIC, off)
        if at < 0 or at + 110 > len(raw):
            break
        header = raw[at:at + 110]
        try:
            fields = [int(header[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        except ValueError:
            off = at + 6
            continue
        filesize, namesize = fields[6], fields[11]
        name_start = at + 110
        name = raw[name_start:name_start + namesize - 1]
        body = name_start + namesize
        body += (-body) % 4
        if name == b"TRAILER!!!":
            break
        out.append({"at": at, "size": filesize, "body": body, "name": name})
        off = body + filesize + ((-(body + filesize)) % 4)
    return out


def strip_encryption(raw: bytes) -> bytes:
    """Remove the /data encryption flags from any fstab inside a decompressed cpio.

    THE HEADER HAS TO BE REWRITTEN WITH THE BODY. A newc header carries `filesize` as eight
    ASCII hex digits and init trusts it; editing the text without updating that field makes
    init read the old length into the next entry:

        init: [libfstab] Error parsing fstab: expected 5 fields, got 1
        init: ReadDefaultFstab(): failed to find device default fstab
        init: FirstStageMount not available -> InitFatalReboot

    ONLY THE FLAG FIELD IS REPLACED, not the whole line. Rebuilding a line from split()
    collapses the tabs and runs of spaces fstab uses, and libfstab then refuses the file for
    the same "expected 5 fields" reason.
    """
    entries = [e for e in cpio_entries(raw) if b"fstab" in e["name"]]
    if not entries:
        return raw

    rebuilt = bytearray()
    cursor = 0
    changed = False
    for entry in entries:
        body = raw[entry["body"]:entry["body"] + entry["size"]]
        if b"fileencryption=" not in body:
            continue
        lines = []
        for line in body.split(b"\n"):
            if b"/data" in line and b"fileencryption=" in line:
                start = line.rfind(b"fileencryption=")
                stop = line.find(b"#", start)
                if stop < 0:
                    stop = len(line)
                field_start = line.rfind(b" ", 0, start) + 1
                flags = [f for f in line[field_start:stop].split(b",")
                         if f and not any(f.startswith(d.encode()) for d in ENCRYPT_FLAGS)]
                line = line[:field_start] + b",".join(flags) + line[stop:]
            lines.append(line)
        new_body = b"\n".join(lines)
        if new_body == body:
            continue
        header = bytearray(raw[entry["at"]:entry["at"] + 110])
        header[6 + 6 * 8:14 + 6 * 8] = f"{len(new_body):08x}".encode()
        rebuilt += raw[cursor:entry["at"]]
        rebuilt += bytes(header)
        rebuilt += raw[entry["at"] + 110:entry["body"]]
        rebuilt += new_body
        rebuilt += b"\x00" * ((-len(new_body)) % 4)
        cursor = entry["body"] + entry["size"] + ((-entry["size"]) % 4)
        changed = True

    if not changed:
        return raw
    rebuilt += raw[cursor:]
    return bytes(rebuilt)


def strip_stream(blob: bytes) -> bytes:
    """Decompress one legacy stream, strip the flags, recompress it on its own."""
    raw = subprocess.run(["lz4", "-d", "-c", "-"], input=blob, capture_output=True).stdout
    patched = strip_encryption(raw)
    if patched is raw:
        return blob
    packed = subprocess.run(["lz4", "-l", "-c", "-"], input=patched, capture_output=True)
    if packed.returncode != 0:
        raise SystemExit(f"=== lz4 failed recompressing: {packed.stderr[:200]!r} ===")
    return packed.stdout


def patch_ramdisk(src: str, dest: str, noencrypt: bool = False) -> None:
    """Append one more legacy-LZ4 stream carrying the fstab, optionally un-encrypting /data.

    THE SDK RAMDISK IS A CHAIN OF TWO LZ4 LEGACY STREAMS, not one. Android's initrd
    reader consumes streams until the input ends, so appending a complete stream is
    exactly what the format already does. Rebuilding the cpio and recompressing it as a
    SINGLE stream makes init never run at all: 340 kernel restarts, no `init:` line.

    WHY --noencrypt PATCHES STREAMS 1 AND 2 INSTEAD OF APPENDING A THIRD

    The appended stream only covers the earliest mounts (system, metadata, super). /data is
    `latemount`, and by then init has taken its entry from the SDK's own
    `first_stage_ramdisk/fstab.ranchu` inside stream 1:

        2.43s  init: Mount point /dev/block/vdc @ /data uses metadata encryption
        5.25s  init: Userdata mounted using /vendor/etc/fstab.ranchu result : 6

    The decision at 2.4 s is made from the ramdisk copy, which is why editing only the
    vendor partition changes nothing. Both are edited: this function for the ramdisk,
    and the vendor fstab separately.
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

    body = data
    if noencrypt:
        # EVERY STREAM THAT CARRIES AN fstab IS PATCHED, one at a time, so the chain keeps
        # its structure. Stream 0 holds /init and the kernel modules; the fstab is in 1.
        rebuilt = bytearray()
        offset = 0
        patched = 0
        while offset + 4 <= len(body) and body[offset:offset + 4] == LZ4_LEGACY_MAGIC:
            start = offset
            offset += 4
            while offset + 4 <= len(body):
                size = struct.unpack("<I", body[offset:offset + 4])[0] & 0x7FFFFFFF
                if size == 0 or offset + 4 + size > len(body):
                    offset += 4
                    break
                offset += 4 + size
                if offset + 4 <= len(body) and body[offset:offset + 4] == LZ4_LEGACY_MAGIC:
                    break
            stream = strip_stream(body[start:offset])
            if stream is not body[start:offset]:
                patched += 1
            rebuilt += stream
        rebuilt += body[offset:]
        body = bytes(rebuilt)
        log(f"noencrypt: patched {patched} ramdisk stream(s)")

    with open(dest, "wb") as fh:
        fh.write(body + packed.stdout)

    # VERIFY THE CONTENT SURVIVED, in the file that was just written.
    check = subprocess.run(["lz4", "-d", "-c", dest], capture_output=True).stdout
    if b"/dev/block/by-name/system" not in check:
        raise SystemExit("=== the fstab did not survive the round trip ===")
    if noencrypt:
        for line in check.split(b"\n"):
            if b"/data" in line and b"fileencryption=" in line:
                raise SystemExit(f"=== /data is still encrypted: {line[:160]!r} ===")
    log(f"ramdisk {dest}: {len(data)} + {len(packed.stdout)} B, fstab present"
        + (", /data encryption OFF" if noencrypt else ""))


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


# THE GUEST'S OWN NETWORK SETUP, APPENDED TO A SCRIPT SELINUX ALREADY TRUSTS.
#
# ANDROID DOES NOT BRING UP ETHERNET BY ITSELF. This guest proves it: the virtio-net device
# is present (pci 1af4:1000), the driver loads, /sys/class/net lists eth0 - and eth0 comes
# up with flags 0x1002 (BROADCAST, no IFF_UP), operstate "down", no address. Nothing asks
# it to; AOSP's phone images are configured for WiFi.
#
# Driven by hand from the console the same interface works:
#
#     ip link set eth0 up                    flags 0x1002 -> 0x1003, operstate down -> up
#     ip addr add 172.30.0.2/24 dev eth0
#     ip route add default via 172.30.0.1    rc=0
#
# WHY NOT A SERVICE OF OUR OWN - FOUR ATTEMPTS, FOUR MEASURED REFUSALS
#
#     on property:sys.boot_completed=1   never fired; that trigger needs a TRANSITION, and
#                                        the property is 1 by then (read from /metadata/b)
#     on late-init + exec                fired, refused: init may not execute a shell at
#                                        all - `File /system/bin/sh(labeled
#                                        "u:object_r:shell_exec:s0") has incorrect label or
#                                        no domain transition from u:r:init:s0`
#     class late_start / class hal       never started on this image
#     our own .sh via /vendor/bin/sh     refused again, with vendor_shell_exec:
#                                        `File /vendor/bin/sh(labeled
#                                        "u:object_r:vendor_shell_exec:s0") has incorrect
#                                        label ...`
#
# WHAT DOES WORK IS ALREADY IN THE IMAGE. AOSP ships /vendor/bin/init.ranchu-net.sh, which
# brings up eth1 with ifconfig, started by the `ranchu-net` service:
#
#     service ranchu-net /vendor/bin/init.ranchu-net.sh
#         class late_start
#         user root
#         group root wakelock wifi
#         oneshot
#         disabled    # Started on post-fs-data
#
# and that file carries `u:object_r:goldfish_setup_exec:s0`, so its domain is right. The
# eth0 configuration is APPENDED TO THAT FILE and started by that existing service. The
# only thing added is the `on post-fs-data` trigger without the virtiowifi condition, since
# this guest has no VirtioWifi.
NETWORK_PREPEND = b"""\
# --- PATH first, added by easydiskbake -------------------------------------------
# The script's own first command is `getprop`, and an init service inherits no PATH worth
# speaking of - without this the shell answers 127 before reaching anything below.
export PATH=/vendor/bin:/system/bin:/system/xbin
"""

NETWORK_APPEND = b"""\

# --- eth0, added by easydiskbake -------------------------------------------------
# Android leaves the emulated ethernet interface down (flags 0x1002, operstate down, no
# address). lib/net.py's bridge gives it a fixed address, so this states it.
# `/vendor/bin/toybox_vendor ip`, BECAUSE THE POLICY ALLOWS THAT ONE AND NOT
# /system/bin/ip.
#
# Three attempts at the command itself, each refused by SELinux with a different subject:
#
#     ifconfig ...              found and allowed, but /vendor/bin has no `route`
#     /system/bin/ip ...        denied outright:
#                                 avc: denied { getattr } for comm="init.ranchu-net"
#                                 path="/system/bin/ip" scontext=u:r:goldfish_setup:s0
#                                 tcontext=u:object_r:system_file:s0 permissive=0
#     /vendor/bin/toybox_vendor  allowed - and it is the one that has every command needed.
#
# The vendor policy in device/generic/goldfish/sepolicy/vendor/goldfish_setup.te grants
#
#     allow goldfish_setup vendor_toolbox_exec:file execute_no_trans;
#     allow goldfish_setup vendor_file:file execute_no_trans;
#
# and toybox_vendor is a vendor_file. It carries ifconfig, ip AND route - checked by
# reading its own command table, since the binary is bionic-linked and cannot run here:
#
#     toybox_vendor has: brctl, ifconfig, ip, route
#
# The one line that would have made /system/bin/ip work is commented out in AOSP itself:
#
#     #allow goldfish_setup system_file:file { execute getattr open read };
# THE SDK SCRIPT ALREADY FAILED WITH 127 BEFORE THESE LINES.
#
# The script starts with `getprop ro.boot.qemu.virtiowifi`, and `getprop` comes from PATH.
# An init service inherits almost nothing, so the shell answers 127 for its first command
# and the script exits - our lines are never reached. Setting PATH first is what makes both
# the SDK's commands and ours resolvable.
export PATH=/vendor/bin:/system/bin:/system/xbin
/vendor/bin/toybox_vendor ip link set eth0 up
/vendor/bin/toybox_vendor ip addr add 172.30.0.2/24 dev eth0
/vendor/bin/toybox_vendor ip route add default via 172.30.0.1 dev eth0
# ANDROID ROUTES BY POLICY: without this rule the default route above is in a table that
# `ip rule` never consults, and every packet falls through to `32000: from all unreachable`.
# Measured - the interface was up, addressed and correctly routed, and ping still answered
# "Network is unreachable" until this line was added.
/vendor/bin/toybox_vendor ip rule add from all lookup main pref 31000
# THE RULE, WITHOUT WHICH THE ROUTE ABOVE DOES NOTHING. Android routes by policy and the
# shipped rule table never consults `main`; every packet falls through to
#
#     32000:  from all unreachable
#
# so an interface that is up, addressed and correctly routed still answers "Network is
# unreachable". Measured on the working adb path: with this rule the bridge answers
# 2 packets transmitted, 2 received; without it, nothing leaves the guest.
/vendor/bin/toybox_vendor ip rule add from all lookup main pref 31000
"""

# THE TRIGGER, IN ITS OWN .rc. A vendor .rc IS parsed (init says so: "Parsing file
# /vendor/etc/init/..."), so this only needs to start the service AOSP already defined.
# SSHD, STARTED FROM THE SAME PLACE THE NETWORK IS.
#
# Termux ships no init script and Android has no service for it, so this is the thin piece
# that runs the binary. It is a shell command inside a vendor service, NOT a new SELinux
# domain: the binary is vendor_file, which goldfish_setup may execute.
#
# `-D` KEEPS IT IN THE FOREGROUND, which is what makes init treat it as a daemon rather than
# a oneshot. `-p 8022` is Termux's default port and the one lib/net.py already forwards.
SSHD_RC = b"""\
# sshd from Termux's openssh, started once the network is up.
#
# The host keys are generated on first start by sshd itself (`-R` regenerates, but a missing
# key is created automatically), so nothing has to be shipped.
service sshd /data/data/com.termux/files/usr/bin/sshd
    class late_start
    user root
    group root
    oneshot
    disabled

on late-init
    start sshd
"""

NETWORK_RC = b"""\
# Start the SDK's own network script, which now also brings eth0 up.
#
# The `ranchu-net` service is defined in /vendor/etc/init/hw/init.ranchu.rc with
# `disabled`, and AOSP starts it only when ro.boot.qemu.virtiowifi=1 - a condition this
# guest does not meet. Starting it here, on an event init cannot miss, is the whole change.
# THE SDK'S OWN SERVICE, WHICH ALREADY WORKS.
#
# A service of our own was tried with `seclabel u:r:goldfish_setup:s0` and `stdio_to_kmsg`
# and still exited 127, so the domain is not the thing to redefine - the existing
# `ranchu-net` service definition is already correct and simply needs starting.
on late-init
    start ranchu-net
"""


def carve_partition(image: str, name: str) -> tuple[str, int, int]:
    """Copy one GPT partition out of an image so debugfs can edit it.

    DEBUGFS NEEDS A FILESYSTEM, NOT A DISK. Both SDK images are GPTs whose single partition
    starts at LBA 2048; pointing debugfs at the whole file makes it look for a superblock at
    offset 0 and refuse. Returns the carved file and the LBA range, which is what
    put_partition() needs to put it back.
    """
    parts = read_gpt(image)
    if name not in parts:
        raise SystemExit(f"=== {image} has no `{name}` GPT partition ===")
    first, last = parts[name]
    carved = f"{image}.{name}-part"
    with open(image, "rb") as fh, open(carved, "wb") as out:
        fh.seek(first * SECTOR)
        remaining = (last - first + 1) * SECTOR
        while remaining > 0:
            chunk = fh.read(min(8 * 2 ** 20, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)
    return carved, first, last


def put_partition(image: str, carved: str, first: int, last: int) -> None:
    """Write an edited partition back at the offset it came from, in place.

    THE GPT AROUND IT IS NOT TOUCHED, which is the point: header, entry array and both
    checksums stay as they were, so nothing has to be recomputed.
    """
    with open(image, "r+b") as out, open(carved, "rb") as src:
        out.seek(first * SECTOR)
        remaining = (last - first + 1) * SECTOR
        while remaining > 0:
            chunk = src.read(min(8 * 2 ** 20, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)


def debugfs_write(fs_image: str, path: str, body: bytes, mode: int = 0o100644) -> None:
    """Create or replace a file inside an ext4 image with debugfs.

    REMOVE FIRST, THEN WRITE. debugfs has no in-place rewrite: `write` onto an existing name
    fails, and `rm` followed by `write` reuses the blocks that were just freed, so a body
    longer than the old one still fits.
    """
    staged = fs_image + ".staged"
    with open(staged, "wb") as fh:
        fh.write(body)
    try:
        subprocess.run(["debugfs", "-w", "-R", f"rm {path}", fs_image],
                       capture_output=True, text=True)
        done = subprocess.run(["debugfs", "-w", "-R",
                               f"write {staged} {path}", fs_image],
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(f"=== debugfs write {path} failed: "
                             f"{(done.stderr or done.stdout)[:300]} ===")
        # `sif` TAKES A 16-BIT FIELD, SO THE MODE HAS TO BE OCTAL WITH A LEADING ZERO.
        #
        #     sif ... mode 100755     Value '100755' exceeds field mode maximum 65535
        #     sif ... mode 0100755    Mode: 0755  Type: regular
        #
        # `f"{0o100755:o}"` produces "100755", which is 33261 in decimal - larger than the
        # field holds, so debugfs refuses and the file keeps the 0644 it was written with.
        # init then answers `cannot execv(...): Permission denied` and exits 127, which is
        # three layers away from the octal prefix that caused it.
        done = subprocess.run(
            ["debugfs", "-w", "-R", f"sif {path} mode 0{mode:o}", fs_image],
            capture_output=True, text=True)
        if "exceeds field" in (done.stdout or "") + (done.stderr or ""):
            raise SystemExit(
                f"=== debugfs refused the mode for {path}: "
                f"{(done.stdout or done.stderr).strip()[:160]} ===")
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def add_network(cache: str, sdk_system: str) -> None:
    """Install the eth0 bring-up .rc into the vendor partition INSIDE super.

    /vendor IS NOT vendor.img. That file is an argument to the emulator and nothing mounts
    it. The guest says so itself:

        init: [libfs_mgr] Created logical partition vendor on device /dev/block/dm-4
        init: [libfs_mgr] __mount(source=/dev/block/dm-4,target=/vendor,type=ext4)=0

    so `/vendor` is a logical partition of `super`, and an .rc written into vendor.img is
    never read. Two boots with a correct-looking file had no effect for exactly that reason.

    THE EXTENT DESCRIBES TWO DIFFERENT SPACES. liblp gives the vendor offset RELATIVE TO
    super, and super sits at LBA 4096 in system.img but at 1819216 on the cache disk this
    builder writes. Mixing them lands 1819216 sectors early, at something that is not the
    vendor filesystem:

        === the vendor partition inside super is not ext4 (magic 8a1d) ===

    So the extent comes from the table and the LBA comes from the disk being edited.
    """
    with open(sdk_system, "rb") as fh:
        sdk_super = read_gpt(sdk_system)["super"][0]
        fh.seek(sdk_super * SECTOR)
        head = fh.read(4 * 2 ** 20)
    lp = lp_partitions(head)
    if "vendor" not in lp:
        raise SystemExit("=== super has no `vendor` logical partition ===")
    vendor_off, vendor_sectors = lp["vendor"]

    super_lba = read_gpt(cache)["super"][0]
    start = (super_lba + vendor_off) * SECTOR
    size = vendor_sectors * SECTOR

    carved = cache + ".vendor-part"
    try:
        with open(cache, "rb") as fh, open(carved, "wb") as out:
            fh.seek(start)
            remaining = size
            while remaining > 0:
                chunk = fh.read(min(8 * 2 ** 20, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)

        with open(carved, "rb") as fh:
            fh.seek(1024 + 0x38)
            magic = fh.read(2)
        if magic != b"\x53\xef":
            raise SystemExit(
                f"=== the vendor partition inside super is not ext4 (magic {magic.hex()}) ===")

        # APPEND TO THE SCRIPT THAT ALREADY HAS THE RIGHT SELINUX LABEL.
        existing = subprocess.run(["debugfs", "-R", "cat /bin/init.ranchu-net.sh", carved],
                                  capture_output=True, text=True).stdout
        if "eth0, added by easydiskbake" in existing:
            log("  (eth0 configuration already present)")
        else:
            if "ifconfig eth1" not in existing:
                raise SystemExit(
                    "=== /bin/init.ranchu-net.sh is not the script this expected; it no "
                    "longer mentions eth1, so appending to it may not be safe ===")
            # THE SELINUX LABEL HAS TO BE PUT BACK, AND THIS IS THE WHOLE REASON THREE
            # ATTEMPTS FAILED AT THE LAST STEP.
            #
            # debugfs_write replaces the file (rm + write), and a replaced file comes back
            # as u:object_r:unlabeled:s0 - the extended attribute debugfs does not carry
            # over. init then refuses to start it:
            #
            #     File /vendor/bin/init.ranchu-net.sh(labeled "u:object_r:unlabeled:s0")
            #         has incorrect label or no domain transition from u:r:init:s0
            #
            # The label is read back from the ORIGINAL through the vendor file_contexts
            # rule, which is what the platform itself would use.
            # PREPENDED AFTER THE SHEBANG LINE, so PATH is set before the SDK's own
            # `getprop` on line 4 of the script.
            lines = existing.split("\n", 1)
            head = lines[0] + "\n"
            rest = lines[1] if len(lines) > 1 else ""
            debugfs_write(carved, "/bin/init.ranchu-net.sh",
                          (head + NETWORK_PREPEND.decode() + rest).encode()
                          + NETWORK_APPEND, mode=0o100755)
            subprocess.run(["debugfs", "-w", "-R",
                            "ea_set /bin/init.ranchu-net.sh "
                            "security.selinux u:object_r:goldfish_setup_exec:s0",
                            carved], capture_output=True, text=True)

        debugfs_write(carved, "/etc/init/eth0up.rc", NETWORK_RC + SSHD_RC)

        # VERIFY BY READING BACK. A truncated script looks exactly like an interface that
        # did not come up.
        sh_back = subprocess.run(["debugfs", "-R", "cat /bin/init.ranchu-net.sh", carved],
                                 capture_output=True, text=True).stdout
        if "eth0, added by easydiskbake" not in sh_back or "toybox_vendor ip link set eth0 up" not in sh_back:
            raise SystemExit("=== the eth0 lines did not survive the write ===")
        label = subprocess.run(["debugfs", "-R",
                                "ea_list /bin/init.ranchu-net.sh", carved],
                               capture_output=True, text=True).stdout
        if "goldfish_setup_exec" not in label:
            raise SystemExit(
                f"=== the SELinux label was not restored, init will refuse this file "
                f"as unlabeled: {label.strip()[:120]} ===")
        rc_back = subprocess.run(["debugfs", "-R", "cat /etc/init/eth0up.rc", carved],
                                 capture_output=True, text=True).stdout
        if "start ranchu-net" not in rc_back:
            raise SystemExit("=== the .rc did not survive the write ===")

        with open(cache, "r+b") as out, open(carved, "rb") as src:
            out.seek(start)
            remaining = size
            while remaining > 0:
                chunk = src.read(min(8 * 2 ** 20, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        log(f"vendor (in super, {size >> 20} MiB): eth0 bring-up installed")
    finally:
        if os.path.exists(carved):
            os.unlink(carved)


# SSH, FROM TERMUX'S OWN PACKAGES.
#
# WHY TERMUX AND NOT A STATIC BUILD. Android's C library is bionic, not glibc, so a
# statically linked Debian dropbear is a foreign binary on this guest. Termux's packages are
# compiled FOR Android against bionic - `sshd` needs libandroid-support, libcrypto, libldns
# and libc, and libc is the guest's own:
#
#     INTERP          /system/bin/linker64       <- the guest's dynamic loader
#     0x01 (NEEDED)   libandroid-support.so
#     0x01 (NEEDED)   libcrypto.so.3
#     0x01 (NEEDED)   libldns.so
#     0x01 (NEEDED)   libc.so                    <- from the system partition
#
# THE PREFIX IS COMPILED IN. The binary has /data/data/com.termux/files/usr baked into it
# for its config, host keys and moduli, so the files are unpacked at exactly that path
# rather than at /vendor/... - a symlink would not help, because the path is a string in
# the binary, not a lookup.
SSH_PREFIX = "/data/data/com.termux/files/usr"
SSH_ARCHIVE = "termux-ssh.tar.zst"


def add_ssh(userdata: str, payload: str) -> None:
    """Unpack the Termux prefix into /data/data/com.termux, where sshd expects it.

    NOT INTO vendor, AND THE REASON IS ARITHMETIC. The vendor partition inside super is
    101 MiB with EIGHT HUNDRED inodes total:

        Inode count:  800
        Free inodes:  9
        Free blocks:  83

    and this payload is 1761 files and 20 MiB. Five hundred of those writes failed with

        write: Could not allocate inode in ext2 filesystem

    before anything else could go wrong. /data is where the files belong anyway - the path
    is compiled into the binary:

        /data/data/com.termux/files/usr/bin/sshd
        /data/data/com.termux/files/usr/etc/ssh/modssh_host_ecdsa_key

    so no symlink and no policy exception is needed: this is the path sshd asks for.

    AND IT HAS TO GO IN BEFORE THE FIRST BOOT. Once the guest has started, /data is
    metadata-encrypted and every byte of it, superblocks included, is ciphertext on the
    host - measured: 0x53EF before the first boot, 0xfcbf after. Writing into a fresh
    userdata.img is the only moment the filesystem is readable from here.
    """
    if not os.path.isfile(payload):
        raise SystemExit(f"=== no ssh payload at {payload} ===")
    if not os.path.isfile(userdata):
        raise SystemExit(f"=== no {userdata} to install ssh into ===")

    staging = userdata + ".ssh-staging"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        unpacked = subprocess.run(["tar", "-x", "-f", payload, "-C", staging],
                                  capture_output=True, text=True)
        if unpacked.returncode != 0:
            raise SystemExit(f"=== tar failed: {unpacked.stderr[:300]} ===")

        prefix = "/" + SSH_PREFIX.lstrip("/")
        commands: list[str] = []
        made: set[str] = set()
        staged = 0

        def mkdirs(path: str) -> None:
            parts = path.strip("/").split("/")
            for i in range(1, len(parts) + 1):
                d = "/" + "/".join(parts[:i])
                if d in made:
                    continue
                commands.append(f"mkdir {d}")
                made.add(d)

        for root, dirs, files in os.walk(staging):
            rel = os.path.relpath(root, staging)
            dest_dir = prefix if rel == "." else f"{prefix}/{rel}"
            mkdirs(dest_dir)
            for name in files:
                source = os.path.join(root, name)
                target = f"{dest_dir}/{name}"
                if os.path.islink(source):
                    real = os.path.realpath(source)
                    if not os.path.exists(real) or not real.startswith(staging):
                        continue
                    source = real
                try:
                    mode = os.stat(source).st_mode & 0o7777
                except OSError:
                    continue
                commands.append(f"rm {target}")
                commands.append(f"write {source} {target}")
                commands.append(f"sif {target} mode 0{0o100000 | mode:o}")
                staged += 1

        # A DIRECTORY THE BINARY NEEDS TO WRITE INTO. sshd creates its host keys on first
        # start, and /data is writable, but the prefix directories come from this image and
        # would otherwise be read-only to a non-root sshd.
        commands.append(f"mkdir {prefix}/tmp")

        script = userdata + ".ssh-cmds"
        out_path = userdata + ".ssh-out"
        with open(script, "w") as fh:
            fh.write("\n".join(commands) + "\n")
        with open(out_path, "w") as out_fh:
            done = subprocess.run(["debugfs", "-w", "-f", script, userdata],
                                  stdout=out_fh, stderr=subprocess.STDOUT)
        with open(out_path, errors="replace") as fh:
            text = fh.read()
        os.unlink(out_path)
        os.unlink(script)

        allocated = text.count("Allocated inode")
        failures = [ln for ln in text.splitlines()
                    if "do_write_internal" in ln or "Could not allocate" in ln]
        log(f"ssh: {allocated} of {staged} files written")
        if done.returncode != 0 or len(failures) > 3:
            for ln in failures[:5]:
                log(f"  debugfs: {ln[:180]}")
            raise SystemExit(f"=== ssh install wrote {allocated} of {staged} files ===")

        check = subprocess.run(["debugfs", "-R", f"stat {prefix}/bin/sshd", userdata],
                               capture_output=True, text=True)
        if "Type: regular" not in ((check.stdout or "") + (check.stderr or "")):
            raise SystemExit("=== sshd did not land in /data ===")
        log(f"ssh: sshd installed at {prefix}/bin/sshd")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def patch_vendor(vendor: str) -> None:
    """Strip the /data encryption flags from /vendor/etc/fstab.ranchu, in place.

    THE VENDOR PARTITION IS WHERE THE LATE MOUNT READS ITS FSTAB. The ramdisk's copy decides
    early (see patch_ramdisk), and this one decides the late mount:

        init: Userdata mounted using /vendor/etc/fstab.ranchu result : 6

    Editing only one of the two leaves the guest encrypted by whichever it read, which is
    what made a half-fix look like no fix at all.

    THE IMAGE IS A GPT. debugfs wants the filesystem, not the disk, so the `vendor` partition
    is carved out, edited, and written back at the same offset - the GPT around it, including
    its checksums, is untouched.
    """
    parts = read_gpt(vendor)
    if "vendor" not in parts:
        raise SystemExit(f"=== {vendor} has no `vendor` GPT partition ===")
    first, last = parts["vendor"]
    carved = vendor + ".part"
    try:
        with open(vendor, "rb") as fh, open(carved, "wb") as out:
            fh.seek(first * SECTOR)
            remaining = (last - first + 1) * SECTOR
            while remaining > 0:
                chunk = fh.read(min(8 * 2 ** 20, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)

        result = subprocess.run(["debugfs", "-R", "cat /etc/fstab.ranchu", carved],
                                capture_output=True, text=True)
        text = result.stdout
        if "/data" not in text:
            raise SystemExit("=== /vendor/etc/fstab.ranchu has no /data line ===")
        lines = []
        for line in text.splitlines():
            if line.lstrip().startswith("#") or not line.strip():
                lines.append(line)
                continue
            fields = line.split("	")
            if len(fields) < 5:
                fields = line.split()
            if len(fields) >= 5 and fields[1] == "/data":
                flags = [f for f in fields[4].split(",")
                         if f and not any(f.startswith(d) for d in ENCRYPT_FLAGS)]
                line = line.replace(fields[4], ",".join(flags), 1)
            lines.append(line)
        staged = carved + ".fstab"
        with open(staged, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        for command in ("rm /etc/fstab.ranchu",
                        f"write {staged} /etc/fstab.ranchu"):
            done = subprocess.run(["debugfs", "-w", "-R", command, carved],
                                  capture_output=True, text=True)
            if done.returncode != 0:
                raise SystemExit(f"=== debugfs '{command}' failed: "
                                 f"{(done.stderr or done.stdout)[:300]} ===")
        os.unlink(staged)

        check = subprocess.run(["debugfs", "-R", "cat /etc/fstab.ranchu", carved],
                               capture_output=True, text=True).stdout
        for line in check.splitlines():
            if line.split()[1:2] == ["/data"] and "fileencryption=" in line:
                raise SystemExit(f"=== /data still encrypting: {line[:160]} ===")

        with open(vendor, "r+b") as out, open(carved, "rb") as src:
            out.seek(first * SECTOR)
            remaining = (last - first + 1) * SECTOR
            while remaining > 0:
                chunk = src.read(min(8 * 2 ** 20, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        log(f"vendor {vendor}: /data encryption flags removed")
    finally:
        if os.path.exists(carved):
            os.unlink(carved)


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
    p.add_argument("--ssh-payload", default="",
                   help="a tar.zst of the Termux prefix to install, giving the guest sshd")
    p.add_argument("--no-net", dest="net", action="store_false",
                   help="do not install the eth0 bring-up service into vendor")
    p.add_argument("--noencrypt", action="store_true",
                   help="do not encrypt /data, so the disk is readable from any host")
    p.set_defaults(net=True)
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
    # cache.img IS REBUILT ONLY WHEN THERE IS NOT ONE, AND THE REASON IS THE SAME AS /data's.
    #
    # It holds the `metadata` partition, and /data's decryption key lives there:
    #
    #     /metadata/vold
    #     /metadata/password_slots
    #
    # so rebuilding it throws the key away while leaving the encrypted /data in place. The
    # guest then boots, cannot open /data, and stops in apexd-bootstrap with nothing in the
    # log naming the cause - measured on run 42478503710, and the reason a restored pair
    # could not be used at all.
    #
    # THE OTHER PARTITIONS ARE UNCHANGED BY A BOOT - system and super are read-only - so
    # keeping the file keeps nothing stale: it is the same system with its metadata written.
    cache = os.path.join(work, "cache.img")
    if has_cache(cache) and not a.force_userdata:
        log(f"cache {cache}: KEPT (an existing disk, and the key that opens /data is on it)")
    else:
        build_cache(os.path.join(sdk, "system.img"), cache)
        log("  (a fresh cache.img means a fresh /data: the key is made with it)")

    # THE GUEST'S NETWORK, INTO THE PARTITION THAT IS ACTUALLY MOUNTED AT /vendor.
    if a.net:
        add_network(cache, os.path.join(sdk, "system.img"))

    userdata = os.path.join(work, "userdata.img")
    if userdata_filesystem(userdata, force=a.force_userdata):
        log(f"userdata {userdata}: {USERDATA_BYTES >> 30} GiB ext4, freshly made")
        log("  (an existing /data is KEPT by default - its applications are the point)")
    else:
        log("  to start from a blank device: --force-userdata")
        log("  to carry this /data to another machine: python3 android/15/persist.py "
            "--save")

    # THE SSH PAYLOAD GOES INTO /data, AFTER THE FILESYSTEM EXISTS AND BEFORE ANY BOOT.
    #
    # `--ssh-payload` names the tarball: 4 MB compressed, built once rather than derived
    # from the system image. Absent, the step is skipped and the image is what it was.
    if a.ssh_payload:
        add_ssh(userdata, a.ssh_payload)

    # ENCRYPTION IS DECIDED IN TWO PLACES AND BOTH ARE PATCHED HERE.
    #
    # The ramdisk's own fstab.ranchu decides the early, first-stage mount (2.4 s in a boot);
    # the vendor partition's decides the late one (`Userdata mounted using
    # /vendor/etc/fstab.ranchu`). Editing one leaves the other, and a half-fix looks exactly
    # like no fix.
    if a.noencrypt:
        # BOTH COPIES, AND THE SECOND ONE IS THE ONE THE GUEST ACTUALLY MOUNTS.
        #
        # mkdisks stages the vendor image TWICE - `vendor-patched.img` for `-vendor`, and
        # `vendor.img` because the emulator looks for that name inside `-sysdir`. Only the
        # first was patched, and the guest still encrypted /data:
        #
        #     vendor.img          wait,check,quota,fileencryption=...  <- mounted at /vendor
        #     vendor-patched.img  wait,check,quota,latemount            <- patched, unused
        #
        # `init: Userdata mounted using /vendor/etc/fstab.ranchu` names /vendor, and that is
        # the first file. Both are patched here.
        #
        # stage() HARDLINKS vendor.img WHEN IT CAN, so either file may be the SDK's own
        # inode. Patching in place would edit /opt/android-sdk - the next run, and every
        # other image built on this machine, would inherit the change.
        sdk_vendor = os.path.join(sdk, "vendor.img")
        for name in ("vendor-patched.img", "vendor.img"):
            vendor = os.path.join(work, name)
            if os.stat(vendor).st_nlink > 1:
                os.unlink(vendor)
                shutil.copyfile(sdk_vendor, vendor)
            patch_vendor(vendor)

    patch_ramdisk(os.path.join(sdk, "ramdisk.img"),
                  os.path.join(work, "ramdisk.img"), noencrypt=a.noencrypt)
    log(f"\nDISKS_READY {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
