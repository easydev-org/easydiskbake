"""The EFI System Partition of a disk that is NOT running: read it, edit it, put it back.

    import esp
    part = esp.find_esp(esp.partitions("disk.qcow2"))
    fat = esp.extract("disk.qcow2", part, "esp.fat")
    ... mcopy / mmd on `fat` ...
    esp.write_back("disk.qcow2", part, fat)

WHY THIS EXISTS, AND WHY IT IS NOT `qemu-img convert`

The obvious way to edit a partition is to unpack the disk, edit the file, and encode it again.
On a 32 GiB image that is 32 GiB of temporary space and a full re-encode, twice, and it is what
the first version of the BlissOS install did - on a runner with 40 GB free, with the ISO beside
it.

A qcow2 is addressable at a byte offset through `qemu-io`, so none of that is necessary. The GPT
is 34 sectors at each end of the disk and an ESP is 260 MiB; the operation touches megabytes.

THE PART THAT IS NOT OBVIOUS: WRITING BACK ONLY WHAT CHANGED.

`qemu-io` cannot copy from a file into a disk in one step, but it CAN write a range FROM a file:

    qemu-io -c "write -s <path> <offset> <length>" disk.qcow2

so the edited ESP goes back block by block. Skipping blocks that are byte-identical to what is
already there is NOT an optimisation: every cluster a write touches is allocated, so a full
write-back grows the published image by the ESP's whole size even though two files changed.

WHAT WAS MEASURED, AND IS THE REASON FOR THE SHAPE OF THIS FILE

`qemu-img dd` HAS NO OFFSET ON ITS OUTPUT. `skip` applies to the INPUT. Measured: a
`qemu-img dd bs=1M skip=1 count=260` produced a file whose FIRST bytes were the ESP's, and
writing that back with `of=disk.qcow2` overwrote the disk's own GPT and first partition with ESP
contents - the result would not mount, and mtools said "non DOS media". A raw view was the only
thing addressable by offset, which is what forced the 32 GiB convert this module replaces.
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile

SECTOR = 512

# C12A7328-F81F-11D2-BA4B-00A0C93EC93B, as GPT stores it: the first three fields little-endian,
# so the bytes are 28 73 2a c1 ... and comparing against the string form matches nothing. That
# mismatch reports "there is no ESP" about a disk that has one.
ESP_TYPE = "28732ac11ff8d211ba4b00a0c93ec93b"


def log(message: str) -> None:
    import time
    print(f"[esp {time.strftime('%H:%M:%S')}] {message}", flush=True)


def _qio(disk: str, *commands: str) -> str:
    argv = ["qemu-io", "-f", "qcow2"]
    for command in commands:
        argv += ["-c", command]
    argv.append(disk)
    done = subprocess.run(argv, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(
            f"qemu-io failed ({done.returncode}):\n{(done.stderr or done.stdout)[:800]}")
    return done.stdout


def read_at(disk: str, offset: int, length: int) -> bytes:
    """`length` bytes of `disk` from `offset`, read through qemu-io's hexdump.

    qemu-io has no way to write raw bytes to stdout, so `read -v` is parsed. It is a stable
    format - eight hex bytes, an ASCII column, one line per 16 bytes:

        00000200:  45 46 49 20 50 41 52 54 00 00 01 00 5c 00 00 00  EFI.PART........

    The ASCII column can contain anything, so only the sixteen fields between the first two
    spaces are read - splitting on the separator instead of counting fields is what turns
    "EFI.PART" into an integer and silently loses the line.
    """
    out = _qio(disk, f"read -v {offset} {length}")
    data = bytearray()
    for line in out.splitlines():
        if ":" not in line or line.startswith("read "):
            continue
        _, rest = line.split(":", 1)
        fields = rest.split()
        hexes = [f for f in fields if len(f) == 2 and all(c in "0123456789abcdef" for c in f)]
        for h in hexes[:16]:
            data.append(int(h, 16))
    if len(data) < length:
        raise RuntimeError(f"read {len(data)} of {length} bytes at {offset:#x} from {disk}")
    return bytes(data[:length])


def partitions(disk: str) -> list[dict]:
    """The GPT's partitions, each with the byte offset and length of its area.

    Read from the disk rather than from a tool: `sgdisk` and `parted` are not in the bake image,
    and the header format has not changed since 2000.
    """
    header = read_at(disk, SECTOR, 92)
    if header[:8] != b"EFI PART":
        raise RuntimeError(f"{disk}: no GPT header at LBA 1; this is not a GPT disk")

    entry_lba = struct.unpack("<Q", header[72:80])[0]
    count = struct.unpack("<I", header[80:84])[0]
    size = struct.unpack("<I", header[84:88])[0]
    if size < 128 or count == 0 or count > 1024:
        raise RuntimeError(f"{disk}: a GPT entry table of {count}x{size} is not plausible")

    table = read_at(disk, entry_lba * SECTOR, count * size)
    found = []
    for index in range(count):
        entry = table[index * size:(index + 1) * size]
        if len(entry) < 128 or entry[:16] == b"\x00" * 16:
            continue
        first = struct.unpack("<Q", entry[32:40])[0]
        last = struct.unpack("<Q", entry[40:48])[0]
        found.append({
            "index": index,
            "offset": first * SECTOR,
            "bytes": (last - first + 1) * SECTOR,
            "type": entry[:16].hex(),
            "name": entry[56:128].decode("utf-16-le", "replace").rstrip("\x00"),
        })
    return found


def find_esp(parts: list[dict]) -> dict:
    for part in parts:
        if part["type"] == ESP_TYPE:
            return part
    raise RuntimeError(
        "no EFI System Partition in this disk's GPT\n"
        + "\n".join(f"  {p['index']}: {p['type']} {p['name']!r} {p['bytes'] >> 20} MiB"
                    for p in parts))


def extract(disk: str, part: dict, dest: str) -> str:
    """Copy the partition's bytes to `dest`, which mtools can then read as a FAT image."""
    with open(dest, "wb") as out:
        written = 0
        chunk = 4 << 20
        while written < part["bytes"]:
            take = min(chunk, part["bytes"] - written)
            out.write(read_at(disk, part["offset"] + written, take))
            written += take
    return dest


