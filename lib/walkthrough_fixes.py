"""Assert that every fix the macOS walkthrough depends on is still in the tree.

    python3 lib/walkthrough_fixes.py

WHY A CHECK AND NOT A COMMENT

Each of these was found the hard way - one per CI run, or one per forty seconds on a local
guest - and every one of them is a one-line difference that a later edit can undo without
noticing. The unit tests in lib/steps_test.py cover the INTERPRETER's behaviour; this covers the
WALKTHROUGHS' and the drivers' specific measurements, which no unit test can exercise without a
running macOS.

WHAT EACH ONE COST

    at+type, one action          three extra presses per pass on the account pane, whose form
                                 breaks when it is typed into twice
    python: needs a screen       the ssh stage got a HeadlessDriver with no socket and waited
                                 out its own 120-second 'no QMP socket at ' (empty path)
    a crop restores the frame    the country pane never matched, because a cropped read of the
                                 ACCOUNT pane was still the screen when its turn came
    waits_for alternatives       a stage resumed mid-wizard waited out 1800s on the first pane
    ends_at crops                the wizard ends on the LOGIN WINDOW, whose only text is a
                                 twelve-pixel password placeholder
    QMP ownership (x2)           'OSError: Bad file descriptor' one line after a socket that
                                 had been working
    Remote Login is a toggle     three runs left sshd CLOSED, and the next step waited 600s
    account pane, 180s + once    six, then twenty-five, then eighty seconds were all too short;
                                 too short meant the form was filled in twice
    two title bands              the account pane's body says 'sign in with your Apple Account',
                                 so the Apple Account test matched it too
    login-window end             see ends_at, above
    three --steps                the point of the exercise
"""

from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(relative: str) -> str:
    with open(os.path.join(REPO, relative), encoding="utf-8") as fh:
        return fh.read()


def load_yaml(relative: str):
    import yaml

    return yaml.safe_load(read(relative))


def main() -> int:
    checks: list[tuple[str, str, bool]] = []

    def check(label: str, path: str, ok: bool) -> None:
        checks.append((label, path, ok))

    steps_src = read("lib/steps.py")
    ssh_src = read("macos/15/ssh.py")
    seq = load_yaml("macos/15/steps/sequoia.yaml")
    workflow = read(".github/workflows/macos15.yml")

    # --- the interpreter ---------------------------------------------------------------
    check("at+type is ONE action, not a type then a click", "lib/steps.py",
          'elif step["at"] is None and step["type"] is not None:' in steps_src)
    check("python: counts as needing a screen (both checks)", "lib/steps.py",
          steps_src.count('s["python"] or s["run"] or s["key"]') == 2)
    check("a cropped read puts the whole-frame words back", "lib/steps.py",
          "driver.words = whole" in steps_src)
    check("waits_for accepts alternatives, not one phrase", "lib/steps.py",
          "alternatives.append(entry if isinstance(entry, list) else [str(entry)])" in steps_src)
    check("ends_at crops are an end condition", "lib/steps.py",
          'for spec in steps.document.get("ends_at") or []:' in steps_src)
    check("the boot gate also accepts ends_at crops", "lib/steps.py",
          "end_crops = [s for s in (steps.document.get(\"ends_at\") or [])" in steps_src)

    # --- the ssh driver ----------------------------------------------------------------
    check("dismiss_keyboard_assistant closes only what it opened", "macos/15/ssh.py",
          "if owned:" in ssh_src)
    check("enable_remote_login closes only what it opened", "macos/15/ssh.py",
          "if q_owned:" in ssh_src)
    # A CALL, NOT THE EXACT TEXT: the probe grew a port argument, and a check that pins the
    # call's spelling makes every future signature change look like a missing fix.
    check("Remote Login is read before it is toggled", "macos/15/ssh.py",
          "def _remote_login_listening" in ssh_src
          and "_remote_login_listening(guest" in ssh_src)

    # --- the walkthrough ---------------------------------------------------------------
    by_name = {s.get("name"): s for s in seq.get("steps") or []}

    account = by_name.get("create a mac account") or {}
    do = account.get("do") or []
    check("the account pane waits 180s after Continue", "macos/15/steps/sequoia.yaml",
          bool(do) and do[-1].get("settle") == 180)
    check("the account pane is answered at most once", "macos/15/steps/sequoia.yaml",
          account.get("once") is True)
    check("the account pane matches its own title band", "macos/15/steps/sequoia.yaml",
          account.get("region") == [300, 300, 1620, 340])

    apple = by_name.get("sign in to apple account") or {}
    check("Apple Account matches its own title band", "macos/15/steps/sequoia.yaml",
          apple.get("region") == [300, 420, 1620, 460])

    ends = [e.get("name") for e in (seq.get("ends_at") or [])]
    check("the walkthrough ends on the login window", "macos/15/steps/sequoia.yaml",
          "the login window's password field" in ends)

    gates = seq.get("waits_for") or []
    check("waits_for lists the panes, so a resumed disk is not stuck",
          "macos/15/steps/sequoia.yaml", len(gates) >= 10)

    # --- the workflow ------------------------------------------------------------------
    check("all three stages pass --steps", ".github/workflows/macos15.yml",
          workflow.count("--steps macos/15/steps/") == 3)
    for stage, walk in (("setup", "setup.yaml"), ("desktop", "sequoia.yaml"),
                        ("ssh", "ssh.yaml")):
        check(f"{stage} is driven by {walk}", ".github/workflows/macos15.yml",
              f"--steps macos/15/steps/{walk}" in workflow)

    # --- report ------------------------------------------------------------------------
    width = max(len(label) for label, _path, _ok in checks)
    failures = 0
    for label, path, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<{width}}  ({path})")
        failures += 0 if ok else 1

    print()
    print(f"=== {len(checks) - failures}/{len(checks)} WALKTHROUGH FIXES PRESENT ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
