"""Install kubuntu from its Live ISO, through its own graphical installer.

    python3 linux/kubuntu/install.py --iso out/iso/<the pinned ISO> \
                                    --out out/kubuntu.qcow2

WHAT THIS FILE IS. It is a DECLARATION, not an implementation. The bake skeleton - disk,
guest, recording, the QMP driver, the walk, the post-mortem frame, the empty-disk check, the
compression - is `lib/livebake.py`, shared by every `linux/<distro>/install.py`. What is
below is the only part that is kubuntu's own.

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
    name="kubuntu",
    # `kubuntu` ALONE IS NOT ENOUGH ANY MORE, AND RUN 6eeff43 IS WHY.
    #
    # That was OR-by-nature because the live desktop draws `kubuntu` across the middle of the
    # screen - but the ISO's own GRUB menu says `Install Kubuntu` too, and matching it declared
    # the session up while the machine was still ON THE BOOT MENU:
    #
    #     look boot: 58 words :: GNU | GRUB | version | 2.14 | or | Install | Kubuntu | ...
    #     === the walk: install.yaml ===
    #     click (1326,841) - install kubuntu on the live desktop   <- on a GRUB MENU
    #
    # THE TWO WORDS THAT ARE ONLY ON THE SESSION are its greeter's: `Try Kubuntu` and
    # `Install Kubuntu` are both cards there, and the boot menu has no `Try`.
    #
    # (The A boot is what exposed this. The B boot skipped GRUB by extracting the kernel, so a
    # marker that matched a menu entry never had a menu to match.)
    # THE MEDIUM BOOTS ITSELF - no -kernel, no -initrd, no -append.
    #
    # This ISO's own GRUB has `set timeout=30`, so it takes its first entry and starts without
    # a keystroke; the entry also carries the cmdline this file used to hand-write
    # (archisobasedir/archisolabel, root=live:CDLABEL, console=), which is where those values
    # came from in the first place.
    #
    # MEASURED THE A WAY IN A LOCAL POD: the live desktop arrives with no keypress and no
    # hand-written cmdline. See lib/livebake.py `_boot_args` for why the extracted-kernel path
    # still exists.
    desktop=livebake.DesktopWhen(any_of=('try kubuntu',)),
    mem_mb=4096,
)


def main(argv: list[str]) -> int:
    return livebake.cli(BAKE, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
