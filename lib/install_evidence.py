"""What a finished install looks like, so a bake cannot publish a half-written disk.

    from install_evidence import finished_cleanly, floor_for

    ok = finished_cleanly(guest_exited, seconds_used, args.timeout, note)
    ok = ok and above_floor(disk, floor_for("freebsd"), note)

WHY THIS IS A MODULE AND NOT FOUR COPIES

Every bake here has the same two holes, and they were open in all four:

    1. A TIMEOUT WAS NOT A FAILURE. The install loop was

           while time.time() < deadline:
               if not guest.alive(): break
               time.sleep(15)
           else:
               log("=== DID NOT FINISH IN {}s ===")

       and then execution carried straight on to "the disk holds a UFS filesystem, so
       compress and publish it". FreeBSD run 41638444422 did exactly that:

           13:48:42  === BSDINSTALL DID NOT FINISH IN 3600s ===
           13:48:44  [bsd   UFS magic at offset 364407: 19015419]
           13:48:48  BAKED freebsd -> out/freebsd.qcow2 (319 MiB)

       The workflow reported success. The disk was a partial install.

    2. A FILESYSTEM MAGIC WAS TAKEN AS COMPLETION. `check_disk_ufs` asks whether there is
       a UFS superblock, which is true after the first megabyte is written. It cannot tell
       1104 MiB of aborted install from 26 GiB of finished system.

So a bake is finished when BOTH of these hold, and neither is inferable from the other:

    the guest exited on its own  - the installer reached its own end and rebooted
    the disk is at least <floor> - something with a full system was written

A timeout fails on the first test. A disk that died mid-copy fails on the second.

THE FLOORS ARE MEASURED, NOT GUESSED

Each is below what that system has actually produced and above what a partial install
leaves behind:

    ubuntu    8192 MiB   measured 8293 in run 41656407481, 19584 in an earlier pass
    kubuntu  10240 MiB   measured 11960
    freebsd   2048 MiB   measured a complete install in the GB range; 1104 MiB was
                         the aborted one from 41638444422
    openbsd   1024 MiB   a set install with X11 is about this size

They are deliberately conservative: the purpose is to catch "the disk is essentially
empty", not to police the size of a legitimate install that shrank a little.
"""

from __future__ import annotations

import os

# The smallest a finished install of each kind has been measured at, rounded down with
# room to spare. See the module docstring for the measurements behind each number.
FLOOR_MIB = {
    "ubuntu": 8192,
    "kubuntu": 10240,
    "freebsd": 2048,
    "openbsd": 1024,
}


def floor_for(kind: str) -> int:
    """The MiB floor for this kind. Unknown kinds get the lowest floor rather than 0."""
    return FLOOR_MIB.get(kind, 1024)


def disk_mib(disk: str) -> int:
    """Allocated size of a qcow2, in MiB.

    st_blocks, not st_size: a qcow2 is sparse, so its apparent size is the virtual disk
    size from creation and never changes. The allocated blocks are what grow as the guest
    writes.
    """
    try:
        return os.stat(disk).st_blocks * 512 >> 20
    except OSError:
        return 0


def finished_cleanly(guest_exited: bool, seconds_used: float, timeout: int,
                     note) -> bool:
    """Did the install reach its own end, rather than being cut off by the clock?

    `guest_exited` is the loop's own result - True when qemu went away by itself, which is
    what an installer does after its last step on every system here. A False means the
    loop ran out of time, and that is a failed bake whatever the disk looks like.
    """
    if guest_exited:
        note(f"the installer finished on its own after {seconds_used:.0f}s")
        return True
    note(f"=== THE INSTALLER WAS STILL RUNNING AFTER {timeout}s ===")
    note("  nothing is published from an install that never reached its end, whatever")
    note("  the disk already contains - a partition table and a superblock are not a")
    note("  system, and shipping one produces a disk that boots to nothing")
    return False


def above_floor(disk: str, floor_mib: int, note, kind: str = "") -> bool:
    """Is the disk big enough to be a whole system?

    THIS IS THE SECOND HALF, AND IT IS NOT REDUNDANT. A guest can exit early - a kernel
    panic, a killed installer, an unmounted filesystem - and still leave a UFS or ext4
    superblock behind. The first half would pass that; this one catches it.
    """
    size = disk_mib(disk)
    if size >= floor_mib:
        note(f"the disk holds {size} MiB, above the {floor_mib} MiB floor"
             + (f" for {kind}" if kind else ""))
        return True
    note(f"=== THE DISK HOLDS ONLY {size} MiB ===")
    note(f"  a finished {kind or 'system'} occupies at least {floor_mib} MiB here, so")
    note("  this is an install that stopped early rather than one that completed")
    return False
