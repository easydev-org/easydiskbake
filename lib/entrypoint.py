#!/usr/bin/env python3
"""Say that a stage's own `main()` is not the supported way to run it.

WHY A STAGE STILL HAS A `main()` AT ALL

The workflow files call it:

    python3 windows/11/ssh.py --gold out/windows11-gold.qcow2 --out out/windows11-ssh.qcow2

Changing that line is the LAST step of migrating a stage, not the first - a repository that cannot
run cannot be verified, and a stage is only replaced once its workflow file has been rewritten and
proven. Until then the program works and says what it is.

WHAT IT SAYS

    $ python3 windows/11/ssh.py --gold gold.qcow2
    [easyvm] this stage is not an entry point; its workflow file is
    [easyvm]   easyvm windows/11/steps/ssh.yaml --boot-disk gold.qcow2
    [easyvm]   (EASYVM_STRICT=1 makes this a refusal)

and with `EASYVM_STRICT=1` it stops instead, after the same two lines, so a script or a CI step
that has drifted back to the old form fails loudly rather than quietly working.

WHY NOT JUST DELETE THE OLD PATH

Because it IS the path today, for twenty-four programs, and deleting them is a change nobody can
review in one piece. This file is the seam: it keeps the old entry working, makes it visible, and
gives the migration a way to be finished - the day every workflow says `easyvm`, every one of these
guards can be deleted with a straight face.

THE WORKFLOW FILE IS DERIVED, NOT GUESSED. A stage's file is `<family-dir>/steps/<stage>.yaml`,
and that is checked before it is printed: a guard that suggests a path which does not exist is
worse than no guard, because it is confidently wrong.
"""

from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def note_stage(name: str, workflow: str, *, extra: str = "") -> None:
    """Report that `name` was run directly instead of through its workflow file.

    `workflow` is a path RELATIVE TO THE REPOSITORY, e.g. "windows/11/steps/ssh.yaml". It is
    checked before being printed, because a suggestion to run a file that is not there reads as
    a bug in this helper rather than as a fact about the stage.
    """
    path = os.path.join(REPO, workflow)
    hint = f"  easyvm {workflow}"
    if extra:
        hint += " " + extra
    print(f"[easyvm] this stage is not an entry point; its workflow file is", file=sys.stderr)
    if os.path.isfile(path):
        print(f"[easyvm] {hint}", file=sys.stderr)
    else:
        print(f"[easyvm]   ({workflow} does not exist - the stage has not been migrated yet)",
              file=sys.stderr)
    if os.environ.get("EASYVM_STRICT", "") not in ("", "0", "false"):
        print("[easyvm] EASYVM_STRICT is set, so this is a refusal rather than a warning.",
              file=sys.stderr)
        print("=== THIS STAGE IS NOT AN ENTRY POINT ===", file=sys.stderr)
        raise SystemExit(2)
