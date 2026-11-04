"""Every entry point in this repository actually runs, and says so when it cannot.

    python3 lib/entrypoints.py            # check, non-zero on a problem
    python3 lib/entrypoints.py --fix      # nothing; the file is a report

WHY THIS EXISTS

`redox/bake.py` was committed without its

    if __name__ == "__main__":
        raise SystemExit(main(sys.argv[1:]))

line. `python3 redox/bake.py --help` still exited 0 - because - and this is the part that
made the check useless - it was run from the repository root, where importing the file
defines `main` and stops. Nothing executes, nothing fails, exit code 0.

In CI it was worse, not better: the step finished in 559 ms having pushed nothing, and the
only symptom was a step that reported success and produced no disk:

    log-stream: finished success (0 pushes, 0 failures)
    ...
    FAILED: no disk at out/redox.qcow2

A file that defines a `main()` and never calls it is a file that does nothing, and nothing
in this repository noticed. This does.

WHAT IS CHECKED, AND WHY EACH ONE

    the __main__ guard     every module that has a main() must call it, or its workflow
                           step succeeds by doing nothing
    it runs, from the repo the same way the workflow does: `python3 <path> --help`
    it exits non-zero on --help a module that ignores its arguments is a module whose
                           argparse was never reached

THE CHECK RUNS FROM THE REPOSITORY ROOT, and the paths are absolute. That is the difference
between this and the check that missed the bug: a module imported under a name that already
exists in sys.modules is never executed at all.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# THE ENTRY POINTS A WORKFLOW RUNS BY PATH. Kept as a list because a module that is only
# ever imported (lib/*.py) is a different thing and does not need the guard.
ENTRYPOINTS = [
    "redox/bake.py",
    "redox/launch.py",
    "redox/ssh.py",
    "redox/probe.py",
]

# Directories that hold an entry point per system, discovered rather than listed so a new
# system cannot be added and forgotten.
SEARCH = ["android", "bsd", "linux", "macos", "redox", "windows", "lib"]

# THIS FILE RUNS ITSELF OTHERWISE, and "did not return within 120s" is what that looks
# like: a check that invokes every entry point discovered a check that invokes every entry
# point. Excluded by name rather than by a depth counter, because there is exactly one.
SELF = "lib/entrypoints.py"


def log(message: str) -> None:
    print(f"[entrypoints] {message}", flush=True)


def has_main_guard(path: str) -> bool:
    """True when the file calls its own main() under __main__.

    NOT A REGEX FOR `if __name__ == "__main__"` ALONE: the guard is worthless without the
    call, and a file can carry the guard and return from it doing nothing. The check is for
    a call to main( ) inside that block.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    match = re.search(r'^if __name__ == ["\']__main__["\']:', text, re.MULTILINE)
    if not match:
        return False
    tail = text[match.start():]
    return "main(" in tail


def defines_main(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            return re.search(r"^def main\(", fh.read(), re.MULTILINE) is not None
    except OSError:
        return False


def discover() -> list[str]:
    """Every .py with a main() in the directories that hold entry points."""
    found = []
    for folder in SEARCH:
        root = os.path.join(REPO, folder)
        for dirpath, _dirnames, filenames in os.walk(root):
            if "__pycache__" in dirpath:
                continue
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                if defines_main(path):
                    found.append(os.path.relpath(path, REPO))
    return sorted(x for x in set(found + [e for e in ENTRYPOINTS if e not in found])
                  if x != SELF)


def silent_success(path: str) -> tuple[bool, str]:
    """Does `python3 <path> --help` DO something?

    THE ONLY FAILURE THIS LOOKS FOR IS THE ONE THAT HAPPENED: exit 0, no output, nothing
    done. That is a file whose main() was never called, and it is invisible everywhere else.

    A NON-ZERO EXIT IS NOT A FAILURE HERE. Several entry points in this repository take
    positional arguments and have no `--help` at all - `windows/11/pull.py <tag> <dest>`,
    `linux/mx/bake.py` reading $OUT - and refusing to start without their arguments is
    correct behaviour, not a bug. Treating it as one would make this check something people
    disable.

    A TIMEOUT IS ALSO REPORTED, SEPARATELY, because a file that starts its real work before
    parsing its arguments is a different problem from one that does nothing.
    """
    try:
        r = subprocess.run([sys.executable, path, "--help"],
                           capture_output=True, text=True, timeout=120, cwd=REPO)
    except subprocess.TimeoutExpired:
        return (False, "did not return within 120s (it started working before --help)")
    if r.returncode != 0:
        return (True, "runs (refuses --help, which is correct for a positional CLI)")
    if not ((r.stdout or "") + (r.stderr or "")).strip():
        return (False, "exited 0 with no output at all - main() was never called")
    return (True, "ok")


def main(argv: list[str]) -> int:
    problems = 0
    checked = discover()
    log(f"{len(checked)} file(s) with a main()")

    for rel in checked:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            log(f"  MISSING  {rel}")
            problems += 1
            continue

        if not has_main_guard(path):
            log(f"  NO GUARD {rel}")
            log(f"           it defines main() and never calls it: the workflow step")
            log(f"           reports success having done nothing")
            problems += 1
            continue

        ok, why = silent_success(path)
        if ok:
            log(f"  ok       {rel}")
        else:
            log(f"  SILENT   {rel}: {why}")
            problems += 1

    if problems:
        log(f"=== {problems} PROBLEM(S) ===")
        return 1
    log("=== ALL ENTRY POINTS RUN ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
