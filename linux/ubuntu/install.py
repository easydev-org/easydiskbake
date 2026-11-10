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
    kernel=('casper/vmlinuz','casper/initrd'),
    cmdline='console=ttyS0,115200n8 --- console=ttyS0,115200n8',
    desktop=livebake.DesktopWhen(any_of=('ubuntu', 'install',)),
    mem_mb=8192,
)


def main(argv: list[str]) -> int:
    return livebake.cli(BAKE, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
