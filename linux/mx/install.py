"""Install mx from its Live ISO, through its own graphical installer.

    python3 linux/mx/install.py --iso out/iso/<the pinned ISO> \
                                    --out out/mx.qcow2

WHAT THIS FILE IS. It is a DECLARATION, not an implementation. The bake skeleton - disk,
guest, recording, the QMP driver, the walk, the post-mortem frame, the empty-disk check, the
compression - is `lib/livebake.py`, shared by every `linux/<distro>/install.py`. What is
below is the only part that is mx's own.

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
    name="mx",
    kernel=('antiX/vmlinuz','antiX/initrd.gz'),
    cmdline='quiet splash lang=en_US tz=UTC',
    # AND, NOT OR. MX's boot prints `MX` on the console long before `MX Welcome` is drawn,
    # so any_of would have matched a text console - and the walk's first step clicks the
    # welcome window. Its own driver always said `"mx" in text and "install" in text`.
    desktop=livebake.DesktopWhen(all_of=('mx', 'install')),
    mem_mb=4096,
)


def main(argv: list[str]) -> int:
    return livebake.cli(BAKE, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