def write_back(disk: str, part: dict, source: str) -> int:
    """Put an edited FAT image back, writing only the blocks that differ. Returns bytes written."""
    changed = 0
    block = 4 << 20
    written = 0
    with open(source, "rb") as fh, tempfile.TemporaryDirectory() as tmp:
        while written < part["bytes"]:
            same = fh.read(min(block, part["bytes"] - written))
            if not same:
                break
            at = part["offset"] + written
            if read_at(disk, at, len(same)) != same:
                piece = os.path.join(tmp, "block.bin")
                with open(piece, "wb") as out:
                    out.write(same)
                _qio(disk, f"write -s {piece} {at} {len(same)}")
                changed += len(same)
            written += len(same)
    return changed


def fallback_tree(disk: str, *, source: str, names: tuple[str, ...],
                  config: str = "", rewrite: tuple[str, str] = ()) -> int:
    """Copy `names` from `source` on the ESP to /EFI/BOOT, where a firmware looks when no entry matches.

    THIS IS THE STEP THAT MAKES A DISK STARTABLE WITHOUT THE INSTALLER'S NVRAM.

    UEFI's removable-media path is one location: `\\EFI\\BOOT\\BOOTX64.EFI` on the ESP. An
    installer that registers its loader with `efibootmgr` writes the entry into the VARIABLES
    image, not onto the disk - so a disk started with a fresh NVRAM has nothing pointing at it:

        BdsDxe: failed to load Boot0001 "UEFI Misc Device" ... Not Found
        BdsDxe: starting Boot0002 "EFI Internal Shell"

    measured, with the firmware then sitting in its own shell and the disk visible as FS0:.

    `rewrite` REPLACES ONE LINE OF THE COPIED CONFIG, and it exists because BlissOS' installer
    records

        search.fs_uuid 37cd4a0d-2c84-4e46-b914-79a9b00d27b5 root

    BEFORE it formats the partition - so the UUID names a filesystem that does not exist, and the
    boot stops at

        error: no such device: 37cd4a0d-...

    `search --file` asks the question the missing device stood in for ("find the partition that
    holds the system") and answers it from the disk as it is now.

    Returns the number of loader files copied; zero means the source tree was not where the
    caller said, and the caller treats that as a failure.
    """
    parts = partitions(disk)
    part = find_esp(parts)
    log(f"ESP: partition {part['index']}, {part['bytes'] >> 20} MiB at {part['offset']:#x}")

    with tempfile.TemporaryDirectory() as tmp:
        fat = os.path.join(tmp, "esp.fat")
        extract(disk, part, fat)
        env = dict(os.environ, MTOOLS_SKIP_CHECK="1")

        def mtool(*argv: str) -> tuple[int, str]:
            done = subprocess.run(list(argv) + ["-i", fat], capture_output=True,
                                  text=True, env=env)
            return done.returncode, (done.stdout + done.stderr).strip()

        code, out = mtool("mmd", "::/EFI/BOOT")
        # "already exists" is fine: a re-run must not fail on its own previous work.
        if code != 0 and "already exists" not in out.lower():
            raise RuntimeError(f"mmd ::/EFI/BOOT failed: {out[-200:]}")

        copied = 0
        for name in names:
            remote = f"::{source}/{name}"
            if os.name and subprocess.run(["mdir", "-i", fat, remote], capture_output=True,
                                          text=True, env=env).returncode != 0:
                continue
            code, out = mtool("mcopy", "-o", remote, f"::/EFI/BOOT/{name}")
            if code != 0:
                raise RuntimeError(f"copying {name} failed: {out[-200:]}")
            copied += 1
            log(f"  /EFI/BOOT/{name}")

        if config:
            source_cfg = f"{source}/{config}"
            code, out = mtool("mcopy", "-o", f"::{source_cfg}", f"::/EFI/BOOT/{config}")
            if code != 0:
                raise RuntimeError(f"copying {config} failed: {out[-200:]}")
            log(f"  /EFI/BOOT/{config}")

        if rewrite:
            old_line, new_line = rewrite
            local = os.path.join(tmp, config)
            got = subprocess.run(["mtype", "-i", fat, f"::/EFI/BOOT/{config}"],
                                 capture_output=True, text=True, env=env)
            if got.returncode != 0:
                raise RuntimeError(f"could not read /EFI/BOOT/{config}: "
                                   f"{(got.stdout + got.stderr)[-200:]}")
            body = got.stdout
            hit = [ln for ln in body.splitlines() if ln.startswith(old_line)]
            if hit:
                body = body.replace(hit[0], new_line)
                with open(local, "w", encoding="ascii") as fh:
                    fh.write(body)
                code, out = mtool("mcopy", "-o", local, f"::/EFI/BOOT/{config}")
                if code != 0:
                    raise RuntimeError(f"rewriting /EFI/BOOT/{config} failed: {out[-200:]}")
                log(f"  /EFI/BOOT/{config}: {old_line.strip()} -> {new_line}")

        listing = subprocess.run(["mdir", "-i", fat, "-/", "::/EFI/BOOT"],
                                 capture_output=True, text=True, env=env).stdout
        log("  /EFI/BOOT now holds:")
        for line in listing.splitlines():
            line = line.strip()
            if line and not line.startswith(("Volume", "Directory", "Total", "Free",
                                             "bytes free")):
                log(f"    {line}")

        written_bytes = write_back(disk, part, fat)
        log(f"  wrote {written_bytes >> 20} MiB of {part['bytes'] >> 20} MiB back")
    return copied


