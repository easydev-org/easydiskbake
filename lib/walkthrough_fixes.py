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
    # A `python:` STEP NEEDS A SCREEN UNLESS IT SAYS OTHERWISE, IN BOTH PLACES THAT DECIDE.
    #
    # The default is conservative because most extensions drive the GUI - and forgetting it was
    # a crash (`HeadlessDriver` has no `key`). But macos/15/steps/dev.yaml's two extensions fetch
    # and copy a file over ssh, never touching the screen, and being told they needed a QMP
    # socket is what stopped run 42632192846 before its first step. `python_screen: false` is the
    # declaration, and it has to be honoured by BOTH needs_screen expressions or a walkthrough
    # passes one check and dies in the other.
    check("python: needs a screen unless the file says it does not (both checks)",
          "lib/steps.py",
          steps_src.count('s["python"] and s.get("python_screen") is not False') == 2)
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
    # THREE ARE PASSED ON THE COMMAND LINE AND THE FOURTH IS READ FROM INSIDE ITS STAGE.
    #
    # setup, desktop and ssh are entry points of their own, so their YAML is a `--steps`
    # argument. dev is not: macos/15/dev.py starts the machine, and the walk is the middle of
    # it - the same shape windows/11/ssh.py and devtools.py use. A `dev.yaml` on the command
    # line would need a second entry point that duplicates the recording and the publishing,
    # which is the "one fact in two places" mistake this repository keeps paying for.
    check("three stages pass --steps", ".github/workflows/macos15.yml",
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

    # --- windows: the two stages that were Python are files now ---------------------------
    #
    # THE BASE STAGE NEEDS NO WALKTHROUGH, and that is not an omission: Windows installs from
    # answer/Autounattend.xml, which setup itself consumes in three passes (windowsPE,
    # specialize, oobeSystem). There is no pane to answer and no sequence to type, so a YAML
    # file for it would be an empty list pretending to be a driver.
    #
    # WHAT WAS PYTHON WAS THE PART THAT IS A SEQUENCE: open PowerShell, type the winget line,
    # type the sshd statements, wait for the port, push the guest settings; then install four
    # packages and prove three of them work. Both stages still start a machine and publish a
    # disk - that stays in Python - and both call lib/steps.py now.
    win_ssh = read("windows/11/ssh.py")
    win_dev = read("windows/11/devtools.py")
    check("windows ssh is driven by ssh.yaml", "windows/11/ssh.py",
          'steps/ssh.yaml' in win_ssh and "stepslib.load(WALK)" in win_ssh)
    check("windows dev is driven by dev.yaml", "windows/11/devtools.py",
          'steps/dev.yaml' in win_dev and "stepslib.run_file(" in win_dev)
    # THE KEYBOARD AND THE WALK SHARE ONE SOCKET. qemu accepts one client, so a Driver that
    # opened its own would block on the greeting and read as a hung guest.
    check("the windows walk reuses the keyboard's QMP connection", "windows/11/ssh.py",
          "q=kb.q" in win_ssh)
    # `true` IS NOT A WINDOWS COMMAND. session.wait_for_ssh's own docstring records run
    # 41844808248 losing fifteen minutes to an sshd that was working.
    win_ssh_walk = load_yaml("windows/11/steps/ssh.yaml")
    win_dev_walk = load_yaml("windows/11/steps/dev.yaml")
    gates = [s for s in (win_ssh_walk.get("steps") or []) if s.get("require_ssh")]
    check("the windows sshd waits use a probe cmd.exe has", "windows/11/steps/ssh.yaml",
          bool(gates) and all(s.get("ssh_probe") == "echo ok" for s in gates))
    dev_gates = [s for s in (win_dev_walk.get("steps") or []) if s.get("require_ssh")]
    check("the dev stage's sshd waits use the same probe", "windows/11/steps/dev.yaml",
          bool(dev_gates) and all(s.get("ssh_probe") == "echo ok" for s in dev_gates))
    # PSReadLine DROPS KEYSTROKES BELOW ~150 ms, measured: run 41997580476 typed
    # `winget install` and the guest received `get install`. Every typing step says so.
    typers = [s for s in (win_ssh_walk.get("steps") or []) if s.get("type")]
    check("every windows typing step uses the measured 150 ms gap",
          "windows/11/steps/ssh.yaml",
          bool(typers) and all(float(s.get("type_gap", 0)) >= 0.15 for s in typers))

    # --- macos dev: the toolchain, and the one asset Apple will not serve ---------------
    #
    # XCODE IS THE ONLY THING HERE THAT CANNOT BE FETCHED FROM ITS VENDOR. Apple's endpoint
    # answers an unauthenticated request with a redirect:
    #
    #     HTTP/2 302   location: https://developer.apple.com/unauthorized/
    #
    # measured 2026-11-09, and the `ADCDownloadAuth` cookie trick now answers 403 as well. So
    # the file comes from archive.org, and the sha1 is Apple's own number for that build -
    # which is the only thing that makes an archive an identity check rather than a hope.
    mac_dev = read("macos/15/dev.py")
    check("macos dev is driven by dev.yaml", "macos/15/dev.py",
          "steps/dev.yaml" in mac_dev and "stepslib.run_file(" in mac_dev)
    # THE CHECK IS FOR THE COMMAND, NOT THE PHRASE. The file still *explains* why Homebrew is
    # the prerequisite and what a `brew install` would do, and a grep for the words would fail
    # on the prose that documents their absence. A `run(...)`/`subprocess` call with it is the
    # thing that would mean the sequence had crept back.
    check("macos dev does not run brew itself any more", "macos/15/dev.py",
          "subprocess.run" not in mac_dev and "brew install " not in
          "".join(line for line in mac_dev.splitlines()
                  if not line.lstrip().startswith("#")))
    try:
        import xcode_asset
        pin = xcode_asset.PINNED.get("15") or {}
        check("xcode for macOS 15 is pinned with a sha1", "lib/xcode_asset.py",
              len(pin.get("sha1") or "") == 40 and "26.3" in str(pin.get("version")))
        # 26.3 is the LAST release whose `requires` is macOS 15.x; 26.4 needs 26.2. A later
        # pin would download 2.7 GiB and then refuse to install.
        check("the xcode pin is the top of the range macOS 15 can run",
              "lib/xcode_asset.py", str(pin.get("version")) == "26.3")
    except Exception as exc:                          # noqa: BLE001
        check(f"xcode_asset imports ({exc})", "lib/xcode_asset.py", False)
    # THE INSTALLER IS A FILE, NOT A QUOTED LINE: it expands, moves, selects, accepts the
    # license and installs the platforms, and a failure between those leaves a selected Xcode
    # with no SDK - which answers --version and cannot build.
    import os as osmod
    check("the xcode installer is a script in the repository",
          "macos/15/xcode-install.sh",
          osmod.path.isfile(osmod.path.join(REPO, "macos/15/xcode-install.sh")))
    # AND THE PROOF COMPILES: an SDK that lists a version but cannot build is not a toolchain.
    mac_dev_walk = load_yaml("macos/15/steps/dev.yaml")
    names = [s.get("name") for s in (mac_dev_walk.get("steps") or [])]
    check("the dev stage compiles something with swift", "macos/15/steps/dev.yaml",
          "prove swift compiles" in names)

    # XCODE IS FIRST BECAUSE EVERYTHING ELSE IS BUILT ON WHAT IT INSTALLS.
    #
    # The command line tools - git, clang, swift, make - are the ones the .pkg installers below
    # expect to find, and `xcodebuild -runFirstLaunch` is what installs them.
    #
    # AND HOMEBREW IS GONE ENTIRELY, WHICH IS THE LARGER POINT: this disk is x86_64 and
    # Homebrew's installer refuses that -
    #
    #     Homebrew on macOS is only supported on Apple Silicon processors!
    #
    # (install.sh lines 170-173). Run 42644304192 reached it after Xcode had expanded. So the
    # stage installs from the publishers' own Intel packages, and an assertion checks that no
    # `brew install` survives in it.
    order = [n for n in names if n in ("expand and select xcode", "install node",
                                       "install python")]
    check("xcode is installed before the packages that need its tools",
          "macos/15/steps/dev.yaml",
          order[:1] == ["expand and select xcode"])
    check("the macos dev stage does not use homebrew", "macos/15/steps/dev.yaml",
          "brew install" not in "".join(
              line for line in read("macos/15/steps/dev.yaml").splitlines()
              if not line.lstrip().startswith("#")))

    # SUDO OVER SSH HAS NO TTY, SO EVERY PRIVILEGED CALL HAS TO BE HANDED THE PASSWORD.
    #
    #     sudo: no tty present and no askpass program specified
    #     sudo: a password is required
    #
    # lib/macclean.py carries this lesson already (run 42390179102); the Xcode installer is
    # where it would bite next, and it needs it six times.
    xcode_sh = read("macos/15/xcode-install.sh")
    check("the xcode installer upgrades sudo for a tty-less channel",
          "macos/15/xcode-install.sh",
          "sudo -S -p ''" in xcode_sh
          and all(("sudo_" + rest) in xcode_sh or ("sudo_" + rest) == "sudo_"
                  for rest in (" rm -rf", " mv ", " xcode-select", " xcodebuild",
                               " DevToolsSecurity")))
    check("no bare sudo survives in the xcode installer", "macos/15/xcode-install.sh",
          not any(line.strip().startswith("sudo ")
                  for line in xcode_sh.splitlines()))

    # THE PROOF ASKS WHERE A TOOL IS, NOT WHAT VERSION IT ANSWERS.
    #
    # /usr/bin/python3 exists on every macOS and prints a version - it is the one the command
    # line tools install, and its pip refuses to write. A check that only read a version number
    # would pass on a disk whose python cannot install anything.
    toolchain = next((s for s in (mac_dev_walk.get("steps") or [])
                      if s.get("name") == "prove the toolchain"), {})
    body = str(toolchain.get("script") or "")
    # THE EXPECTED PREFIX IS /usr/local NOW, because that is where nodejs.org's and
    # python.org's .pkg installers put things. /usr/bin/python3 still exists on the disk and
    # still answers --version - it is Xcode's, and its pip refuses to write - so the check has
    # to name the prefix rather than accept any answer.
    check("the toolchain proof checks the path, not the version",
          "macos/15/steps/dev.yaml",
          "command -v" in body and "/usr/local/*" in body)

    # --- the bootstrap launcher: the shell each disk already has ----------------------
    #
    # NO PYTHON, AND THAT IS THE REQUIREMENT RATHER THAN A SIMPLIFICATION. The share and its
    # launcher are installed by their own stage, BEFORE the toolchain - so a launcher that needed
    # python3 would do nothing on a disk that has not had the second stage yet, which is the one
    # thing a login hook must not do.
    try:
        import bootstrap
        check("the bootstrap script is a shell script on macOS and .ps1 on Windows",
              "lib/bootstrap.py",
              bootstrap.SCRIPT["macos"] == "bootstrap.sh"
              and bootstrap.SCRIPT["windows"] == "bootstrap.ps1")
        check("the macos launcher is .sh", "lib/bootstrap.py",
              bootstrap.launcher_name("macos") == "bootstrap-launcher.sh")
        check("the windows launcher is .ps1", "lib/bootstrap.py",
              bootstrap.launcher_name("windows") == "bootstrap-launcher.ps1")
        mac_body = bootstrap.launcher_body("macos")
        win_body = bootstrap.launcher_body("windows")
        # THE MARKER IS WRITTEN ONLY ON SUCCESS, in both. A failing bootstrap that marked
        # itself done would never run again.
        check("the macos launcher marks success only", "lib/bootstrap.py",
              "FAILED" in mac_body and 'date > "$MARKER"' in mac_body)
        check("the windows launcher marks success only", "lib/bootstrap.py",
              "FAILED" in win_body and "Set-Content -Path $Marker" in win_body)
        # AND NEITHER INVOKES AN INTERPRETER THE DISK MAY NOT HAVE. The word appears twice in
        # these bodies - in the comments that say it is not used - so the check is for a call
        # rather than for the word: no `python` at the start of a command, and the macOS one is
        # run by /bin/sh (see macos_plist).
        invokes = [line for line in (mac_body + "\n" + win_body).splitlines()
                   if not line.lstrip().startswith("#") and "python" in line.lower()]
        check("neither launcher invokes python", "lib/bootstrap.py", not invokes)
        check("the macos launcher runs under /bin/sh", "lib/bootstrap.py",
              "/bin/sh" in bootstrap.macos_plist("__X__"))
        # EVERY SYMBOL lib/steps.py REACHES FOR HAS TO EXIST. A rewrite of this module once
        # dropped `windows_ps1` while the macOS branch kept working, so the loss only showed up
        # in a Windows CI run - one full cycle later. This is the cheap check that would have
        # caught it at the desk.
        # EVERY SYMBOL lib/steps.py REACHES FOR HAS TO EXIST. A rewrite of this module once
        # dropped `windows_ps1` while the macOS branch kept working, so the loss only showed up
        # in a Windows CI run - one full cycle later. This is the cheap check that would have
        # caught it at the desk.
        #
        # PARSED OUT OF THE CODE, NOT THE PROSE: the comments here mention bootstrap.plist and
        # bootstrap.py - paths and former names - and a check that read those would fail on the
        # documentation rather than on the code.
        import ast
        wanted = set()
        for node in ast.walk(ast.parse(read("lib/steps.py"))):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "bootstrap"):
                wanted.add(node.attr)
        missing = sorted(n for n in wanted if not hasattr(bootstrap, n))
        if missing:
            check(f"every bootstrap symbol lib/steps.py uses exists (missing: "
                  f"{', '.join(missing)})", "lib/bootstrap.py", False)
        else:
            check("every bootstrap symbol lib/steps.py uses exists", "lib/bootstrap.py", True)
    except Exception as exc:                          # noqa: BLE001
        check(f"bootstrap imports ({exc})", "lib/bootstrap.py", False)

    # THE SHARE IS ITS OWN STAGE NOW, so the launcher belongs to share.yaml rather than to dev.
    for path in ("macos/15/steps/share.yaml", "windows/11/steps/share.yaml"):
        walk = load_yaml(path)
        names = [s.get("name") for s in (walk.get("steps") or [])]
        check(f"{path} installs the launcher", path, "install the bootstrap launcher" in names)
        check(f"{path} mounts the share", path, "mount the host share" in names)
        # AND INSTALLS NO TOOLCHAIN, which is what lets it run on a disk straight out of bake.
        body = read(path)
        check(f"{path} installs no toolchain", path,
              "brew install" not in body and "winget install" not in body)
        # AND HARD-CODES NO ADDRESS IN A COMMAND. The server is a fact about the run, not about
        # the disk, so a literal typed into an ssh line would bake the CI bridge network into a
        # published image. The literal is allowed ONCE, as the var's documented default - that is
        # the single place a reader looks to change it.
        check(f"{path} names the server once, as a var default", path,
              body.count("172.30.0.1") == 1)

    # THE AUTHENTICATION IS CLIENT-SPECIFIC, AND BOTH CLIENTS FAILED THE OBVIOUS WAY.
    #
    # macOS: `mount_smbfs` as a normal account against Samba is refused while root succeeds, so
    # the mount belongs to a LaunchDaemon and the bootstrap to a LaunchAgent. One credential,
    # two triggers, and the file the agent waits for is the handover.
    #
    # Windows: a CONNECTION - with or without a drive letter - belongs to the logon session that
    # made it, and every `ssh host command` starts a new one. Only the CREDENTIAL survives, so
    # `cmdkey` writes the vault and the launcher addresses the share by UNC path.
    try:
        import hostshare
        mac = hostshare.mount_commands()["macos"]
        win = hostshare.mount_commands()["windows"]
        conf = hostshare.config_text("/tmp/x")

        # ONE ACCOUNT, AND IT HAS NO PASSWORD. That is what lets BOTH clients work with the
        # credential they can actually produce: mount_smbfs sends an empty password and cannot be
        # made to send anything else from a script, and a Windows network logon cannot reach the
        # credential vault at all. Six macOS runs and one Windows run established both halves.
        check("the share account has no password", "lib/hostshare.py",
              hostshare.SMB_PASSWORD == "")
        check("the server accepts an empty password", "lib/hostshare.py",
              "null passwords = yes" in conf)
        # AND THE EMPTY ONE IS STILL CHECKED. `null passwords` must not become "any password".
        # `guest ok = no` plus the real account is what keeps a wrong password refused.
        check("the share is not open to an unknown name", "lib/hostshare.py",
              "guest ok = no" in conf and "map to guest = Never" in conf)

        # BOTH CLIENTS MOUNT THE SAME ACCOUNT WITH NO PASSWORD, spelled for each shell.
        # THE COLON IS THE SPELLING, not a test for an absent password: SMB_PASSWORD is "",
        # which is a substring of everything, so "password not in command" cannot be asserted.
        check("the macos mount uses the no-password form", "lib/hostshare.py",
              f"//{hostshare.SMB_USER}:@" in mac)
        check("the windows mount passes an empty password", "lib/hostshare.py",
              '"" /user:' + hostshare.SMB_USER in win)
        # NOT `nsmb.conf`, WHICH HAS NO `password` KEYWORD AT ALL.
        check("macos does not pretend nsmb.conf can hold a password", "lib/hostshare.py",
              "nsmb.conf" not in mac)
        # NOT A PTY: it was an attempt to smuggle a password to a client that does not need one
        # any more. SUDO STAYS, because mounting a filesystem is privileged - what changed is
        # that it no longer carries a share password, only the login password it always needed.
        check("the macos mount needs no pty", "lib/hostshare.py", "script -q" not in mac)
        check("the macos mount is still privileged", "lib/hostshare.py", "sudo -S" in mac)
        # NOT `cmdkey`: CredWriteW answers ERROR_NO_SUCH_LOGON_SESSION for a network logon, which
        # Win32-OpenSSH hardcodes. Run 42748563969 measured the refusal.
        check("the windows mount does not pretend the vault is reachable", "lib/hostshare.py",
              "cmdkey" not in win)
        # AND NO DRIVE LETTER: it belongs to the logon session that made it (run 42737785611).
        check("the windows mount claims no drive letter", "lib/hostshare.py",
              "net use Z:" not in win)

        # --- THE FOUR HOUSE RULES, EACH ONE CHECKABLE -------------------------------
        #
        # 1. THE ACCOUNT HAS NO PASSWORD. Checked above; repeated here because it is a stated
        #    requirement rather than an implementation detail.
        check("1. the smb account has no password", "lib/hostshare.py",
              hostshare.SMB_PASSWORD == "")

        # 2. THE JOB IS CALLED vm-bootstrap ON BOTH DISKS, with no product name in it.
        check("2. the macOS agent is org.vm.bootstrap", "lib/bootstrap.py",
              bootstrap.MACOS_LABEL == "org.vm.bootstrap")
        check("2. the windows task is vm-bootstrap", "lib/bootstrap.py",
              bootstrap.WINDOWS_TASK == "vm-bootstrap")
        check("2. the mount daemon is org.vm.smbmount", "lib/bootstrap.py",
              bootstrap.MACOS_MOUNT_LABEL == "org.vm.smbmount")
        # AND NOTHING ANYWHERE STILL SAYS THE OLD NAME, which is how a rename half-happens.
        for path in ("lib/bootstrap.py", "lib/steps.py", "lib/launch_selftest.py",
                     "macos/15/steps/share.yaml", "windows/11/steps/share.yaml"):
            body = read(path)
            check(f"2. {path} has no old job name", path,
                  "easydiskbake-bootstrap" not in body
                  and "org.easydiskbake" not in body)

        # 3. THE FALLBACK: NO SCRIPT ON THE SHARE MEANS NOTHING RUNS.
        mac_agent = bootstrap.launcher_body("macos", "172.30.0.1", "guest", "shared")
        win_agent = bootstrap.launcher_body("windows", "172.30.0.1", "guest", "shared", "")
        for family, body in (("macos", mac_agent), ("windows", win_agent)):
            check(f"3. the {family} launcher refuses to run without a script", "lib/bootstrap.py",
                  "nothing to run" in body and "exit 0" in body)
        # AND IT MUST NOT INVENT ONE, so the branch that runs the script is the one that checked.
        check("3. the macOS launcher checks before it runs", "lib/bootstrap.py",
              mac_agent.index('if [ ! -f "$SCRIPT" ]') < mac_agent.index('/bin/sh "$SCRIPT"'))
        check("3. the windows launcher checks before it runs", "lib/bootstrap.py",
              win_agent.index("if (-not $found)") < win_agent.index("-File $Script"))

        # 4. THE PROBE WRITES hello.txt INTO THE SHARE, AND NOTHING ELSE GETS WRITTEN THERE.
        #    A file on the disk would be published; a file on the share cannot be.
        for path, name in (("lib/bootstrap_probe.sh", "sh"),
                           ("lib/bootstrap_probe.ps1", "ps1")):
            body = read(path)
            check(f"4. {path} writes hello.txt into the share", path, "hello.txt" in body)
            check(f"4. {path} writes nothing else into the share", path,
                  body.count("> \"$OUT\"") <= 2 and "hello.txt" in body)
        # AND THE LAUNCHER KEEPS ITS OWN BOOKKEEPING OUT OF THE SHARE, because when the mount
        # failed the share's path is a local directory and anything written there is in the image.
        check("4. the macOS launcher logs outside the share", "lib/bootstrap.py",
              'LOG="$HOME/' in bootstrap.launcher_body("macos", "1.1.1.1", "guest", "shared"))
        check("4. the windows launcher logs outside the share", "lib/bootstrap.py",
              "Join-Path $env:USERPROFILE" in
              bootstrap.launcher_body("windows", "1.1.1.1", "guest", "shared", ""))
        # AND THE PROBE REFUSES TO WRITE WHEN THE SHARE IS NOT REALLY MOUNTED: the mount point is
        # a local directory, and a hello.txt there would ship with the disk.
        probe = read("lib/bootstrap_probe.sh")
        check("4. the probe checks the mount before writing", "lib/bootstrap_probe.sh",
              'mount | grep -q " on $SHARE "' in probe)
        check("4. the probe refuses into the guest, not the share", "lib/bootstrap_probe.sh",
              "bootstrap-refused.txt" in probe
              and probe.index("bootstrap-refused.txt") < probe.index('> "$OUT"'))
    except Exception as exc:                          # noqa: BLE001
        check(f"hostshare imports ({exc})", "lib/hostshare.py", False)

    # AND THE dev STAGES NO LONGER CARRY THE SHARE.
    for path in ("macos/15/steps/dev.yaml", "windows/11/steps/dev.yaml"):
        walk = load_yaml(path)
        names = [s.get("name") for s in (walk.get("steps") or [])]
        check(f"{path} no longer installs the share", path,
              "mount the host share" not in names
              and "install the bootstrap launcher" not in names)

    # --- macos dev: the toolchain, and the one asset Apple will not serve ---------------
    #
    # XCODE IS THE ONLY THING HERE THAT CANNOT BE FETCHED FROM ITS VENDOR. Apple's endpoint
    # answers an unauthenticated request with a redirect:
    #
    #     HTTP/2 302   location: https://developer.apple.com/unauthorized/
    #
    # measured 2026-11-09, and the `ADCDownloadAuth` cookie trick now answers 403 as well. So
    # the file comes from archive.org, and the sha1 is Apple's own number for that build -
    # which is the only thing that makes an archive an identity check rather than a hope.
    mac_dev = read("macos/15/dev.py")
    check("macos dev is driven by dev.yaml", "macos/15/dev.py",
          "steps/dev.yaml" in mac_dev and "stepslib.run_file(" in mac_dev)
    # THE CHECK IS FOR THE COMMAND, NOT THE PHRASE. The file still *explains* why Homebrew is
    # the prerequisite and what a `brew install` would do, and a grep for the words would fail
    # on the prose that documents their absence. A `run(...)`/`subprocess` call with it is the
    # thing that would mean the sequence had crept back.
    check("macos dev does not run brew itself any more", "macos/15/dev.py",
          "subprocess.run" not in mac_dev and "brew install " not in
          "".join(line for line in mac_dev.splitlines()
                  if not line.lstrip().startswith("#")))
    try:
        import xcode_asset
        pin = xcode_asset.PINNED.get("15") or {}
        check("xcode for macOS 15 is pinned with a sha1", "lib/xcode_asset.py",
              len(pin.get("sha1") or "") == 40 and "26.3" in str(pin.get("version")))
        # 26.3 is the LAST release whose `requires` is macOS 15.x; 26.4 needs 26.2. A later
        # pin would download 2.7 GiB and then refuse to install.
        check("the xcode pin is the top of the range macOS 15 can run",
              "lib/xcode_asset.py", str(pin.get("version")) == "26.3")
    except Exception as exc:                          # noqa: BLE001
        check(f"xcode_asset imports ({exc})", "lib/xcode_asset.py", False)
    # THE INSTALLER IS A FILE, NOT A QUOTED LINE: it expands, moves, selects, accepts the
    # license and installs the platforms, and a failure between those leaves a selected Xcode
    # with no SDK - which answers --version and cannot build.
    import os as osmod
    check("the xcode installer is a script in the repository",
          "macos/15/xcode-install.sh",
          osmod.path.isfile(osmod.path.join(REPO, "macos/15/xcode-install.sh")))
    # AND THE PROOF COMPILES: an SDK that lists a version but cannot build is not a toolchain.
    mac_dev_walk = load_yaml("macos/15/steps/dev.yaml")
    names = [s.get("name") for s in (mac_dev_walk.get("steps") or [])]
    check("the dev stage compiles something with swift", "macos/15/steps/dev.yaml",
          "prove swift compiles" in names)

    # XCODE IS FIRST BECAUSE EVERYTHING ELSE IS BUILT ON WHAT IT INSTALLS.
    #
    # The command line tools - git, clang, swift, make - are the ones the .pkg installers below
    # expect to find, and `xcodebuild -runFirstLaunch` is what installs them.
    #
    # AND HOMEBREW IS GONE ENTIRELY, WHICH IS THE LARGER POINT: this disk is x86_64 and
    # Homebrew's installer refuses that -
    #
    #     Homebrew on macOS is only supported on Apple Silicon processors!
    #
    # (install.sh lines 170-173). Run 42644304192 reached it after Xcode had expanded. So the
    # stage installs from the publishers' own Intel packages, and an assertion checks that no
    # `brew install` survives in it.
    order = [n for n in names if n in ("expand and select xcode", "install node",
                                       "install python")]
    check("xcode is installed before the packages that need its tools",
          "macos/15/steps/dev.yaml",
          order[:1] == ["expand and select xcode"])
    check("the macos dev stage does not use homebrew", "macos/15/steps/dev.yaml",
          "brew install" not in "".join(
              line for line in read("macos/15/steps/dev.yaml").splitlines()
              if not line.lstrip().startswith("#")))

    # SUDO OVER SSH HAS NO TTY, SO EVERY PRIVILEGED CALL HAS TO BE HANDED THE PASSWORD.
    #
    #     sudo: no tty present and no askpass program specified
    #     sudo: a password is required
    #
    # lib/macclean.py carries this lesson already (run 42390179102); the Xcode installer is
    # where it would bite next, and it needs it six times.
    xcode_sh = read("macos/15/xcode-install.sh")
    check("the xcode installer upgrades sudo for a tty-less channel",
          "macos/15/xcode-install.sh",
          "sudo -S -p ''" in xcode_sh
          and all(("sudo_" + rest) in xcode_sh or ("sudo_" + rest) == "sudo_"
                  for rest in (" rm -rf", " mv ", " xcode-select", " xcodebuild",
                               " DevToolsSecurity")))
    check("no bare sudo survives in the xcode installer", "macos/15/xcode-install.sh",
          not any(line.strip().startswith("sudo ")
                  for line in xcode_sh.splitlines()))

    # THE PROOF ASKS WHERE A TOOL IS, NOT WHAT VERSION IT ANSWERS.
    #
    # /usr/bin/python3 exists on every macOS and prints a version - it is the one the command
    # line tools install, and its pip refuses to write. A check that only read a version number
    # would pass on a disk whose python cannot install anything.
    toolchain = next((s for s in (mac_dev_walk.get("steps") or [])
                      if s.get("name") == "prove the toolchain"), {})
    body = str(toolchain.get("script") or "")
    # THE EXPECTED PREFIX IS /usr/local NOW, because that is where nodejs.org's and
    # python.org's .pkg installers put things. /usr/bin/python3 still exists on the disk and
    # still answers --version - it is Xcode's, and its pip refuses to write - so the check has
    # to name the prefix rather than accept any answer.
    check("the toolchain proof checks the path, not the version",
          "macos/15/steps/dev.yaml",
          "command -v" in body and "/usr/local/*" in body)

    # --- windows scope and elevation ----------------------------------------------------
    #
    # THREE PACKAGES, THREE DIFFERENT SCOPES IN THEIR MANIFESTS, so the stage names one.
    #
    #   Python.Python.3.13   burn    user AND machine   - winget prefers user without --scope
    #   OpenJS.NodeJS.LTS    wix     machine only, ElevationRequirement: elevatesSelf
    #   Git.Git              inno    user AND machine   - it decides by admin membership
    #
    # Without --scope they land in two different trees, and a launcher that named a path would
    # be right about one of them.
    # ALL SEVEN, because "install to the system" is a property of the stage and not of the
    # three that happened to be ambiguous. The four others were machine-only already - naming
    # the scope says so at the call site instead of leaving a reader to check a manifest.
    for pkg, step_name in (("Microsoft.VisualStudio.2022.BuildTools", "install the build tools"),
                           ("Microsoft.WindowsSDK.10.0.26100", "install the windows sdk"),
                           ("Microsoft.DotNet.SDK.10", "install the dotnet sdk"),
                           ("Microsoft.PowerShell", "install powershell 7"),
                           ("Python.Python.3.13", "install python"),
                           ("OpenJS.NodeJS.LTS", "install node"),
                           ("Git.Git", "install git")):
        step = next((s for s in (win_dev_walk.get("steps") or [])
                     if s.get("name") == step_name), {})
        body = str(step.get("ssh") or step.get("script") or "")
        check(f"{step_name} pins machine scope", "windows/11/steps/dev.yaml",
              pkg in body and "--scope machine" in body)

    # POWERShell IS THE ONE WHERE THE SCOPE PICKS A DIFFERENT FILE, not just a different tree.
    # Its first three installers are msix (per-user Appx, no `pwsh` on the machine PATH) and the
    # wix/MSI variant comes after them - so dropping the flag would leave `pwsh -v` failing
    # while the package is genuinely installed for that account.
    pwsh = next((s for s in (win_dev_walk.get("steps") or [])
                 if s.get("name") == "install powershell 7"), {})
    check("powershell installs the MSI, not the per-user msix",
          "windows/11/steps/dev.yaml",
          "--scope machine" in str(pwsh.get("ssh") or ""))

    # AND IT ONLY WORKS BECAUSE THE ANSWER FILE TURNS UAC OFF. `elevatesSelf` means a UAC prompt
    # when the session is not already elevated, and a prompt over ssh is a dialog nobody can
    # click - a hang, not an error. EnableLUA=false removes the filtered token entirely.
    win_answer = read("windows/11/answer/Autounattend.xml")
    check("the answer file leaves UAC off, which the dev stage depends on",
          "windows/11/answer/Autounattend.xml", "EnableLUA>false" in win_answer)
    check("that dependency is written down where it is used",
          "windows/11/answer/Autounattend.xml", "elevatesSelf" in win_answer)

    # --- the launch self-test: hello.txt, and why it is not in the disk ------------------
    #
    # THE DEV STAGE CANNOT PROVE THE LAUNCHER, for two structural reasons: the launcher fires at
    # LOGIN and dev is already logged in, and anything dev wrote to prove it would land on the
    # published disk. So a second launch does it, and ~/hello.txt dies with that launch.
    for wf, family in (("macos15", "macos"), ("windows-11", "windows")):
        text = read(f".github/workflows/{wf}.yml")
        check(f"{wf} has a launch job that checks the bootstrap",
              f".github/workflows/{wf}.yml",
              "lib/launch_selftest.py" in text and f"--family {family}" in text)
    selftest = read("lib/launch_selftest.py")
    check("the self-test copies the disk instead of opening the artifact",
          "lib/launch_selftest.py", "shutil.copyfile" in read("lib/boot_machine.py"))
    check("the self-test reads ~/hello.txt back over ssh",
          "lib/launch_selftest.py", "hello.txt" in selftest)
    # AND IT NEVER PUBLISHES. A launch job that pushed a tag would put the probe's output on a
    # disk somebody boots.
    mac_wf = read(".github/workflows/macos15.yml")
    launch_block = mac_wf[mac_wf.index("  launch_dev:"):]
    check("the macos launch job publishes nothing", ".github/workflows/macos15.yml",
          "oci-push" not in launch_block)

    # THE PROBE IS THE TEST, so it has to assert the things that can fail separately: py thon
    # ran, the share was mounted, and the account is the one that logged in.
    probe = read("lib/bootstrap_probe.py")
    check("the probe reports whether the share was readable", "lib/bootstrap_probe.py",
          "share=" in probe and "readable" in probe)
    # getpass, NOT USER: a launchd agent and a Task Scheduler task run with a smaller
    # environment, and `USER` is one of the things that can be absent.
    check("the probe finds the user without relying on $USER", "lib/bootstrap_probe.py",
          "getpass" in probe)

    # --- imports that resolve to the wrong directory -----------------------------------
    #
    # `from lib import machine` asks lib/ for a module called machine. It is not there - it is
    # macos/15/machine.py, beside the stage - and the failure is
    #
    #     ImportError: cannot import name 'machine' from 'lib' (unknown location)
    #
    # which arrives AFTER a 16 GiB disk has been pulled, so the whole stage is spent on an
    # import. Run 42624293500 lost its dev stage to exactly this.
    for path in ("macos/15/dev.py", "lib/boot_machine.py"):
        try:
            body = read(path)
        except Exception:                                 # noqa: BLE001
            continue
        # CODE, NOT PROSE. Both files explain this mistake in a comment - that is the point of
        # the comment - so a check for the words would fail on the documentation that warns
        # about them.
        code = "\n".join(line for line in body.splitlines()
                         if not line.lstrip().startswith("#"))
        check(f"{path}: does not ask lib for macos/15's modules", path,
              "from lib import machine" not in code
              and "from lib import opencore" not in code)

    # --- the xcode installer: one name, in the repository and in the call ---------------
    #
    # IT WAS TWO NAMES AND THAT COST TWO RUNS. The module copied the file into the guest as
    # `install-xcode.sh` while the file on disk is `xcode-install.sh`, and the YAML then ran
    # `bash ~/install-xcode.sh`:
    #
    #     42634570965   copying .../steps/install-xcode.sh   -> 255, no such file on the host
    #     42636875683   the guest has all 2738 MiB, then
    #                   bash: /Users/docker/install-xcode.sh: No such file or directory
    #
    # Each fix moved the mismatch one step later. This asserts the three places agree.
    try:
        # THE EXTENSION LIVES BESIDE THE WALKTHROUGH IT SERVES, not in lib/ - see the note in
        # macos/15/dev.py about `from lib import machine`. The path is added here because this
        # checker runs from the repository root.
        import sys as _sys
        _ext = osmod.path.join(REPO, "macos", "15", "steps")
        if _ext not in _sys.path:
            _sys.path.insert(0, _ext)
        import macos_dev_asset
        name = macos_dev_asset.SCRIPT_NAME
    except Exception as exc:                          # noqa: BLE001
        check(f"macos_dev_asset imports ({exc})", "macos/15/steps/macos_dev_asset.py", False)
        name = ""
    check("the xcode installer exists under the name the module copies",
          "macos/15/xcode-install.sh",
          bool(name) and osmod.path.isfile(osmod.path.join(REPO, "macos", "15", name)))
    dev_walk_text = read("macos/15/steps/dev.yaml")
    check("the walkthrough runs the installer under that name", "macos/15/steps/dev.yaml",
          bool(name) and f"/{name}" in dev_walk_text)

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
