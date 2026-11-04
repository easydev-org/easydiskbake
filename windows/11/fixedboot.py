r"""Make a Windows disk bootable without its OVMF variables.

    python3 windows/11/fixedboot.py --disk out/windows11.qcow2

THE QUESTION THIS ANSWERS

Every Windows stage in this repository carries `<job>_bootstate` - an archive holding
`windows.vars`, the OVMF variable store. The boot entry "Windows Boot Manager" is written into
it by the Windows installer and exists nowhere else, so a disk on its own starts like this:

    BdsDxe: failed to load Boot0001 "UEFI Misc Device"  ... : Not Found
    BdsDxe: failed to load Boot0002 "UEFI Misc Device 2" ... : Not Found
    BdsDxe: failed to load Boot0003 "UEFI HTTPv4 (MAC:...)" : Not Found
    BdsDxe: failed to load Boot0004 "UEFI HTTPv6 (MAC:...)" : Not Found
    BdsDxe: loading Boot0005 "EFI Internal Shell"
    Shell>

That is a real dependency on two extra files, and it is the kind of thing that makes an
artifact awkward to hand to anyone: "here is the disk, and also the firmware state, or it will
not start".

CAN IT BE REMOVED?

UEFI has a standard fallback: a removable-media path the firmware tries when no boot entry
resolves. `\\EFI\\BOOT\\BOOTX64.EFI` on the EFI System Partition. Windows writes its loader to
`\EFI\Microsoft\Boot\bootmgfw.efi`, which is only reachable through a Boot#### entry; nothing
puts it at the fallback path, because on real hardware the installer's NVRAM entry is enough.

So if `bootmgfw.efi` (and the BCD it reads) exist at the fallback path, a firmware with a blank
variable store should find them. That is the hypothesis, and this module is what tests it -
NOT a claim that it works.

WHAT IT DOES, AND WHY IT NEEDS A RAW COPY

The ESP is a FAT filesystem inside a GPT partition, and `mcopy -i <image>@@<offset>` edits a FAT
filesystem at a byte offset in a FILE. qcow2 is not a file whose bytes are the disk's bytes, so
the disk is converted to raw, edited, and converted back. Three passes over 12-16 GiB, and it
only ever runs in the bake that produces the disk - a stage that pulls a finished disk would
have to do the same work on every pull.

THE FALLBACK IS ADDITIVE

Nothing is removed and no existing path is changed: the copy is written in addition to
`\EFI\Microsoft\Boot\bootmgfw.efi`, so a disk that also has its NVRAM entry boots exactly as it
did before. That is what makes this safe to try on a real bake rather than a throwaway one -
the failure mode of the hypothesis being wrong is that nothing changes.
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


# WHERE WINDOWS PUTS ITS LOADER, AND WHERE THE FIRMWARE LOOKS WITHOUT BEING TOLD.
WIN_LOADER = "/EFI/Microsoft/Boot/bootmgfw.efi"
WIN_BCD = "/EFI/Microsoft/Boot/BCD"
FALLBACK_LOADER = "/EFI/BOOT/BOOTX64.EFI"
# THE BCD IS READ FROM A PATH RELATIVE TO THE LOADER'S OWN DIRECTORY in the fallback case, so a
# second copy has to sit beside it. Windows' bootmgr searches \EFI\Microsoft\Boot first and the
# current directory second; putting both in place covers either behaviour.
FALLBACK_BCD = "/EFI/BOOT/BCD"


def gpt_partitions(raw: str) -> list[dict]:
    """The GPT's partitions, with the byte offset of each.

    READ DIRECTLY FROM THE IMAGE, not from a tool: `sgdisk -p` is not in the bake image and
    `parted` is not either, and the header format is 40 lines of specification that has not
    changed since 2000. A protective MBR at 0 and a GPT header at LBA 1, entries at the LBA the
    header names.
    """
    with open(raw, "rb") as fh:
        fh.seek(512)
        header = fh.read(92)
        if header[:8] != b"EFI PART":
            raise SystemExit(f"{raw}: no GPT header at LBA 1; this is not a GPT disk")

        entry_lba = struct.unpack("<Q", header[72:80])[0]
        count = struct.unpack("<I", header[80:84])[0]
        size = struct.unpack("<I", header[84:88])[0]
        if size < 128 or count == 0 or count > 1024:
            raise SystemExit(f"{raw}: a GPT entry table of {count}x{size} is not plausible")

        fh.seek(entry_lba * 512)
        table = fh.read(count * size)

    parts = []
    for i in range(count):
        e = table[i * size:(i + 1) * size]
        if len(e) < 128 or e[:16] == b"\x00" * 16:
            continue
        first = struct.unpack("<Q", e[32:40])[0]
        last = struct.unpack("<Q", e[40:48])[0]
        parts.append({
            "index": i,
            "offset": first * 512,
            "bytes": (last - first + 1) * 512,
            "type": e[:16].hex(),
            "name": e[56:128].decode("utf-16-le", "replace").rstrip("\x00"),
        })
    return parts


# THE ESP's PARTITION TYPE GUID, byte for byte as GPT stores it.
ESP_TYPE = "c12a7328f81f11d2ba4b00a0c93ec93b"


def find_esp(parts: list[dict]) -> dict:
    for p in parts:
        if p["type"] == ESP_TYPE:
            return p
    raise SystemExit(
        "no EFI System Partition in this disk's GPT\n"
        + "\n".join(f"  {p['index']}: {p['type']} {p['name']!r} {p['bytes'] >> 20} MiB"
                    for p in parts)
    )


def run(argv: list[str], what: str) -> None:
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{what} failed ({r.returncode}):\n"
                         f"{(r.stderr or r.stdout or '')[:800]}")


def mcopy_at(raw: str, offset: int, src: str, dst: str) -> None:
    """Copy one file into the FAT filesystem that starts at `offset` inside `raw`."""
    run(["mcopy", "-o", "-i", f"{raw}@@{offset}", src, f"::{dst}"], f"mcopy {dst}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the qcow2 to make self-booting")
    p.add_argument("--keep-raw", action="store_true",
                   help="leave the raw intermediate beside the disk, for inspection")
    args = p.parse_args(argv)

    if not os.path.isfile(args.disk):
        raise SystemExit(f"no disk at {args.disk}")

    work = os.path.dirname(os.path.abspath(args.disk))
    raw = os.path.join(work, os.path.basename(args.disk) + ".raw")

    log(f"converting {args.disk} to raw")
    run(["qemu-img", "convert", "-O", "raw", args.disk, raw], "qemu-img convert to raw")

    try:
        parts = gpt_partitions(raw)
        log("partitions:")
        for part in parts:
            log(f"  {part['index']}: {part['name'] or '(unnamed)'} "
                f"{part['bytes'] >> 20} MiB at {part['offset']:#x}")

        esp = find_esp(parts)
        log(f"ESP: partition {esp['index']}, {esp['bytes'] >> 20} MiB at {esp['offset']:#x}")

        # THE LOADER IS EXTRACTED FIRST, so the failure says "this disk has no bootmgfw.efi"
        # rather than "mcopy wrote an empty file".
        with tempfile.TemporaryDirectory() as tmp:
            loader = os.path.join(tmp, "bootmgfw.efi")
            bcd = os.path.join(tmp, "BCD")

            run(["mcopy", "-o", "-i", f"{raw}@@{esp['offset']}", f"::{WIN_LOADER}", loader],
                f"mcopy {WIN_LOADER} out")
            size = os.path.getsize(loader)
            log(f"  {WIN_LOADER}: {size} bytes")
            if size < 4096:
                raise SystemExit(f"{WIN_LOADER} is {size} bytes; that is not a PE image")

            # mkdir -p for both directories: /EFI/BOOT usually exists on a Windows ESP (the
            # firmware's own fallback bootloader lives there), but nothing guarantees it.
            for d in ("::/EFI", "::/EFI/BOOT"):
                subprocess.run(["mmd", "-i", f"{raw}@@{esp['offset']}", d],
                               capture_output=True, text=True)

            mcopy_at(raw, esp["offset"], loader, FALLBACK_LOADER)
            log(f"  wrote {FALLBACK_LOADER}")

            # THE BCD, IF THE DISK HAS ONE. bootmgr can boot a Windows installed on the same
            # volume without it on some firmware, and cannot on others; copying it costs 40 KiB
            # and removes a variable from the experiment.
            r = subprocess.run(
                ["mcopy", "-o", "-i", f"{raw}@@{esp['offset']}", f"::{WIN_BCD}", bcd],
                capture_output=True, text=True)
            if r.returncode == 0:
                mcopy_at(raw, esp["offset"], bcd, FALLBACK_BCD)
                log(f"  wrote {FALLBACK_BCD} ({os.path.getsize(bcd)} bytes)")
            else:
                log(f"  no {WIN_BCD} on this disk; the loader will use the one beside it")

        log("converting back to qcow2")
        tmp_out = args.disk + ".fixed"
        run(["qemu-img", "convert", "-O", "qcow2", raw, tmp_out], "qemu-img convert to qcow2")
        os.replace(tmp_out, args.disk)
        log(f"done: {args.disk} ({os.path.getsize(args.disk) >> 20} MiB)")
        return 0
    finally:
        if args.keep_raw:
            log(f"raw kept at {raw}")
        elif os.path.exists(raw):
            os.unlink(raw)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
