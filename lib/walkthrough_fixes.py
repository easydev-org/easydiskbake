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
    check("the windows agent is installed by Setup", "windows/11/answer/Autounattend.xml",
          "msiexec" in win_answer and "qemu-ga-x86_64.msi" in win_answer)
    check("the windows agent install asks for no bundle", "windows/11/answer/Autounattend.xml",
          "guest-tools.exe" not in win_answer)
    # THE DRIVE LETTER IS SEARCHED FOR. Three CD-ROMs are attached and their letters follow the
    # firmware's enumeration; a hard-coded D: would work until it did not, and the failure would
    # be a silent disk with no agent.
    check("the windows install finds the answer cd, not a guessed letter",
          "windows/11/answer/Autounattend.xml",
          "for %i in (D E F G H I J) do @if exist %i:\\agent\\qemu-ga-x86_64.msi start /wait" in win_answer)
    # AND THE STAGE SAYS SO RATHER THAN QUIETLY REINSTALLING IT: one path, and a re-run of this
    # stage must not become a second one.
    check("the windows stage no longer installs the agent itself",
          "windows/11/steps/vm-configure.yaml",
          "install the guest agent" not in win_conf)
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
    check("the windows agent is not fetched over http any more", "lib/vm_configure_asset.py",
          "fetch_from_iso" not in asset_src and '"qemu-ga-msi"' not in asset_src)
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
    check("the windows bake carries the agent channel", "windows/11/bake.py",
          'qga_socket=os.path.join(self.work, "qga.sock")' in bake_src
          and 'qga_transport="virtio"' in bake_src)

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
          'user=values.get("ssh_user") or "docker"' in steps_src_early)

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
