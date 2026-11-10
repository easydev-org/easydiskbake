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
    # THE MEDIUM BOOTS ITSELF - no -kernel, no -initrd, no -append.
    #
    # Read out of the ISO: `boot/grub/grub.cfg` has `set timeout=60`, so it takes its first
    # entry with no keystroke. The entry loads `/antiX/vmlinuz` and `/antiX/initrd.gz` - the
    # same two members this file used to extract - and passes its own `lang=en_US tz=UTC`.
    #
    # 60 s IS THE LONGEST MENU HERE, and it is inside the 210-second desktop budget, so the
    # wait covers it. If this image ever stops auto-booting the fix is `grub_key='ret'` with a
    # `grub_delay` past 60, the way mint's is set.
    # AND, NOT OR. MX's boot prints `MX` on the console long before `MX Welcome` is drawn,
    # so any_of would have matched a text console - and the walk's first step clicks the
    # welcome window. Its own driver always said `"mx" in text and "install" in text`.
    desktop=livebake.DesktopWhen(all_of=('mx', 'install')),
    mem_mb=4096,
    timeout=210,
)


def main(argv: list[str]) -> int:
    return livebake.cli(BAKE, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
