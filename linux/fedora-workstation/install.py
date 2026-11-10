"""Install fedora from its Live ISO, through its own graphical installer.

    python3 linux/fedora-workstation/install.py --iso out/iso/<the pinned ISO> \
                                    --out out/fedora.qcow2

WHAT THIS FILE IS. It is a DECLARATION, not an implementation. The bake skeleton - disk,
guest, recording, the QMP driver, the walk, the post-mortem frame, the empty-disk check, the
compression - is `lib/livebake.py`, shared by every `linux/<distro>/install.py`. What is
below is the only part that is fedora's own.

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
    name="fedora-workstation",
    # THE MEDIUM BOOTS ITSELF - no -kernel, no -initrd, no -append.
    #
    # This ISO's own GRUB has `set timeout=10`, so it takes its first entry and starts without
    # a keystroke; the entry also carries the cmdline this file used to hand-write
    # (archisobasedir/archisolabel, root=live:CDLABEL, console=), which is where those values
    # came from in the first place.
    #
    # MEASURED THE A WAY IN A LOCAL POD: the live desktop arrives with no keypress and no
    # hand-written cmdline. See lib/livebake.py `_boot_args` for why the extracted-kernel path
    # still exists.
    desktop=livebake.DesktopWhen(any_of=('type to search', 'activities', 'install fedora',)),
    # ANACONDA INSTALLS NO USER, SO THE DISK IS BOOTED A SECOND TIME.
    #
    # GNOME's first-boot wizard is what creates the account, and it only runs on the installed
    # disk with no medium attached - so `first boot` is the second walk, not a step in the
    # first one. See `livebake._second_boot` for the whole reason.
    second_boot="first boot",
    mem_mb=4096,
)


def main(argv: list[str]) -> int:
    return livebake.cli(BAKE, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
