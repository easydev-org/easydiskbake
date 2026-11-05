"""Clean a running macOS guest over ssh, then let APFS hand its free blocks back.

    from lib.clean_disk import clean_running
    ok = clean_running("macos", port=2223, note=log)

WHAT THIS IS FOR, AND WHY IT IS THE LAST SYSTEM TO GET ONE

Linux has lib/linclean.py (`fstrim`), FreeBSD lib/bsdclean.py (`zpool trim -w`), Windows
lib/winclean.py (`Optimize-Volume -ReTrim`). macOS had nothing, so its disk grew across four
stages - setup, desktop, ssh, dev - and was published at whatever size it reached.

THE MECHANISM HAS TO BE UNDERSTOOD BEFORE ANY OF THESE COMMANDS MEAN ANYTHING.

APFS is copy-on-write with reference counting: when a file is deleted its extents become
free inside the container, and there is no bitmap to update as there is on ext4. That makes
APFS GOOD at freeing space internally and does NOTHING about the qcow2 underneath, because
the free extents are a fact APFS knows and the virtio device was never told.

The bridge is discard. With `discard=unmap` on the target drive (macos/15/machine.TARGET_DRIVE)
every trim the guest issues reaches qemu, and qemu drops the cluster from the qcow2. Without
it, `diskutil` can trim all day and the image will not lose a byte.

THE COMMAND IS `diskutil apfs trim`, AND IT TAKES A MOMENT TO EXPLAIN.

    diskutil apfs trim /            tell APFS the free extents on / may be discarded
    diskutil apfs trim status       report how far the trim has got; it runs in the background

macOS issues trims on a weekly timer by default, so a bake that ran for an hour has never
run one. `diskutil apfs trim` forces it now. `status` is polled because the work is
asynchronous and the first version of this file would have reported success on the strength
of the request rather than the result.

WHY NOT `diskutil secureErase` OR `diskutil zeroDisk`. Those write. lib/linclean.py measured
what writing costs on a 128 GB disk - one second of `fstrim` released 116 GiB, then
forty-nine seconds of zeroing allocated 9 GB of clusters back - and lib/clean_disk.py's note
about "copy-on-write allocates new blocks before the old ones are released" is an argument
against exactly that. DISCARD writes nothing, which is the only kind of cleaning a
copy-on-write filesystem can do safely.

WHAT IT CANNOT DO. The recovery volume and the Preboot/VM volumes are APFS volumes in the
same container and `diskutil apfs trim /` targets one mount point. Trimming the container
would need `diskutil apfs trim <device>`, which this does not do yet - see the note in
clean().
"""

from __future__ import annotations

import os
import time


def log(message: str) -> None:
    print(f"[macclean {time.strftime('%H:%M:%S')}] {message}", flush=True)


# USED MiB ON /, ONE NUMBER, the same quantity lib/linclean.py measures - so the before/after
# pair across systems means the same thing.
#
# `df -m` prints 1M blocks. macOS's df is BSD df and accepts -m, which is why this is one
# command and not a pipeline of stat/awk.
USED = "df -m / | awk 'NR==2 {print $3}'"


def _used(port: int) -> int | None:
    from session import ssh

    status, out = ssh(port, USED, timeout=120)
    if status != 0:
        # THE OUTPUT IS PRINTED BECAUSE THIS LINE HAS FAILED WITHOUT ONE BEFORE. Run
        # 42074798774 reached this with the guest up and sshd answering - the stage's own
        # wait_for_ssh had just passed - and the whole clean ended with
        #
        #     === THE GUEST DID NOT ANSWER `df` ===
        #     nothing was measured, so nothing will be reported
        #
        # which is true and says nothing about why. `wait_for_ssh`'s probe is `true`, a shell
        # BUILTIN that returns without forking; this is a PIPELINE, and a login shell that
        # cannot run one fails here and not there. The status and the text distinguish "df is
        # not the BSD df", "awk is not on PATH", and "the session died" - three different
        # repairs - and only the print can tell them apart.
        log(f"  `{USED}` returned {status}: {(out or '(no output)')[:300]}")
        return None
    lines = [x.strip() for x in (out or "").splitlines() if x.strip().isdigit()]
    return int(lines[-1]) if lines else None


def _allocated_mib(disk: str | None) -> int:
    """MiB the qcow2 actually occupies, or -1. lib/clean_disk.py already has this."""
    if not disk:
        return -1
    import clean_disk

    try:
        return clean_disk.allocated_mib(disk)
    except Exception:                                 # noqa: BLE001 - reporting only
        return -1


