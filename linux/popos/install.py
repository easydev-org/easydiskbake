"""Install popos from its Live ISO, through its own graphical installer.

    python3 linux/popos/install.py --iso out/iso/<the pinned ISO> \
                                    --out out/popos.qcow2

WHAT THIS FILE IS. It is a DECLARATION, not an implementation. The bake skeleton - disk,
guest, recording, the QMP driver, the walk, the post-mortem frame, the empty-disk check, the
compression - is `lib/livebake.py`, shared by every `linux/<distro>/install.py`. What is
below is the only part that is popos's own.

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
    name="popos",
    kernel=(),
    cmdline='',
    # THE INSTALLER'S OWN TITLE, OR ITS FIRST PANE'S HEADING - NOT THE WORD "pop" ANYWHERE.
    #
    # This was simplified to ('install', 'pop') when the twelve drivers were folded into
    # this file, and that is too loose: casper prints `Begin: Running /scripts/casper-bottom`
    # and `Moving mount points...` while the desktop is still coming up, so the wait returned
    # True against a TEXT CONSOLE and the walk's first step found no footer button (run
    # 38bb031's popos-install: `=== NO FOOTER BUTTON ON THIS PANE ===` one second in).
    #
    # The three phrases below are the original's, measured on this image: the window title
    # `Install Pop!_OS 24.04 LTS`, the first pane `Select a Language`, and the product name.
    desktop=livebake.DesktopWhen(
        any_of=('select a language', 'install pop', 'pop!_os')),
    mem_mb=4096,
    timeout=300,
)


def main(argv: list[str]) -> int:
    return livebake.cli(BAKE, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
