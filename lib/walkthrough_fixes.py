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
    # AND THE WALKTHROUGH PATH NEEDS THE SAME PROTECTION, which is a different guard: the one
    # above is a socket probe that only Python can make. The switch is a toggle, so the step
    # that presses it must be conditional on what the pane already says - measured by turning
    # a running guest's Remote Login OFF with it, then waiting ten minutes for the sshd it had
    # just stopped.
    ssh_walk = load_yaml("macos/15/steps/ssh.yaml")
    ssh_steps = {s.get("name"): s for s in ssh_walk.get("steps") or []}
    toggler = ssh_steps.get("turn remote login on") or {}
    check("the toggle is only pressed when the pane says it is off",
          "macos/15/steps/ssh.yaml", "unless" in toggler)
    check("the pane is read before the toggle is pressed",
          "macos/15/steps/ssh.yaml", "the remote login pane is up" in ssh_steps)
    # THE RESULT HAS TO BE THERE BEFORE ITS ROW IS CLICKED, and this is the check that run
    # 42584262403 failed on: the click went out while the sidebar was still filtering, landed
    # on the row's future position, and the pane never swapped. A settle() is a guess about how
    # long a filter takes.
    check("the search result is waited for before its row is clicked",
          "macos/15/steps/ssh.yaml", "the remote login result is on screen" in ssh_steps)
    # AND THE ROW IS CLICKED AGAIN IF THE PANE DID NOT SWAP. macOS 15 draws this as a sheet on
    # one run and an inline sidebar row on the next; the retry is conditional on the pane's own
    # words, so a machine that never swaps is still a failure.
    retry = ssh_steps.get("open the remote login pane again") or {}
    check("the row click is retried when the pane did not open",
          "macos/15/steps/ssh.yaml", "unless" in retry)
    # THE SEARCH FIELD IS CLEARED AS PART OF TYPING, not by a separate click. Two steps means
    # the typing one has no `at:` and takes the type-into-focus branch, which clears nothing -
    # and System Settings remembers the last query, so a second run searched for
    # 'remote loginremote login' and got No Results.
    searcher = ssh_steps.get("search for remote login") or {}
    check("the search field is cleared before the query is typed",
          "macos/15/steps/ssh.yaml",
          bool(searcher.get("at")) and bool(searcher.get("type"))
          and searcher.get("modifier") == "meta_l")

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

    # --- YAML BOOLEANS THAT SHOULD HAVE BEEN WORDS ---------------------------------------
    #
    # YAML 1.1 reads on/off/yes/no as booleans, so an unquoted matcher
    #
    #     - [remote, login, on]
    #
    # parses as ['remote', 'login', True] and the driver dies with
    # "'bool' object has no attribute 'lower'". Found on a live guest, in the ssh stage's
    # first run as YAML - the word "on" is in the pane's own title, so this is not a
    # contrived case.
    import glob as globmod

    poisoned = []
    for walk_path in (globmod.glob(os.path.join(REPO, "*", "steps", "*.yaml"))
                      + globmod.glob(os.path.join(REPO, "*", "*", "steps", "*.yaml"))):
        document = load_yaml(os.path.relpath(walk_path, REPO))
        for step in (document.get("steps") or []):
            for key in ("until", "until_any", "expect", "expect_any", "when",
                        "unless", "ends"):
                value = step.get(key)

                def scan(node):
                    if isinstance(node, list):
                        return any(scan(item) for item in node)
                    return not isinstance(node, str)
                if value is not None and scan(value):
                    poisoned.append(f"{os.path.basename(walk_path)}:{step.get('name')}:{key}")
    check("no YAML boolean where a matcher expects a word",
          "*/steps/*.yaml", not poisoned)
    if poisoned:
        for one in poisoned:
            print(f"       poisoned matcher: {one}")

    # --- the workflow ------------------------------------------------------------------
    check("all three stages pass --steps", ".github/workflows/macos15.yml",
          workflow.count("--steps macos/15/steps/") == 3)
    for stage, walk in (("setup", "setup.yaml"), ("desktop", "sequoia.yaml"),
                        ("ssh", "ssh.yaml")):
        check(f"{stage} is driven by {walk}", ".github/workflows/macos15.yml",
              f"--steps macos/15/steps/{walk}" in workflow)

    # --- the panes do not overlap --------------------------------------------------------
    #
    # A PANE SET IS AN ORDERED LIST, so a matcher that fires on somebody else's pane wins if it
    # is declared earlier - and the walk still reaches its end condition, so the stage is
    # green while a step never ran. That is exactly what `data and privacy` did to `analytics`:
    # both matched the Analytics screen, the privacy step is 115 lines above, so the analytics
    # step - whose whole job is to untick "Share Mac Analytics with Apple" - never ran, and
    # both CI runs of the desktop stage were green with analytics sharing enabled on the disk.
    #
    # The check is the full matrix of matchers against the frames of the run that showed it,
    # when those frames are checked out. It reads like a lot for one comparison; the point is
    # that a single-frame test cannot see this failure at all.
    try:
        import pane_matrix_test
        matrix_ok = pane_matrix_test.main() == 0
    except SystemExit:
        matrix_ok = False
    except Exception:                                  # noqa: BLE001
        matrix_ok = True                               # no recording checked out: not a failure
    check("no desktop pane matches another pane's screen", "macos/15/steps/sequoia.yaml",
          matrix_ok)

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
