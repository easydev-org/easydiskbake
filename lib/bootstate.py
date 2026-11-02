"""The firmware and TPM state a baked Windows disk needs in order to boot.

    from lib import bootstate
    bootstate.save(work, disk_path)      # after a successful bake
    bootstate.restore(work, disk_path)   # before a stage that starts from it

WHY A DISK ALONE IS NOT ENOUGH, MEASURED THE HARD WAY

A qcow2 written by this bake contains a complete Windows 11. Started with a fresh
OVMF_VARS and a fresh swtpm, it does not boot:

    BdsDxe: failed to load Boot0001 "UEFI Misc Device"  ... : Not Found
    BdsDxe: failed to load Boot0002 "UEFI Misc Device 2" ... : Not Found
    BdsDxe: failed to load Boot0003 "UEFI HTTPv4 (MAC:...)" : Not Found
    BdsDxe: failed to load Boot0004 "UEFI HTTPv6 (MAC:...)" : Not Found
    BdsDxe: loading Boot0005 "EFI Internal Shell"
    Shell>

The firmware tried every entry it had and fell through to its own shell. The entry that
matters - "Windows Boot Manager" - was written into the OVMF variables during setup and
does not exist in the template. The same is true of the TPM: Windows 11 seals state
against it, and a different one is a different machine.

www.easysandbox-windows reaches the same conclusion, in the comment above its own check:

    windows.vars carries the UEFI boot entry (Boot0005 "Windows Boot Manager"), and
    without it the firmware falls back to the generic hard-disk entry Boot0002, fails to
    boot, and dockur gives up with
        ERROR: Timeout while waiting for QEMU to boot the machine

WHAT IS IN THE ARCHIVE

    windows.vars   OVMF's variable store: the boot order and the boot entries in it
    tpm/           the swtpm state directory: the PCRs and the sealed secrets
    identity.json  the serial and MAC the machine was installed with, for the record

Three files, a few hundred KiB. The disk is the gigabyte; this is the part that makes the
disk start, and publishing one without the other produces an artifact nobody can use.
"""

from __future__ import annotations

import glob
import os
import shutil
import tarfile

# The archive is named after the disk so two bakes in one work directory cannot collide.
SUFFIX = ".bootstate.tar.gz"


def archive_path(disk: str) -> str:
    """Where the bootstate for `disk` lives: beside it, named after it."""
    return os.path.join(os.path.dirname(os.path.abspath(disk)),
                        os.path.basename(disk) + SUFFIX)


def save(work: str, disk: str) -> str | None:
    """Collect the firmware and TPM state next to `disk`. Returns the archive, or None.

    NOT FATAL WHEN THERE IS NOTHING TO SAVE. A bake that failed before it ever started a
    VM has no variables to keep, and turning that into a second failure would hide the
    first. The caller publishes the archive when it exists and says so when it does not.
    """
    members = []

    # OVMF's variable store. bake.py names it OVMF_VARS.fd; a stage may name it
    # macos.vars or windows.vars. All are looked for rather than assumed, because the
    # cost of missing it is an artifact that will not boot and says so only at the
    # firmware.
    for pattern in ("OVMF_VARS.fd", "windows.vars", "macos.vars", "*.vars"):
        for found in glob.glob(os.path.join(work, pattern)):
            if os.path.isfile(found) and os.path.getsize(found) > 0:
                members.append(os.path.basename(found))
                break

    # THE LOCK IS NOT STATE. swtpm writes tpm/.lock to claim the directory for the
    # running process, and packing it means the restored copy claims to be in use by a
    # process that no longer exists - which swtpm answers with
    #
    #     TPM result for CMD_INIT: 0x9 operation failed
    #
    # and qemu exits. The file is excluded here and deleted again in restore(), because
    # archives published before this change still carry one.
    tpm = os.path.join(work, "tpm")
    if os.path.isdir(tpm) and os.listdir(tpm):
        members.append("tpm")

    identity = os.path.join(work, "identity.json")
    if os.path.isfile(identity):
        members.append("identity.json")

    if not members:
        return None

    dest = archive_path(disk)
    with tarfile.open(dest, "w:gz") as tar:
        for name in members:
            path = os.path.join(work, name)

            def skip(info):
                # Exclude the swtpm lock; see the comment above where members is built.
                parts = info.name.split(os.sep)
                return None if parts[-2:] == ["tpm", ".lock"] else info

            tar.add(path, arcname=name, filter=skip)
    return dest


