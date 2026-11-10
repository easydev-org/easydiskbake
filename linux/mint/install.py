"""Install mint from its Live ISO, through its own graphical installer.

    python3 linux/mint/install.py --iso out/iso/<the pinned ISO> \
                                    --out out/mint.qcow2

WHAT THIS FILE IS. It is a DECLARATION, not an implementation. The bake skeleton - disk,
guest, recording, the QMP driver, the walk, the post-mortem frame, the empty-disk check, the
compression - is `lib/livebake.py`, shared by every `linux/<distro>/install.py`. What is
below is the only part that is mint's own.

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
    name="mint",
    # THE MEDIUM BOOTS ITSELF. Measured on this ISO in a local pod: its GRUB has NO TIMEOUT
    # and was still on screen at t=390s, so one Enter takes the highlighted entry and the live
    # desktop follows 30 seconds later - and the real walk then ran all 19 steps from it.
    grub_key='ret',
    grub_delay=25.0,
    desktop=livebake.DesktopWhen(any_of=('install linux mint', 'menu mint',)),
    mem_mb=4096,
)


def main(argv: list[str]) -> int:
    return livebake.cli(BAKE, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
