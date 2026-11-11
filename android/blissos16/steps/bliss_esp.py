"""The two BlissOS steps that are not actions on a screen: the bootloader and the verdict.

    python: bliss_esp:fallback
    python: bliss_esp:verify

WHY THESE ARE EXTENSIONS AND NOT A STAGE

`python:` is the escape hatch lib/steps.py documents for "a measured exception" - something that
is genuinely neither a `key:`, an `ssh:` nor one of the machine steps. Both of these qualify:

    fallback   edits a FAT filesystem inside a partition at a byte offset in a qcow2. There is
               no vocabulary for that and there should not be one.
    verify     reads the same partition back and asserts a directory entry is in it.

What they are NOT is a decision about the run or a program with its own argument list. The
verdict is "the step failed", which the engine already knows how to report with the screen beside
it - see the `except StepError` in lib/steps.py's `walk`.

WHY THE WORK COMES FROM lib/esp.py

Because there is exactly one implementation of "read the ESP out of a qcow2, edit it, put it back
only where it changed", and `windows/11/fixedboot.py` wrote it first. A second copy here would be
a second set of measurements of a FAT filesystem's first bytes, which is the kind of duplication
this repository has paid for more than once.

WHY THE RETURN VALUE IS THE VERDICT

`python:` steps are `bool`-valued: True passes, False fails the run (unless `optional`). So a
missing loader is a failed step with a message, not a traceback - which is what a returned False
gets, see `_run_python`.

WHAT WAS MEASURED, AND IS WHY `search.fs_uuid` IS REWRITTEN

The installer writes /EFI/BLISSOS/GRUB.CFG containing

    search.fs_uuid 37cd4a0d-2c84-4e46-b914-79a9b00d27b5 root

and that UUID is the one it recorded BEFORE it formatted vda2. Formatting makes a new filesystem
with a new UUID, so the value names a filesystem that does not exist, and the first boot of the
finished disk stops at

    error: no such device: 37cd4a0d-2c84-4e46-b914-79a9b00d27b5.
    GNU GRUB  version 2.12-1

`search --file` asks the question the missing device stood in for ("find the partition holding
the system") and answers it from the disk as it is now.
"""

from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for path in (REPO, os.path.join(REPO, "lib")):
    if path not in sys.path:
        sys.path.insert(0, path)

import esp  # noqa: E402

# WHERE THE INSTALLER PUTS THE LOADER, AND THE NAMES IT WRITES. Measured on the ESP of a run that
# worked: BOOTX64, BOOTIA32, GRUBX64, MMX64 and MMIA32, plus GRUB.CFG. The copy is the same files
# under the one path a firmware falls back to, so a disk does not depend on an NVRAM entry.
LOADER_DIR = "/EFI/BLISSOS"
LOADER_NAMES = ("BOOTX64.EFI", "BOOTIA32.EFI", "GRUBX64.EFI", "MMX64.EFI", "MMIA32.EFI")
LOADER_CONFIG = "GRUB.CFG"

# THE LINE THE INSTALLER WROTE, AND WHAT REPLACES IT. Both halves are here rather than in the
# YAML because a `python:` step's arguments are not a vocabulary - see lib/steps.py's note on
# ACTION_ONLY_KEYS. What the file controls is WHETHER the step runs.
STALE_SEARCH = "search.fs_uuid"
GOOD_SEARCH = "search --no-floppy --set=root --file /android-2024-10-11/kernel"


def _disk(driver, step) -> str:
    """The qcow2 this run owns, as the session knows it."""
    session = getattr(driver, "session", None)
    for candidate in (getattr(session, "disk", "") if session else "",
                      getattr(driver, "disk_path", "") or ""):
        if candidate and os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError(
        "no disk to read: this step needs the `disk:` and `boot:` steps to have run first")


def fallback(driver, step) -> bool:
    """Copy the ESP's Grub2 to /EFI/BOOT and fix the config's stale UUID search."""
    try:
        disk = _disk(driver, step)
        copied = esp.fallback_tree(
            disk,
            source=LOADER_DIR,
            names=LOADER_NAMES,
            config=LOADER_CONFIG,
            rewrite=(STALE_SEARCH, GOOD_SEARCH))
    except Exception as exc:                            # noqa: BLE001 - reported as a verdict
        print(f"  the bootloader could not be placed: {exc}", flush=True)
        return False
    if copied == 0:
        print(f"  {LOADER_DIR} held none of {', '.join(LOADER_NAMES)}", flush=True)
        return False
    print(f"  {copied} loader file(s) copied to the fallback path", flush=True)
    return True


def verify(driver, step) -> bool:
    """The bootloader is on the ESP, read back from the disk as bytes."""
    try:
        disk = _disk(driver, step)
        found, filesystem = esp.holds_a_loader(disk)
    except Exception as exc:                            # noqa: BLE001 - reported as a verdict
        print(f"  the ESP could not be read: {exc}", flush=True)
        return False
    print(f"  ESP filesystem: {filesystem}", flush=True)
    if not found:
        print("  the ESP holds no BOOTX64.EFI or grubx64.EFI: this disk will not start", flush=True)
    return found