def restore(work: str, disk: str) -> bool:
    """Put the firmware and TPM state back. Returns False when there is no archive.

    EXTRACTED INTO `work`, NOT BESIDE THE DISK, because that is where the caller's
    vm.Shape points: the vars file and the tpm directory are read from the work
    directory by path, and an archive that unpacked itself elsewhere would be silently
    ignored.

    ONE NAME, NO SEARCHING. The archive belongs beside the disk it describes and is
    named `<disk>.bootstate.tar.gz`; `save()` writes exactly that path and this reads
    exactly that path. A stage that renames its working copy of the disk renames the
    archive with it - see adopt() - rather than leaving this function to guess which of
    several archives in a directory was meant.
    """
    src = archive_path(disk)
    if not os.path.isfile(src):
        return False

    os.makedirs(work, exist_ok=True)
    with tarfile.open(src, "r:gz") as tar:
        # THE ARCHIVE IS LOCAL AND WRITTEN BY THIS REPOSITORY, but tarfile still refuses
        # absolute paths and ".." by default, and that default is worth keeping.
        tar.extractall(work)

    # THE LOCK FILE MUST NOT COME BACK WITH THE STATE.
    #
    # swtpm creates tpm/.lock to mark the directory as in use, and save() packs the whole
    # tpm/ directory - including that file. Restoring it tells the next swtpm that another
    # process owns the state, and it refuses to initialise:
    #
    #     qemu-system-x86_64: tpm-emulator: TPM result for CMD_INIT: 0x9 operation failed
    #
    # and qemu exits twelve seconds after it starts. Measured on launch-windows run
    # 41501928264, which reported "qemu exited during the boot" with no other detail.
    #
    # A lock is about the process that held it, not about the state; it has no meaning
    # once that process is gone.
    lock = os.path.join(work, "tpm", ".lock")
    if os.path.exists(lock):
        os.unlink(lock)

    # swtpm wants a directory it created; make sure the state is where it will look.
    return True


def adopt(disk: str, archive: str) -> str:
    """Move `archive` to where `disk` expects it. Returns the destination path.

    WHY THIS IS A MOVE AND NOT A LOOKUP. A stage copies the published disk to a working
    name before starting it:

        out/windows11-gold.qcow2   ->   ssh-work/disk.qcow2

    and the bootstate has to follow, because restore() looks for
    `ssh-work/disk.qcow2.bootstate.tar.gz` and nothing else. Run 41477859630 failed with
    "no bootstate archive was found" while the archive was sitting in `out/` under the
    published disk's name - one rename away from being found, and not found.

    Falling back to "any *.bootstate.tar.gz in the directory" would fix that run and
    quietly break the one where two disks are baked side by side, so the rename is
    explicit: the caller says which archive belongs to which disk.
    """
    dest = archive_path(disk)
    if os.path.abspath(archive) == os.path.abspath(dest):
        return dest
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    shutil.move(archive, dest)
    return dest


def describe(disk: str) -> str:
    """One line for a log: whether the bootstate exists and what is in it."""
    src = archive_path(disk)
    if not os.path.isfile(src):
        return f"{os.path.basename(disk)}: NO BOOTSTATE - the published disk will not boot"
    try:
        with tarfile.open(src, "r:gz") as tar:
            names = tar.getnames()
    except (OSError, tarfile.TarError) as exc:
        return f"{os.path.basename(src)}: unreadable ({exc})"
    return (f"{os.path.basename(src)}: {os.path.getsize(src) >> 10} KiB, "
            f"{', '.join(sorted(set(n.split('/')[0] for n in names)))}")