def holds_a_loader(disk: str) -> tuple[bool, str]:
    """Whether the ESP holds an EFI loader, read as bytes rather than as a directory name.

    THE VERDICT IS READ OFF THE DISK BECAUSE THE INSTALLER'S WINDOW IS NOT ON THE CONSOLE. The
    TUI draws to tty0 and the serial line gets kernel messages only - measured: a whole
    successful BlissOS install wrote 224 bytes there - so "installed successfully" can never be
    grepped for. An ESP with BOOTX64.EFI or grubx64.EFI in it is what a finished install leaves
    behind and what a failed one cannot: two AUTO_INSTALL runs produced FAT32 ESPs with no
    BOOTX64.EFI, and both disks fell through to PXE.

    A read of the ESP's first 6 MiB is enough: a FAT32 root directory of a bootloader-sized tree
    lives there, and `qemu-img dd` reads a window of the image without materialising it. (Two
    earlier versions of this check failed: `qemu-img convert -O raw` allocated the whole 32 GiB
    and the container was OOM-killed at 12 GiB, and reading the qcow2 file directly found nothing
    because the ESP's bytes are not at that offset in the container file until clusters are
    mapped.)
    """
    part = find_esp(partitions(disk))
    window = read_at(disk, part["offset"], min(6 << 20, part["bytes"]))
    fat32 = window[82:90] == b"FAT32   "
    fat16 = window[54:62].startswith(b"FAT")
    filesystem = "FAT32" if fat32 else ("FAT16" if fat16 else "NOT a FAT volume")
    found = [tag for tag in (b"BOOTX64", b"grubx64") if tag in window]
    for tag in (b"BOOTX64", b"grubx64"):
        log(f"  {tag.decode():8} present: {tag in window}")
    return bool(found), filesystem
