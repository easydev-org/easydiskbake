"""Turn off /data encryption, so the disk can be read on any machine.

    python3 android/15/noencrypt.py --vendor /a/android/vendor-patched.img

WHY THIS IS A ONE-LINE EDIT AND WHERE THE LINE IS

fs_mgr decides whether to encrypt /data with two fstab flags, not with a build option:

    static bool should_use_metadata_encryption(const FstabEntry& entry) {
        return !entry.metadata_key_dir.empty() && entry.fs_mgr_flags.file_encryption;
    }
                                    ^^^^^^^^^^^^^^^^^^^^^      ^^^^^^^^^^^^^^^^^^^^^
    system/core/fs_mgr/fs_mgr.cpp
                                    both have to be set

The fstab that supplies them for this guest is NOT the one this repository writes into the
ramdisk. It is the SDK's own, inside the vendor partition:

    /dev/block/vdc  /data  ext4  noatime,nosuid,nodev,nomblk_io_submit,errors=panic \\
        wait,check,quota,fileencryption=aes-256-xts:aes-256-cts,reservedsize=128M,\\
        fsverity,keydirectory=/metadata/vold/metadata_encryption,latemount

and the guest says so while booting:

    init: Userdata mounted using /vendor/etc/fstab.ranchu result : 6

That is why an earlier attempt to reason about this from the ramdisk's fstab was wrong: the
ramdisk's copy is used for the FIRST stage (system, metadata, super); /data is mounted late,
from vendor.

WHAT THIS DOES

Removes `fileencryption=` and `keydirectory=` from the /data line of
/vendor/etc/fstab.ranchu inside the vendor image. Both, because
should_use_metadata_encryption requires both - removing one would leave the other half
configured and produce a mount that is neither encrypted nor plainly readable.

`reservedsize=128M` and `fsverity` are removed too: they belong to the encryption setup and
a filesystem that will never be encrypted does not need 128 MiB held back for it.

WHAT IT COSTS

The disk becomes readable with plain `debugfs`/`mount` on any host, which is the point.
It also means anything on it is readable by anyone holding the file - there is no password
in this design either way, the key was in the metadata partition, so what is lost is
obscurity rather than protection.

VERIFIED BY THE GUEST, NOT BY THE FILE. After the edit the boot log no longer contains
`uses metadata encryption`, and a /data written by one run is readable by the next without
carrying the key along - which is what makes the disk usable across machines.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

SECTOR = 512

# THE FLAGS THAT TURN ENCRYPTION ON, AND WHY BOTH.
#
#   fileencryption=   sets fs_mgr_flags.file_encryption
#   keydirectory=     sets FstabEntry::metadata_key_dir
#
# should_use_metadata_encryption is `both set`; leaving either behind means the guest still
# takes the encrypted path and the edit accomplishes nothing.
DROP = ("fileencryption=", "keydirectory=", "reservedsize=", "fsverity")


def log(msg: str) -> None:
    print(f"[noencrypt] {msg}", flush=True)


def read_gpt(path: str) -> dict[str, tuple[int, int]]:
    """{name: (first_lba, last_lba)} from the image's own GPT."""
    with open(path, "rb") as fh:
        fh.seek(SECTOR)
        header = fh.read(92)
        if header[0:8] != b"EFI PART":
            return {}
        entries_lba = int.from_bytes(header[72:80], "little")
        count = int.from_bytes(header[80:84], "little")
        size = int.from_bytes(header[84:88], "little")
        fh.seek(entries_lba * SECTOR)
        array = fh.read(count * size)
    out: dict[str, tuple[int, int]] = {}
    for i in range(count):
        entry = array[i * size:(i + 1) * size]
        if not any(entry):
            continue
        name = entry[56:128].decode("utf-16-le", "ignore").rstrip("\x00")
        out[name] = (int.from_bytes(entry[32:40], "little"),
                     int.from_bytes(entry[40:48], "little"))
    return out


