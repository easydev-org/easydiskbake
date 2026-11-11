"""Clean a running macOS guest over ssh, then let APFS hand its free blocks back.

    from lib.clean_disk import clean_running
    ok = clean_running("macos", port=2222, note=log)

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

THERE IS NO TRIM COMMAND ON macOS, AND THIS FILE USED TO INVENT ONE.

It called `diskutil apfs trim /` and polled `diskutil apfs trim status`. Neither verb
exists - on a real 15.8.2 guest `diskutil apfs` lists its 21 verbs and trim is not among
them. macOS has never shipped a user-facing trim: on a real Mac the storage driver issues it
on a weekly timer, and `trimforce` only toggles third-party-SSD TRIM on and off.

Worse, the bridge has nothing to carry. Measured on that same guest with discard=unmap on
the drive:

    before                    4,802,776 blocks
    after writing 2000 MiB    8,899,136 blocks
    after rm + sync           8,899,136 blocks   <- not one cluster released

APFS does not forward a discard to virtio-blk when a file is unlinked. So the guest cannot
ask and the image cannot shrink, and the honest thing this cleaner can do is delete the bytes
it can delete and report both numbers - what the guest freed and what the image lost - so the
gap is measured instead of assumed.

WHY NOT `diskutil secureErase` OR `diskutil zeroDisk`. Those write. lib/linclean.py measured
what writing costs on a 128 GB disk - one second of `fstrim` released 116 GiB, then
forty-nine seconds of zeroing allocated 9 GB of clusters back - and lib/clean_disk.py's note
about "copy-on-write allocates new blocks before the old ones are released" is an argument
against exactly that. DISCARD writes nothing, which is the only kind of cleaning a
copy-on-write filesystem can do safely.

WHAT IT CANNOT DO. It cannot shrink the image. The deletion frees space inside the guest,
and `df` reports that; the qcow2 keeps every cluster it ever allocated. Anything that could
shrink it would have to write - `secureErase freespace` refuses on a read-only system volume
anyway, and writing is what the copy-on-write argument above rules out.
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
    ("installer leftovers", "SUDO_DO rm -rf '/macOS Install Data' 2>/dev/null; true"),
    ("software update cache", "SUDO_DO rm -rf /Library/Updates/* 2>/dev/null; true"),
    ("log archive", "SUDO_DO log erase --all 2>/dev/null; SUDO_DO rm -rf "
                    "/var/db/diagnostics/* /var/db/uuidtext/* 2>/dev/null; true"),
    ("user caches", "rm -rf ~/Library/Caches/* 2>/dev/null; true"),
    ("trash", "rm -rf ~/.Trash/* 2>/dev/null; true"),
]

# ASK, DO NOT WAIT FOREVER. The trim runs in the background and `status` reports a percentage;
# 300s is generous for a 128 GB volume (the Linux trim of the same size took one second).
#
# THERE IS NO `diskutil apfs trim`, AND THAT IS A FACT ABOUT macOS, NOT A TYPO.
# This file was written against a command that does not exist. On a real 15.8.2 guest:
#
#     $ diskutil apfs trim status
#     diskutil: did not recognize APFS verb "trim"; type "diskutil apfs" for a list
#     $ diskutil apfs            # the verbs it does have, all 21 of them
#     list listUsers listSnapshots listVolumeGroups convert create createContainer
#     deleteContainer resizeContainer addVolume deleteVolume deleteVolumeGroup
#     eraseVolume changeVolumeRole unlockVolume lockVolume changePassphrase
#     setPassphraseHint encryptVolume decryptVolume deleteSnapshot defragment
#     updatePreboot syncPatchUsers
#
# truncate IS NOT AMONG THEM, and nothing else in the list discards free extents. macOS has
# never shipped a user-facing trim verb: TRIM on a real Mac is issued by the storage driver
# on a weekly timer, and there is no `fstrim`, no `trimforce` that forces one now
# (`trimforce enable` only toggles third-party-SSD TRIM on and off).
#
# SO THE TRIM IS NOT PART OF THIS CLEANER ANY MORE, AND THE MEASUREMENT SAYS WHY IT CANNOT BE.
# Measured on that same guest with the drive carrying discard=unmap:
#
#     before                   4,802,776 blocks
#     after writing 2000 MiB   8,899,136 blocks
#     after rm + sync          8,899,136 blocks     <- not one cluster released
#
# APFS does not forward a discard to a virtio-blk device when a file is unlinked, so there is
# nothing for qemu's discard=unmap to receive. The two alternatives were tried and both are
# refused by the same guest:
#
#     diskutil secureErase freespace 0 /
#       Erasing freespace only works on mounted and writable volumes
#
# - and it is the wrong tool anyway: it WRITES, which is what lib/linclean.py's measurement
# says not to do on a copy-on-write image (one second of fstrim released 116 GiB; forty-nine
# seconds of zeroing allocated 9 GB back).
#
# WHAT THIS MEANS FOR THE IMAGE SIZE. Deleting files still frees space INSIDE the guest, and
# `df` reports it, but the qcow2 does not shrink. The report below prints both numbers so the
# difference is visible rather than assumed - "freed by deleting" and "image allocated" are
# different facts, and only the first one is something this cleaner can cause.

# SUDO HAS NO TTY OVER SSH, SO IT HAS TO BE HANDED THE PASSWORD ON STDIN.
#
# Every command above runs through session.ssh(), which is `sshpass ... ssh host command` -
# no terminal is allocated, so sudo cannot prompt, and it does not fall back to anything:
#
#     sudo: no tty present and no askpass program specified
#     sudo: a password is required
#
# That is exactly what run 42390179102 hit at the last step of an otherwise green stage:
#
#     16:23:09  sshd admitted docker on port 2222
#     16:23:09  === cleaning the guest, over ssh ===
#     16:23:20  sudo: a password is required
#     16:23:20  === diskutil apfs trim WAS REFUSED (exit 1) ===
#     16:23:20  === THE CLEAN DID NOT FINISH ===
#               nothing is published: the disk is not what this stage claims to produce
#
# The three rm/log steps before it looked like "no change" rather than failing, because each
# ends in `2>/dev/null; true` - so the surface only showed up at the trim, which reports.
#
# `sudo -S` reads the password from stdin and `-p ''` suppresses the prompt text. The password
# is the account's own, the same one sshpass just used to log in. macclean owns this
# substitution rather than session.ssh, because sudo is a macOS detail and session is shared
# with Linux and Windows.
SUDO_TEMPLATE = "printf '%s\\n' {password} | sudo -S -p '' "


def _sudo_password() -> str:
    """The account's password, quoted for the shell it is about to be echoed into."""
    import shlex
    return shlex.quote(os.environ.get("MACOS_PASSWORD", "admin"))


