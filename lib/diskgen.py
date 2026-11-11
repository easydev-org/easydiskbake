"""Give a blank qcow2 the partition table an installer expects to find.

    import diskgen
    diskgen.blank("/work/blissos16.qcow2", 32)
    diskgen.gpt(disk, esp_mib=260)

WHY THIS IS A LIBRARY AND NOT PART OF A STAGE

Two things every "install onto a disk" stage does, and neither is a fact about a screen:

    create the file        `qemu-img create`, which is one line and three copies of it in the
                           tree before this module existed
    write a partition table `sfdisk` on a REGULAR FILE, which is what a qcow2 becomes once it is
                           unpacked - /dev/nbd0 is not available in this container and
                           `qemu-nbd` cannot load the module

WHY THE TABLE IS WRITTEN BEFORE THE INSTALLER RUNS AT ALL

Because that is a dual-boot user's path. Their ESP already exists; an installer that is allowed
to partition the disk itself takes a different branch (BlissOS' `install_hd` calls
`auto_partition`, and with `AUTO_INSTALL` set that path is `sgdisk --zap-all`). A table written
here makes the installer list the partitions it found and ask which is which, which is both the
path this repository wants to test and the one where the bootloader question actually appears.

THE FILESYSTEM IS NOT MADE HERE. The installer's own `make_esp` runs mkdosfs on the partition
the walkthrough selects, and doing it twice only rewrites what this wrote.
"""

from __future__ import annotations

import os
import subprocess

SECTOR = 512

# The two type GUIDs, in sfdisk's own spelling. EF00 is the ESP; 8300 would be the same Linux
# type under an MBR, and the long form is what a GPT needs.
ESP_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX_GUID = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"


def log(message: str) -> None:
    import time
    print(f"[diskgen {time.strftime('%H:%M:%S')}] {message}", flush=True)


def blank(path: str, gigabytes: int) -> str:
    """A sparse qcow2 of `gigabytes`. An existing file is replaced, because a bake starts clean."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    done = subprocess.run(["qemu-img", "create", "-f", "qcow2", path, f"{gigabytes}G"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"qemu-img create failed: {done.stderr[-300:]}")
    log(f"created a blank {gigabytes} GiB qcow2 at {path}")
    return path


def gpt(disk: str, *, esp_mib: int = 260, log=log) -> dict:
    """Write a GPT with one ESP and one Linux partition filling the rest.

    THE NUMBERS ARE THE SAME ONES `sgdisk --new=1::+260M --typecode=1:EF00 --largest-new=2`
    PRODUCES: the first aligned sector is 2048, 532480 sectors is 260 MiB, and Linux takes the
    rest minus the 34 sectors the backup GPT occupies.

    THE UNPACK IS THE PRICE OF sfdisk. It reads a script from stdin and works on a regular file,
    so the qcow2 is converted to raw, partitioned, and encoded back. The caller gets the same file
    it passed in. `lib/esp.py` is what avoids this cost for LATER edits - a partition table can
    only be written this way.
    """
    raw = disk + ".raw"
    if os.path.exists(raw):
        os.unlink(raw)
    log("unpacking the qcow2 so a partition table can be written into it")
    made = subprocess.run(["qemu-img", "convert", "-f", "qcow2", "-O", "raw", disk, raw],
                          capture_output=True, text=True)
    if made.returncode != 0:
        raise RuntimeError(f"qemu-img convert failed: {made.stderr[-300:]}")

    try:
        sectors = os.path.getsize(raw) // SECTOR
        esp_start = 2048
        esp_sectors = (esp_mib << 20) // SECTOR
        data_start = esp_start + esp_sectors
        data_end = sectors - 34
        if data_end <= data_start:
            raise RuntimeError(
                f"a {sectors * SECTOR >> 20} MiB disk cannot hold a {esp_mib} MiB ESP "
                f"and a data partition")

        script = (
            "label: gpt\n"
            f"start={esp_start}, size={esp_sectors}, type={ESP_GUID}, name=EFI\n"
            f"start={data_start}, size={data_end - data_start + 1}, "
            f"type={LINUX_GUID}, name=Android\n"
        )
        log(f"writing a GPT: ESP {esp_start}..{esp_start + esp_sectors - 1}, "
            f"data {data_start}..{data_end}")
        wrote = subprocess.run(["sfdisk", raw], input=script, capture_output=True, text=True)
        if wrote.returncode != 0:
            raise RuntimeError(f"sfdisk failed: {(wrote.stdout + wrote.stderr)[-400:]}")
    finally:
        if os.path.exists(raw):
            back = subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2", raw, disk],
                                  capture_output=True, text=True)
            os.unlink(raw)
            if back.returncode != 0:
                raise RuntimeError(f"qemu-img convert back failed: {back.stderr[-300:]}")

    log("the disk has a GPT, an ESP and a data partition")
    return {"esp_start": esp_start, "esp_sectors": esp_sectors,
            "data_start": data_start, "data_sectors": data_end - data_start + 1}