# THE STEPS BEFORE THE TRIM. Each is one command, and each is deleted-for and not measured,
# because on macOS the things that hold space in a fresh install are few and large:
#
#   the installer's own leftovers   /macOS Install Data holds the staged installer - several
#                                   GB that exist only to be run again
#   the software update cache       downloaded update payloads, never used by a bake
#   the log archive                 unified logging keeps its own store, and a VM that has
#                                   booted four times has one
STEPS = [
    ("installer leftovers", "sudo rm -rf '/macOS Install Data' 2>/dev/null; true"),
    ("software update cache", "sudo rm -rf /Library/Updates/* 2>/dev/null; true"),
    ("log archive", "sudo log erase --all 2>/dev/null; sudo rm -rf "
                    "/var/db/diagnostics/* /var/db/uuidtext/* 2>/dev/null; true"),
    ("user caches", "rm -rf ~/Library/Caches/* 2>/dev/null; true"),
    ("trash", "rm -rf ~/.Trash/* 2>/dev/null; true"),
]

# ASK, DO NOT WAIT FOREVER. The trim runs in the background and `status` reports a percentage;
# 300s is generous for a 128 GB volume (the Linux trim of the same size took one second).
TRIM_REQUEST = "sudo diskutil apfs trim / 2>&1"
TRIM_STATUS = "sudo diskutil apfs trim status 2>&1"


def clean(*, port: int, note=print, ssh_seconds: int = 900,
          disk: str | None = None) -> bool:
    """Release what the guest can release. True when the trim reported itself complete.

    `disk` is the qcow2 on the host. It is used for one thing: reporting how much the IMAGE
    shrank, which is a different number from what the guest freed. A trim can report 100 GiB
    released and leave a qcow2 that holds every cluster it ever allocated, if `discard=unmap`
    is missing from the drive - and that is the failure this whole file would otherwise hide.
    """
    from session import ssh

    note("=== cleaning the guest, over ssh ===")

    # THE IMAGE SIZE BEFORE ANY OF THIS, so the report can say what the DISK lost as well as
    # what the guest freed. Those are different numbers and only the first is on the wire -
    # see the note at the end of this function.
    image_before = _allocated_mib(disk) if disk else None

    start = _used(port)
    if start is None:
        note("=== THE GUEST DID NOT ANSWER `df` ===")
        note("  nothing was measured, so nothing will be reported")
        return False
    note(f"  used before: {start} MB")

    previous = start
    for what, command in STEPS:
        status, out = ssh(port, command, timeout=ssh_seconds)
        now = _used(port)
        if now is None:
            note(f"  {what}: the guest stopped answering")
            return False
        freed = previous - now
        note(f"  {what}: {freed:+d} MB (now {now} MB)" if freed else
             f"  {what}: no change (now {now} MB)")
        previous = now

    # THE TRIM ITSELF, AND THE POLLING THAT MAKES IT EVIDENCE.
    #
    # `diskutil apfs trim` RETURNS WHEN THE REQUEST IS ACCEPTED, NOT WHEN THE WORK IS DONE.
    # Treating exit 0 as success would report a trim that had not started, which is the same
    # shape of mistake as the FreeBSD cleaner making a ZFS trim look like a failed fsck.
    note("  discarding the free blocks (diskutil apfs trim)")
    status, out = ssh(port, TRIM_REQUEST, timeout=600)
    for line in (out or "").strip().splitlines():
        if line.strip():
            note(f"    {line.strip()[:170]}")
    if status != 0:
        note(f"=== diskutil apfs trim WAS REFUSED (exit {status}) ===")
        note("  the lines above say why; a volume that cannot trim (an HFS+ root, or a")
        note("  guest that is not the installed system) reports it here")
        if disk:
            note(f"  the image was NOT shrunk: {disk}")
        return False

    # POLL `status` UNTIL IT STOPS REPORTING PROGRESS.
    #
    # The text varies by release - "Percent complete: 42%", "Trim is complete", "There is no
    # trim in progress" - so the test is not a phrase match. A trim is finished when two
    # consecutive samples, five seconds apart, say the same thing.
    deadline = time.time() + 300
    last = None
    stable = 0
    while time.time() < deadline:
        status, out = ssh(port, TRIM_STATUS, timeout=120)
        text = " ".join((out or "").split())[:170]
        if status != 0:
            note(f"  trim status failed (exit {status}): {text}")
            break
        if text == last:
            stable += 1
            if stable >= 2:
                note(f"    {text}")
                break
        else:
            stable = 0
            note(f"    {text}")
        last = text
        time.sleep(5)
    else:
        note("  the trim was still reporting progress after 300s; continuing")

    # AND THE NUMBER THAT PROVES IT REACHED THE QCOW2.
    #
    # What the guest released is `df`'s business. What the IMAGE lost is `qemu-img`'s, and
    # only the second one is on the wire. When discard=unmap is missing the two disagree
    # completely, and printing both is what makes that visible instead of theoretical.
    end = _used(port)
    note(f"  freed by deleting: {start - previous} MB")
    if end is not None:
        note(f"  used after:        {end} MB")

    if disk and os.path.isfile(disk):
        try:
            after = _allocated_mib(disk)
        except Exception as exc:                      # noqa: BLE001 - reporting only
            note(f"  could not measure the image: {exc}")
            after = -1
        if after >= 0:
            delta = "" if image_before is None or image_before < 0 else \
                f" ({after - image_before:+d} MiB)"
            note(f"  image allocated:   {after} MiB{delta}")

    return True
