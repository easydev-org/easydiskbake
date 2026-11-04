"""Bake a macOS 26 (Tahoe) disk: recovery in, a disk that stops at the Setup Assistant.

    python3 macos/26/bake.py --release 26 --out out/macos26.qcow2

WHAT IS SHARED AND WHAT IS NOT

The pipeline around the installer is 15's, imported rather than copied: lib/opencore.py
builds OpenCore, the firmware pair and the APFS driver from the pins; macos/15/machine.py
describes the machine; lib/assistant.py reads the Setup Assistant; lib/compress.py writes
the artifact. A second copy of any of those would be a second place to disagree.

THE INSTALLER IS THE PART THAT DIFFERS, and that is why this directory exists.
macos/15/install.py clicks a measured list of coordinates - Disk Utility, Erase, Reinstall,
Continue, Agree, Agree-in-the-sheet, the disk card, Continue - and those numbers were
measured on Sequoia's installer. Tahoe's may be the same and may not be, and a coordinate
that has moved is indistinguishable from a click that was ignored. That is exactly what the
README in this directory was written to warn about before the port was attempted.

SO THE DRIVER DOES NOT TRUST THE COORDINATES WITHOUT THE SCREEN AGREEING.

Where 15 can click a fixed point after a fixed wait, this waits for the PANE'S OWN TEXT and
only then sends a click. The phrases it looks for are the ones that cannot occur anywhere
else on that screen:

    Disk Utility's detail pane   "capacity:" and "child"
    the erase sheet              `erase "<device>"?`
    the installer's licence      "agree" near the foot of the window

A pane that does not match stops the run with its name in the log. A bake that fails at the
third pane and says which one is worth more than one that clicks through six it never saw.

WHAT THE README SAYS ABOUT 26, KEPT HERE BECAUSE IT DECIDES THE DESIGN

    the privacy pane's Continue leads into a pairing handshake a container cannot complete,
    and the only escape is a `Not Now` that is replaced by the failure alert in about two
    seconds

That is the SETUP ASSISTANT, not the installer, and this stage stops before the wizard is
driven - see the stage boundary in macos/15/bake.py. The marker
`/private/var/db/.AppleSetupDone` is the intended answer and belongs to the desktop stage.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

_M15 = os.path.join(os.path.dirname(HERE), "15")


def _load(name: str, path: str):
    """Import one of macos/15's modules by path.

    BY PATH, NOT BY NAME. This repository already has two modules called `desktop` and two
    called `install`, and a bare `import install` resolves to whichever directory reached
    sys.path first. Loading by path says exactly which file is used.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # REGISTERED IN sys.modules UNDER `name`, because macos/26/bake.py has to reach back
    # into macos15_bake to redirect its HERE at this directory. A module loaded by
    # spec.loader.exec_module() is not registered automatically, and sys.modules.get()
    # then returns None.
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


bake15 = _load("macos15_bake", os.path.join(_M15, "bake.py"))


class Baker(bake15.Baker):
    """15's baker, with its installer replaced by this release's.

    ONLY run_installer IS OVERRIDDEN. Everything else - the recovery download, the
    OpenCore assets, the machine, the frame capture, the Setup Assistant wait, the
    compression and the publish - is release-independent and is inherited unchanged. The
    one thing that is not is which script drives the installer, and that is exactly what a
    per-release directory exists to say.
    """

    def run_installer(self, guest) -> bool:
        """Run this release's installer driver instead of 15's.

        THE SUBPROCESS CONTRACT IS THE SAME. macos/26/install.py accepts --qmp, --frames
        and --disk and returns 0 only when it reached the end of the sequence, which is
        what macos/15/bake.py's own run_installer checks. Everything below this method -
        the output forwarding, the timeout, the exit code - is inherited.
        """
        # THE PARENT FINDS THE SCRIPT THROUGH ITS OWN MODULE DIRECTORY, so handing it a
        # different one without copying sixty lines of process management means pointing
        # that module's HERE at this directory for the duration of the call. One attribute
        # on one module object, restored in a finally.
        #
        # THE FILE IS CHECKED FIRST. Redirecting to a directory with no install.py would
        # make the parent fail with "No such file", which reads as a missing script rather
        # than as a mistake in this file.
        mine = os.path.join(HERE, "install.py")
        if not os.path.exists(mine):
            print(f"=== {mine} IS MISSING ===", flush=True)
            print("  this release has no installer driver, so 15's coordinates will NOT", flush=True)
            print("  be used against a different installer", flush=True)
            return False

        bake_module = sys.modules.get("macos15_bake")
        if bake_module is None:
            print("=== macos15_bake IS NOT REGISTERED ===", flush=True)
            return False

        previous = bake_module.HERE
        try:
            bake_module.HERE = HERE
            return super().run_installer(guest)
        finally:
            bake_module.HERE = previous


def main(argv: list[str]) -> int:
    args = bake15.parse(argv)

    print("=== macos 26 (tahoe) ===", flush=True)
    print("  the installer sequence is shared with 15 and has not been measured on 26.",
          flush=True)
    print("  every pane is confirmed by its text before a click is sent; the run stops",
          flush=True)
    print("  at the first pane that does not match, and names it.", flush=True)
    return 0 if Baker(args).run() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
