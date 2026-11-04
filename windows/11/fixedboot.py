r"""Make a Windows disk bootable without its OVMF variables.

    python3 windows/11/fixedboot.py --disk out/windows11.qcow2

THE QUESTION THIS ANSWERS

Every Windows stage in this repository carries `<job>_bootstate` - an archive holding
`windows.vars`, the OVMF variable store. The boot entry "Windows Boot Manager" is written into
it by the Windows installer and exists nowhere else, so a disk on its own starts like this:

    BdsDxe: failed to load BOOT0001 "UEFI Misc Device"  ... : Not Found
    BdsDxe: failed to load BOOT0002 "UEFI Misc Device 2" ... : Not Found
    BdsDxe: failed to load BOOT0003 "UEFI HTTPv4 (MAC:...)" : Not Found
    BdsDxe: failed to load BOOT0004 "UEFI HTTPv6 (MAC:...)" : Not Found
    BdsDxe: loading BOOT0005 "EFI Internal Shell"
    Shell>

That is a real dependency on two extra files, and it is what makes an artifact awkward to hand
to anyone: "here is the disk, and also the firmware state, or it will not start".

CAN IT BE REMOVED?

UEFI has a standard fallback: `\EFI\BOOT\BOOTX64.EFI` on the EFI System Partition, which the
firmware tries when no boot entry resolves. Windows puts its loader at
`\EFI\Microsoft\Boot\bootmgfw.efi` and reaches it only through a BOOT#### entry; nothing puts it
at the fallback path, because on real hardware the installer's NVRAM entry is enough.

So if `bootmgfw.efi` - and the BCD it reads - exist at the fallback path, a firmware with a blank
variable store should find them. That is the hypothesis, and this module is what tests it. It is
NOT a claim that it works.

WHY qemu-io AND NOT A RAW COPY

The first version converted the disk to raw, edited it with `mcopy -i <file>@@<offset>`, and
converted back. Three copies of 16 GiB, and it needed a runner with 90 GB free - which the
standard GitHub runner does not have, so the experiment failed in `prepare` before it reached
the disk at all.

`qemu-io` reads and writes at a byte offset INSIDE a qcow2, so the partition table can be read
and the ESP's FAT can be edited without ever materialising the disk's bytes. What it cannot do is
run mtools, which wants a file - so the FAT is extracted to a small file, edited there, and
written back. An ESP is 100 MiB; the whole operation touches megabytes, not gigabytes.

WHAT IS KEPT

Nothing is removed and no existing path changes: the copy is written in addition to
`\EFI\Microsoft\Boot\bootmgfw.efi`, so a disk that also has its NVRAM entry boots exactly as it
did before. That is what makes this safe to try on a real bake - the failure mode of the
hypothesis being wrong is that nothing changes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))


def log(message: str) -> None:
    print(f"[fixedboot] {message}", flush=True)


WIN_LOADER = "/EFI/Microsoft/Boot/bootmgfw.efi"
WIN_BCD = "/EFI/Microsoft/Boot/BCD"
FALLBACK_LOADER = "/EFI/BOOT/BOOTX64.EFI"
FALLBACK_BCD = "/EFI/BOOT/BCD"

SECTOR = 512
# THE ESP TYPE GUID, IN THE BYTE ORDER GPT STORES IT.
#
# The GUID is C12A7328-F81F-11D2-BA4B-00A0C93EC93B, and GPT writes its first three fields
# LITTLE-ENDIAN - so the bytes on disk are 28 73 2a c1 1f f8 d2 11 ... and not the string's
# order. Comparing against the string form matches nothing, which is a failure that says
# "there is no ESP" about a disk that has one.
ESP_TYPE = "28732ac11ff8d211ba4b00a0c93ec93b"


def qio(disk: str, *commands: str) -> str:
    """Run qemu-io commands against `disk`, returning its output."""
    argv = ["qemu-io", "-f", "qcow2"]
    for c in commands:
        argv += ["-c", c]
    argv.append(disk)
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"qemu-io failed ({r.returncode}):\n{(r.stderr or r.stdout)[:800]}")
    return r.stdout


def read_at(disk: str, offset: int, length: int) -> bytes:
    """`length` bytes of `disk` starting at `offset`, through qemu-io's hexdump.

    qemu-io has no way to write raw bytes to stdout, so its `read -v` output is parsed. It is a
    stable format - eight hex bytes, an ASCII column, one line per 16 bytes - and the input is a
    partition table, whose offsets are known.
    """
    out = qio(disk, f"read -v {offset} {length}")
    data = bytearray()
    for line in out.splitlines():
        # qemu-io prints, per 16 bytes:
        #
        #     00000200:  45 46 49 20 50 41 52 54 00 00 01 00 5c 00 00 00  EFI.PART........
        #
        # Offset, two spaces, sixteen hex bytes, two spaces, ASCII. The ASCII column can
        # contain any byte, so only the sixteen fields between the first two spaces are read -
        # splitting on the separator instead of counting fields is what turns "EFI.PART" into
        # integers and silently loses the line.
        if ":" not in line or line.startswith("read "):
            continue
        _, rest = line.split(":", 1)
        fields = rest.split()
        hexes = [f for f in fields if len(f) == 2 and all(c in "0123456789abcdef" for c in f)]
        for h in hexes[:16]:
            data.append(int(h, 16))
    if len(data) < length:
        raise SystemExit(f"read {len(data)} of {length} bytes at {offset:#x} from {disk}")
    return bytes(data[:length])


def gpt_partitions(disk: str) -> list[dict]:
    """The GPT's partitions, with the byte offset of each.

    READ FROM THE DISK WITH qemu-io, not from a tool: `sgdisk` is not in the bake image and
    `parted` is not either, and the header format has not changed since 2000.
    """
    header = read_at(disk, SECTOR, 92)
    if header[:8] != b"EFI PART":
        raise SystemExit(f"{disk}: no GPT header at LBA 1; this is not a GPT disk")

    entry_lba = struct.unpack("<Q", header[72:80])[0]
    count = struct.unpack("<I", header[80:84])[0]
    size = struct.unpack("<I", header[84:88])[0]
    if size < 128 or count == 0 or count > 1024:
        raise SystemExit(f"{disk}: a GPT entry table of {count}x{size} is not plausible")

    table = read_at(disk, entry_lba * SECTOR, count * size)
    parts = []
    for i in range(count):
        e = table[i * size:(i + 1) * size]
        if len(e) < 128 or e[:16] == b"\x00" * 16:
            continue
        first = struct.unpack("<Q", e[32:40])[0]
        last = struct.unpack("<Q", e[40:48])[0]
        parts.append({
            "index": i,
            "offset": first * SECTOR,
            "bytes": (last - first + 1) * SECTOR,
            "type": e[:16].hex(),
            "name": e[56:128].decode("utf-16-le", "replace").rstrip("\x00"),
        })
    return parts


def find_esp(parts: list[dict]) -> dict:
    for p in parts:
        if p["type"] == ESP_TYPE:
            return p
    raise SystemExit(
        "no EFI System Partition in this disk's GPT\n"
        + "\n".join(f"  {p['index']}: {p['type']} {p['name']!r} {p['bytes'] >> 20} MiB"
                    for p in parts)
    )


def run(argv: list[str], what: str) -> subprocess.CompletedProcess:
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{what} failed ({r.returncode}):\n{(r.stderr or r.stdout)[:800]}")
    return r


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the qcow2 to make self-booting")
    args = p.parse_args(argv)

    if not os.path.isfile(args.disk):
        raise SystemExit(f"no disk at {args.disk}")

    parts = gpt_partitions(args.disk)
    log("partitions:")
    for part in parts:
        log(f"  {part['index']}: {part['name'] or '(unnamed)'} "
            f"{part['bytes'] >> 20} MiB at {part['offset']:#x}")

    esp = find_esp(parts)
    log(f"ESP: partition {esp['index']}, {esp['bytes'] >> 20} MiB at {esp['offset']:#x}")

    with tempfile.TemporaryDirectory() as tmp:
        fat = os.path.join(tmp, "esp.fat")
        log(f"extracting the ESP ({esp['bytes'] >> 20} MiB) to {fat}")
        # A FAT filesystem is self-describing: mtools can operate on the partition's bytes as a
        # file, which is all an ESP is. 100 MiB of memcpy replaces 16 GiB of convert.
        with open(fat, "wb") as fh:
            written = 0
            chunk = 4 << 20
            while written < esp["bytes"]:
                n = min(chunk, esp["bytes"] - written)
                fh.write(read_at(args.disk, esp["offset"] + written, n))
                written += n
        log(f"  {os.path.getsize(fat) >> 20} MiB")

        loader = os.path.join(tmp, "bootmgfw.efi")
        run(["mcopy", "-o", "-i", fat, f"::{WIN_LOADER}", loader], f"mcopy {WIN_LOADER} out")
        size = os.path.getsize(loader)
        log(f"  {WIN_LOADER}: {size} bytes")
        if size < 4096:
            raise SystemExit(f"{WIN_LOADER} is {size} bytes; that is not a PE image")

        for d in ("::/EFI", "::/EFI/BOOT"):
            subprocess.run(["mmd", "-i", fat, d], capture_output=True, text=True)

        # -o overwrites, so a disk that already has a fallback loader gets the Windows one.
        run(["mcopy", "-o", "-i", fat, loader, f"::{FALLBACK_LOADER}"],
            f"mcopy {FALLBACK_LOADER} in")
        log(f"  wrote {FALLBACK_LOADER}")

        bcd = os.path.join(tmp, "BCD")
        r = subprocess.run(["mcopy", "-o", "-i", fat, f"::{WIN_BCD}", bcd],
                           capture_output=True, text=True)
        if r.returncode == 0:
            run(["mcopy", "-o", "-i", fat, bcd, f"::{FALLBACK_BCD}"],
                f"mcopy {FALLBACK_BCD} in")
            log(f"  wrote {FALLBACK_BCD} ({os.path.getsize(bcd)} bytes)")
        else:
            log(f"  no {WIN_BCD}; the loader will use the one beside it")

        # WRITE THE FAT BACK - ONLY THE PARTS THAT CHANGED.
        #
        # qemu-io cannot read from a file and write to the disk in one step, but it CAN write a
        # range FROM a file: `write -s <path> <offset> <len>`. So the edited ESP is written
        # block by block, skipping blocks that are byte-identical to what is already there.
        #
        # THAT SKIP IS NOT AN OPTIMISATION. Writing 100 MiB into a qcow2 allocates every cluster
        # it touches, so a full write-back would grow the image by 100 MiB even though only two
        # files changed - and the artifact this stage produces is the one people download.
        log("writing the ESP back, block by block")
        changed = 0
        block = 4 << 20
        written = 0
        with open(fat, "rb") as fh:
            while written < esp["bytes"]:
                same = fh.read(min(block, esp["bytes"] - written))
                at = esp["offset"] + written
                if not same:
                    break
                if read_at(args.disk, at, len(same)) != same:
                    part = os.path.join(tmp, "blk.bin")
                    with open(part, "wb") as out:
                        out.write(same)
                    run(["qemu-io", "-f", "qcow2",
                         "-c", f"write -s {part} {at} {len(same)}", args.disk],
                        f"qemu-io write {at:#x}")
                    changed += len(same)
                written += len(same)
        log(f"  wrote {changed >> 20} MiB of {esp['bytes'] >> 20} MiB")

    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