def _with_sudo(command: str) -> str:
    """Replace the SUDO_DO placeholder with a sudo that can actually authenticate."""
    return command.replace("SUDO_DO", SUDO_TEMPLATE.format(password=_sudo_password()))


def clean(*, port: int, note=print, ssh_seconds: int = 900,
          disk: str | None = None) -> bool:
    """Delete what the guest can delete, and report what happened.

    TRUE MEANS THE DELETIONS RAN, NOT THAT THE IMAGE SHRANK. The guest can free space and the
    qcow2 can keep every cluster it ever allocated - measured, see TRIM_IS_NOT_A_THING - so
    the return value is about whether the cleaner did its work, and the two size numbers it
    prints are the evidence for what that work was worth.

    `disk` is the qcow2 on the host, used only to report what the IMAGE lost as well as what
    the guest freed. Those are different numbers and only the second is something this
    cleaner can cause.
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
        status, out = ssh(port, _with_sudo(command), timeout=ssh_seconds)
        now = _used(port)
        if now is None:
            note(f"  {what}: the guest stopped answering")
            return False
        freed = previous - now
        note(f"  {what}: {freed:+d} MB (now {now} MB)" if freed else
             f"  {what}: no change (now {now} MB)")
        previous = now

    # NO TRIM STEP. See the note above TRIM_IS_NOT_A_THING: macOS has no verb for it, and
    # APFS does not forward a discard to virtio when a file is unlinked, so there is nothing
    # to invoke and nothing to wait for. The deletion above is the whole of what this cleaner
    # can do; the image size is reported below so the difference is a number, not a promise.

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
