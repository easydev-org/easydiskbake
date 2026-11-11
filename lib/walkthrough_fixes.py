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


def _dnsmasq_argv_is_valid() -> bool:
    """Assemble `net.up()`'s dnsmasq arguments and ask dnsmasq whether it would accept them.

    A BAD OPTION NAME DOES NOT FAIL A CHECK, IT FAILS THE STAGE - so the check has to be the
    binary's own parser. Skipped (and reported) when dnsmasq is not installed here, because a
    repository that cannot run the parser should say so rather than claim a pass.

    Run 42909208639 is why this exists: `--leasefile=` instead of `--dhcp-leasefile=` produced
    `dnsmasq: bad command line options: try --help`, and the whole vm-configure stage went with it.
    """
    import shutil
    import subprocess as sp

    binary = shutil.which("dnsmasq") or "/usr/sbin/dnsmasq"
    if not os.path.isfile(binary):
        print("  (no dnsmasq here; the command line was not verified against its parser)")
        return True
    sys.path.insert(0, os.path.join(REPO, "lib"))
    import net  # noqa: PLC0415

    cfg = (
        f"dhcp-authoritative;dhcp-range={net.GUEST_IP},{net.GUEST_IP};"
        f"dhcp-option=option:netmask,{net.MASK};"
        f"dhcp-option=option:router,{net.BRIDGE_IP};"
        f"dhcp-option=option:dns-server,{net.BRIDGE_IP};"
        f"dhcp-option=option:mtu,{net.MTU};"
        f"interface={net.BRIDGE};bind-interfaces;no-ping;filter-AAAA;"
        f"pid-file=/var/run/dnsmasq.vm.pid;log-facility=/var/log/dnsmasq.vm.log;"
        f"dhcp-leasefile={net.LEASE_FILE};"
    )
    argv = [binary, "--conf-file=/dev/null", "--test"]
    argv += ["--" + opt for opt in cfg.rstrip(";").split(";")]
    result = sp.run(argv, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"  dnsmasq rejected the command line: "
              f"{(result.stderr or result.stdout).strip()[:200]}")
    return result.returncode == 0


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
    # A `python:` STEP NEEDS A SCREEN UNLESS IT SAYS OTHERWISE, AND THERE IS ONE PLACE THAT SAYS SO.
    #
    # The default is conservative because most extensions drive the GUI - and forgetting it was
    # a crash (`HeadlessDriver` has no `key`). But macos/15/steps/dev.yaml's two extensions fetch
    # and copy a file over ssh, never touching the screen, and being told they needed a QMP
    # socket is what stopped run 42632192846 before its first step. `python_screen: false` is the
    # declaration.
    #
    # IT USED TO SAY "both checks", because there were two copies of the expression - `run_file`
    # and the CLI - and a walkthrough could pass one and die in the other. They had already
    # drifted (only one knew about `until_dark`, `wait_for_change` or `sweep`) and BOTH were
    # missing `desktop:` and `screenshot:`, which is what gave android/blissos16/steps/launch.yaml
    # a headless driver for a file whose second step is "wait for a desktop". One function now,
    # `_needs_screen`, called from both places - so the check is that the expression appears ONCE
    # and that both call sites use it.
    check("python: needs a screen unless the file says it does not (one answer, two callers)",
          "lib/steps.py",
          steps_src.count('s["python"] and s.get("python_screen") is not False') == 1
          and steps_src.count("needs_screen = _needs_screen(steps)") == 2)
    # AND THE THREE STEPS THAT READ THE FRAMEBUFFER WITHOUT CLICKING OR TYPING ARE IN IT.
    check("desktop: and screenshot: need a screen too", "lib/steps.py",
          'or s["desktop"] or s["screenshot"]' in steps_src)
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

    # AND THE STAGE LEAVES BOTH OF THOSE TO THE FILE. The desktop stage used to start qemu, type
    # the password at the login window and run a wallpaper loop of its own - three things that
    # are now `boot:`, the epilogue's three login steps and `python: ssh_panes:wait_for_desktop`.
    # The failure this pins is the one that keeps happening: a copy of a measurement that nobody
    # updated when the original moved.
    desktop_src = read("macos/15/desktop.py")
    check("the desktop stage does not start qemu itself", "macos/15/desktop.py",
          "vm.Guest(" not in desktop_src and "machine.TARGET_DRIVE" not in desktop_src)
    check("the desktop stage does not log in by itself", "macos/15/desktop.py",
          "def _type_login_password" not in desktop_src)
    check("the desktop stage does not wait for a desktop itself", "macos/15/desktop.py",
          "def wait_for_desktop" not in desktop_src)
    check("the desktop stage boots through its file", "macos/15/desktop.py",
          "MachineSession(" in desktop_src and "args.shutdown" in desktop_src)

    # AND THE SETUP STAGE, WHICH IS THE LAST OF THE FOUR. Its boot carries one extra thing the
    # others do not: the recovery image ON A USB DEVICE, because macOS installs FROM removable
    # media and the disk it installs TO is not the disk it boots FROM.
    setup_walk = load_yaml("macos/15/steps/setup.yaml")
    setup_names = [s.get("name") for s in (setup_walk.get("steps") or [])]
    check("the setup stage boots its own machine", "macos/15/steps/setup.yaml",
          setup_names and setup_names[0] == "boot the recovery image")
    check("the setup stage stops its own machine", "macos/15/steps/setup.yaml",
          setup_names and setup_names[-1] == "stop the machine")
    bake_src = read("macos/15/bake.py")
    check("the setup stage passes the recovery image to the boot", "macos/15/bake.py",
          '"recovery": recovery' in bake_src)
    check("the setup stage does not start qemu itself", "macos/15/bake.py",
          "vm.Guest(" not in bake_src and "machine.make_guest" not in bake_src)
    check("the setup stage does not wait for the wizard twice", "macos/15/bake.py",
          "def wait_for_assistant" not in bake_src)
    # AND THE RECOVERY IMAGE IS AN OPTION, NOT WHAT EVERY macOS GUEST GETS. An extra USB disk is
    # one more row in Disk Utility, and the erase step chooses its target by CAPACITY.
    vm_sess2 = read("lib/vm_session.py")
    # AND EVERY macOS STAGE TELLS THE SESSION IT IS A MACOS GUEST. This is not cosmetic:
    # `family` is what picks the qga transport (isa-serial for macOS, virtio for everyone else)
    # and what selects the shutdown command. macos/15/bake.py passed `self.args.release` - the
    # string "15" - which would have started a guest the session did not recognise.
    for path in ("macos/15/bake.py", "macos/15/desktop.py", "macos/15/ssh.py",
                 "macos/15/dev.py", "macos/15/vm_configure.py"):
        body = read(path)
        if "MachineSession(" not in body:
            continue
        check(f"{path} says its guest is macos", path,
              '"macos", ' in body.split("MachineSession(", 1)[1].split(")")[0])

    check("the recovery image is a boot option", "lib/vm_session.py",
          'recovery = (boot_options or {}).get("recovery") or ""' in vm_sess2)

    gates = seq.get("waits_for") or []
    check("waits_for lists the panes, so a resumed disk is not stuck",
          "macos/15/steps/sequoia.yaml", len(gates) >= 10)

    # AND THE PANE FILE HAS A PREAMBLE AND AN EPILOGUE, which is what makes it a stage rather
    # than half of one: the wizard is panes, and the machine it runs on has to be started before
    # the first look and stopped after the last. See lib/steps.py's Walk.__init__.
    import steps as stepsmod

    seq_walk = stepsmod.Walk(seq, path="macos/15/steps/sequoia.yaml")
    check("the desktop pane file boots its own machine", "macos/15/steps/sequoia.yaml",
          [s.name for s in seq_walk.preamble] == ["boot the disk"])
    check("the desktop pane file stops its own machine", "macos/15/steps/sequoia.yaml",
          seq_walk.epilogue and seq_walk.epilogue[-1]["shutdown"] is not None)
    # THE VERDICT STEPS ARE THE ONES THAT MAY BE SKIPPED ON A FAILED WALK, and the shutdown is
    # not one of them: stopping the machine has to happen on every exit.
    verdict = [s.name for s in seq_walk.epilogue if s["skip_when_unfinished"]]
    check("the desktop's login and verdict are skipped on a failed walk",
          "macos/15/steps/sequoia.yaml",
          "click the password field" in verdict and "wait for the desktop" in verdict)
    check("the shutdown is never skipped", "macos/15/steps/sequoia.yaml",
          all(not s["skip_when_unfinished"] for s in seq_walk.epilogue if s["shutdown"]))

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
    # --- THE FIRST STAGE WITH NO PYTHON BESIDE IT AT ALL -------------------------------
    #
    # android/blissos16 is the first system in this repository whose whole stage is its files:
    #
    #     steps/install.yaml   make the disk, boot the ISO's installer, answer ten panes,
    #                          place the fallback loader, read the ESP, publish
    #     steps/launch.yaml    boot the installed disk with NO -kernel and NO cdrom, and wait
    #                          for Android's own UI
    #
    # There is no install.py and no launch.py. What used to be in them is either a step
    # (`disk:` with `disk_gb:`/`disk_gpt:`, `boot:` with `os: android`, `compress:`) or a
    # `python:` EXTENSION for the one act that is not an action on a screen - editing a FAT
    # filesystem inside a partition of a qcow2.
    #
    # IT IS ALSO THE FIRST STAGE THAT PROVES `os:` IS COMPLETE. Before this, `os: android`
    # needed two firmware keys from a Python caller (`opts["firmware_code"]`), which made the
    # word a half-statement; lib/vm_session.py now finds OVMF and makes a fresh variable store
    # itself, the same way `os: windows` and `os: linux` already did.
    check("the blissos install stage is a workflow file, with no program beside it",
          "android/blissos16",
          not os.path.exists(os.path.join(REPO, "android/blissos16/install.py")))
    check("the blissos launch stage is a workflow file, with no program beside it",
          "android/blissos16",
          not os.path.exists(os.path.join(REPO, "android/blissos16/launch.py")))
    bliss_install = read("android/blissos16/steps/install.yaml")
    bliss_launch = read("android/blissos16/steps/launch.yaml")
    # IT MAKES ITS OWN DISK. Before `disk_gb:` every install-onto-a-disk stage wrote six lines of
    # Python for this, and none of them was about a screen.
    check("the blissos install makes the disk it installs to", "android/blissos16/steps/install.yaml",
          "disk_gb:" in bliss_install and "disk_gpt: true" in bliss_install)
    # AND NAMES ONE MACHINE IN ONE WORD, for both of its shapes.
    check("both blissos stages name the machine with os:",
          "android/blissos16/steps/*.yaml",
          "    os: android" in bliss_install and "    os: android" in bliss_launch)
    # THE LAUNCH DOES NOT HAND THE GUEST A KERNEL, BECAUSE THE POINT IS THAT THE DISK BOOTS ITSELF.
    #
    # THE CHECK IS ON WHAT THE FILE ASKS FOR, NOT ON THE WORD, and that distinction is the whole
    # check: the file's own comments say "no -kernel" several times, so a `"kernel" not in body`
    # test fails on the explanation of the property it is checking. What matters is that the step
    # does not name an ISO - `--boot-iso` is what puts `os: android` into its installer shape and
    # passes `-kernel`/`-initrd` - and that no step hands qemu one directly.
    check("the blissos launch does not hand the guest a kernel",
          "android/blissos16/steps/launch.yaml",
          "boot_iso" not in bliss_launch
          and "kernel_append" not in bliss_launch
          and "    boot: true\n    os: android" in bliss_launch)
    # AND THE VERDICT IS THE SHARED JUDGE, NOT A WORD LIST IN A STAGE. `desktop: android` is
    # lib/desktop.py's `is_android_desktop`, which is where the greyscale stretch lives.
    check("the blissos launch waits for a desktop, not for words it typed itself",
          "android/blissos16/steps/launch.yaml", "    desktop: android" in bliss_launch)
    check("lib/desktop.py has an android judge", "lib/desktop.py",
          '"android": is_android_desktop' in read("lib/desktop.py"))
    # AND THE FLAG CHECKER READS `easyvm`, OR THE WORKFLOW LINE ABOVE IS UNCHECKED.
    #
    # `lib/flagcheck.py` compared `python3 X.py` against X.py's own argparse, and `easyvm` has
    # none: it hands the whole list to lib/steps.py, which is where every option lives. A workflow
    # that moved to `easyvm` therefore had its flags checked by nothing - measured before this was
    # added: 137 calls checked, and the two `./easyvm` lines in windows-11.yml and blissos16.yml
    # were not among them.
    flag_src = read("lib/flagcheck.py")
    check("the flag checker reads easyvm's arguments", "lib/flagcheck.py",
          "EASYVM = re.compile" in flag_src and "EASYVM_PARSER" in flag_src)
    # AND THE WORKFLOW RUNS THE FILES. `easyvm` is the one supported entry point.
    bliss_wf = read(".github/workflows/blissos16.yml")
    check("the blissos workflow runs its workflow files",
          ".github/workflows/blissos16.yml",
          "./easyvm android/blissos16/steps/install.yaml" in bliss_wf
          and "./easyvm android/blissos16/steps/launch.yaml" in bliss_wf)

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

    # --- ONE WAY TO START A GUEST ---------------------------------------------------------
    #
    # THIS IS THE FIX FOR THE CLEAN THAT TIMED OUT. Three places started a guest and only one of
    # them knew the disk now expects a share on the bridge, so a clean booted a machine whose
    # launchd job retried a mount that could never work and never let sshd answer (run 42756290022).
    # The lesson is structural: one implementation, and the environment around the machine decided
    # with the machine.
    try:
        import vm_session
        check("one module starts a guest", "lib/vm_session.py",
              callable(getattr(vm_session, "start", None)))
        # AND EVERY CALLER USES IT, so a new stage cannot quietly build its own machine.
        for path in ("lib/clean_step.py", "lib/boot_machine.py"):
            body = read(path)
            check(f"{path} starts guests through vm_session", path,
                  "vm_session.start(" in body and "vm.Guest(" not in body)
        # THE SHARE COMES UP BEFORE THE MACHINE. A disk carrying the mount daemon tries to mount
        # within seconds of launchd starting, so a server that arrives second is a retry.
        src = read("lib/vm_session.py")
        check("the share is served before the guest boots", "lib/vm_session.py",
              src.index("hostshare.start(") < src.index("_start_machine("))
        # AND IT IS TORN DOWN WITH THE GUEST, because a leftover smbd holds 172.30.0.1:445 and the
        # next stage in this container would find it serving the wrong directory.
        check("the share is stopped with the guest", "lib/vm_session.py",
              "hostshare.stop()" in src)
        # CLEANING MUST NOT COPY, and the self-test must. One call, two policies, both stated.
        check("clean modifies the disk in place", "lib/clean_step.py",
              "copy=False" in read("lib/clean_step.py"))
        check("the self-test still works on a copy", "lib/boot_machine.py",
              "copy=True" in read("lib/boot_machine.py"))
        # macOS DOES NOT SHUT DOWN ON AN ACPI POWER BUTTON, and waiting longer cannot fix that.
        #
        # It receives `system_powerdown`, puts up the "Restart / Sleep / Cancel / Shut Down"
        # dialog and waits for an answer. Measured: five minutes and then
        #
        #     [vm] the guest did not power off within 300s; killing it      (run 42762244256)
        #
        # on a guest that had booted in thirty seconds. So the shutdown is a COMMAND over ssh, and
        # ACPI is the fallback for a guest whose ssh is gone.
        session_src = read("lib/vm_session.py")
        check("macos is shut down with a command, not the power button", "lib/vm_session.py",
              "shutdown -h now" in session_src)
        # AND A GUEST ON AN OVERLAY SKIPS THE WHOLE CONVERSATION. Its disk is a throwaway child
        # of a read-only parent, so a kill destroys nothing - and both polite paths have failure
        # modes here (macOS ignores the power button; a loaded guest outlasts any timeout), each
        # of which costs minutes. The careful path stays for a guest that owns the real disk.
        check("an overlay guest is killed, not asked", "lib/vm_session.py",
              "overlay is not None" in session_src and "acpi_first=False" in session_src)
        check("acpi stays the fallback without an overlay", "lib/vm_session.py",
              session_src.index("shutdown_command(") < session_src.rindex("guest.stop("))
        check("the power-button timeout is still generous", "lib/vm.py",
              "acpi_timeout: int = 300" in read("lib/vm.py"))
        # AND THE PROCESS IS REAPED BEFORE THE DISK IS READ. `poll()` says the process left the
        # run queue, not that its descriptors are closed - and the caller's next act is to open
        # the qcow2. Run 42763433967 printed "=== the disk ===" and then died with no verdict.
        check("the guest is reaped before the disk is read", "lib/vm.py",
              "self.proc.wait()" in read("lib/vm.py"))
        # AND A READ THAT CANNOT FINISH SAYS SO. qemu-io blocks forever on an image that still has
        # a writer; without a timeout the job is killed and the log simply stops.
        check("a disk read cannot hang forever", "lib/smoke.py",
              "timeout=120" in read("lib/smoke.py"))
        # AND stop() MUST NOT BE ABLE TO KILL ITS OWN CALLER.
        #
        # Run 42766989537 printed `=== SIGNALLED: SIGTERM (15) ===` two milliseconds after
        # `the guest powered off` - the exact moment stop() runs - and stop() was the only code
        # sending a signal. Three things now stand between a wrong pidfile and the caller:
        #
        #     the target is checked to be smbd before it is written down
        #     the target is checked again before it is signalled
        #     the signal is sent by a `kill` subprocess, not by os.kill in this process
        hostshare_src = read("lib/hostshare.py")
        check("the recorded pid is proven to be smbd", "lib/hostshare.py",
              'REFUSING TO RECORD A PID THAT IS NOT smbd' in hostshare_src)
        check("a stale pid is not signalled", "lib/hostshare.py",
              'is not smbd' in hostshare_src)
        # THE KILL RUNS IN A CHILD, so a wrong pid cannot take this process with it.
        check("the signal is sent by a subprocess", "lib/hostshare.py",
              'subprocess.run(["kill"' in hostshare_src)
        # AND A STAGE THAT IS ABOUT TO EXIT DOES NOT STOP THE SHARE AT ALL.
        #
        # The container ends with the step, so the daemon goes with it. Stopping it explicitly
        # bought nothing and cost five runs: the SIGTERM arrived inside hostshare.stop(), one
        # line after `kill` returned, and seven experiments found no sender inside this process.
        # macos/15/ssh.py never calls it and never had the problem.
        check("a stage that exits leaves the share to the container", "macos/15/vm_configure.py",
              "share=False" in read("macos/15/vm_configure.py"))
        # AND THE SIGNAL NAMES ITSELF, so the next occurrence is not a decode exercise.
        check("a signal is reported by name", "macos/15/vm_configure.py",
              "SIGNALLED:" in read("macos/15/vm_configure.py"))
        # AND EVERY STAGE THAT OWNS A GUEST GOES THROUGH THE ONE STOP, so a new stage cannot
        # inherit the sixty-second kill.
        #
        # macos/15/dev.py IS NOT IN THIS LIST ANY MORE and that is the change, not an omission:
        # its machine is started and stopped by `boot:`/`shutdown:` in dev.yaml, and the stage
        # that kept a stop() of its own would have been the fourth place that had to know how a
        # guest ends. `shutdown:` reaches vm_session.stop through lib/steps.py's MachineSession.
        for path in ("macos/15/vm_configure.py", "lib/clean_step.py"):
            check(f"{path} stops through vm_session", path,
                  "vm_session.stop(" in read(path))
        # AND THE SSH STAGE'S MACHINE IS THE FILE'S, TOO - asserted by what is ABSENT, because a
        # stop() that reappears is the regression and a grep for the step cannot see one.
        for path in ("macos/15/ssh.py", "macos/15/dev.py"):
            body = read(path)
            check(f"{path} leaves the machine to its file", path,
                  "vm_session.stop(" not in body and "guest.stop(" not in body)
            check(f"{path} boots through its file", path,
                  "boot: true" in read(os.path.join(os.path.dirname(path), "steps",
                                                    "ssh.yaml" if "ssh.py" in path else "dev.yaml")))
    except Exception as exc:                          # noqa: BLE001
        check(f"vm_session imports ({exc})", "lib/vm_session.py", False)

    # --- THE GUEST AGENT, WHICH IS HOW A GUEST SHUTS DOWN WITHOUT A DIALOG -----------------
    #
    # macOS answers an ACPI power button with "Restart / Sleep / Cancel / Shut Down" and waits
    # for a click, which no headless guest can give (runs 42762244256, 42778795463). The agent is
    # the supported route: the request goes to a process inside the guest that calls the system's
    # own shutdown, and it needs no ssh - so it also works on a guest whose sshd never came up.
    try:
        import qga
        check("the guest agent client exists", "lib/qga.py",
              all(hasattr(qga.Agent, m) for m in ("sync", "ping", "wait", "shutdown")))
        # THE SHUTDOWN REPLY IS EXPECTED TO DIE. The guest powers off while its agent is still
        # writing the answer, so raising there would report every success as a failure.
        check("a shutdown whose reply never arrives is not an error", "lib/qga.py",
              "except (AgentError, OSError):\n            pass" in read("lib/qga.py"))
        # AND THE GUEST GETS A DEVICE TO ANSWER ON.
        vm_src = read("lib/vm.py")
        check("the machine can carry an agent channel", "lib/vm.py",
              "qga_socket" in vm_src and "org.qemu.guest_agent.0" in vm_src)
        # TWO TRANSPORTS, BECAUSE THE GUEST-SIDE AGENTS DISAGREE. Windows qemu-ga wants
        # virtio-serial; the macOS agent Apple ships never loads under QEMU and the third-party
        # one moved to ISA serial.
        check("macos gets isa-serial and windows virtio-serial", "lib/vm.py",
              "isa-serial" in vm_src and "virtserialport" in vm_src)
        # THE ORDER: AGENT, THEN SSH, THEN THE POWER BUTTON - and the power button is the only one
        # of the three that cannot work on macOS.
        vs = read("lib/vm_session.py")
        check("the guest agent is tried before ssh", "lib/vm_session.py",
              vs.index("_shutdown_via_agent") < vs.index("SHUTDOWN_COMMAND.get"))
        # AND THE STAGE THAT INSTALLS IT IS NAMED FOR THE MACHINE, NOT THE SHARE.
        #
        # WINDOWS IS THE EXCEPTION AND IT IS A MOVE, NOT A RELAXATION: its agent is installed by
        # Setup from the answer file (see the checks below), because a disk whose FIRST boot has
        # no agent cannot be shut down by one. So the shared checks here are the two that are
        # still true of both files, and the per-platform install lives where it happens.
        for path in ("macos/15/steps/vm-configure.yaml", "windows/11/steps/vm-configure.yaml"):
            walk = load_yaml(path)
            names = [s.get("name") for s in (walk.get("steps") or [])]
            check(f"{path} proves the agent answers", path,
                  "prove the guest agent answers" in names)
            check(f"{path} serves the share", path, "serve the host share" in names)
            check(f"{path} mounts the share", path, "mount the host share" in names)
            # THE HOOK BECAME SEVERAL STEPS, so this follows what they do rather than one entry
            # that used to do all of it.
            check(f"{path} still installs the login hook", path,
                  any("launcher" in (n or "") for n in names)
                  and any(("login agent" in (n or "") or "logon task" in (n or ""))
                          for n in names))
    except Exception as exc:                          # noqa: BLE001
        check(f"qga imports ({exc})", "lib/qga.py", False)

    # --- THE WINDOWS INSTALLER IS A WiX BURN BUNDLE, AND ITS SILENT FLAG IS `/quiet` ------
    #
    # The first version passed `/S /v "/qn /norestart"` - NSIS's flag followed by an MSI property
    # string. Burn does not recognise `/S`, so it showed its window and waited; `-Wait` waited
    # with it; and an `ssh:` step prints nothing until its command returns, so the stage sat for
    # ten minutes with no output at all (run 42797556702).
    #
    # AND THE BUNDLE ITSELF IS GONE. Its Burn bootstrapper carries a NetFxChainer, and under
    # /quiet it never returned on this guest - two runs of ten and twelve minutes with no output,
    # and the command never finished, so not even a deadline could report (42797556702,
    # 42800603925). The MSI it would have installed comes out of the virtio-win ISO the bake
    # already pulls, and msiexec finishes.
    #
    # AND IT IS NOW SETUP THAT RUNS IT, NOT THIS STAGE. The install moved to the answer file's
    # specialize pass, so the disk's FIRST boot already has an agent: a shutdown by that route was
    # impossible on exactly the disk that had never been configured, which is the disk a
    # verification stage is given. What this stage owes is the proof, and a proof is only worth
    # something if the step that produces it is still there.
    win_conf = read("windows/11/steps/vm-configure.yaml")
    win_answer = read("windows/11/answer/Autounattend.xml")
    # IT IS NOT INSTALLED BY SETUP ANY MORE - see "the answer file installs no guest agent"
    # below for the three runs that decided it.
    check("the windows agent install asks for no bundle", "windows/11/answer/Autounattend.xml",
          "guest-tools.exe" not in win_answer)
    # THE DRIVE LETTER IS ENUMERATED, NOT GUESSED. Three CD-ROMs are attached and their letters
    # follow the firmware's enumeration; a hard-coded D: would work until it did not, and the
    # failure would be a silent disk with no agent.
    #
    # THIS ASSERTION USED TO PIN `start /wait`. IT WAS ASSERTING THE BUG. `start /wait` blocks
    # until the child exits and has no deadline, so an msiexec that does not return holds the
    # specialize pass forever - which is run 42822577507: the install finished, the machine
    # rebooted into it (Boot000A "Windows Boot Manager" on the console), and no desktop appeared
    # for the remaining 85 minutes. A previous run of the same stage without this pass reached
    # its desktop in 1192s.
    # AND WITH NO STEP LEFT IN THE ANSWER FILE, THERE IS NO DRIVE LETTER TO FIND. The
    # enumeration that used to be here is now vm-configure's `fetch_from_iso`, which reads the
    # ISO on the host where there are no drive letters at all.
    # AND IT CANNOT HOLD THE MACHINE. A synchronous installer is a pass that can never end.
    # THE DEADLINE AND THE STATUS FILE MOVED WITH THE STEP, to vm-configure's pnputil and
    # msiexec. They are asserted there, below.
    # AND vioserial IS NOT IN $WinPEDriver$, BECAUSE THAT IS WHAT BROKE A BAKE.
    #
    # The list of drivers Setup stages into the image it installs has to be the drivers the
    # INSTALL needs. vioserial is the guest agent's device - it is needed by the finished system,
    # and `pnputil` in specialize is where a running system gets it. Run 42839116205 is the
    # measurement: with vioserial in that list, Setup rebooted "Windows Boot Manager" eight times
    # and died at 54% with "The computer restarted unexpectedly"; the run before the driver
    # existed booted it zero times and reached a desktop in 1192s.
    winiso2 = read("lib/winiso.py")
    winpe = winiso2.split("WINPE_DRIVERS = ", 1)[1].split("\n", 1)[0]
    check("vioserial is not a windowsPE driver", "lib/winiso.py",
          "vioserial" not in winpe)
    check("the agent's driver still travels on the answer cd", "lib/winiso.py",
          "extract_driver_for_agent" in winiso2 and 'AGENT_DRIVER = "vioserial"' in winiso2)
    # AND THE ANSWER FILE INSTALLS NEITHER THE AGENT NOR ITS DRIVER, BECAUSE THE DEVICE IS THE
    # PROBLEM. Three runs say so, and the last two differ only in where the driver was staged:
    #
    #     42839116205   vioserial in $WinPEDriver$   "restarted unexpectedly" at ~1450s
    #     42858732386   vioserial in agent/          "restarted unexpectedly" at ~1400s
    #     42209791462   no virtio-serial device      desktop at 1192s, job 34 minutes
    #
    # The console says it in one number: 985 bytes of UEFI text repeated for 59 heartbeats on
    # the failing runs, and 3 on the one that reached a desktop.
    check("the answer file installs no guest agent", "windows/11/answer/Autounattend.xml",
          "msiexec" not in win_answer and "pnputil" not in win_answer)
    check("the windows bake has no agent channel", "windows/11/bake.py",
          "qga_socket=" not in read("windows/11/bake.py"))

    # AND NO RunSynchronousCommand ANYWHERE MAY WAIT FOREVER.
    #
    # specialize runs BEFORE the desktop and before any of this repository's deadlines exist:
    # the bake's own --timeout is watching the screen, and a pass that never returns is not a
    # screen that never changes. So the one unbounded construct that matters here is a
    # synchronous wait, and the answer file may only use one with a deadline on it.
    import re as _re
    sync_paths = _re.findall(r"<Path>(.*?)</Path>", win_answer, _re.S)
    blocking = [p for p in sync_paths
                if "start /wait" in p or ("msiexec" in p and "WaitForExit(" not in p)]
    check("no answer-file pass waits without a deadline", "windows/11/answer/Autounattend.xml",
          not blocking)

    # AND THE STAGE SAYS SO RATHER THAN QUIETLY REINSTALLING IT: one path, and a re-run of this
    # stage must not become a second one.
    check("vm-configure installs the agent", "windows/11/steps/vm-configure.yaml",
          "install the guest agent" in win_conf)
    check("vm-configure installs the driver first", "windows/11/steps/vm-configure.yaml",
          win_conf.index("install the driver the agent talks on")
          < win_conf.index("install the guest agent"))
    check("the windows stage still proves the agent", "windows/11/steps/vm-configure.yaml",
          "prove the guest agent answers" in win_conf)
    # AND THE MSI COMES OUT OF THE virtio-win ISO, not from a second URL that has to be kept in
    # step - the extraction now happens in the module that BUILDS the answer CD, which is the
    # only place that can put the file where the answer file's path points.
    winiso_src = read("lib/winiso.py")
    check("the answer iso extracts the agent from the virtio-win iso", "lib/winiso.py",
          "guest-agent/qemu-ga-x86_64.msi" in winiso_src and "extract_agent_msi" in winiso_src)
    # AND THE BAKE PUTS IT ON THE ANSWER DISC, which is the half that makes the specialize step
    # able to find it at all.
    check("the answer iso carries the agent msi", "lib/winiso.py",
          "AGENT_MSI_MEMBER" in winiso_src)
    check("the answer iso puts it where the answer file looks", "lib/winiso.py",
          'AGENT_DIR = "agent"' in winiso_src)
    # AND THE HTTP HALF OF THAT FETCH IS GONE, so a Windows stage cannot reinstall what Setup
    # already installed. One path, and the module says so.
    asset_src = read("lib/vm_configure_asset.py")
    check("the windows agent comes out of the ISO, not a second URL",
          "lib/vm_configure_asset.py",
          "fetch_from_iso" in asset_src and '"qemu-ga-msi"' in asset_src
          and "fetch_driver" in asset_src)
    # AND AN ISO WITHOUT THE AGENT IS REFUSED, not silently accepted. The answer file installs
    # the file by path, so a drivers-only repack produces a Windows disk that boots, answers
    # nothing on its agent port, and only says so two stages later. Run 42804093226 is that
    # failure: a 56 MiB ISO, a specialize step pointing at a file that was not there, and a
    # `7z` extraction that reported "no files to process" into a log nobody read.
    check("an iso with no agent is a hard error", "lib/winiso.py",
          "has no {AGENT_MSI_MEMBER}" in winiso_src)
    # AND THE BAKE GIVES THE GUEST THE PORT THE AGENT TALKS ON. The answer file installs it in
    # the same boot, so a machine without the device installs a service it cannot start.
    bake_src = read("windows/11/bake.py")
    # AND THE BAKE HAS NONE: see the note beside the Shape for the three runs that prove a
    # virtio-serial device during Setup is what stopped the install.
    check("the windows bake carries no agent channel", "windows/11/bake.py",
          "qga_socket=" not in bake_src)

    # --- A STEP DECLARES WHAT IT NEEDS, AND A MISSING CAPABILITY STOPS THE RUN -----------
    #
    # `vm_session.stop()` used to try the agent, then ssh, then the power button, then kill, each
    # falling silently into the next. On macOS the third one could never work - the power button
    # opens a dialog - so a "graceful" shutdown was a 300-second wait and a kill that reported
    # itself as clean. A capability that is declared, and checked, and refused, is the fix.
    try:
        import capabilities
        # A MISSING CAPABILITY NAMES ITSELF rather than becoming a timeout somewhere else.
        ok, why = capabilities.check("qga", guest=None)
        check("a missing capability says which one", "lib/capabilities.py",
              not ok and "qga" in why)
        ok2, why2 = capabilities.check("teleport", guest=None)
        check("an unknown capability is refused by name", "lib/capabilities.py",
              not ok2 and "unknown capability" in why2)
        # AND ALL OF THEM ARE CHECKED BEFORE THE FIRST IS RAISED, so one run reports every
        # missing capability rather than one per attempt.
        raised = ""
        try:
            capabilities.require(["qga", "teleport"], guest=None)
        except capabilities.Broken as exc:
            raised = str(exc)
        check("every missing capability is reported at once", "lib/capabilities.py",
              "qga" in raised and "teleport" in raised)
    except Exception as exc:                          # noqa: BLE001
        check(f"capabilities imports ({exc})", "lib/capabilities.py", False)

    # --- ONE QMP CONNECTION PER SOCKET, ENFORCED IN THE CLIENT -------------------------
    #
    # qemu's `-qmp unix:...,server=on,wait=off` serves ONE client per socket, and a second
    # connect() SUCCEEDS and then never gets a greeting - so the failure is a HANG, not an
    # error, and it happens inside whatever timeout the caller happens to have. Four stages
    # fixed that at four call sites by remembering to pass `q=` down; the fifth forgot, and
    # `dismiss_post_login_windows` sat in its 240-second timeout producing no output at all.
    #
    # So the guarantee is a property of the CLIENT now, and these read that rather than reading
    # each call site for a keyword. A future caller cannot get it wrong because there is nothing
    # to get right.
    qmp_src = read("lib/qmp.py")
    check("the qmp client shares one connection per socket", "lib/qmp.py",
          "_open: \"dict[str, QMP]\" = {}" in qmp_src and "def __new__" in qmp_src)
    check("a live connection is handed to the next caller", "lib/qmp.py",
          "existing._holders += 1" in qmp_src and "existing.alive()" in qmp_src)
    check("a closed connection is not handed out", "lib/qmp.py", "def alive" in qmp_src)
    # AND CLOSING IS REFERENCE COUNTED, because every stage does `q = QMP(...); ...; q.close()`
    # and the first close used to be the last. A step that closed while another was reading was
    # `OSError: Bad file descriptor` on a connection that looked fine - a run this repository
    # has already paid for.
    check("closing releases one holder, not the socket", "lib/qmp.py",
          "if self._holders > 0:" in qmp_src)
    # AND THE OLD ADVICE IS GONE. The docstring used to argue the wait was unavoidable and set
    # the greeting timeout to 120 seconds instead, which turns a hang into a longer hang.
    check("the client no longer argues the wait is unavoidable", "lib/qmp.py",
          "no version of this that avoids the wait" not in qmp_src)

    # --- A JOB'S OUTPUT NAMES WHAT IT PUBLISHED, NOT WHAT IT WAS GIVEN ----------------
    #
    # The chain was broken for weeks and every stage "succeeded". Each job declared
    #
    #     outputs:
    #       tag: ${{ inputs.desktop_from || steps.publish.outputs.tag }}
    #
    # so a run started with `desktop_from=macos15-setup` published `macos15-desktop` and then
    # told the NEXT stage to pull `macos15-setup` - the disk it had been given, not the one it
    # built. `ssh` therefore booted the Setup Assistant every time, and the failure read as
    # "the wizard never finished", which sent rounds of debugging into the wizard.
    #
    # Run 42860953845 is the measurement: desktop's oci-push published digest 3bf133ab3b68 from
    # input f7bd5255e8b0, and the ssh job's own oci-pull printed
    #
    #     pulling ghcr.io/easydev-org/easydiskbake:macos15-setup
    #     layer sha256:f7bd5255e8b0...
    #
    # - the input. The one screenshot that explained it is the ssh stage's last frame: the Setup
    # Assistant's country list, with VoiceOver reading "table, Row 1 of 257".
    macos_workflow = load_yaml(".github/workflows/macos15.yml")
    for job, built in (("setup", "macos15-setup"), ("desktop", "macos15-desktop"),
                       ("ssh", "macos15-ssh"), ("vm-configure", "macos15-vm-configure"),
                       ("dev", "macos15-dev")):
        declared = (macos_workflow["jobs"][job].get("outputs") or {}).get("tag")
        check(f"the {job} job outputs what it published", ".github/workflows/macos15.yml",
              declared is not None and built in declared)
    # AND THE WINDOWS CHAIN HAS THE SAME RULE, because it had the same bug.
    #
    # Its `tag:` was `${{ inputs.base_publish_tag }}`, which is EMPTY unless a caller sets it -
    # so oci-push fell back to `kind` plus a timestamp and published `windows-base-20261110-...`
    # while the job told the next one to pull `windows11`. That is unverifiable from inside the
    # run: the push says "published", the pull of a different name says "no manifest", and
    # neither mentions the other. `windows11`, `windows11-ssh` and `windows11-vm-configure`
    # were all absent from the registry after a run whose every step said completed.
    win_wf = load_yaml(".github/workflows/windows-11.yml")
    for job, built in (("base", "windows11"), ("ssh", "windows11-ssh"),
                       ("vm-configure", "windows11-vm-configure"), ("dev", "windows11-dev")):
        cfg = win_wf["jobs"][job]
        declared = (cfg.get("outputs") or {}).get("tag")
        push = [st for st in cfg.get("steps", []) if st.get("id") == "publish"]
        pushed = push[0]["with"]["tag"] if push else None
        check(f"the windows {job} job outputs what it published",
              ".github/workflows/windows-11.yml",
              declared is not None and built in declared and declared == pushed)

    # AND NOTHING POINTS AT THE OLD NAME. `share` became `vm-configure`, and the dev job's
    # condition kept saying `needs.share.outputs.tag` - a reference to a job that does not
    # exist, which quietly made the condition true only when `dev_from` was passed by hand.
    # CODE, NOT PROSE: the comment above explains the old name, which is the point of it.
    wf_code = "\n".join(line for line in read(".github/workflows/macos15.yml").splitlines()
                        if not line.lstrip().startswith("#"))
    check("no job still needs a job that was renamed", ".github/workflows/macos15.yml",
          "needs.share." not in wf_code)

    # AND EACH STAGE RECORDS ITS INPUT'S DIGEST BEFORE IT MOVES THE DISK, so the push can tell
    # whether the stage produced anything.
    #
    # `distinct-from` wanted a PATH, and the path is gone by push time: `working_copy` MOVES the
    # disk (the published copy is still in the registry, so copying 28 GiB buys nothing). The
    # first attempt failed exactly there -
    #
    #     FAILED: distinct-from names out/macos15-setup.qcow2, which does not exist
    #
    # which is the check working, against a path that cannot be relied on.
    for path in ("macos/15/desktop.py", "macos/15/ssh.py", "macos/15/dev.py"):
        body = read(path)
        check(f"{path} records its input digest", path,
              'os.path.join(work, "input.sha256")' in body and "_sha256(args.disk)" in body)
        # AND BEFORE THE MOVE, not after: hashing a file the next line consumes is the only
        # order where the number exists.
        check(f"{path} hashes before it moves", path,
              body.index("_sha256(args.disk)") < body.index("working_copy("))
    mac_wf = read(".github/workflows/macos15.yml")
    for kind in ("macos15-desktop", "macos15-ssh", "macos15-dev"):
        check(f"the {kind} push compares against that digest", ".github/workflows/macos15.yml",
              f"path: out/{kind}.qcow2" in mac_wf
              and "${{ steps.input_digest.outputs.sha256 }}" in mac_wf)

    # AND `copy` IS NEVER DERIVED FROM `run_overlay`, BECAUSE THEY ARE THE SAME THING TWICE.
    #
    # `copy=True` asks vm_session for a PROTECTIVE overlay over the disk it was handed. A stage
    # that already made one - `--overlay` - hands that diff instead and must not have a second
    # made on top. A stage that did not make one hands a WORKING COPY it moved into work, and
    # that file is the artifact; wrapping it protects nothing and sends every write into a
    # session overlay that is deleted on the way out.
    #
    # Run 42831281414 is that second case, and it cost a whole chain:
    #
    #     [overlay] overlay out/session-overlay.qcow2
    #     [overlay] released the overlay (2639 MiB written this run)
    #     [oci-push] Exists f7bd5255e8b0 macos15-desktop.qcow2
    #
    # 2639 MiB of wizard answers went into a diff that was deleted, the published disk was the
    # untouched base, and `macos15-desktop` ended up with the same content digest as
    # `macos15-setup`. The next stage booted it and found the Setup Assistant still on screen.
    for path in ("macos/15/ssh.py", "macos/15/dev.py", "macos/15/desktop.py"):
        # CODE, NOT PROSE. All three files explain this mistake in a comment - that is the point
        # of the comment - so a check for the words would fail on the documentation that warns
        # about them.
        body = read(path)
        code = "\n".join(line for line in body.splitlines()
                         if not line.lstrip().startswith("#"))
        # TWO SPELLINGS, because the three stages are not written the same way: the two that
        # build a dict say `"copy": False`, and desktop.py passes `boot_options=dict(...,
        # copy=False)` into an existing mapping.
        check(f"{path} does not wrap its own disk twice", path,
              "run_overlay is None" not in code
              and ('"copy": False' in code or "copy=False" in code))

    # AND THE FUNCTION THE CALLER'S BOOT OPTIONS GO TO HAS TO ACCEPT THEM.
    #
    # `start()` passed `boot_options=` and `_start_machine()` did not declare it, so every macOS
    # boot through the walkthrough died with
    #
    #     TypeError: _start_machine() got an unexpected keyword argument 'boot_options'
    #
    # (run 42815176349, the first run after the stage stopped starting qemu itself). A keyword
    # argument that nothing calls is invisible; this is the check that says the two ends agree.
    vm_src_sig = read("lib/vm_session.py")
    check("the machine takes the options the starter passes", "lib/vm_session.py",
          "boot_options: dict | None = None):" in vm_src_sig
          and "boot_options=boot_options)" in vm_src_sig)

    # AND A SHUTDOWN THAT TIMED OUT STILL KILLS QEMU, BECAUSE THE NEXT STEP READS THE DISK.
    #
    # `vm_session.stop()` caught the "did not stop in time" error, logged one line, and fell
    # through to a `finally` that closes the overlay and the share and never touches qemu. The
    # process kept its write lock and the disk check that follows failed on a disk that was
    # perfectly fine:
    #
    #     20:46:17  [vm]  Connection to 172.30.0.1 closed by remote host.
    #     20:51:17  [vm]  the guest did not shut down within 300s
    #     20:51:17  [disk] qemu-io: can't open device out/dev.qcow2: Failed to get "write" lock
    #     20:51:17  === THE DISK WILL NOT BOOT ===
    #
    # Run 42897536838: twenty walkthrough steps answered, and then the stage reported that its
    # own output would not boot. The run before it had the same shutdown succeed in SIX seconds,
    # so the lock is the whole difference.
    vs_src = read("lib/vm_session.py")
    stop_body = vs_src[vs_src.index("def stop(guest,"):]
    stop_body = stop_body[:stop_body.index("\n@contextlib.contextmanager")]
    fail_path = stop_body[stop_body.index("except Exception as exc:"):]
    fail_path = fail_path[:fail_path.index("finally:")]
    check("a timed-out shutdown still kills the guest", "lib/vm_session.py",
          "guest.stop(acpi_first=False)" in fail_path)

    # AND `net.up()` DOES NOT KILL A DHCP SERVER THAT IS ALREADY SERVING.
    #
    # It said "idempotent" and was not, in the one way that costs a guest its address: every call
    # ran `pkill -x dnsmasq` and started a new one, dropping every lease. A guest that had already
    # booted cannot ask again until its next renew, so it sits at a login window with no network
    # while the stage waits 900 seconds for sshd and reports
    #
    #     ssh: connect to host 172.30.0.1 port 2222: No route to host
    #
    # Run 42890005370 is the measurement: net.up() ran twice, eight seconds apart - clean_step's
    # own call and vm_session.start's - and the guest never appeared. Its screenshot is a fully
    # booted login window (vmuser, the clock, a WiFi icon) on a machine whose DHCP server had just
    # been restarted underneath it.
    net_src = read("lib/net.py")
    body = net_src[net_src.index("def up("):]
    body = body[:body.index("def down(")]
    check("the bridge does not restart DHCP on every call", "lib/net.py",
          'running = _sh("pgrep -x dnsmasq").returncode == 0' in body)
    # AND THE SKIP HAS TO COVER THE `dnsmasq` LINE, NOT ONLY THE KILL.
    #
    # The first version of this fix skipped `pkill` and then started a second dnsmasq anyway,
    # which cannot bind while the first holds 172.30.0.1:
    #
    #     === dnsmasq DID NOT START ===
    #       dnsmasq: failed to create listening socket for 172.30.0.1: Address already in use
    #
    # and took a whole stage down (run 42895632528). The check is an indentation test, because
    # that is the only thing that distinguishes "guarded" from "guarded the wrong half".
    tail = body[body.index('running = _sh("pgrep -x dnsmasq").returncode == 0'):]
    tail = tail[:tail.index("# NO GEOIP")]
    guarded = [line for line in tail.splitlines()
               if "pkill -x dnsmasq" in line or line.strip().startswith("started = _sh(")]
    check("skipping a running dnsmasq also skips starting one", "lib/net.py",
          len(guarded) == 2 and all(line.startswith("        ") for line in guarded))
    # AND A RUNNING SERVER IS ONLY KEPT FOR THE GUEST IT IS SERVING.
    #
    # The range is ONE ADDRESS, and OpenCore writes identity.json per WORK DIRECTORY - so every
    # stage of a macOS chain has a different MAC for the same disk. Keeping the lease across
    # stages therefore left the next guest unable to lease the only address, and it sat at its
    # own login window for 900 seconds. Run 42899662670:
    #
    #     21:01:10  [net] dnsmasq is already running ...   <- lease belongs to the previous MAC
    #     21:01:32  [opencore] identity: ... / 00:16:CB:90:D9:FE (written to ...clean-work/...)
    #     21:01:32  [net] dnsmasq is already running ...   <- and cannot be leased again
    #
    # Both halves are checked, because either one alone leaves the bug: the caller has to pass
    # the MAC, and `up()` has to act on it.
    check("net.up can be told which guest it serves", "lib/net.py",
          'mac: str = ""' in body or 'mac: str = ""' in net_src)
    check("a different guest gets the one address", "lib/net.py",
          "dnsmasq.vm.mac" in net_src and "served != mac.strip().lower()" in net_src)
    # AND THE LEASE FILE HAS TO GO WITH THE RESTART, BECAUSE dnsmasq READS IT BACK.
    #
    # The first attempt at this bug restarted dnsmasq for the new MAC and stopped there. The
    # guest then asked 185 times and was never answered:
    #
    #     DHCPDISCOVER(qemubr0) 00:16:cb:17:61:d5 no address available
    #
    # The new instance had adopted the OLD lease from dnsmasq's default file, and the range is
    # ONE ADDRESS - so the restart meant to free it freed nothing. Run 42905230912.
    #
    # dnsmasq's own log is what settled it, and that log line is now collected on a failed clean
    # (`clean_step.py`), which is how the difference between "never asked" and "was refused"
    # became visible at all.
    check("dnsmasq has a lease file this module can clear", "lib/net.py",
          "LEASE_FILE =" in net_src and "leasefile={LEASE_FILE}" in net_src)
    check("a restart clears the lease it is freeing", "lib/net.py",
          "os.unlink(LEASE_FILE)" in net_src)
    # AND A CLEAN THAT IS KILLED SAYS SO, WITH THE NUMBERS THAT IDENTIFY THE SENDER.
    #
    # Run 42913402988's clean exited 241 - `-15 & 0xFF`, a Python process killed by SIGTERM - half
    # a second after `the guest powered off`, and the re-compress that follows never printed. Two
    # candidates and no way to tell them apart from a log that ends mid-sentence: the share
    # teardown signalling a reused pid, or the runner. A handler cannot save the exit status, but
    # it can name the signal and the ppid/pgid, which is what separates those cases.
    clean_src = read("lib/clean_step.py")
    check("a signalled clean reports the signal", "lib/clean_step.py",
          "_install_signal_reporting" in clean_src and "SIGNALLED:" in clean_src)
    # AND THE SHARE TEARDOWN REFUSES ITS OWN PID. That is the in-process candidate, and it is
    # cheap to exclude even before a run proves which one it was.
    check("the share teardown never signals the caller", "lib/hostshare.py",
          "pid == os.getpid()" in read("lib/hostshare.py"))
    # AND A CLEAN THAT FINISHED IS NOT FAILED BY THE RE-COMPRESS BEING KILLED.
    #
    # The workflow used to trust the exit code alone, so run 42916758079 reported failure for a
    # disk that had been cleaned and shut down correctly - the SIGTERM arrived half a second after
    # `the guest powered off`, before the re-compress printed a line.
    #
    # THE RECORD DECIDES NOW, AND ONLY WHEN IT SAYS SO. `clean_step.py` rewrites
    # out/clean-macos.txt on every completed step and writes CLEANED at the end; the workflow
    # passes a non-zero exit only when that line is absent. A clean that genuinely did not finish
    # still fails, which is the part that must not be lost in the convenience.
    # AND THE VERDICT IS TAKEN OUTSIDE THE PROCESS THAT GETS KILLED.
    #
    # The first version of this judged inside the clean step's own `bash -c`, and run 42919582582
    # showed why that cannot work: the SIGTERM that ended the clean ALSO ended that shell -
    #
    #     [clean-step] === SIGNALLED: SIGTERM (15) === ppid=418 pid=419 pgid=418
    #
    # with 418 being the `bash -c` and the group both - so the `if grep ... exit 0` lines never
    # ran and the job reported the signal's 241.
    #
    # A SEPARATE STEP cannot be that process. `continue-on-failure` on the streamer keeps the job
    # alive long enough for it to run, and it passes only when the stage's own record says the work
    # was done.
    mac_clean_step = read(".github/workflows/macos15.yml")
    seg = mac_clean_step[mac_clean_step.index("name: clean the disk"):]
    seg = seg[:seg.index("keep the last frame")]
    check("the clean step does not fail on its own exit code",
          ".github/workflows/macos15.yml", 'continue-on-failure: "true"' in seg)
    check("a separate step judges the clean", ".github/workflows/macos15.yml",
          "name: did the clean actually finish" in seg)
    check("a finished clean survives a killed re-compress",
          ".github/workflows/macos15.yml",
          'grep -q "^CLEANED " out/clean-macos.txt' in seg and "exit 0" in seg)
    check("a clean that did not finish still fails",
          ".github/workflows/macos15.yml", "THE CLEAN DID NOT FINISH" in seg)
    # AND THE SIGNAL IS NAMED IN THE ARTIFACTS, so the exit code is explainable after the fact.
    check("a signalled clean leaves a note naming the signal", "lib/clean_step.py",
          "clean-signal.txt" in read("lib/clean_step.py"))
    # AND THE SCREEN IS KEPT ON BEFORE THE STAGE THAT HAS NO SHELL YET.
    #
    # Run 42920273280 is the deadlock: sshd never listened, so the ssh-based `do not blank the
    # screen` step never ran, so the monitor blanked during the 600-second wait, so
    # out/ssh-work/steps/failed-wait-for-sshd.png was a completely black frame - and the
    # STEP1/STEP2/STEP3 marks added to diagnose that failure were typed onto a dark display.
    #
    # Any step that turns the display off only once ssh works cannot help the stage whose job is
    # to find out whether ssh works.
    win_ssh_yaml = read("windows/11/steps/ssh.yaml")
    check("the screen is kept on before ssh exists", "windows/11/steps/ssh.yaml",
          "keep the screen on before ssh exists" in win_ssh_yaml)
    # AND IT COMES BEFORE THE INSTALL, so a failed install still leaves a lit screen to read.
    check("the screen step precedes the ssh install", "windows/11/steps/ssh.yaml",
          win_ssh_yaml.index("keep the screen on before ssh exists")
          < win_ssh_yaml.index("name: install openssh"))
    # AND THE SSH COMMAND OPENS THE FIREWALL, BECAUSE sshd LISTENING IS NOT THE SAME AS REACHABLE.
    #
    # Run 42923725037's post-mortem frame was readable for the first time - the screen is kept on
    # before the install now - and it showed the whole guest side working:
    #
    #     Found OpenSSH Preview [Microsoft.OpenSSH.Preview] Version 10.4.0.0
    #     Successfully installed
    #     STEP1=ok      Set-Service sshd -StartupType Automatic
    #     STEP2=ok      the sshd_config rewrite
    #     STEP3=ok      Start-Service sshd
    #     22            Get-NetTCPConnection -State Listen -LocalPort 22
    #
    # sshd was listening on 22 and the host's forward to it timed out, because the firewall was
    # dropping the inbound connection - and the rule that opens it had been in the original one-
    # liner until 8fd3e91 rewrote that line to print STEP marks and dropped it.
    #
    # `b8c51ff` had `New-NetFirewallRule -Name sshd-22 ...`; the rewrite lost it. Asserted against
    # the file, and against the run above, because "the service is up" is the exact claim that
    # hid this for three runs.
    win_ssh = read("windows/11/steps/ssh.yaml")
    check("the ssh command opens the firewall port", "windows/11/steps/ssh.yaml",
          "New-NetFirewallRule -Name sshd-22" in win_ssh)
    check("the firewall step reports its own result", "windows/11/steps/ssh.yaml",
          "STEP4" in win_ssh)
    # AND THE SUCCESS PATH LEAVES THE `try`, OR THE MARKER AND THE COMPRESS ARE DEAD CODE.
    #
    # `clean_step.main()` ends its clean in a `try` whose `finally` does the shutdown, and BOTH
    # the re-compress and the `CLEANED` marker live after that block - because the shutdown has to
    # happen first, including when the clean failed. The `try` used to end in `return 0`, so:
    #
    #     no clean ever re-compressed its disk
    #     no clean ever wrote CLEANED
    #     every workflow that reads CLEANED was reading a file nothing wrote
    #
    # It surfaced only when the separate verdict step finally ran (run 42925475964):
    #
    #     === THE CLEAN DID NOT FINISH: no CLEANED line ===
    #
    # about a clean that had finished. An AST check rather than a grep, because the bug is the
    # SHAPE - a `return` inside the block - and the fix is the absence of that shape.
    import ast as _ast

    tree = _ast.parse(read("lib/clean_step.py"))
    mfn = [n for n in _ast.walk(tree)
           if isinstance(n, _ast.FunctionDef) and n.name == "main"][0]
    try_block = [n for n in _ast.walk(mfn) if isinstance(n, _ast.Try) and n.finalbody][0]
    last = try_block.body[-1].end_lineno
    zero_returns = [n.lineno for n in _ast.walk(try_block)
                    if isinstance(n, _ast.Return)
                    and isinstance(n.value, _ast.Constant)
                    and n.value.value == 0
                    and try_block.body[0].lineno <= n.lineno <= last]
    check("the clean's success path is not an early return", "lib/clean_step.py",
          not zero_returns)
    # AND A CLEAN THAT HAS ALREADY DONE ITS WORK SURVIVES ONE INTERRUPTION.
    #
    # Five runs were killed in the same window: after `the guest powered off`, with the disk
    # cleaned, before the record and the re-compress. Every candidate for the sender was excluded -
    # `hostshare.stop()` now logs what it signals, refuses a pid that is this process, re-checks
    # the cmdline immediately before, and sends from a child in its own session that is measured
    # not to be able to reach the parent. It is still arriving.
    #
    # What is established: the WORK IS DONE when it arrives. Dying there throws away a cleaned disk
    # and produces the same record as a clean that never ran. So one SIGTERM is survived when the
    # disk is clean, and a second is obeyed - a supervisor that repeats itself is not asking.
    #
    # A signal before the work is done is obeyed immediately, which is the case that must not
    # become tolerant.
    clean2 = read("lib/clean_step.py")
    check("a cleaned disk survives one SIGTERM", "lib/clean_step.py",
          "the clean is finished; recording it and continuing" in clean2)
    check("a second signal is obeyed", "lib/clean_step.py",
          "obeying it" in clean2 and "signum == signal.SIGTERM and state == 1" in clean2)
    check("the cleaned flag is set before the shutdown", "lib/clean_step.py",
          clean2.index('_ARTIFACTS["cleaned"] = True')
          < clean2.index('vm_session.stop(guest, acpi_first=ok'))
    # AND A SHARE THAT LOST ITS PRIVATE DIRECTORY GETS ONE MORE ATTEMPT.
    #
    # Run 42941249853 (launch_dev) failed before the guest booted:
    #
    #     === smbd DID NOT BIND 172.30.0.1:445 IN 10s ===
    #       Could not open tdb: No such file or directory
    #     === THE HOST COULD NOT SERVE ~/shared ===
    #
    # `os.makedirs(PRIVATE_DIR, exist_ok=True)` runs a few lines above the config write, so the
    # directory existed and then something removed it. Samba's own message names that case, so the
    # retry is conditional on it rather than a blanket second try - and the state of the directory
    # is printed either way, because "exists / writable / owner / entries" is what that message
    # cannot say.
    hs = read("lib/hostshare.py")
    check("a share that lost its tdb directory retries", "lib/hostshare.py",
          'if "Could not open tdb" in tail:' in hs)
    check("the retry rebuilds the private directory", "lib/hostshare.py",
          "shutil.rmtree(PRIVATE_DIR" in hs and "os.makedirs(PRIVATE_DIR" in hs)
    check("the private directory state is reported", "lib/hostshare.py",
          "writable={os.access(PRIVATE_DIR, os.W_OK)}" in hs)
    # AND THE WINDOWS CLEAN GETS AN HOUR, AND SAYS WHERE ITS TIME WENT.
    #
    # Run 42937358029's clean was still inside its PowerShell script at thirty-one minutes, so the
    # 1800-second ssh budget expired and the stage was killed -
    #
    #     [clean-step] === SIGNALLED: SIGTERM (15) === ppid=305 pid=312 pgid=312
    #     [clean-step]   signal 1, and the clean has NOT finished; obeying it
    #
    # (the tolerance from 13f59c9 correctly REFUSING to save it, because the work was not done).
    # Nothing in that script is bounded at 1800: it walks the filesystem for its size reports,
    # retrims a 128 GiB volume, and sits behind Defender. A timeout that fires during normal work
    # is a wrong answer, not a safety net - the job limit is the real ceiling.
    #
    # And since the log could not distinguish a slow retrim from a hung shell, every section now
    # announces itself with a clock.
    wc_src = read("lib/winclean.py")
    check("the windows clean gets an hour", "lib/winclean.py",
          "timeout: int = 3600" in wc_src)
    check("an expired clean is reported, not raised", "lib/winclean.py",
          "except subprocess.TimeoutExpired:" in wc_src
          and "THE CLEAN DID NOT FINISH IN" in wc_src)
    ps = read("windows/11/clean.ps1")
    check("the windows clean announces each step", "windows/11/clean.ps1",
          "function Step(" in ps and ps.count('Step "') >= 4)
    check("the retrim announces itself", "windows/11/clean.ps1",
          'Step "trim (no zeroing)' in ps)
    # AND THE RETRY STOPS EVERY smbd, NOT ONLY THE ONE IT RECORDED.
    #
    # Run 42942106447's retry fired and failed the same way, and the reason was in the printed
    # directory state:
    #
    #     private dir ...: entries=['msg.sock', 'passdb.tdb', ...]
    #     Could not open tdb: No such file or directory
    #
    # `msg.sock` and those tdb files are OPENED EXCLUSIVELY by an smbd. A stray one from an earlier
    # stage in the same container is the "no such file" - the file is there and somebody else has
    # it. `stop()` only kills the pid it recorded, so the retry now clears them all first, by
    # reading /proc and matching argv[0] == smbd AND the exact `-s <conf>` argument (never a
    # `pkill -f` pattern, which is the mistake `stop()` already documents).
    hs2 = read("lib/hostshare.py")
    check("the retry clears every smbd", "lib/hostshare.py",
          "_sh_kill_all_smbd()" in hs2 and "def _sh_kill_all_smbd" in hs2)
    check("smbd is matched by argv, not by a pattern", "lib/hostshare.py",
          'parts[0] not in (b"smbd", b"/usr/sbin/smbd")' in hs2)
    check("the retry waits longer than the first attempt", "lib/hostshare.py",
          "max(wait, 30.0)" in hs2)
    # AND THE STRAY-SMBD KILL IS THE LIKELY ANSWER TO A QUESTION FIVE ROUNDS COULD NOT SETTLE.
    #
    # Run 42943819411's launch_dev is the first run that printed the step before dying:
    #
    #     [share]   stopped 1 smbd process(es) still holding the private directory
    #     <nothing else - no retry, no bind, no COULD NOT SERVE>
    #
    # The smbd it signalled is in this process's own group, because `smbd -D --no-process-group`
    # never leaves it. `os.kill(pid, 15)` to a member of your own group is enough to take you with
    # it - measured:
    #
    #     parent pgid=33148  target pgid=33148
    #     helper rc=0
    #     parent survived, target poll=-15
    #
    # which is exactly why the same signal now goes through a child in its own session. This is
    # also the shape behind every `=== SIGNALLED: SIGTERM === ppid=N pid=N pgid=N` in the clean
    # runs: a kill this process issued, reaching a member of its own group.
    hs3 = read("lib/hostshare.py")
    stray = hs3[hs3.index("def _sh_kill_all_smbd"):]
    stray = stray[:stray.index("def stop() -> None:")]
    check("the stray-smbd kill is isolated too", "lib/hostshare.py",
          "start_new_session=True" in stray and "os.setsid()" in stray)
    # AND THE COMPONENT STORE IS COMPACTED WITHOUT `/ResetBase`, SO IT CANNOT EAT THE STAGE.
    #
    # Two runs spent their entire `install ssh` step inside this one command, which reads as a
    # hang because the step boundary is an hour wide:
    #
    #     42937358029   <- the clean was later killed at 1800s
    #     42943833902   <- `compact the component store` started 02:21:17 and never reported
    #
    # Every other step of that walkthrough totalled about seven minutes, so this is the whole
    # cost. `/StartComponentCleanup` removes superseded components; `/ResetBase` additionally
    # discards every superseded version of everything - un-uninstallable updates afterwards - and
    # nothing in this repository reads the component store or uninstalls an update from it.
    #
    # It is also `optional: true`, and `session.ssh` returns 124 on a timeout, which takes the
    # same `status != 0` path that `optional` exempts. Verified here rather than assumed.
    win_steps = read("windows/11/steps/ssh.yaml")
    check("the component store is not ResetBase'd", "windows/11/steps/ssh.yaml",
          "StartComponentCleanup /ResetBase" not in win_steps
          and "Cleanup-Image /StartComponentCleanup\n" in win_steps)
    check("the component store cannot fail the stage", "windows/11/steps/ssh.yaml",
          "optional: true" in win_steps.split("name: compact the component store", 1)[1][:400])
    check("a step timeout is exempted by optional", "lib/session.py",
          "return 124" in read("lib/session.py"))
    # AND THE WINDOWS SSH STAGE RECORDS ITSELF, WHICH IT NEVER DID.
    #
    # `install ssh` is ONE GitHub step that runs the entire walkthrough, so its duration says
    # nothing about where the time went. Two runs sat inside it for an hour and the only evidence
    # was the timestamps of `===` markers; the frames that would have shown the screen were never
    # captured, because `windows/11/ssh.py` created a `shots` directory and started no sampler.
    #
    # macos/15/ssh.py has started `lib/snapshots.py` and `lib/live.py` since it was fixed for the
    # same reason - a stage that fails must still leave a video - and its artifact proves it
    # (run 42874167311: macos15-ssh-recording, 3789241 bytes). The Windows stage gets the same
    # pair, started before the machine so the boot and the lock screen are in the file.
    # IT IS A `record:` STEP NOW, NOT A PAIR OF SUBPROCESSES IN THE DRIVER. The driver started
    # the sampler and the encoder itself for one commit; `record:` is an atomic step shared by
    # every family, so the file says whether a stage is filmed and the driver only drives.
    wssh_yaml = read("windows/11/steps/ssh.yaml")
    check("the windows ssh stage records through its file", "windows/11/steps/ssh.yaml",
          "record: install.mp4" in wssh_yaml and "record: false" in wssh_yaml)
    # AND THE DRIVER NO LONGER STARTS ONE, because two encoders writing the same file is worse
    # than none: whichever finishes second wins and the other's frames are lost.
    wssh = read("windows/11/ssh.py")
    check("the windows driver does not record twice", "windows/11/ssh.py",
          "livelib.start(" not in wssh)
    wf = read(".github/workflows/windows-11.yml")
    check("the windows ssh recording is uploaded", ".github/workflows/windows-11.yml",
          "windows11-ssh-recording" in wf and "out/ssh-work/install.mp4" in wf)
    # --- ONE ENTRY POINT, AND STAGES ARE NOT PROGRAMS ---------------------------------
    #
    # Every stage used to be a program with its own `main()`, its own argument list and its own
    # copy of "copy the disk, start qemu, run the walkthrough, stop it, compress" - twenty-four of
    # them, about ten thousand lines, and the part that differs between two stages is almost never
    # the part that was written twice (`macos/15/ssh.py` 852 lines and `windows/11/ssh.py` 827 are
    # the same shape).
    #
    # `easyvm` is the one supported way to run a workflow file. The old entry points still work
    # while their callers are migrated - a repository that cannot run cannot be verified - but
    # they say so, and `EASYVM_STRICT=1` refuses.
    check("there is one entry point for a workflow file", "easyvm",
          "def main(argv" in read("easyvm") and "import steps" in read("easyvm"))
    ep = read("lib/entrypoint.py")
    check("a bypassing stage says so", "lib/entrypoint.py",
          "this stage is not an entry point" in ep)
    check("a bypassing stage can be refused", "lib/entrypoint.py",
          "EASYVM_STRICT" in ep and "raise SystemExit(2)" in ep)
    # AND THE SUGGESTED PATH IS CHECKED BEFORE IT IS PRINTED. A guard that points at a file which
    # does not exist is worse than no guard: it is confidently wrong.
    check("the guard verifies the file it names", "lib/entrypoint.py",
          "os.path.isfile(path)" in ep)
    for stage in ("windows/11/ssh.py", "macos/15/ssh.py"):
        check(f"{stage} is guarded", stage, "entrypoint.note_stage(" in read(stage))
    # --- PREPARING A DISK AND WAITING FOR A DESKTOP ARE STEPS NOW ----------------------
    #
    # Both were the same six (or ninety) lines in every stage that needed them:
    #
    #     working_copy(args.disk, os.path.join(work, "desktop.qcow2"), ...)
    #
    # and a desktop wait that macOS expressed as `python: ssh_panes:wait_for_desktop` - an
    # extension existing only to run a loop - while `windows/11/ssh.py` kept a 92-line function
    # with its own brightness thresholds. The JUDGEMENT was already shared: lib/desktop.py has
    # `is_windows_desktop` and `is_macos_desktop`, and says in its own comment that a second
    # opinion about what a desktop looks like is a second thing to keep in step.
    st = read("lib/steps.py")
    check("a disk can be prepared by a step", "lib/steps.py",
          '"disk",        # PREPARE A DISK' in st and "wc.working_copy(" in st)
    # AND IT DOES NOT GUESS `copy`: working_copy documents that only the caller knows whether the
    # input is the only copy, and guessing wrong either wastes a 28 GiB copy or destroys the only
    # copy of an hour's work. `disk_copy` is a statement.
    check("the disk step does not guess whether to copy", "lib/steps.py",
          'copy=bool(step.get("disk_copy"))' in st)
    # AND THE BOOT USES WHAT THE STEP PREPARED, by pointing the session at it - one place, not two.
    check("the boot uses the prepared disk", "lib/steps.py",
          "session.disk = prepared" in st)
    check("a desktop wait is a step", "lib/steps.py",
          '"desktop",     # WAIT FOR A DESKTOP' in st and "is_windows_desktop" in st)
    # AND THE MACHINE FACTS STAY IN lib/ - a TPM is one of them, and both Windows stages had
    # their own copy of the same `start_swtpm`.
    check("the tpm is one implementation", "lib/swtpm.py",
          "def start(" in read("lib/swtpm.py") and "def stop(" in read("lib/swtpm.py"))
    check("the machine honours boot_options['tpm']", "lib/vm_session.py",
          'boot_options.get("tpm")' in read("lib/vm_session.py"))
    # AND THE OUTPUT IS A STEP TOO, WHICH WAS THE LAST PIECE OF PYTHON EVERY STAGE KEPT.
    #
    # `windows/11/ssh.py` ended with
    #
    #     if ok:
    #         compress.compress(disk, args.out)
    #         log(f"BAKED {args.out} ...")
    #
    # inside a `finally`, and `macos/15/ssh.py`, `desktop.py`, `dev.py` and `vm_configure.py` each
    # had the same three lines in their own. That is not a machine fact or a screen fact - it is
    # "write the disk where the workflow looks for it", which is a step.
    #
    # IT IS A STEP AFTER THE LAST REAL ONE, NOT A `finally`: a failed walk leaves a disk that the
    # failed steps may have half-configured, and compressing it publishes that.
    check("a disk can be published by a step", "lib/steps.py",
          "compressmod.compress(source, dest)" in st)
    check("the compress refuses to write over its input", "lib/steps.py",
          "would write onto its own input" in st)
    for f in ("macos/15/steps/ssh.yaml", "windows/11/steps/ssh.yaml"):
        body = read(f)
        check(f"{f} publishes its own disk", f, "compress: \"{{ out }}\"" in body)
        # AND THE OUTPUT NAME IS A RUN FACT, from the command line - not baked into the file.
        check(f"{f} takes the output path as a variable", f,
              "from: args.out" in body)
        # AND IT COMES AFTER THE LAST REAL STEP. A `compress:` before `shutdown:` would publish a
    # disk the guest is still writing to, which is the failure the ordering exists to prevent.
    for f in ("macos/15/steps/ssh.yaml", "windows/11/steps/ssh.yaml"):
        body = read(f)
        check(f"{f} compresses after the machine is stopped", f,
              body.index("compress: \"{{ out }}\"") > body.index("record: false")
              or body.index("compress: \"{{ out }}\"") > body.index("shutdown:"))
    # AND THE WINDOWS WORKFLOW RUNS THE FILE, NOT A PROGRAM.
    #
    # `install ssh` used to be `python3 windows/11/ssh.py`, where the walkthrough was one call
    # inside a program that also prepared the disk, started the machine, waited for a desktop and
    # compressed the result. All four are steps in the file now - `disk:`, `boot:`, `desktop:`,
    # `compress:` - so the step names the file and where its output goes.
    win_wf = read(".github/workflows/windows-11.yml")
    check("the windows ssh stage runs its workflow file",
          ".github/workflows/windows-11.yml",
          "./easyvm windows/11/steps/ssh.yaml" in win_wf)
    # AND THE BUDGET ARRIVES AS A VARIABLE. The same disk is slower on a loaded runner, so the
    # wait reads `{{ boot_timeout }}` rather than a number measured once - and the var is declared,
    # because a template that names an undeclared key fails at load.
    win_ssh = read("windows/11/steps/ssh.yaml")
    check("the desktop wait reads the run's budget", "windows/11/steps/ssh.yaml",
          'timeout: "{{ boot_timeout }}"' in win_ssh
          and "from: args.boot_timeout" in win_ssh)
    # AND `--boot-timeout` EXISTS ON THE CLI, or the workflow line is a flag that is not there.
    check("the boot budget is a command-line argument", "lib/steps.py",
          '"--boot-timeout"' in st)
    # AND A `disk:` STEP IN A FILE WITH NO `boot:` SAYS SO, rather than "no disk_from" - those
    # send the reader to two different files.
    check("a disk step without a boot is explained", "lib/steps.py",
          "prepares a disk for `boot:`, and this file has none" in st)
    # --- THE OVERLAY IS THE DEFAULT, AND PUBLISHING FLATTENS IT ------------------------
    #
    # `working_copy` moves the input by default. That is right for a disk pulled from ghcr and
    # still there - but a stage that FAILS has already consumed it, so the retry pays 11 to 20 GiB
    # of download again, and `working_copy`'s own note says moving can destroy the only copy of an
    # hour's work. An overlay leaves the base byte-for-byte intact, re-checks it on close, and
    # costs one flatten at publish time - which is the same `qemu-img convert -c` the compress
    # step was already doing.
    #
    # `boot:` ALREADY DEFAULTED TO IT (`copy=... else not android`). What was missing is that a
    # file which let `boot:` make the overlay had no way to tell `compress:` to flatten it - only
    # a Python stage that passed its own Overlay could. So the artifact would have been a diff
    # whose parent is the base: a file that looks like a disk and is not one.
    check("every boot records the overlay it made", "lib/steps.py",
          "self.flatten_from = overlay" in st)
    check("compress flattens instead of copying a diff", "lib/steps.py",
          "flatten.flatten(dest)" in st and "flatten.close()" in st)
    # AND A FAILED WALK RELEASES IT. Without this the base's size and mtime are never re-checked
    # on the one path where a stage did something unexpected.
    check("a failed walk still releases the overlay", "lib/steps.py",
          "the overlay could not be released" in st)
    # AND AN OVERLAY IS NOT A MACOS FEATURE. `start_on` required `assets` (the OpenCore build),
    # which made a qcow2 backing chain reachable only from a macOS stage - the parameter is
    # optional now, and `_boot_on_existing_overlay` no longer demands it.
    check("an overlay boot does not require OpenCore assets", "lib/steps.py",
          "NO `assets` REQUIREMENT" in st)
    check("start_on is not macOS-only", "lib/vm_session.py",
          "assets: dict | None = None" in read("lib/vm_session.py"))
    # --- THE TWO BUGS THE FIRST REAL `compress:` FOUND ---------------------------------
    #
    # Run 42949499311 was the first time `compress:` ever ran, and it failed with
    #
    #     could not flatten the overlay: qemu-img: Could not open 'session-overlay.qcow2':
    #     Failed to get shared "write" lock
    #
    # on a path that was `out/ssh-work/out/windows11-ssh.qcow2`. Two separate faults:
    check("a compress path is relative to the repository", "lib/steps.py",
          "dest = os.path.join(REPO, dest)" in st)
    # AND THE MACHINE IS STOPPED BEFORE THE DISK IS READ. `windows/11/steps/ssh.yaml` had no
    # `shutdown:` at all - macOS' file has had one since its conversion, this one was converted
    # later and the step was not carried over, so qemu still held the overlay.
    win_ssh2 = read("windows/11/steps/ssh.yaml")
    check("the windows stage stops its machine", "windows/11/steps/ssh.yaml",
          "shutdown: kill" in win_ssh2)
    check("the machine stops before the disk is published", "windows/11/steps/ssh.yaml",
          win_ssh2.index("name: stop the machine")
          < win_ssh2.index('compress: "{{ out }}"'))
    # AND STOPPING IT DOES NOT DELETE THE OVERLAY THAT IS STILL OWED TO `compress:`.
    # `vm_session.stop(overlay=...)` deletes the file; fixing only the missing `shutdown:` would
    # have moved this failure one step later instead of removing it.
    check("an overlay owed to compress survives the shutdown", "lib/steps.py",
          'getattr(self, "flatten_from", None) is None' in st)
    # AND THE FLATTEN FINALLY COMPLETED - run 42952160130, the first time it ever did:
    #
    #     [overlay] flattening session-overlay.qcow2 -> windows11-ssh.qcow2
    #     [overlay]   out/windows11-ssh.qcow2 (10050 MiB)
    #     [overlay] released the overlay (1664 MiB written this run)
    #     [steps]     BAKED out/windows11-ssh.qcow2 (10050 MiB)
    #     [steps] === WALKTHROUGH COMPLETE: 29 step(s) ran ===
    #
    # 1664 MiB of writes became a 10050 MiB standalone disk from a 10543 MiB read-only base. The
    # three faults before it are the reason the assertions above exist.
    #
    # WHAT THAT RUN ALSO FOUND, one step later: `clean_step` calls `vm_session.start` with no
    # `boot_options`, and `_start_machine` then read `boot_options.get("tpm")` on None -
    #
    #     AttributeError: 'NoneType' object has no attribute 'get'
    #
    # because the parameter's default is `| None = None`. Two fixes: the body treats None as "no
    # options", and a Windows clean now asks for the TPM its disk was installed against.
    vs = read("lib/vm_session.py")
    check("_start_machine tolerates no boot_options", "lib/vm_session.py",
          "boot_options = boot_options or {}" in vs)
    cl = read("lib/clean_step.py")
    check("a windows clean boots with its TPM", "lib/clean_step.py",
          '"tpm": True' in cl)
    # AND `tpm` IS NOT `bootstate`. They are different facts - a saved firmware archive versus a
    # TPM the installed system refuses to boot without - and reusing one for the other would have
    # been a coincidence that reads as a design.
    check("tpm is its own key, not bootstate", "lib/clean_step.py",
          '"bootstate": True, "tpm": True' in cl)
    # AND `record:` TAKES THE FRAMES IT ENCODES, BECAUSE NOTHING ELSE WAS.
    #
    # Run 42952160130 uploaded `windows11-ssh-failed-screen` and nothing else:
    #
    #     [steps]   recording: out/ssh-work/install.mp4
    #     ##[warning]No files were found with the provided path: out/ssh-work/install.mp4
    #
    # `lib/live.py` returns without writing anything when there are no frames (`if total == 0:
    # return`). A stage whose steps are all `key:` and `ssh:` never screenshots, so `shots/` stayed
    # empty - and `record:` was encoding nothing. `lib/snapshots.py` is what takes them; the macOS
    # stages have always started it by hand, and a YAML file had no way to say so.
    check("a record step starts its own sampler", "lib/steps.py",
          "driver._sampler = subprocess.Popen" in st)
    # AND THE SAMPLER STOPS BEFORE THE FINAL ENCODE, so the last second is on disk rather than
    # written while the encoder is reading.
    rec = st[st.index('if step["record"] is not None:'):]
    check("the sampler stops before the encoder", "lib/steps.py",
          rec.index("sampler.terminate()") < rec.index("recorder.stop(final=True)"))
    # AND THE WINDOWS CLEAN STREAMS, BECAUSE TWENTY SILENT MINUTES LOOK LIKE A HANG.
    #
    # `subprocess.run(capture_output=True)` holds every line until the process exits, so a clean
    # spending twenty minutes in `Optimize-Volume -ReTrim` is indistinguishable from one that
    # hung - and the only verdict available was the 3600-second budget expiring. Run 42954262213
    # sat in `cleaning the guest` long enough for that to be the whole question.
    #
    # `clean.ps1` already prints a timestamped `Step` line per section; this is what lets those
    # lines out as they happen. The deadline is enforced by `proc.wait(timeout=...)` over the
    # WHOLE command, not by reading the pipe to EOF first - that loop would have waited for the
    # guest to finish talking before the clock even started.
    wc = read("lib/winclean.py")
    check("the windows clean streams its steps", "lib/winclean.py",
          "subprocess.Popen(" in wc and "note(f\"    {text[:180]}\")" in wc)
    check("its deadline covers the whole command", "lib/winclean.py",
          "proc.wait(timeout=timeout)" in wc and "threading.Thread" in wc)
    # AND A CAPABILITY CHECK IS RETRIED WHEN THE GUEST IS SLOW RATHER THAN ABSENT.
    #
    # Run 42954262213's vm-configure failed on its SECOND copy step, having just succeeded on the
    # first:
    #
    #     copy the driver's payload in: a capability this step declares is missing
    #       ssh: ssh on port 2222 answered 124: the command did not finish in 5s
    #
    # The command that "did not finish" was `echo ok`, on a guest whose scp had landed a second
    # earlier. Five seconds is not a timeout for a Windows shell starting PowerShell under
    # Defender - and a check that refuses work because a healthy guest was slow is worse than no
    # check, because it looks like a diagnosis.
    #
    # 124 (did not finish) is retried with a longer budget; 255 (connection failed) and 127
    # (client missing) are not, because retrying those turns a real problem into a slow one.
    cap = read("lib/capabilities.py")
    check("a slow ssh capability probe is retried", "lib/capabilities.py",
          "for attempt in range(1, retries + 1):" in cap)
    check("only a timeout is retried", "lib/capabilities.py",
          "if status != 124:" in cap)
    # AND A WINDOWS GUEST MOUNTS THE SHARE WITHOUT A PASSWORD, WHICH `net use` CANNOT EXPRESS.
    #
    # Run 42958385108, the first time a Windows guest tried to mount this share with the password
    # the account now has (none). Everything before it in that stage passed:
    #
    #     install the driver the agent talks on   PNPUTIL_EXIT=0
    #     install the guest agent                 MSIEXEC_EXIT=0
    #     prove the guest agent answers           service: Running / Automatic
    #     mount the host share                    The option /PERSISTENT=NO is unknown.
    #
    # `net use \\host\shared "" /user:guest /persistent=no` was parsed as a share name followed
    # by an empty password and then a switch it did not recognise. `New-SmbMapping` takes the
    # credential as an object, so there is no positional ambiguity - and `-ErrorAction Stop` makes
    # a failure raise rather than print a warning the exit status never sees.
    hs = read("lib/hostshare.py")
    # CODE, NOT PROSE: the note above explains why `net use` was replaced, which is the point of
    # it, and a grep over the whole file cannot tell a comment from a command.
    hs_code = "\n".join(line for line in hs.splitlines() if not line.lstrip().startswith("#"))
    check("a windows mount does not use net use", "lib/hostshare.py",
          "New-SmbMapping" in hs_code and "net use \\\\" not in hs_code)
    # AND A LEFTOVER MAPPING IS REMOVED FIRST: the command runs once per ssh invocation by design
    # (a mapping dies with its logon session), so anything already there is a previous attempt's.
    check("a stale windows mapping is removed first", "lib/hostshare.py",
          "Remove-SmbMapping -Force" in hs)
    # AND ONE LATE SIGTERM IS SURVIVED BY A STAGE, NOT ONLY BY THE CLEAN.
    #
    # Run 42959216978's vm-configure did EVERYTHING:
    #
    #     install the driver the agent talks on   PNPUTIL_EXIT=0
    #     install the guest agent                 MSIEXEC_EXIT=0
    #     prove the guest agent answers           service: Running / Automatic
    #     mount the host share                    -> 0   (New-SmbMapping)
    #     copy the launcher in                    -> 0
    #     prove the logon task is registered      TaskName: \vm-bootstrap / Status: Ready
    #     18 step(s) answered  ->  return 0
    #
    # and then exit 241 - `-15 & 0xFF` - before the compress in its `finally` could run. A stage
    # that had done everything published nothing.
    #
    # The same signal has killed macOS cleans the same way since 42913402988, and every candidate
    # for its sender has been excluded by measurement. What is established is that the work is
    # finished when it arrives, so one is survived and a second is obeyed.
    ep2 = read("lib/entrypoint.py")
    check("one late SIGTERM can be survived", "lib/entrypoint.py",
          "def survive_a_late_sigterm(" in ep2 and "SIGTERM ignored once" in ep2)
    check("a second signal is obeyed", "lib/entrypoint.py",
          "signal {_TOLERATED['signals']}" in ep2)
    # THE FIRST VERSION OF THIS CHECK NAMED `windows/11/devtools.py`, WHICH IS THE DEV JOB'S
    # PROGRAM. The vm-configure job runs `windows/11/vm_configure.py`, so the check passed on a
    # file the failing job never executed - and run 42959948715 reproduced the same exit 241 with
    # no `[entrypoint]` line. A check that names the file that happens to be open is not a check;
    # the loop below derives the file list from the shape instead.
    check("the vm-configure program installs the handler", "windows/11/vm_configure.py",
          "entrypoint.tolerate_one_sigterm()" in read("windows/11/vm_configure.py"))
    # AND THE TOLERANCE IS INSTALLED BY EVERY STAGE PROGRAM, NOT ONLY THE ONE THAT FAILED.
    #
    # The first version put `survive_a_late_sigterm()` into `windows/11/devtools.py` - which is
    # the DEV job's program - and dispatched the VM-CONFIGURE job, which runs
    # `windows/11/vm_configure.py`. Run 42959948715 then showed the same shape as the run before:
    #
    #     [devtools] 18 step(s) answered
    #     [share]    signalling smbd 286 with SIGTERM (this process is 173, group 173)
    #     ##[error] Process completed with exit code 241.
    #
    # with no `[entrypoint]` line at all, because the file that would have printed one was never
    # executed. A fix applied to "the file I was reading" instead of "the files that have this
    # shape" is how a bug survives a commit that names it.
    #
    # So the shape is the contract: every program a workflow runs that compresses a disk and has
    # its own __main__ installs the handler, and arms it where its own work becomes true.
    import glob
    stage_programs = [
        f for f in sorted(glob.glob("macos/**/*.py", recursive=True)
                          + glob.glob("windows/**/*.py", recursive=True))
        if "compress.compress(" in read(f) and 'if __name__ == "__main__":' in read(f)
    ]
    check("there are stage programs to check", "windows/11/vm_configure.py",
          len(stage_programs) >= 9)
    for prog in stage_programs:
        body = read(prog)
        check(f"{prog} installs the signal handler", prog,
              "entrypoint.tolerate_one_sigterm()" in body)
        check(f"{prog} arms it when its work is done", prog,
              "entrypoint.work_is_finished(" in body)
    # AND NOTHING IS TOLERATED BEFORE THE STAGE SAYS ITS OWN WORK IS FINISHED. One call for both
    # would ignore the first signal whenever it arrived, which a cancel satisfies just as well.
    ep3 = read("lib/entrypoint.py")
    check("installing the handler tolerates nothing yet", "lib/entrypoint.py",
          '_TOLERATED = {"armed": False' in ep3)
    check("only an armed stage survives a signal", "lib/entrypoint.py",
          '_TOLERATED["signals"] == 1 and _TOLERATED["armed"]' in ep3)
    check("an unarmed stage says the work has not finished", "lib/entrypoint.py",
          "and the work has NOT finished" in ep3)
    # AND THE ARM IS REACHABLE - the first attempt put it after `return 0`.
    import ast as _ast
    for prog in stage_programs:
        gtree = _ast.parse(read(prog))
        mains = [n for n in _ast.walk(gtree)
                 if isinstance(n, _ast.FunctionDef) and n.name == "main"]
        if not mains:
            continue
        arm_lines, ret_lines = [], []
        for node in _ast.walk(mains[0]):
            if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute) \
                    and node.func.attr == "work_is_finished":
                arm_lines.append(node.lineno)
            if isinstance(node, _ast.Return) and isinstance(node.value, _ast.Constant) \
                    and node.value.value == 0:
                ret_lines.append(node.lineno)
        if arm_lines and ret_lines:
            check(f"{prog} arms before it returns", prog, min(arm_lines) < min(ret_lines))
    # AND EVERY `needs.<job>.outputs` REFERENCE NAMES A JOB THAT EXISTS, THAT THIS JOB DECLARES
    # IN ITS OWN `needs`, AND THAT EXPORTS A `tag`.
    #
    # Three ways this was wrong at once, and any of them makes a chain that cannot reach its own
    # middle jobs:
    #
    #   windows-11.yml dev   `needs.share.outputs.tag` - `share` was renamed `vm-configure` and
    #                        this line kept the old name, so the second operand of the `||` was
    #                        always empty and `dev` could only start when `dev_from` was typed by
    #                        hand. It also had no `outputs:` at all, so `launch_dev` inherited
    #                        the same problem one job later.
    #   macos15.yml     dev  the condition was fixed to `needs.vm-configure` when the rename
    #                        happened, and the PULL step below it kept `needs.ssh` - which is
    #                        empty in a job whose `needs` is `vm-configure`. Reachable, and it
    #                        would have failed its first step.
    #
    # A missing or renamed job is not a YAML error (the expression is a string until GitHub
    # evaluates it, and an unknown `needs.<x>` evaluates to empty rather than raising), so the
    # file parses, the workflow loads, and the job is simply never selected. This check is the
    # only thing between that and another silent `skipped` in a run graph.
    try:
        import yaml as _yaml
    except ImportError:
        _yaml = None
    if _yaml is not None:
        import re as _re

        # AND IT PARSES WITHOUT A DUPLICATE KEY, WHICH `safe_load` DOES NOT REPORT.
        #
        # Fixing the `needs.share` condition introduced a SECOND `outputs:` block in the dev job,
        # because the first one was already there - I had decided it was missing from a diff of
        # the condition rather than from the file. PyYAML keeps the last duplicate key silently,
        # so this check passed on a file GitHub refused to load:
        #
        #     could not create workflow dispatch event: HTTP 422: Invalid Argument -
        #     failed to parse workflow: (Line: 551, Col: 5): 'outputs' is already defined
        #
        # A validator that tolerates what the runner rejects is worse than none, because it
        # turns "the workflow will not load" into "the job was skipped again".
        class _Strict(_yaml.SafeLoader):
            pass

        def _no_duplicates(loader, node, deep=False):
            seen = {}
            for key_node, _ in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in seen:
                    raise ValueError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
                seen[key] = True
            return _yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

        _Strict.add_constructor(_yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)
        for wf in ("windows-11.yml", "macos15.yml"):
            try:
                doc = _yaml.load(read(f".github/workflows/{wf}"), Loader=_Strict) or {}
            except ValueError as exc:
                check(f"{wf} has no duplicate keys", f".github/workflows/{wf}", False)
                continue
            check(f"{wf} has no duplicate keys", f".github/workflows/{wf}", True)
            wjobs = doc.get("jobs") or {}
            problems = []
            for jname, jbody in wjobs.items():
                text = _yaml.dump(jbody)
                declared = jbody.get("needs")
                declared = [declared] if isinstance(declared, str) else (declared or [])
                for ref in sorted(set(_re.findall(r"needs\.([A-Za-z0-9_-]+)\.outputs", text))):
                    if ref not in wjobs:
                        problems.append(f"{jname} needs.{ref}: no such job")
                    elif ref not in declared:
                        problems.append(f"{jname} needs.{ref}: not in its own needs {declared}")
                    elif not (wjobs[ref].get("outputs") or {}).get("tag"):
                        problems.append(f"{jname} needs.{ref}: that job exports no tag")
            check(f"{wf} only reads outputs it declares", f".github/workflows/{wf}",
                  not problems, )
    # AND `optional: true` IS NOT ON A STEP WHOSE FAILURE ANOTHER STEP WILL REPORT AS SOMETHING
    # ELSE. Run 42964952432 is the whole argument:
    #
    #     === install python
    #       'winget' is not recognized as an internal or external command,   (exit 1)
    #     ... seven times, each optional, each ignored ...
    #     === prove python node and git
    #       MISSING python / MISSING node / MISSING git
    #     === THE THREE THE macOS DISK ALSO HAS ARE NOT ALL HERE ===
    #
    # The verdict names three missing tools. The fact was one missing COMMAND, and seven steps
    # above it had already said so. A step that installs a package or proves the toolchain works
    # is a verdict, and a verdict cannot be optional: `optional` exists for a check whose
    # NEGATIVE result is information ("note what is absent"), not for the claim the stage makes.
    dev_yaml = read("windows/11/steps/dev.yaml")
    dev_doc = None
    try:
        import yaml as _y2
        dev_doc = _y2.safe_load(dev_yaml)
    except Exception:                                      # noqa: BLE001
        pass
    VERDICTS = ("install ", "build and run", "prove ")
    DIAGNOSTIC = ("note ",)
    if dev_doc:
        opt = [s["name"] for s in dev_doc["steps"]
               if s.get("optional") and not s["name"].startswith(DIAGNOSTIC)]
        check("no install or proof step on the windows dev disk is optional",
              "windows/11/steps/dev.yaml", not opt)
    # WINGET IS NOT ON THE PATH AN SSH SESSION SEES, and each step must resolve it itself.
    #
    # `winget` is an alias under %LOCALAPPDATA%\Microsoft\WindowsApps, registered per-user and
    # absent from the machine PATH. `ssh exec` gets the environment sshd captured, so it cannot
    # name winget - which is why all seven installs above failed with "not recognized" while the
    # bake's own `winget install` (typed into a real console session) worked.
    winget_steps = [s for s in (dev_doc or {}).get("steps", [])
                    if "winget install" in str(s.get("script", ""))]
    check("every winget install resolves the binary itself", "windows/11/steps/dev.yaml",
          len(winget_steps) >= 7 and all(
              "GetEnvironmentVariable('Path'" in s["script"] for s in winget_steps))
    check("no winget install relies on the bare name", "windows/11/steps/dev.yaml",
          not any(str(s.get("ssh", "")).startswith("winget") for s in
                  (dev_doc or {}).get("steps", [])))
    # AND THE CONFIGURATION STEPS WHOSE EFFECT IS INVISIBLE UNTIL A LATER STAGE ARE NOT OPTIONAL
    # EITHER. `hold windows update` and `turn off restore points` had no read-back at all: the
    # first lets an update land after the disk is published, the second lets restore points eat
    # ten gigabytes during the clean. `do not blank the screen` keeps its `optional` because
    # `prove the power settings took` reads MONITOR/STANDBY back - that is what a backstop is.
    win_ssh = read("windows/11/steps/ssh.yaml")
    win_doc = None
    try:
        import yaml as _y3
        win_doc = _y3.safe_load(win_ssh)
    except Exception:                                      # noqa: BLE001
        pass
    if win_doc:
        names = {s["name"]: s for s in win_doc["steps"]}
        for must_fail in ("hold windows update", "turn off restore points",
                          "no hibernation file"):
            check(f"{must_fail!r} can fail the stage", "windows/11/steps/ssh.yaml",
                  not names.get(must_fail, {}).get("optional", False))
        check("'do not blank the screen' keeps its read-back", "windows/11/steps/ssh.yaml",
              names.get("do not blank the screen", {}).get("optional", False))
    # AND `--override` TAKES WINGET'S OWN SWITCHES WITH IT, WHICH THE FILE SAID AND I DID NOT READ.
    #
    # Eight hundred lines above the step:
    #
    #     "a package with an --override also cannot take --accept-package-agreements - override
    #      replaces winget's own switch set"
    #
    # I wrote `--quiet --wait --norestart` beside `--override` anyway, and run 42965954028
    # answered in fifteen seconds:
    #
    #     Argument name was not recognized for the current command: '--quiet'
    #
    # The override string is handed to the Visual Studio bootstrapper, so the switches that would
    # have controlled winget belong INSIDE it. This is the second time in this thread that a
    # failure was already documented in the file it happened in.
    import yaml as _y4
    dev2 = _y4.safe_load(read("windows/11/steps/dev.yaml"))
    buildtools = next((x for x in dev2["steps"]
                       if x["name"] == "install the build tools"), {})
    # CODE, NOT PROSE. The note above the command explains the trap by naming the switches, so a
    # grep over the whole script finds them in a comment - which is exactly how the first version
    # of this check reported both halves as failing on a correct file.
    body = "\n".join(l for l in str(buildtools.get("script") or "").splitlines()
                      if not l.lstrip().startswith("#"))
    override_line = next((l for l in body.splitlines() if "--override" in l), "")
    check("the build tools override carries its own quiet/wait",
          "windows/11/steps/dev.yaml",
          "--quiet" in override_line and "--wait" in override_line)
    # THE OUTSIDE OF THE COMMAND MUST NOT ALSO CARRY THEM - winget rejects the whole invocation.
    outside = " ".join(l for l in body.splitlines() if "--override" not in l)
    check("no winget switch is passed beside --override", "windows/11/steps/dev.yaml",
          "--quiet" not in outside and "--wait" not in outside)
    # AND "ALREADY INSTALLED" IS A SUCCESS, WHICH WINGET REPORTS AS EXIT CODE 43.
    #
    # Run 42966695727, the first run where the BuildTools install ever worked - 13m42s, exit 0:
    #
    #     script install-buildtools.ps1 (powershell) -> 0
    #     === install the windows sdk
    #       Found an existing package already installed. Trying to upgrade ...
    #       No available upgrade found.
    #       No newer package versions are available from the configured sources.
    #       -> 43
    #
    # 43 is APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE - present, nothing to do. The VCTools
    # workload had already brought the SDK, which THIS FILE SAYS IT DOES seventeen lines above
    # the step. So the stage failed on its own success, and would have done so on every rebuild.
    #
    # `windows/11/devtools.py` already holds this judgement in Python; the YAML did not.
    dev3 = _y4.safe_load(read("windows/11/steps/dev.yaml"))
    plain = [x for x in dev3["steps"]
             if x["name"] in ("install the windows sdk", "install the dotnet sdk",
                              "install powershell 7", "install python", "install node",
                              "install git")]
    check("there are six plain winget installs to check", "windows/11/steps/dev.yaml",
          len(plain) == 6)
    for step in plain:
        body3 = str(step.get("script") or "")
        # AND THE COMPARISON IS ON THE LOW BYTE, BECAUSE $LASTEXITCODE IS AN HRESULT.
        #
        # Three attempts compared `$rc -eq 43` and all three failed, and the printed value is what
        # finally explained it:
        #
        #     WINGET_EXIT=-1978335189
        #
        # -1978335189 is 0x8A15002B - winget's APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE as a
        # signed int32 - and `& 0xFF` is the 43 that `lib/steps.py` logs as `-> 43`. The exit
        # status in the log and the value in the variable were the same fact spelled two ways,
        # and the branch compared one against the other.
        check(f"{step['name']} compares the low byte, not the HRESULT",
              "windows/11/steps/dev.yaml",
              "$low = $rc -band 0xFF" in body3 and "$low -eq 43" in body3)
        # ANY OTHER NON-ZERO STILL FAILS - the rule must not become "ignore winget".
        check(f"{step['name']} still fails on any other code",
              "windows/11/steps/dev.yaml", "exit $rc" in body3)
    # AND THE BUILD TOOLS ARE NOT IN THAT SET: an explicit --version either installs that
    # version or it does not, and 43 there would hide a pin that no longer exists.
    bt = next(x for x in dev3["steps"] if x["name"] == "install the build tools")
    check("the pinned build tools do not accept 43",
          "windows/11/steps/dev.yaml", "ALREADY_PRESENT" not in str(bt.get("script") or ""))
    # AND THE BRIDGE IS UP BEFORE THE SHARE IS SERVED, IN EVERY PROGRAM THAT DOES BOTH.
    #
    # `hostshare.start()` writes `interfaces = <bridge address>` into samba's config and waits for
    # smbd to bind there. That address is created by `net.up()`, which stages normally reach
    # through `vm.Guest.start()`. `lib/launch_selftest.py` calls `hostshare.start()` FIRST - it
    # has to, because the launcher waits for the share - and it did not call `net.up()` at all:
    #
    #     05:58:16  private dir ... exists=True writable=True entries=['msg.sock', ...]
    #     05:58:26  samba lost its private directory; recreating it and starting smbd once more
    #
    # smbd was asked to bind an address that did not exist, waited ten seconds, failed, and fell
    # into the tdb retry - which made the failure read as samba's problem rather than as an
    # ordering mistake in the caller. `lib/vm_session.py` learned this at run 42813552966 and says
    # so in capitals; this program never got the fix.
    import ast as _ast5
    launch_src = read("lib/launch_selftest.py")
    tree5 = _ast5.parse(launch_src)
    main5 = next(n for n in _ast5.walk(tree5)
                 if isinstance(n, _ast5.FunctionDef) and n.name == "main")

    def first_call_line(node, needle):
        hits = [c.lineno for c in _ast5.walk(node)
                if isinstance(c, _ast5.Call) and needle in _ast5.unparse(c.func)]
        return min(hits) if hits else None

    up_line = first_call_line(main5, ".up")
    share_line = first_call_line(main5, "hostshare.start")
    check("launch_selftest brings the bridge up", "lib/launch_selftest.py", up_line is not None)
    check("it brings the bridge up before serving the share", "lib/launch_selftest.py",
          up_line is not None and share_line is not None and up_line < share_line)
    # AND THE SAME RULE WHEREVER ELSE BOTH APPEAR.
    for prog in ("lib/vm_session.py",):
        src5 = read(prog)
        if "hostshare.start(" in src5 and "netmod.up(" in src5:
            check(f"{prog} serves the share after the bridge",
                  prog, src5.index("netmod.up(") < src5.index("hostshare.start("))
    # AND THE LAUNCH AGENT RETRIES ONCE, BECAUSE `RunAtLoad` IS A SINGLE INSTANT AND THAT INSTANT
    # IS NOT ALWAYS USABLE.
    #
    # Run 42971367742 logged in, a Keyboard Setup Assistant appeared 36 seconds AFTER the password
    # went in, and no ~/shared/hello.txt was written. The plist was in ~/Library/LaunchAgents and
    # the agent's own log was empty - so launchd either fired at an unusable moment or fired into a
    # session that was not ready, and `RunAtLoad` has nothing after it to fall back on.
    #
    # `StartInterval` IS THAT FALLBACK and the launcher's own marker is what makes it safe: a
    # second run after a successful one finds the marker and does nothing, which the launcher's
    # comment already describes. 90 s is past the launcher's one-minute wait for ~/shared, so the
    # retry cannot overlap the first attempt, and inside the probe's 180 s window at most two fit.
    import plistlib as _plistlib
    plist_doc = None
    try:
        plist_doc = _plistlib.loads(read("lib/bootstrap.py").split('return f"""', 1)[1]
                                    .split('"""', 1)[0]
                                    .replace("{MACOS_LABEL}", "org.vm.bootstrap")
                                    .replace("{launcher}", "/tmp/l.sh").encode())
    except Exception:                                      # noqa: BLE001
        pass
    if plist_doc is not None:
        check("the launch agent still runs at load", "lib/bootstrap.py",
              plist_doc.get("RunAtLoad") is True)
        check("the launch agent retries once", "lib/bootstrap.py",
              plist_doc.get("StartInterval") == 90)
        check("the retry is not a supervisor", "lib/bootstrap.py",
              plist_doc.get("KeepAlive") is False)
    # AND THE FAILED LAUNCH PRINTS ITS DIAGNOSIS EVEN WHEN IT FINDS NOTHING. `|| true` and a
    # `2>/dev/null` made a missing file indistinguishable from an unanswered ssh call: the run
    # printed the block's two headers and then nothing, so the whole story was "the launcher did
    # not run" when the useful fact was whether the agent had ever been loaded.
    ls_src = read("lib/launch_selftest.py")
    check("a failed launch says what each probe returned", "lib/launch_selftest.py",
          'ssh -> {st}, {len(body)} byte(s)' in ls_src)
    check("and asks the plist where it actually lives", "lib/launch_selftest.py",
          "~/Library/LaunchAgents/" in ls_src and "/tmp/org.vm.bootstrap.err" in ls_src)
    # AND THE MACOS MOUNT POINT IS NOT CREATED WHEN IT IS ALREADY A MOUNT POINT.
    #
    # Run 42973656275, the second launch after the share had ever been mounted:
    #
    #     mount -> 64
    #         mount_smbfs: mount error: /Users/vmuser/shared: File exists
    #     === THE SHARE DID NOT MOUNT; THE LAUNCHER WILL FIND NO bootstrap.sh ===
    #
    # `mkdir -p ~/shared` was the first thing the command did on every call. On a disk that has
    # never mounted the share that is necessary; on one that has - which is every run after the
    # first, because ~/shared lives in the guest's home and the home is on the disk - the
    # directory is already there and macOS refuses to mount OVER an existing directory.
    #
    # `mount` IS THE TEST, NOT `test -d`: a mount point IS a directory, so `-d` cannot tell the
    # two apart, which is the confusion that produced `File exists`.
    hs2 = read("lib/hostshare.py")
    mac_cmd = None
    try:
        import sys as _sys
        if REPO not in _sys.path:
            _sys.path.insert(0, REPO)
        import hostshare as _hs
        mac_cmd = _hs.mount_commands("172.30.0.1", login_password="admin")["macos"]
    except Exception:                                      # noqa: BLE001
        pass
    if mac_cmd:
        check("a macos mount tests for an existing mount", "lib/hostshare.py",
              'mount | grep -q' in mac_cmd)
        check("it does not blindly mkdir over a mount point", "lib/hostshare.py",
              not mac_cmd.lstrip().startswith("mkdir"))
        # AN EMPTY LEFTOVER IS CLEARED; A NON-EMPTY ONE IS REFUSED BY NAME rather than mounted
        # over, because a directory with files in it is somebody's data and not this command's
        # to delete.
        check("an empty leftover is removed", "lib/hostshare.py", "rmdir" in mac_cmd)
        check("a non-empty leftover is reported, not deleted", "lib/hostshare.py",
              "NOT_EMPTY" in mac_cmd)
        # AND AN EXISTING MOUNT IS UNMOUNTED, NOT SKIPPED. `mount_smbfs` refuses a mount point
        # that is still a mount with the SAME `File exists` it gives for a bare directory, so the
        # two states want opposite fixes and produce one message. Run 42975762321 proved the skip
        # was wrong: the log showed the mount command's own error and NONE of the preparation,
        # because the preparation had been skipped as "already fine".
        mount_script = getattr(_hs, "MACOS_MOUNT_SCRIPT", "")
        check("an existing mount is unmounted first", "lib/hostshare.py",
              'mounted" = yes' in mount_script and "umount" in mount_script)
        # AND EVERY PRIVILEGED STEP IS INSIDE ONE `sudo sh -s`, because a password can only be
        # fed to sudo once per command: `printf pw | sudo -S a; sudo b` gives the second sudo a
        # closed stdin. The script arrives on stdin so nothing in it survives another quoting
        # layer.
        check("the privileged steps share one sudo", "lib/hostshare.py",
              "sudo -S -p '' sh -s" in mac_cmd)
        check("the mount script arrives on stdin", "lib/hostshare.py",
              "MACOS_MOUNT_SCRIPT" in read("lib/hostshare.py"))
    # AND A REGISTERED PER-USER MSIX IS REMOVED BEFORE THE MACHINE MSI IS INSTALLED.
    #
    # Run 42973132779, the first run where the low-byte fix let the powershell step get past its
    # own exit-code check:
    #
    #     Found PowerShell [Microsoft.PowerShell] Version 7.6.10.0
    #     Successfully verified installer hash
    #     The current system configuration does not support the installation of this package.
    #     WINGET_EXIT=-1978334957          (0x8A150113)
    #
    # The file already warned that Microsoft.PowerShell ships five installers with the msix ones
    # first and that `--scope machine` picks the wix/MSI. What it did not say is that winget still
    # FINDS the msix first and refuses to install over it: the scope chooses what to install, not
    # what to ignore. So the registered copy is removed first, and a failure to remove is printed
    # rather than swallowed.
    dev4 = _y4.safe_load(read("windows/11/steps/dev.yaml"))
    ps7 = next((x for x in dev4["steps"] if x["name"] == "install powershell 7"), {})
    body4 = str(ps7.get("script") or "")
    # THE INSTALLER *TYPE* IS NAMED, AND `--scope machine` ALONE IS NOT THE SAME THING.
    #
    #     Found PowerShell [Microsoft.PowerShell] Version 7.6.10.0
    #     Successfully verified installer hash
    #     The current system configuration does not support the installation of this package.
    #     WINGET_EXIT=-1978334957          (0x8A150113)
    #
    # READ THE MISSING LINE. The dotnet SDK, in the same run, printed `Downloading https://...`
    # before installing. This step went from "licensed to you by its owner" straight to
    # "Successfully verified installer hash" - so it was not fetching the MSI at all. It had
    # selected a different INSTALLER TYPE that was already on the machine, and a packaged app
    # (MSIX) cannot be installed in the system context - which is what the message says.
    #
    # The file already listed both types four sentences above the step and concluded that
    # `--scope machine` "selects the wix/MSI variant". A scope is not a type: winget's maintainer
    # says as much in microsoft/winget-pkgs#367912 - "you can use --installer-type to force WinGet
    # to use the desired installer type" - and that thread shows 7.5.4 downloading its MSI while
    # 7.5.5+ fails exactly as this run did.
    check("powershell installs the wix/MSI installer type",
          "windows/11/steps/dev.yaml", "--installer-type wix" in body4)
    check("and still asks for machine scope",
          "windows/11/steps/dev.yaml", "--scope machine" in body4)
    check("a failed clean reports what DHCP saw", "lib/clean_step.py",
          "DHCPDISCOVER" in read("lib/clean_step.py")
          or "dnsmasq.vm.log" in read("lib/clean_step.py"))
    check("the guest start passes its MAC to the bridge", "lib/vm.py",
          "mac=self.shape.tap_mac" in vm_src)
    # AND THE DEFAULTS HAVE TO BE FILLED FIRST, or the MAC arrives empty and the check above is
    # decorative.
    start_at = vm_src.index("mac=self.shape.tap_mac")
    check("the MAC default is filled before the bridge is set up", "lib/vm.py",
          vm_src.index("self.shape.tap_mac = self.shape.tap_mac or net.GUEST_MAC") < start_at)

    # AND THE SHARE STARTS AFTER THE BRIDGE IT BINDS TO. smbd's `interfaces` line names
    # net.BRIDGE_IP, and net.up() is what creates that address - so a share started first
    # listens on nothing and reports "smbd DID NOT BIND 172.30.0.1:445 IN 10s" while samba's own
    # log says "waiting for connections". Run 42813552966 lost a macOS ssh stage to it before
    # its first step. The order is asserted, because it is one line of setup either way round.
    vs_src = read("lib/vm_session.py")
    check("the bridge is up before the share binds", "lib/vm_session.py",
          vs_src.index("netmod.up(") < vs_src.index("hostshare.start(served)"))
    # AND THE SSH STAGE DOES NOT ASK FOR A SHARE AT ALL: its disk predates vm-configure, so no
    # launchd job mounts one. Serving it unasked turns an smbd problem into a boot failure for a
    # guest that never wanted a share.
    check("the ssh stage does not serve a share it has no job for", "macos/15/ssh.py",
          'share="",' in read("macos/15/ssh.py"))

    # --- THE ACCOUNT NAME, WHICH IS BAKED INTO EACH DISK -------------------------------
    #
    # The name is not a setting. Windows' answer file CREATES it, so sshd admits it and
    # `C:/Users/<name>` is where the launcher and the task registration live; macOS' Setup
    # Assistant CREATES it, so it is the account every later stage logs in as. A rename that
    # misses one of these produces a disk that answers nothing, three stages after the mistake -
    # which is the same shape as the `dockeruser` failure lib/session.py records.
    #
    # SO THE RULES ARE: the answer file names the account through a PLACEHOLDER (one literal,
    # substituted from one variable), and no stage script falls back to the old name.
    win_answer_src = read("windows/11/answer/Autounattend.xml")
    check("the windows account comes from a placeholder", "windows/11/answer/Autounattend.xml",
          win_answer_src.count("__ADMIN_USER__") == 3)
    check("the windows answer file does not name the old account",
          "windows/11/answer/Autounattend.xml", ">docker<" not in win_answer_src)
    bake_win = read("windows/11/bake.py")
    check("the windows account is substituted from one variable", "windows/11/bake.py",
          'os.environ.get("WINDOWS_ADMIN_USER", "vmuser")' in bake_win
          and '"__ADMIN_USER__": account' in bake_win)
    for path in ("windows/11/ssh.py", "windows/11/vm_configure.py", "windows/11/devtools.py"):
        check(f"{path} reads the account the disk was built with", path,
              'os.environ.get("WINDOWS_ADMIN_USER", "vmuser")' in read(path))
    # MACOS CREATES THE ACCOUNT THROUGH THE WIZARD, so the file's `vars:` is the only place the
    # name appears - and `scutil` must use that value, not a literal.
    for path in ("macos/15/steps/sequoia.yaml", "macos/15/steps/ssh.yaml",
                 "macos/15/steps/vm-configure.yaml", "macos/15/steps/dev.yaml"):
        walk = load_yaml(path)
        users = [v for k, v in (walk.get("vars") or {}).items() if k == "user"]
        check(f"{path} names the account vmuser", path,
              bool(users) and users[0].get("default") == "vmuser")
    check("the macos host name is the account, not a literal", "macos/15/steps/ssh.yaml",
          "scutil --set LocalHostName {{ user }}" in read("macos/15/steps/ssh.yaml"))
    # AND THE STAGE SCRIPTS AGREE. A script that still defaults to `docker` would log into a disk
    # whose account is `vmuser` and report Permission denied against a working sshd.
    for path in ("macos/15/ssh.py", "macos/15/bake.py", "macos/15/desktop.py",
                 "macos/15/dev.py", "macos/15/vm_configure.py"):
        body = read(path)
        check(f"{path} does not default to the old account", path,
              'MACOS_USER", "docker"' not in body and 'MACOS_USER", "vmuser"' in body)
    # AND THE WORKFLOWS PASS THE SAME NAME, because the scripts read it from the environment.
    mac_workflows = read(".github/workflows/macos15.yml")
    check("the macos workflow logs in as vmuser", ".github/workflows/macos15.yml",
          "MACOS_USER=vmuser" in mac_workflows and "MACOS_USER=docker" not in mac_workflows)
    win_workflow = read(".github/workflows/windows-11.yml")
    check("the windows workflow passes the account to both jobs",
          ".github/workflows/windows-11.yml",
          win_workflow.count("WINDOWS_ADMIN_USER=vmuser") == 2)

    # AND NO WALKTHROUGH MAY NAME A SHUTDOWN METHOD ITS OWN FAMILY CANNOT HONOUR.
    #
    # This is the check for run 42822632514: macos/15/steps/setup.yaml ended on
    # `shutdown: "{{ shutdown }}"` with a default of `acpi`, the walkthrough worked perfectly -
    # the Setup Assistant appeared - and the LAST step raised, because macOS refuses the power
    # button. Forty minutes of installation, a correct disk, and nothing published.
    #
    # A FILE THAT CANNOT FINISH ITS LAST STEP CAN BE READ BEFORE IT STARTS. The method may be a
    # `{{ var }}` (it usually is), so the check reads the var's default too.
    macos_files = ["macos/15/steps/setup.yaml", "macos/15/steps/sequoia.yaml",
                   "macos/15/steps/ssh.yaml", "macos/15/steps/dev.yaml",
                   "macos/15/steps/vm-configure.yaml"]
    for path in macos_files:
        walk = load_yaml(path)
        default = ((walk.get("vars") or {}).get("shutdown") or {}).get("default")
        methods = [s.get("shutdown") for s in (walk.get("steps") or []) if s.get("shutdown")]
        check(f"{path} does not ask macOS for the power button", path,
              "acpi" not in (default or "")
              and not any(m == "acpi" for m in methods))

    # AND `requires:` RUNS BEFORE `boot:`, WHICH IS ONE OF THE STEPS THAT CAN CARRY IT.
    #
    # The first version sat after the block that defines `login`, so `boot: requires: [overlay]`
    # was never checked AND the check raised UnboundLocalError on the steps that were reached -
    # run 42804130212 died on its second step. The check reads the account from the driver now,
    # which is available before anything else.
    steps_src_early = read("lib/steps.py")
    check("requires is checked before the lifecycle steps", "lib/steps.py",
          steps_src_early.index("requires:` IS CHECKED") <
          steps_src_early.index('if step["boot"]:'))
    check("the capability check does not depend on a later local", "lib/steps.py",
          'user=values.get("ssh_user") or "vmuser"' in steps_src_early)

    # AND `shutdown:` TAKES A METHOD, NOT A BOOLEAN, WITH NO SILENT CHAIN BEHIND IT.
    steps_src = read("lib/steps.py")
    check("shutdown names a method", "lib/steps.py",
          all(m in steps_src for m in ("guest_agent", '"ssh"', '"acpi"', '"kill"')))
    check("a named shutdown does not fall back", "lib/vm_session.py",
          "the guest agent did not power the machine down" in read("lib/vm_session.py"))
    # MACOS REFUSES THE POWER BUTTON BEFORE WAITING FOR IT, which is the whole point.
    check("acpi is refused on a guest that cannot honour it", "lib/steps.py",
          "shutdown: acpi` cannot work on macOS" in steps_src)

    # THE TWO EVIDENCE PRIMITIVES EXIST, AND THE EXAMPLE USES EVERY PRIMITIVE.
    check("a walkthrough can take a screenshot", "lib/steps.py",
          '"screenshot"' in steps_src)
    check("a walkthrough can record", "lib/steps.py", '"record"' in steps_src)
    try:
        example = load_yaml("macos/15/steps/primitives.yaml")
        names = [s.get("name") for s in (example.get("steps") or [])]
        keys = set()
        for one in (example.get("steps") or []):
            keys |= set(one)
        check("the primitives example boots", "macos/15/steps/primitives.yaml",
              "boot the disk" in names)
        check("the primitives example proves the whole vocabulary",
              "macos/15/steps/primitives.yaml",
              {"boot", "require_ssh", "ssh", "scp", "python", "share_serve", "share_mount",
               "reboot", "shutdown", "screenshot", "record"} <= keys)
        check("the primitives example declares a capability",
              "macos/15/steps/primitives.yaml", "requires" in keys)
    except Exception as exc:                          # noqa: BLE001
        check(f"primitives.yaml loads ({exc})", "macos/15/steps/primitives.yaml", False)

    # --- A RELATIVE `scp:` PATH CAN COME FROM TWO PLACES ---------------------------------
    #
    # A file checked in with the walkthrough lives under REPO; a file a `python:` step generated
    # lives under the driver's work directory. Resolving every relative path against REPO meant
    # the download reported 30900 KiB and the copy looked 30 MiB away:
    #
    #     scp: stat local "…/guest-agent/virtio-win-guest-tools.exe": No such file or directory
    #
    # run 42796278936. The work directory wins when it has the file, because that is the one this
    # run just made.
    check("a relative scp path can be a file a python step just wrote", "lib/steps.py",
          'candidate = os.path.join(work, local)' in read("lib/steps.py"))
    # AND EVERY CALLER UNPACKS WHAT start() ACTUALLY RETURNS.
    #
    # It gained an overlay when verification stages moved off `shutil.copyfile`, and clean_step
    # was not updated:
    #
    #     guest, reached = vm_session.start(...)
    #     ValueError: too many values to unpack (expected 2)
    #
    # which failed on the ASSIGNMENT, before booting anything - and the run then looked like a
    # 900-second wait for a guest that had never started (run 42797599214's clean). A grep is
    # enough to keep the four callers in step, and it is the only place that can.
    import re as _re
    bad = []
    for name in ("lib/steps.py", "lib/boot_machine.py", "lib/clean_step.py"):
        for line in read(name).splitlines():
            m = _re.match(r"\s*([\w,\s]+?)\s*=\s*vm_session\.start\(", line)
            if m and len([p for p in m.group(1).split(",") if p.strip()]) != 3:
                bad.append(f"{name}: {line.strip()[:60]}")
    if bad:
        check(f"every vm_session.start caller unpacks three values ({'; '.join(bad)})",
              "lib/vm_session.py", False)
    else:
        check("every vm_session.start caller unpacks three values", "lib/vm_session.py", True)

    # --- A YAML FILE CAN OWN ITS MACHINE -------------------------------------------------
    #
    # `boot:`, `reboot:` and `shutdown:` are steps, so the whole qemu lifecycle can be declared
    # instead of living in a stage script. Three things have to hold for that to work, and each
    # one was a way it could silently not.
    try:
        import steps as _steps
        check("the lifecycle steps exist", "lib/steps.py",
              {"boot", "reboot", "shutdown"} <= _steps.LIFECYCLE)
        # A FILE THAT BOOTS ITS OWN MACHINE CANNOT BE HANDED A SOCKET, because the socket does
        # not exist until the first step runs. A Driver built the normal way waits out its 180
        # seconds for a file nothing has written.
        check("a booting file gets a deferred driver", "lib/steps.py",
              "isinstance(driver, DeferredDriver)" in read("lib/steps.py")
              or "DeferredDriver(" in read("lib/steps.py"))
        # AND THE FILE IS RESPONSIBLE FOR WHAT IT STARTED: a failed walkthrough that leaves qemu
        # running holds /dev/kvm for every later job in the same container.
        check("a booting file stops what it started", "lib/steps.py",
              "stop_if_running" in read("lib/steps.py"))
        # THE MACHINE COMES FROM vm_session, so there is still one description of a macOS guest.
        check("the lifecycle reuses vm_session", "lib/steps.py",
              "vm_session.start(" in read("lib/steps.py"))
        # AND A REBOOT RECONNECTS. The OCR Screen holds a QMP connection; the first step after a
        # reset talks to a closed socket unless the driver rebuilds it.
        check("a reboot rebuilds the connection", "lib/steps.py",
              "def reconnect" in read("lib/steps.py"))
        # THE EXAMPLE IS A FILE THAT PARSES, because an example that does not is a lie about the
        # vocabulary.
        example = load_yaml("macos/15/steps/lifecycle.yaml")
        names = [s.get("name") for s in (example.get("steps") or [])]
        check("the lifecycle example boots", "macos/15/steps/lifecycle.yaml",
              "boot the disk" in names)
        check("the lifecycle example reboots", "macos/15/steps/lifecycle.yaml",
              "reboot into the same system" in names)
        # `shutdown: kill` NOW, NOT `graceful: false`: the method is named rather than inferred.
        check("the lifecycle example names its shutdown method", "macos/15/steps/lifecycle.yaml",
              any(isinstance(s.get("shutdown"), str) and s["shutdown"] in
                  ("kill", "guest_agent", "ssh", "acpi")
                  for s in (example.get("steps") or [])))
    except Exception as exc:                          # noqa: BLE001
        check(f"lifecycle imports ({exc})", "lib/steps.py", False)

    # --- ONE qemu-img, AND IT IS NOT THE ANDROID ONE ---------------------------------------
    #
    # THE ROOT CAUSE OF SIX RUNS OF FAILURES, and of a workaround in three modules. The
    # Containerfile put the Android SDK ahead of /usr/bin on PATH for EVERY platform's container:
    #
    #     ENV PATH="$ANDROID_SDK_ROOT/platform-tools:$ANDROID_SDK_ROOT/emulator:$PATH"
    #
    # and the emulator ships a 2018 qemu-img. So a bare `qemu-img` in a macOS or Windows bake
    # meant that binary, and it answered
    #
    #     qemu-img: Could not open '...': Unsupported qcow2 feature(s): compression type
    #
    # about a disk this repository had compressed itself one step earlier (run 42775862720).
    try:
        containerfile = read("Containerfile")
        android_env = [line for line in containerfile.splitlines()
                       if line.startswith("ENV PATH=") and "android" in line.lower()]
        check("the Android SDK is not on the shared PATH", "Containerfile", not android_env)
        # AND WHAT NEEDS IT NAMES IT, so removing the PATH entry did not just move the breakage.
        check("the emulator resolves its binaries from the SDK", "lib/emulator.py",
              "sdk_binary(" in read("lib/emulator.py"))
        check("android/15 resolves adb from the SDK", "android/15/run_qemu.py",
              "_adb_binary(" in read("android/15/run_qemu.py"))
        check("the image build checks the SDK by path", "lib/build_image.py",
              "/opt/android-sdk/emulator/emulator" in read("lib/build_image.py"))
        # AND THE COMPRESSION AND CLEAN STEPS STILL NAME THEIR BINARY, which is right on its own
        # merits: four independent call sites re-derived this mistake and two of them failed with
        # a message that named neither the binary nor the disk.
        check("compression names the binary it uses", "lib/compress.py",
              "QEMU_IMG_BINARY" in read("lib/compress.py"))
        check("the disk check uses the same binary as compression", "lib/clean_step.py",
              "compresslib.qemu_img()" in read("lib/clean_step.py"))
        # AND A COMPRESSED DISK IS NOT BOOTED WITH discard=unmap.
        #
        # `discard=unmap` frees clusters as the guest TRIMs them - right for a stage that has
        # finished writing, and wrong for one still reading a zstd qcow2. The kernel stops at
        #
        #     #[EB|LOG:HANDOFF TO XNU] _
        #
        # with no panic (run 42778795463: the clean stage, on the artifact share had just
        # compressed). machine.py already records the trade for the installer, which is why
        # TARGET_DRIVE_PLAIN exists; the clean is the same case one stage later.
        check("the clean boots a compressed disk without discard", "lib/clean_step.py",
              "discard=False" in read("lib/clean_step.py"))
        check("vm_session can omit discard", "lib/vm_session.py",
              "TARGET_DRIVE_PLAIN" in read("lib/vm_session.py"))

        # AND THE VERIFICATION PATHS BOOT AN OVERLAY, NOT A COPY.
        #
        # `shutil.copyfile` on a 15-27 GiB disk cost minutes and a second copy of everything, and
        # protected the source only by nobody opening it. A qcow2 backing chain is the same
        # guarantee for the price of what the run writes, and qemu opens the parent READ-ONLY -
        # so the artifact cannot be written at all, not merely "is a different file".
        check("vm_session boots a copy on an overlay", "lib/vm_session.py",
              "overlaymod.Overlay(" in read("lib/vm_session.py"))
        check("the launch self-test is on an overlay", "lib/boot_machine.py",
              "run_overlay" in read("lib/boot_machine.py"))
        check("the windows launch is on an overlay", "lib/launch_windows.py",
              "overlaymod.Overlay(" in read("lib/launch_windows.py"))
        # AND THE BASE IS CHECKED, because "I did not write to it" is the assumption this whole
        # mechanism exists to stop making.
        check("the overlay proves the base unchanged", "lib/overlay.py",
              "the base is a published artifact and must never be written to" in read("lib/overlay.py"))
    except Exception as exc:                          # noqa: BLE001
        check(f"containerfile reads ({exc})", "Containerfile", False)

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
    for path in ("macos/15/steps/vm-configure.yaml", "windows/11/steps/vm-configure.yaml"):
        walk = load_yaml(path)
        names = [s.get("name") for s in (walk.get("steps") or [])]
        check(f"{path} writes the launcher", path,
              any((n or "").startswith("write the launcher") for n in names))
        check(f"{path} serves the share", path, "serve the host share" in names)
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
        # `New-SmbMapping -UserName guest -Password ''` - the credential is an object, not a
        # positional token, which is what `net use` could not do (see the note above).
        check("the windows mount passes an empty password", "lib/hostshare.py",
              f"-UserName {hostshare.SMB_USER} -Password ''" in win)
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
        # AND /usr/local/bin HAS TO BE CREATED. It is where Homebrew puts things and this disk has
        # no Homebrew yet - the toolchain stage comes after this one - so `cp` into it answered
        # "No such file or directory", which reads like a missing source and is a missing
        # directory. Run 42755669848.
        # THE DAEMON INSTALL MOVED INTO THE WALKTHROUGH when the five-in-one step was split, so
        # the check reads the file that now performs it.
        check("2. the daemon install creates /usr/local/bin", "macos/15/steps/vm-configure.yaml",
              "mkdir -p /Library/LaunchDaemons /usr/local/bin" in
              read("macos/15/steps/vm-configure.yaml"))
        # AND THE DAEMON MUST NOT RETRY IN A TIGHT LOOP. `KeepAlive SuccessfulExit false` alone
        # restarts it whenever it exits non-zero, and the script exits 1 after its own minute of
        # retries - so any machine without a share running (the clean stage, a published disk
        # somebody booted on its own) would attempt a mount every sixty seconds forever.
        plist = bootstrap.macos_mount_plist("/usr/local/bin/vm-smbmount.sh")
        check("2. the mount daemon throttles its retries", "lib/bootstrap.py",
              "<key>ThrottleInterval</key>" in plist)
        # AND IT IS A PLIST, not something that merely looks like one.
        import plistlib
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".plist", delete=False) as fh:
            fh.write(plist)
            path = fh.name
        try:
            parsed = plistlib.load(open(path, "rb"))
            check("2. the mount plist parses", "lib/bootstrap.py",
                  parsed["Label"] == bootstrap.MACOS_MOUNT_LABEL
                  and parsed["ThrottleInterval"] > 0)
        finally:
            os.unlink(path)
        # AND NOTHING ANYWHERE STILL SAYS THE OLD NAME, which is how a rename half-happens.
        for path in ("lib/bootstrap.py", "lib/steps.py", "lib/launch_selftest.py",
                     "macos/15/steps/vm-configure.yaml", "windows/11/steps/vm-configure.yaml"):
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
    # THE STEP IS A `script:` NOW, NOT AN `ssh:` ONE-LINER, because the command has to resolve
    # winget's own path first (see the note on that below) and a line that long does not survive
    # ssh -> cmd - which this file already says about the Build Tools install. So the check reads
    # whichever body the step has, rather than the one it happened to have when it was written.
    pwsh_body = str(pwsh.get("script") or pwsh.get("ssh") or "")
    check("powershell installs the MSI, not the per-user msix",
          "windows/11/steps/dev.yaml", "--scope machine" in pwsh_body)

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
    # THE COPY MOVED TO lib/vm_session.py, and it is a PARAMETER there because cleaning must not
    # copy: clean modifies the disk in place on purpose. So the check is that both callers ask for
    # what they need rather than that one file contains a copyfile.
    check("the self-test copies the disk instead of opening the artifact",
          "lib/vm_session.py", "shutil.copyfile" in read("lib/vm_session.py")
          and "copy=True" in read("lib/boot_machine.py"))
    check("the clean does NOT copy the disk it is about to publish",
          "lib/clean_step.py", "copy=False" in read("lib/clean_step.py"))
    check("the self-test reads hello.txt from the share",
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