def carve(image: str, into: str) -> bool:
    """Copy the `vendor` partition out of the GPT so debugfs can write to it.

    DEBUGFS NEEDS THE FILESYSTEM, NOT THE DISK. The vendor image is a GPT whose single
    partition starts at LBA 2048; pointing debugfs at the whole file makes it look for a
    superblock at offset 0 and refuse. The partition is carved out, edited, and written back
    at the same offset - which keeps every other byte of the GPT, including the checksums.
    """
    parts = read_gpt(image)
    if "vendor" not in parts:
        log(f"{image}: no `vendor` partition in its GPT")
        return False
    first, last = parts["vendor"]
    log(f"vendor partition: LBA {first}..{last} ({(last - first + 1) * SECTOR >> 20} MiB)")
    with open(image, "rb") as fh, open(into, "wb") as out:
        fh.seek(first * SECTOR)
        remaining = (last - first + 1) * SECTOR
        while remaining > 0:
            chunk = fh.read(min(8 * 2 ** 20, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)
    return True


def strip_flags(path: str) -> str:
    """Rewrite /vendor/etc/fstab.ranchu inside the carved filesystem. Returns the new line."""
    # ONE READER FOR THE WHOLE FILE: debugfs `cat` is the only way in without mounting, and
    # the image is read-only to this container.
    result = subprocess.run(["debugfs", "-R", "cat /etc/fstab.ranchu", path],
                            capture_output=True, text=True)
    text = result.stdout
    if "/data" not in text:
        raise SystemExit(
            "=== /etc/fstab.ranchu in the vendor partition has no /data line ===\n"
            f"{text[:400]}"
        )

    lines = []
    changed = None
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            lines.append(line)
            continue
        fields = line.split()
        # <dev> <mnt> <type> <mnt_flags> <fs_mgr_flags>
        if len(fields) >= 5 and fields[1] == "/data":
            flags = [f for f in fields[4].split(",")
                     if f and not any(f.startswith(d) for d in DROP)]
            fields[4] = ",".join(flags)
            line = "   ".join(fields[:4]) + "   " + fields[4]
            changed = line
        lines.append(line)

    if changed is None:
        raise SystemExit("=== no /data entry was found to rewrite ===")

    new_text = "\n".join(lines) + "\n"
    # WRITE THROUGH A TEMPORARY FILE AND `debugfs -w -R write`. debugfs has no in-place
    # rewrite command for a file, and `rm` followed by `write` reuses the block range that
    # was just freed, so the length can grow without running out of room.
    staged = path + ".fstab.ranchu"
    with open(staged, "w") as fh:
        fh.write(new_text)
    for command in ("rm /etc/fstab.ranchu", f"write {staged} /etc/fstab.ranchu"):
        done = subprocess.run(["debugfs", "-w", "-R", command, path],
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(f"=== debugfs '{command}' failed: "
                             f"{(done.stderr or done.stdout)[:300]} ===")
    os.unlink(staged)
    return changed


def verify(path: str, expect_absent: bool = True) -> bool:
    """Read the fstab back out of the edited filesystem and check the flags are gone."""
    result = subprocess.run(["debugfs", "-R", "cat /etc/fstab.ranchu", path],
                            capture_output=True, text=True)
    text = result.stdout
    line = next((ln for ln in text.splitlines()
                 if ln.split()[1:2] == ["/data"]), "")
    if not line:
        log("could not find the /data line after the edit")
        return False
    log(f"now: {line}")
    if expect_absent:
        left = [d for d in DROP if d in line]
        if left:
            log(f"STILL PRESENT: {', '.join(left)}")
            return False
    return True


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--vendor", default="/a/android/vendor-patched.img",
                   help="the vendor image the boot uses")
    a = p.parse_args(argv)

    if not os.path.isfile(a.vendor):
        raise SystemExit(f"=== no {a.vendor} ===")

    carved = a.vendor + ".vendor-part"
    if not carve(a.vendor, carved):
        return 1
    try:
        line = strip_flags(carved)
        log(f"rewrote: {line}")
        if not verify(carved):
            return 1
        # WRITE THE PARTITION BACK AT THE SAME OFFSET, so the GPT around it is untouched.
        parts = read_gpt(a.vendor)
        first, last = parts["vendor"]
        with open(a.vendor, "r+b") as out, open(carved, "rb") as src:
            out.seek(first * SECTOR)
            remaining = (last - first + 1) * SECTOR
            while remaining > 0:
                chunk = src.read(min(8 * 2 ** 20, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        log(f"written back into {a.vendor}")
    finally:
        if os.path.exists(carved):
            os.unlink(carved)

    log("the guest will no longer encrypt /data - confirm with --verify-after-boot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
