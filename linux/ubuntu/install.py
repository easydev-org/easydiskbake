"""Install ubuntu from its Live ISO, through its own graphical installer.

    python3 linux/ubuntu/install.py --iso out/iso/<the pinned ISO> \
                                    --out out/ubuntu.qcow2

WHAT THIS FILE IS. It is a DECLARATION, not an implementation. The bake skeleton - disk,
guest, recording, the QMP driver, the walk, the post-mortem frame, the empty-disk check, the
compression - is `lib/livebake.py`, shared by every `linux/<distro>/install.py`. What is
below is the only part that is ubuntu's own.

THE WALK IS `steps/install.yaml`. Nothing here drives a screen: `lib/steps.py` reads that
file and does it, with each coordinate carrying the frame it was measured from.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import livebake     # noqa: E402

BAKE = livebake.Bakery(
    name="ubuntu",
    # THE MEDIUM BOOTS ITSELF - no -kernel, no -initrd, no -append.
    #
    # Read out of the ISO: `boot/grub/grub.cfg` has `set timeout=30` and its entry is
    # `linux /casper/vmlinuz --- quiet splash`, so it takes the first entry by itself and
    # loads the same casper kernel+initrd this file used to extract.
    #
    # THE `console=ttyS0` THIS FILE CARRIED IS GONE, AND IT WAS NEVER LOAD-BEARING.
    # ubuntu/steps/ssh.yaml has always said so in its own words: "These disks have no getty on
    # ttyS0 - measured on run 42551299241 - so `run:` is the only way in." A console line that
    # no step ever read was diagnosis-only, and it is the ISO's own menu that decides now.
    # AND - ubuntu's driver waits for `welcome` AND `ubuntu`, the installer's first pane.
    desktop=livebake.DesktopWhen(all_of=('welcome', 'ubuntu')),
    mem_mb=8192,
)


def main(argv: list[str]) -> int:
    return livebake.cli(BAKE, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
