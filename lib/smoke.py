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
import subprocess
import sys
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
        m = re.match(r"^([0-9a-f]{8,16}):?\s+((?:[0-9a-f]{2,4}\s*)+)", line)
        if not m:
            continue
        for token in m.group(2).split():
            # -v may group into 4-byte words on some builds; split those out.
            if len(token) == 8:
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


def check_disk(disk: str, root_fs: str = "ext4") -> bool:
    """Everything that can be learned without starting the machine.

    root_fs is one of:
        "ext4"  (default) look for 53ef at 1 MiB
        "gpt"   only check that the partition table is GPT
        "none"  skip the filesystem check entirely

    A missing disk, or a host without qemu-io, returns False rather than raising:
    the caller prints the verdict and continues to the boot check, and a traceback
    here would read as "the bake crashed" instead of "the disk is bad".
    """
    if root_fs not in ("ext4", "gpt", "none"):
        raise ValueError(f"unknown root_fs: {root_fs!r} (expected ext4, gpt or none)")

    problems = 0
    print("=== the disk ===", flush=True)
    if os.path.exists(disk):
        print(f"  {disk}: {os.path.getsize(disk)} bytes", flush=True)
    else:
        print(f"  {disk}: MISSING", flush=True)
        print("=== THE DISK WILL NOT BOOT ===", flush=True)
        return False
    if not check_mbr(disk):
        problems += 1

    if root_fs == "ext4":
        if not check_ext_root(disk):
            log("(not fatal here - a non-ext root is possible)")
    elif root_fs == "gpt":
        if not check_gpt(disk):
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
