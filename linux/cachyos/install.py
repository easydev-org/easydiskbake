"""Install cachyos from its Live ISO, through its own graphical installer.

    python3 linux/cachyos/install.py --iso out/iso/<the pinned ISO> \
                                    --out out/cachyos.qcow2

WHAT THIS FILE IS. It is a DECLARATION, not an implementation. The bake skeleton - disk,
guest, recording, the QMP driver, the walk, the post-mortem frame, the empty-disk check, the
compression - is `lib/livebake.py`, shared by every `linux/<distro>/install.py`. What is
below is the only part that is cachyos's own.

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
    name="cachyos",
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
    after=livebake.After(seconds=230),
    mem_mb=4096,
    # THE INSTALLER MUST NOT SEE AVX-512, OR THE DISK IS NOT PORTABLE.
    #
    # CachyOS SHIPS TWO REPOSITORIES PER PACKAGE, `v3` AND `v4`, and the installer picks one from
    # the CPU it is running on. `v4` requires AVX-512; `v3` requires AVX2. A `v4` disk boots only
    # on hosts with AVX-512, and the CI runner is an AMD EPYC 7763 - Zen 3, which has AVX2 and no
    # AVX-512 at all. The disk it produced panics in the first second:
    #
    #     KERNEL PANIC!
    #     Attempted to kill init! exitcode=0x00000004
    #
    # 4 is SIGILL. Measured on ONE disk (linux-cachyos-ssh, 7190 MiB) in a local pod:
    #
    #     -cpu Skylake-Client   no AVX-512   PANIC
    #     -cpu Skylake-Server   AVX-512      boots to SDDM
    #     -cpu host (Xeon 8336C) AVX-512     boots to SDDM
    #
    # SO THE FIX IS HERE AND NOT IN `-cpu` ON A LATER STAGE. `-cpu` hides CPUID bits; it does not
    # add silicon. A later stage asking for AVX-512 on EPYC gets
    #
    #     qemu-system-x86_64: warning: host doesn't support requested feature: ...
    #
    # and the guest panics exactly as before. The level has to be decided where the packages are
    # CHOSEN, which is this install - so the installer is given a model every runner satisfies.
    #
    # `Skylake-Client` IS x86-64-v3: AVX2, BMI1/2, FMA, no AVX-512. Both the EPYC and the Xeon have
    # every one of those, so the disk installs `v3` packages on either and boots on both.
    # AND IT IS NOW ALSO `lib/vm.py`'s DEFAULT_CPU, so this line is a STATEMENT RATHER THAN AN
    # EXCEPTION: every distribution installs under the same model, and cachyos is the one whose
    # packages would visibly break if that ever changed back.
    cpu="Skylake-Client-v4",
)


def main(argv: list[str]) -> int:
    return livebake.cli(BAKE, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
