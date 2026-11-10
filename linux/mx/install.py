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
    # AND A SECOND MENU AFTER IT, WHICH IS WHY THIS ONE NEEDS A KEYPRESS.
    #
    # Run 12e6add's last frame is not GRUB - it is MX's OWN boot menu, drawn by its sysvinit
    # boot code at 1024x768:
    #
    #     Welcome to MX-25.3_Xfce (Infinity)!
    #       ===> Language - Keyboard - Timezone
    #           Language (lang=en_US)          <- highlighted
    #           Keyboard (kbd=us)
    #           Timezone (tz=America/New_York)
    #
    # and it waits there. So the ISO has TWO menus and only the first has a timeout: GRUB
    # (`set timeout=60`) takes its entry, then this one stops. One Return takes `Language`.
    #
    # THE `lang=en_US` ON IT IS THE SAME VALUE the extracted-kernel cmdline used to pass, so
    # the B path never saw this screen at all - it had already been answered by the cmdline.
    #
    # AND, NOT OR, BUT AGAINST THE WORDS THE A BOOT ACTUALLY SHOWS.
    #
    # MX's own driver used `"mx" in text and "install" in text`, and that was right for the B
    # boot: its transcript was `Installer | Connection | Established`, where `Installer` is the
    # live session's own banner on a text console and `mx` never appears on that screen.
    #
    # THE A BOOT SKIPS THAT SCREEN. Measured on run a0c5896:
    #
    #     look boot: Welcome | to | MX-25.3_ | Xfce | (Infinity)! | Language | - | Keyboard
    #
    # - MX Welcome, the desktop window the walk's first step clicks, with `MX-25.3` in it and
    # no `install` anywhere, so the old AND matched nothing and the bake waited out its budget
    # on a desktop that was already up. `welcome` and `mx` are on that frame together and on no
    # earlier one.
    # THREE STEPS, MEASURED ON THIS MENU, AND EACH ONE WAS WRONG ON ITS OWN.
    #
    # MX stops on its OWN boot menu (not GRUB - GRUB's `set timeout=60` does take its entry).
    # The menu opens on a LANGUAGE SUBMENU with `Language` highlighted:
    #
    #     ===> Language - Keyboard - Timezone
    #          Language (lang=en_US)      <- highlighted
    #          Keyboard (kbd=us)
    #          Timezone (tz=America/New_York)
    #     <=== Back to main menu
    #
    # and its footer says `Use | and |, Hit »ENTER« to select/deselect. Press »e« to edit,
    # »ESC« to go back.` So:
    #
    #     ESC          back to the MAIN menu, which is what the footer promises
    #     Up x4        the main menu opens with `Language - Keyboard - Timezone` STILL selected
    #                  - four rows below the entry that boots. Measured by reading the
    #                  highlight bar's pixels: it moved from y~430 to y 154..185, which is
    #                  `MX 25.3 Xfce x64 (sysvinit)`.
    #     Return       boot it
    #
    # RETURN ON ITS OWN IS THE MISTAKE THIS REPLACES: at the submenu it selects `Language`
    # and opens a sixty-row locale list - run 82f5cf9's entire recording is that list, on
    # `lang=en : English - English`.
    #
    # AND THE FIRST ENTRY IS THE `(sysvinit)` ONE, WHICH IS DELIBERATE. MX ships both; the
    # ssh stage enables sshd with `update-rc.d` and `service`, because `systemctl --now`
    # REFUSES when systemd is not PID 1 (its own words: "cannot be used when systemd is not
    # running"). Booting the systemd entry would change the init system the rest of the disk
    # was measured against.
    grub_key=('esc', 'up', 'up', 'up', 'up', 'ret'),
    # 75 s: GRUB's own 60-second timeout, then this menu's first paint.
    grub_delay=75.0,
    desktop=livebake.DesktopWhen(all_of=('welcome', 'mx')),
    mem_mb=4096,
    # 400 s, UP FROM 210. The budget now covers GRUB's 60-second timeout, the keypress at 75,
    # and the live session after it - the old 210 was measured against the B boot, which had
    # neither menu to wait through.
    timeout=400,
)


def main(argv: list[str]) -> int:
    return livebake.cli(BAKE, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
