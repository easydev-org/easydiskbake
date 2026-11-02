"""Re-check a baked disk without the ISO attached and without the installer's memory.

    python3 windows/11/verify.py out/windows11.qcow2

The bake already checks the disk at the end of its own run, but it does so with the
install ISO still attached to a VM that just installed to it. This is a separate
process reading only the file: if the disk is only valid because of state the
installer left in the VM, this is what finds out.

WHAT IT CHECKS

    the qcow2 opens            qemu-img info
    the MBR has boot code      not just the 0x55AA signature
    the table is GPT           the protective MBR entry, because Windows 11 needs UEFI

It does not boot the disk. Booting it needs a TPM that matches the one it was
installed against, and an OVMF vars file with the boot entry in it - neither of which
travels with the qcow2. That is a property of the artifact, not a gap in this check:
a published Windows disk is not self-contained, and the runtime that uses it has to
supply the same firmware state.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib import smoke  # noqa: E402


def main(argv: list[str]) -> int:
    disk = argv[0] if argv else "out/windows11.qcow2"
    if not os.path.isfile(disk):
        print(f"no disk at {disk}", flush=True)
        return 1

    ok = smoke.check_disk(disk, root_fs="gpt")
    print(f"disk checks: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
