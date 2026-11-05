"""Every `--flag` a workflow passes to a Python script is a flag that script accepts.

    python3 lib/flagcheck.py          # check, non-zero on a problem
    python3 lib/flagcheck.py -v       # also list every call that was checked

WHY THIS EXISTS

Run 42064532959's macos26 desktop job failed in two seconds with

    desktop.py: error: unrecognized arguments: --wizard-timeout 7200

`--wizard-timeout` is a `macos/15/bake.py` argument. It was added in b3e09b4, which raised
the installer's budget - correctly, since the installer says "About 45 minutes remaining" and
the wait it had been given was 30 - and then passed it to BOTH macOS workflows. macos15.yml's
desktop job was never given the line. macos26.yml's desktop job was, because the desktop
block was written by copying the setup block above it.

argparse rejects the whole invocation, so nothing ran: the disk was pulled, the VM was never
started, and the step's only output was a usage message. The cost is not the two seconds, it
is the twenty minutes of pulling a 30 GiB disk first and the run that has to be repeated.

WHAT MAKES THIS CHECKABLE WITHOUT RUNNING ANYTHING

A workflow step is text, and the script it names declares its arguments in `add_argument`
calls. Both sides are in the repository, so "is this flag accepted" is a question about two
files, not about a running machine.

WHAT IS CHECKED

    python3 path/to/script.py --flag ...       a literal script path and literal flags

    --flag "${{ inputs.x || 'y' }}"            the flag is literal; the VALUE is a GitHub
                                               expression and is ignored

WHAT IS DELIBERATELY NOT CHECKED

    $FLAG / ${FLAG}          a flag built from a variable. Resolving it means knowing what
                             the environment holds, which is the run this is meant to
                             replace.
    --flag=$VALUE            the `=` form. Accepted by argparse, never used here, and
                             splitting it correctly needs the same variable resolution.
    a script that is not in the repository (an absolute path into the container, a helper
                             from an action) - there is nothing to compare against.

A FALSE ALARM SWITCHES THE CHECKER OFF, so anything ambiguous is skipped and counted as
skipped rather than guessed at.
"""

from __future__ import annotations

import ast
import glob
import os
import re
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(message: str) -> None:
    print(f"[flagcheck] {message}", flush=True)


# `python3 macos/15/desktop.py` followed by the rest of its command line, which the caller
# has already stripped of expressions and continuations.
CALL = re.compile(r"python3[ \t]+([A-Za-z0-9_./-]+\.py)")

# A literal long option: starts with --, then word characters and dashes. Not `--` alone
# (argument separator) and not `--flag=value` (see the docstring).
FLAG = re.compile(r"(?<![\w$])--[A-Za-z][A-Za-z0-9_-]*")

# A GitHub expression, in either the raw or the quoted-with-default form. Everything from
# `${{` to `}}` is removed before flags are searched for, so an expression that CONTAINS
# something that looks like a flag cannot be mistaken for one.
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.S)


def accepted_flags(path: str) -> set[str] | None:
    """Every option string this script's argparse accepts, or None if it has no argparse.

    PARSED, NOT GREPPED. `add_argument("--out", default=None)` is one call, but the same
    option also appears in help text, in a docstring, and in comments - the docstring at the
    top of this file contains `--wizard-timeout`. Searching the file's text would find all of
    them and accept a flag that does not exist, which is the failure being checked for.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError):
        return None

    found: set[str] = set()
    saw_parser = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else "")
        if name != "add_argument":
            continue
        saw_parser = True
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.startswith("-"):
                    found.add(arg.value)
    return found if saw_parser else None


def calls_in(text: str) -> list[tuple[str, str]]:
    """(script path, the rest of its command line) for every `python3 X.py` in one step.

    THE ORDER OF THESE THREE STEPS IS THE WHOLE CORRECTNESS OF THIS FUNCTION, and the first
    draft had it wrong in a way that made the checker pass on the very bug it was written
    for. It read `--wizard-timeout` in run 42064532959's macos26 desktop job and reported
    "74 CALL(S) CHECKED" with no problem, because:

      * the line held `--assistant-timeout "${{ inputs.assistant_timeout || '2700' }}"`,
      * the tail regex stopped at the first `|`, which is inside that expression,
      * so every flag after it - including `--wizard-timeout` - was never read.

    A GitHub expression contains `||` for defaults and is not a shell pipe. So:

        1. remove the expressions        nothing inside one is a flag or a pipe
        2. join the line continuations   the invocation becomes one line
        3. split on ; && ||              only now are these shell separators
"""
    # 1. THE EXPRESSIONS. Removed whole, including their `||` defaults.
    flat = EXPRESSION.sub("", text)
    # 2. THE CONTINUATIONS, so `python3 x.py \\\n --flag` is one line.
    flat = flat.replace("\\\n", " ")
    # 3. THE SEPARATORS. `;` `&&` `||` end a command, AND SO DOES A NEWLINE - a step
    #    frequently runs several commands one per line (artifacts-publish's prepare runs
    #    `ci.py install-oras`, `ci.py login` and `android_pin.py` that way). Splitting on
    #    separators alone kept only the first of those, which is how this checker first
    #    reported 74 calls where the workflows contain 84.
    commands = re.split(r"(?:;|&&|\|\||\n)\s*", flat)
    found: list[tuple[str, str]] = []
    for command in commands:
        m = CALL.search(command)
        if m:
            found.append((m.group(1), command[m.end():]))
    return found


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    problems = 0
    checked = 0
    skipped = 0

    cache: dict[str, set[str] | None] = {}

    for path in sorted(glob.glob(os.path.join(REPO, ".github", "workflows", "*.yml"))):
        wf = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            log(f"  CANNOT READ {wf}: {exc}")
            problems += 1
            continue

        for job_name, job in sorted((doc.get("jobs") or {}).items()):
            for step in (job.get("steps") or []):
                if not isinstance(step, dict):
                    continue
                body = (step.get("with") or {}).get("command")
                if not isinstance(body, str):
                    continue

                # WHICH SCRIPT IS NAMED IS LITERAL; WHICH JOB RAN IT IS NOT. A step whose
                # `command` is assembled from inputs still contains the `python3 X.py` line
                # when X is fixed - which it is for every stage here.
                for script, tail in calls_in(body):
                    target = os.path.normpath(os.path.join(REPO, script))
                    if not os.path.isfile(target):
                        skipped += 1
                        if verbose:
                            log(f"  skip        {wf}: {job_name} -> {script} (not in the "
                                f"repository)")
                        continue

                    if target not in cache:
                        cache[target] = accepted_flags(target)
                    accepted = cache[target]
                    if accepted is None:
                        skipped += 1
                        if verbose:
                            log(f"  skip        {wf}: {job_name} -> {script} (no argparse)")
                        continue

                    # THE EXPRESSIONS ARE ALREADY GONE - calls_in() removed them before it
                    # split the command, because a `||` inside one is not a shell operator.
                    flat = EXPRESSION.sub("", tail)
                    flat = flat.split("\n")[0]

                    checked += 1
                    for flag in FLAG.findall(flat):
                        if flag not in accepted:
                            log(f"  NOT ACCEPTED {wf}: {job_name} passes {flag} to {script}, "
                                f"which does not declare it")
                            problems += 1
                        elif verbose:
                            log(f"  ok          {wf}: {job_name} {script} {flag}")

    if problems:
        log(f"=== {problems} FLAG(S) A SCRIPT DOES NOT ACCEPT ===")
        return 1
    log(f"=== {checked} CALL(S) CHECKED, {skipped} SKIPPED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
