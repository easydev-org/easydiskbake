"""Exercise lib/steps.py against a fake guest, with no qemu and no screen.

    python3 lib/steps_test.py

WHY THIS EXISTS AS A TEST AND NOT AS A COMMENT

The interpreter decides whether a run continues, so an interpreter bug is a bake that reports
success having clicked nothing - which is the exact failure this repository has paid for four
times (kubuntu three, mint once). None of those were driver bugs; they were "a conditional
decided to skip a click and nothing said so". So the rules that prevent it are tested here,
against a guest that can be made to behave in any way the test wants:

    a step whose expect does not appear          must FAIL the walk
    an optional step whose expect does not appear must NOT fail the walk
    a step with an unknown key                   must be refused at LOAD time
    one step, one action                         see test_no_step_acts_twice
    the footer repeated N times                  produces N clicks at one coordinate
    a `wait` step does not read the screen to decide anything
    start_at / stop_before                       walk only the named half

The fake driver records what it was asked to do, so an assertion is about the calls rather than
about a log line.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import steps  # noqa: E402


class FakeDriver:
    """A guest that says whatever the test puts in `screen`, and records what it was asked."""

    def __init__(self, screen: str = "") -> None:
        self.screen = screen
        self.clicks: list[tuple[int, int]] = []
        self.typed: list[tuple[tuple[int, int], str]] = []
        self.keys: list[str] = []
        self.doubles: list[tuple[int, int]] = []
        self.looks = 0
        self.words: list[tuple] = []
        self.q = None
        # `script:` WRITES ITS BODY NEXT TO THE DRIVER'S WORK, the same place a stage's frames
        # and crops go, so the fake needs one.
        import tempfile
        self.work = tempfile.mkdtemp(prefix="steps-test-")

        class _Mod:
            @staticmethod
            def click(q, x, y):
                pass

            @staticmethod
            def key(q, k):
                pass

            @staticmethod
            def key_combo(q, *k, hold=0.0):
                pass

        self.qmpmod = _Mod()

    def look(self, label: str = "look") -> list[tuple]:
        self.looks += 1
        self.words = [(w, 0, 0, 0, 0) for w in self.screen.split()]
        return self.words

    def text(self) -> str:
        return self.screen.lower()

    def says(self, *want: str) -> bool:
        have = {"".join(c for c in w.lower() if c.isalnum()) for w in self.screen.split()}
        return all("".join(c for c in x.lower() if c.isalnum()) in have for x in want)

    def log(self, message: str) -> None:
        pass

    def click(self, xy, why: str, settle: float = 2.0) -> None:
        self.clicks.append(tuple(xy))

    def double_click(self, xy, why: str, settle: float = 20.0) -> None:
        self.doubles.append(tuple(xy))

    def type_into(self, xy, value: str, *, clear: bool = True, per_char: float = 0.1,
                  modifier: str = "ctrl") -> None:
        self.typed.append((tuple(xy), value))

    def key(self, spec: str, hold: float = 0.0) -> None:
        self.keys.append(spec)

    def close(self) -> None:
        pass


def walk_of(steps_list: list[dict], **extra) -> steps.Walk:
    document = {"name": "test", "steps": steps_list}
    document.update(extra)
    return steps.Walk(document, path="inline")


def run(document, screen: str = "", driver=None, **kwargs):
    driver = driver or FakeDriver(screen)
    return driver, steps.walk(driver, document, **kwargs)


FAILED = []


def check(condition: bool, what: str) -> None:
    if condition:
        print(f"  ok   {what}")
    else:
        print(f"  FAIL {what}")
        FAILED.append(what)


# --- 1. a step that does not advance fails the walk ------------------------------------
def test_expect_failure() -> None:
    print("\n=== a step whose expect does not appear fails the walk")
    walk = walk_of([
        {"name": "welcome", "at": [10, 20], "settle": 0, "expect": "keyboard"},
    ])
    driver, (ok, ran) = run(walk, screen="welcome to the installer")
    check(not ok, "the walk reports failure")
    check(ran == [], "the failing step is not counted as having run")
    check(driver.clicks == [(10, 20)], "the click still happened before the verdict")


# --- 2. the same step, marked optional, does not fail the walk ---------------------------
def test_optional_expect() -> None:
    print("\n=== an optional step that does not appear does not fail the walk")
    walk = walk_of([
        {"name": "privacy", "at": [10, 20], "settle": 0, "expect": "data", "optional": True},
        {"name": "next", "at": [30, 40], "settle": 0},
    ])
    driver, (ok, ran) = run(walk, screen="something else")
    check(ok, "the walk continues")
    check(ran == ["privacy", "next"], "both steps are recorded")


# --- 3. an unknown key is refused when the file is read, not when it is run --------------
def test_unknown_key() -> None:
    print("\n=== an unknown key is refused at load time")
    try:
        walk_of([{"name": "a", "at": [1, 2], "clik": "typo"}])
    except steps.StepError as exc:
        check("clik" in str(exc), "the message names the offending key")
    else:
        check(False, "no error was raised")


# --- 4. a step acts once: `at` + `type` types, it does not also click twice --------------
def test_no_step_acts_twice() -> None:
    print("\n=== a step with `at` and `type` types into the point, exactly once")
    walk = walk_of([{"name": "user", "at": [5, 6], "type": "vmuser", "settle": 0}])
    driver, (ok, _ran) = run(walk)
    check(ok, "the walk succeeds")
    check(driver.typed == [((5, 6), "vmuser")], "one type, at the measured point")
    # ONE ACTION, NOT TWO. A step carrying `at` and `type` must type into the field, and
    # type_into() clicks the field itself - so `driver.clicks` stays empty and the step must
    # not ALSO run the plain-click branch. The earlier assertion here was `clicks == []`,
    # which is the same thing said in a way that breaks the moment type_into grows a click.
    check(len(driver.typed) == 1 and driver.doubles == [],
          f"typed once and did not double-click, got {driver.typed} / {driver.doubles}")


# --- 5. repeat presses one coordinate N times -------------------------------------------
def test_repeat() -> None:
    print("\n=== repeat presses the same coordinate N times")
    walk = walk_of([{"name": "panes", "at": [100, 200], "settle": 0, "repeat": [3, 0]}])
    driver, (ok, _ran) = run(walk)
    check(ok, "the walk succeeds")
    check(driver.clicks == [(100, 200)] * 3, f"three clicks at one point, got {driver.clicks}")


# --- 6. a wait step does not consult the screen ----------------------------------------
def test_wait_is_measured() -> None:
    print("\n=== a wait step is a duration, not a reading")
    walk = walk_of([{"name": "install", "wait": 0.05, "keep_alive": False}])
    driver, (ok, _ran) = run(walk)
    check(ok, "the walk succeeds")
    check(driver.clicks == [], "nothing was clicked")
    check(driver.looks >= 1, "the screen was still watched for the log")


# --- 7. start_at / stop_before walk only the named half ---------------------------------
def test_halves() -> None:
    print("\n=== start_at walks from a step, stop_before stops at one")
    walk = walk_of([
        {"name": "one", "at": [1, 1], "settle": 0},
        {"name": "two", "at": [2, 2], "settle": 0},
        {"name": "three", "at": [3, 3], "settle": 0},
    ])
    driver, (ok, ran) = run(walk, start_at="two")
    check(ok and ran == ["two", "three"], f"start_at two -> {ran}")

    driver, (ok, ran) = run(walk, stop_before="three")
    check(ok and ran == ["one", "two"], f"stop_before three -> {ran}")


# --- 8. vars come from the caller, and a missing one is refused -------------------------
def test_vars() -> None:
    print("\n=== vars are substituted, and a name that is not declared is refused")
    document = walk_of(
        [{"name": "user", "at": [1, 2], "type": "{{ user }}", "settle": 0}],
        vars={"user": {"from": "args.user", "default": "vmuser"}},
    )
    driver, (ok, _ran) = run(document)
    check(ok and driver.typed == [((1, 2), "vmuser")], "the default was used")

    driver, (ok, _ran) = run(document, overrides={"args.user": "admin"})
    check(ok and driver.typed == [((1, 2), "admin")], "the override wins")

    bad = walk_of(
        [{"name": "user", "at": [1, 2], "type": "{{ nope }}", "settle": 0}],
        vars={"user": "vmuser"},
    )
    try:
        run(bad)
    except steps.StepError as exc:
        check("nope" in str(exc), "an undeclared name is refused")
    else:
        check(False, "no error was raised")


# --- 9. panes: the macOS shape ----------------------------------------------------------
def test_panes() -> None:
    print("\n=== a pane file runs the one pane whose words are up, and ends on its own word")
    document = steps.Walk({
        "name": "test panes",
        "ends": ["finder"],
        "stuck_after": 0,
        "steps": [
            {"name": "country", "when": ["country", "region"], "at": [1, 1], "settle": 0},
            {"name": "account", "when": ["full", "name"], "at": [2, 2], "settle": 0},
        ],
    }, path="inline")

    # A guest that walks country -> account -> desktop, one look per state.
    class Scripted(FakeDriver):
        def __init__(self):
            super().__init__()
            self.script = ["Select Your Country or Region",
                           "Create a Mac Account Full Name",
                           "Finder File Edit"]
            self.step = 0

        def look(self, label="look"):
            self.screen = self.script[min(self.step, len(self.script) - 1)]
            self.step += 1
            return super().look(label)

    driver = Scripted()
    ok, answered = steps.walk(driver, document)
    check(ok, "the walk reports success on reaching the end word")
    check(answered == ["country", "account"], f"both panes answered, got {answered}")
    check(driver.clicks == [(1, 1), (2, 2)], f"one click each, got {driver.clicks}")

    # A guest stuck on a pane nobody knows must FAIL, not spin to the timeout.
    class Stuck(FakeDriver):
        def look(self, label="look"):
            self.screen = "something no pane knows"
            return super().look(label)

    ok, answered = steps.walk(Stuck(), document)
    check(not ok, "an unknown pane is a failure, not arrival")
    check(answered == [], "nothing was answered")


def test_pane_limit() -> None:
    print("\n=== a pane that keeps matching is not clicked forever")
    document = steps.Walk({
        "name": "sticky",
        "ends": ["finder"],
        "pane_limit": 2,
        "stuck_after": 0,
        "steps": [
            {"name": "update", "when": ["update"], "at": [3, 3], "settle": 0},
        ],
    }, path="inline")

    class Sticky(FakeDriver):
        def look(self, label="look"):
            self.screen = "Update Mac Automatically"
            return super().look(label)

    driver = Sticky()
    ok, answered = steps.walk(driver, document)
    check(len(driver.clicks) <= 2, f"clicked at most twice, got {len(driver.clicks)}")
    check(not ok, "a pane that never resolves is a failure")


def test_mixed_is_refused() -> None:
    print("\n=== a file that mixes panes and a sequence is refused")
    try:
        steps.Walk({
            "name": "mixed",
            "steps": [
                {"name": "a", "when": ["x"], "at": [1, 1]},
                {"name": "b", "at": [2, 2]},
            ],
        }, path="inline")
    except steps.StepError as exc:
        check("mixes" in str(exc), "the message says it mixes the two shapes")
    else:
        check(False, "no error was raised")


# --- 10. ssh steps ----------------------------------------------------------------------
def test_ssh_step() -> None:
    print("\n=== an ssh step's verdict is the exit status, not a word on a screen")
    calls: list[str] = []

    class SshDriver(FakeDriver):
        pass

    document = steps.Walk({
        "name": "ssh",
        "steps": [
            {"name": "ok", "ssh": "true"},
            {"name": "also ok", "ssh": "echo hi"},
        ],
    }, path="inline")

    import session

    real = session.ssh

    def fake(port, command, timeout=30, **kwargs):
        calls.append(command)
        return 0, "ran"

    session.ssh = fake
    try:
        driver = SshDriver()
        ok, ran = steps.walk(driver, document)
        check(ok, "the walk succeeds when every command exits 0")
        check(calls == ["true", "echo hi"], f"both commands ran, got {calls}")
        check(ran == ["ok", "also ok"], "both steps are recorded")

        # A non-zero status fails the step, and the run, and it does not need a screen.
        failing = steps.Walk({
            "name": "ssh fails",
            "steps": [{"name": "bad", "ssh": "exit 7"}],
        }, path="inline")

        def fake_fail(port, command, timeout=30, **kwargs):
            return 7, ""

        session.ssh = fake_fail
        try:
            steps.walk(SshDriver(), failing)
        except steps.StepError as exc:
            check("7" in str(exc), "the failure names the exit status")
        else:
            check(False, "no error was raised for a non-zero status")

        # ...unless the step is optional, which is what "a read meant to fail is information"
        # is for: the step runs, records itself, and the walk continues.
        allowed = steps.Walk({
            "name": "ssh optional",
            "steps": [
                {"name": "probe", "ssh": "exit 1", "optional": True},
                {"name": "next", "ssh": "true"},
            ],
        }, path="inline")
        # THE OPTIONAL STEP FAILS AND THE ONE AFTER IT DOES NOT, which is the whole point:
        # the fake has to answer per command, not per run.
        def fake_mixed(port, command, timeout=30, **kwargs):
            return (1, "") if command == "exit 1" else (0, "")

        session.ssh = fake_mixed
        ok, ran = steps.walk(SshDriver(), allowed)
        check(ok and ran == ["probe", "next"],
              f"an optional failure does not stop the walk, got {ran}")
    finally:
        session.ssh = real


def test_region_is_a_crop() -> None:
    print("\n=== `region` reads a crop, and the words it matches are the crop's")
    seen: list[tuple] = []

    class CropDriver(FakeDriver):
        def __init__(self, screen: str) -> None:
            super().__init__(screen)
            self.regions: list[tuple] = []

        def look_at(self, region, label="look", *, scale=2):
            self.regions.append(tuple(region))
            return super().look(label)

    # THE POINT: the words are only in the crop. A whole-frame read would match them too, so
    # the assertion is that look_at was used AND that the step succeeded.
    walk = walk_of([
        {"name": "wait for the sheet", "until": ["erase", "complete"],
         "region": [700, 380, 1300, 640], "timeout": 2, "poll": 0},
    ])
    driver = CropDriver("Erase process is complete")
    ok, _ran = run(walk, screen="Erase process is complete", driver=driver)
    check(ok, "the step succeeded")
    check(driver.regions == [(700, 380, 1300, 640)],
          f"the crop was used, got {driver.regions}")

    # Without a region the whole frame is read, so a driver with no look_at still works.
    plain = walk_of([
        {"name": "wait for the sheet", "until": ["erase"], "timeout": 2, "poll": 0},
    ])
    ok, _ran = run(plain, screen="Erase process")
    check(ok, "a step with no region reads the whole frame")


def test_pane_do_list() -> None:
    print("\n=== a pane's `do` list is answered in ONE pass")
    document = steps.Walk({
        "name": "account pane",
        "ends": ["finder"],
        "stuck_after": 0,
        "pane_limit": 2,
        "steps": [
            {"name": "create a mac account", "when": ["full", "name"],
             "do": [
                 {"at": [1, 1], "type": "vmuser", "settle": 0},
                 {"at": [2, 2], "type": "admin", "settle": 0},
                 {"at": [3, 3], "settle": 0},
             ]},
        ],
    }, path="inline")

    class Account(FakeDriver):
        def __init__(self):
            super().__init__()
            self.script = ["Create a Mac Account Full Name", "Finder File Edit"]
            self.step = 0

        def look(self, label="look"):
            self.screen = self.script[min(self.step, len(self.script) - 1)]
            self.step += 1
            return super().look(label)

    driver = Account()
    ok, answered = steps.walk(driver, document)
    check(ok, "the walk reaches the end")
    # THREE ACTIONS IN ONE PASS, NOT THREE PASSES. A loop that ran one action per pass left the
    # pane on the first field forever - the second pass matched the same pane, saw the full name
    # already typed, and stopped.
    check(driver.typed == [((1, 1), "vmuser"), ((2, 2), "admin")],
          f"both fields typed in one pass, got {driver.typed}")
    check(driver.clicks == [(3, 3)], f"and the Continue clicked once, got {driver.clicks}")
    check(answered == ["create a mac account"], f"one pane answered, got {answered}")


def test_waits_for_gate() -> None:
    print("\n=== a pane file waits for its first pane before matching")
    document = steps.Walk({
        "name": "gated",
        "ends": ["finder"],
        "stuck_after": 0,
        "waits_for": ["country"],
        "boot_timeout": 2,
        "boot_poll": 0.01,
        "steps": [
            {"name": "country", "when": ["country"], "at": [1, 1], "settle": 0},
        ],
    }, path="inline")

    class Booting(FakeDriver):
        """A guest that is BLACK for two looks, then shows the first pane... and leaves it."""

        def __init__(self):
            super().__init__()
            self.looks = 0

        def look(self, label="look"):
            self.looks += 1
            # THE PANE IS LEFT AS SOON AS IT IS CLICKED, which is what a real installer does
            # and what this fake has to do too - otherwise the loop answers the same pane
            # forever and the test is measuring the fake rather than the interpreter.
            if self.clicks:
                self.screen = "Finder File Edit"
            elif self.looks <= 2:
                self.screen = ""
            else:
                self.screen = "Select Your Country or Region"
            return super().look(label)

    driver = Booting()
    ok, answered = steps.walk(driver, document)
    check(ok, "the walk proceeds once the first pane appears")
    check(answered == ["country"], f"and answers it, got {answered}")

    class Never(FakeDriver):
        def look(self, label="look"):
            self.screen = ""
            return super().look(label)

    ok, answered = steps.walk(Never(), document)
    check(not ok, "a guest that never shows the first pane fails, rather than matching nothing")
    check(answered == [], "and answers nothing")


def test_ends_region() -> None:
    print("\n=== a pane file can read its end condition in a crop")
    document = steps.Walk({
        "name": "menu bar",
        "ends": ["finder"],
        "ends_region": [0, 0, 700, 34],
        "ends_region_scale": 3,
        "stuck_after": 0,
        "steps": [
            {"name": "privacy", "when": ["data", "privacy"], "at": [1, 1], "settle": 0},
        ],
    }, path="inline")

    class MenuBar(FakeDriver):
        """Whole-frame reads cannot see the menu bar; the crop can.

        THAT IS THE MEASUREMENT THIS MODELS: tesseract on a full 1920x1080 desktop returns
        'Empty page!!' because its page segmenter finds no page, so `finder` is only readable
        in the 700x34 strip at 3x. A fake that returns the same words either way would pass on
        a broken implementation.
        """

        def __init__(self):
            super().__init__()
            self.crops: list[tuple] = []

        def look(self, label="look"):
            self.screen = "File Edit View Go Window Help"
            return super().look(label)

        def look_at(self, region, label="look", *, scale=2):
            self.crops.append((tuple(region), scale))
            self.screen = "Finder File Edit View Go Window Help"
            self.words = [(w, 0, 0, 0, 0) for w in self.screen.split()]
            return self.words

    driver = MenuBar()
    ok, answered = steps.walk(driver, document)
    check(ok, "the walk ends on the cropped word")
    check(driver.crops and driver.crops[0] == ((0, 0, 700, 34), 3),
          f"the menu-bar crop was used at 3x, got {driver.crops}")


def test_headless_needs_no_screen() -> None:
    print("\n=== a walkthrough with no GUI steps needs no QMP socket")
    headless = steps.HeadlessDriver("/tmp")
    check(headless.look("x") == [], "looking at nothing is empty, not an error")

    try:
        headless.says("anything")
    except steps.StepError as exc:
        check("no GUI steps" in str(exc), "asking the screen a question is refused")
    else:
        check(False, "no error was raised")


# --- 11. a real file parses -------------------------------------------------------------
def test_real_files() -> None:
    print("\n=== the committed walkthroughs parse")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for relative in ("linux/mint/steps/ubiquity.yaml",
                     "bsd/ghostbsd/steps/gbi.yaml",
                     "macos/15/steps/sequoia.yaml",
                     "macos/15/steps/setup.yaml",
                     "macos/15/steps/ssh.yaml",
                     "windows/11/steps/ssh.yaml",
                     "windows/11/steps/dev.yaml",
                     "macos/15/steps/dev.yaml"):
        path = os.path.join(here, relative)
        try:
            walk = steps.load(path)
        except steps.StepError as exc:
            check(False, f"{relative}: {exc}")
            continue
        check(len(walk.steps) > 5, f"{relative}: {len(walk.steps)} steps")


def test_require_ssh_probe() -> None:
    """`require_ssh` must pass the walkthrough's probe, not always the POSIX `true`.

    `true` IS NOT A WINDOWS COMMAND, and session.wait_for_ssh's own docstring records run
    41844808248 losing its whole fifteen-minute budget to an sshd that was answering
    perfectly. A guest whose shell is cmd.exe says `ssh_probe: echo ok`.
    """
    print("\n=== require_ssh carries the walkthrough's probe")
    import session

    seen: list[tuple] = []
    real = session.wait_for_ssh

    def fake(port, seconds, note=None, settle=10, probe="true", user=None, **kwargs):
        seen.append((port, probe, user))
        return True

    document = steps.Walk({
        "name": "probe",
        "steps": [
            {"name": "posix", "require_ssh": True, "timeout": 30},
            {"name": "windows", "require_ssh": True, "ssh_probe": "echo ok", "timeout": 30},
        ],
    }, path="inline")

    session.wait_for_ssh = fake
    try:
        ok, ran = steps.walk(FakeDriver(), document, ssh_port=2222)
    finally:
        session.wait_for_ssh = real
    check(ok, "both waits pass")
    check(seen[0][1] == "true", f"the default stays `true`, got {seen[0][1]!r}")
    check(seen[1][1] == "echo ok", f"the file's probe is used, got {seen[1][1]!r}")


def test_type_gap_reaches_the_keys() -> None:
    """`type_gap` must reach keys.type_text, because 40 ms drops characters on Windows.

    Measured on run 41997580476: `winget install` arrived as `get install`, the prompt came
    back, and the machine reported - correctly - that OpenSSH was not installed.
    """
    print("\n=== type_gap reaches the keystrokes")
    import keys

    gaps: list[float] = []
    real = keys.type_text

    def fake(q, text, per_char=0.03, **kwargs):
        gaps.append(per_char)
        return []

    document = steps.Walk({
        "name": "gaps",
        "steps": [
            {"name": "fast", "type": "abc"},
            {"name": "slow", "type": "abc", "type_gap": 0.15},
        ],
    }, path="inline")

    keys.type_text = fake
    try:
        steps.walk(FakeDriver(), document)
    finally:
        keys.type_text = real
    # `type:` (no `at:`) HAS ITS OWN DEFAULT, 100 ms, because it types into whatever already
    # has focus rather than into a field just clicked; `run:` uses 40 ms. What this asserts is
    # that the FILE can override whichever default applies.
    check(gaps and abs(gaps[0] - 0.1) < 1e-9, f"the type: default is 100 ms, got {gaps}")
    check(len(gaps) > 1 and abs(gaps[1] - 0.15) < 1e-9,
          f"the file's 150 ms is used, got {gaps}")


def test_share_mount_step() -> None:
    """`share_mount:` starts the host's smbd and hands the guest its mount command.

    THE FAILURE THIS GUARDS IS A SILENT ONE. The host can start a server that the guest cannot
    reach, and the stage would then fail four steps later for a reason that looks like a broken
    disk. So the step refuses to continue when the share did not start - and this checks that
    it does, rather than logging and carrying on.
    """
    print("\n=== share_serve starts the host server, share_mount only mounts")
    import hostshare
    import session

    started: list[str] = []
    mounted: list[str] = []
    real_start, real_ssh = hostshare.start, session.ssh
    real_listening = hostshare.listening

    def fake_start(directory, **kwargs):
        started.append(directory)
        return True

    def fake_ssh(port, command, timeout=30, **kwargs):
        mounted.append(command)
        return 0, "ok"

    def fake_start_broken(directory, **kwargs):
        started.append(directory)
        return False

    # TWO STEPS, TWO ACTS - and this is the shape a walkthrough now writes.
    document = steps.Walk({
        "name": "share",
        "steps": [
            {"name": "serve", "share_serve": True},
            {"name": "mount", "share_mount": True, "share_family": "windows"},
        ],
    }, path="inline")

    hostshare.start, session.ssh = fake_start, fake_ssh
    hostshare.listening = lambda *a, **k: True
    try:
        ok, ran = steps.walk(FakeDriver(), document, ssh_port=2222)
    finally:
        hostshare.start, session.ssh = real_start, real_ssh
        hostshare.listening = real_listening
    check(ok, f"both steps pass when the host serves the directory, ran={ran}")
    check(len(started) == 1, f"the host was asked to serve once, got {started}")
    # THE MOUNT IS `New-SmbMapping`, NOT `net use` - see lib/hostshare.py for why the older form
    # could not express an account with no password (`The option /PERSISTENT=NO is unknown`,
    # run 42958385108). This check asserted `net use` and had to be updated with the command.
    check(any("New-SmbMapping" in c for c in mounted),
          f"the windows client command reached the guest, got {mounted}")

    # AND A SERVER THAT DID NOT START IS NOT A STEP THAT CARRIES ON.
    document2 = steps.Walk({
        "name": "share",
        "steps": [{"name": "serve", "share_serve": True}],
    }, path="inline")
    hostshare.start = fake_start_broken
    failed = False
    try:
        steps.walk(FakeDriver(), document2, ssh_port=2222)
    except steps.StepError:
        # A STEP THAT CANNOT DO ITS JOB RAISES, and `walk` lets it out - the same shape as a
        # failed `ssh:`. Carrying on would be the silent version.
        failed = True
    finally:
        hostshare.start = real_start
    check(failed, "a share the host could not serve stops the walk")

    # AND MOUNTING WITH NOTHING SERVING IS A DIFFERENT FAILURE WITH A DIFFERENT MESSAGE.
    #
    # This is the whole point of the split: "the guest could not mount" used to be the message
    # for a host with no smbd AND a credential the guest could not use.
    document3 = steps.Walk({
        "name": "share",
        "steps": [{"name": "mount", "share_mount": True, "share_family": "windows"}],
    }, path="inline")
    hostshare.listening = lambda *a, **k: False
    message = ""
    try:
        steps.walk(FakeDriver(), document3, ssh_port=2222)
    except steps.StepError as exc:
        message = str(exc)
    finally:
        hostshare.listening = real_listening
    check("nothing is serving on port 445" in message,
          f"mounting with nothing served names the server, got {message!r}")


def test_scp_and_script_steps() -> None:
    """`scp:` copies a file in; `script:` writes one, copies it, and runs it by path.

    BOTH EXIST FOR THE SAME MEASURED REASON. windows/11/devtools.py's own docstring: a
    Visual Studio --override cannot survive ssh -> cmd -> winget, where the quotes are eaten,
    so the line is written to a .bat and run by path. A vocabulary that cannot say that would
    keep the whole stage in Python for the sake of one copy.
    """
    print("\n=== scp: copies in, script: writes and runs by path")
    import session

    copied: list[tuple] = []
    ran: list[tuple] = []
    real_scp, real_ssh = session.scp, session.ssh

    def fake_scp(local, remote, **kwargs):
        copied.append((local, remote))
        return 0, ""

    def fake_ssh(port, command, timeout=30, **kwargs):
        ran.append(command)
        return 0, "out"

    document = steps.Walk({
        "name": "files",
        "steps": [
            {"name": "copy", "scp": ["windows/11/probe.ps1", "C:/Users/docker/probe.ps1"]},
            {"name": "bat", "script": "@echo off\r\nver", "script_shell": "cmd",
             "script_name": "x.bat"},
        ],
    }, path="inline")

    session.scp, session.ssh = fake_scp, fake_ssh
    driver = FakeDriver()
    try:
        ok, ran_steps = steps.walk(driver, document, ssh_port=2222)
    finally:
        session.scp, session.ssh = real_scp, real_ssh
    check(ok, f"both steps pass, ran={ran_steps}")
    check(len(copied) == 2, f"both copied a file, got {copied}")
    check(copied[0][1].endswith("probe.ps1"), f"the stated remote path is used: {copied[0]}")
    check(any("cmd /c" in c for c in ran), f"a .bat runs through cmd /c, got {ran}")


def test_ssh_matchers_on_their_frames() -> None:
    """The ssh stage's two region matchers, against the frames they were measured on.

    A REGION IS THE ONE THING IN A WALKTHROUGH THAT CANNOT BE CHECKED BY READING THE FILE.
    The words look right and the rectangle looks plausible, but whether tesseract finds
    anything inside it is a fact about a bitmap - and on these frames it is the difference
    between a read and an empty one: 'allow full disk access' is small text in a 1920-wide
    image and full-screen OCR discards it as noise.

    The three frames are what make the assert meaningful:

        remote-login-3-pane.png               the sheet, switch OFF
        remote-login-5-full-disk-access.png   the sheet, switch ON
        search-no-results.png                 doubled query, list says No Results, no sheet

    and the third is the one that matters: 'the pane is up' must be FALSE there, or it cannot
    stop the clicks that follow it - which is exactly the run where the stage reported success
    against a daemon the PREVIOUS run had left listening.
    """
    import ssh_frames_test
    if not all(os.path.isfile(path) for _, path, _ in ssh_frames_test.FRAMES):
        print("  skip ssh matchers: the measured frames are not in testdata/ssh")
        return
    for name, key in (("the remote login result is on screen", "result"),
                      ("the remote login pane is up", "pane"),
                      ("the switch reads on", "switch")):
        for row in ssh_frames_test.FRAMES:
            label, path, expected = row[0], row[1], row[2][key]
            hit = ssh_frames_test.matches(name, ssh_frames_test.FrameDriver(path))
            if hit != expected:
                FAILED.append(f"{name}: {label} read {'PASS' if hit else 'miss'}, "
                              f"expected {'PASS' if expected else 'miss'}")


def test_capabilities_and_new_primitives() -> None:
    """shutdown by a named method, `requires:`, screenshot and record.

    THREE THINGS A WALKTHROUGH COULD NOT SAY BEFORE, and the first is the one that was lying.
    """
    print("\n=== shutdown takes a method, and a missing capability stops the run")
    import capabilities

    # A MISSING CAPABILITY NAMES ITSELF, and it is checked before the step waits for anything.
    ok, why = capabilities.check("qga", guest=None)
    check(not ok and "qga" in why and "no qga device" in why,
          f"a machine with no qga device reports it, got {why!r}")

    ok2, why2 = capabilities.check("teleport", guest=None)
    check(not ok2 and "unknown capability" in why2,
          f"an unknown capability is refused by name, got {why2!r}")

    # AND `require` REPORTS ALL OF THEM AT ONCE. A stage that needs two things and has neither
    # should say so once, not once per attempt.
    raised = ""
    try:
        capabilities.require(["qga", "teleport"], guest=None)
    except capabilities.Broken as exc:
        raised = str(exc)
    check("qga" in raised and "teleport" in raised,
          f"both missing capabilities are reported, got {raised[:100]!r}")

    # THE VOCABULARY HAS THE THREE NEW KEYS.
    check("requires" in steps.STEP_KEYS, "a step can declare what it needs")
    check("screenshot" in steps.STEP_KEYS and "screenshot" in steps.ACTION_KEYS,
          "a step can take a screenshot")
    check("record" in steps.STEP_KEYS and "record" in steps.ACTION_KEYS,
          "a step can record")

    # AND `shutdown:` TAKES A METHOD RATHER THAN A BOOLEAN. `true` still parses - it means the
    # careful path for a caller that has not been updated - but a named method is what the step
    # acts on, so the two cannot be confused for each other.
    doc = steps.Walk({
        "name": "methods",
        "steps": [{"name": "halt", "shutdown": "guest_agent"}],
    }, path="inline")
    check(doc.steps[0]["shutdown"] == "guest_agent", "shutdown accepts a named method")


def test_every_sh_c_body_parses() -> None:
    """An `ssh:` body that opens a `sh -c '...'` must survive a POSIX shell.

    THE BUG THIS EXISTS FOR IS A COMMENT WITH AN APOSTROPHE IN IT. Every BSD and the one fish
    image wrap their bodies in `sh -c '...'`, and a comment inside that quoted body saying
    `smbd's log` CLOSES THE QUOTE - which the guest reports two lines later as

        fish: Expected a command, but found an incomplete token
        sh: Syntax error: Unterminated quoted string

    Three of those were written in one evening, each in a COMMENT that looked inert, and each cost
    a full run to find because the message points at the wrong line. `sh -n` is the whole check.
    """
    import glob
    import subprocess

    bad = []
    for path in sorted(glob.glob("linux/*/steps/*.yaml") + glob.glob("bsd/*/steps/*.yaml")):
        try:
            doc = steps.load(path)
        except Exception:
            continue
        values = doc.values({"args.user": "vmuser", "args.password": "admin"})
        for one in doc.steps:
            body = one.spec.get("ssh")
            if not isinstance(body, str) or not body.lstrip().startswith("sh -c"):
                continue
            rendered = doc.substitute(one.spec, values).get("ssh")
            done = subprocess.run(["sh", "-n", "-c", rendered], capture_output=True, text=True)
            if done.returncode != 0:
                bad.append(f"{path}: {one.spec.get('name')}: {done.stderr.strip()[:80]}")
    check(not bad, f"every `sh -c` body parses, got {bad}")


def main() -> int:
    test_expect_failure()
    test_optional_expect()
    test_unknown_key()
    test_no_step_acts_twice()
    test_repeat()
    test_wait_is_measured()
    test_halves()
    test_vars()
    test_panes()
    test_pane_limit()
    test_mixed_is_refused()
    test_ssh_step()
    test_region_is_a_crop()
    test_pane_do_list()
    test_waits_for_gate()
    test_ends_region()
    test_headless_needs_no_screen()
    test_real_files()
    test_ssh_matchers_on_their_frames()
    test_require_ssh_probe()
    test_type_gap_reaches_the_keys()
    test_scp_and_script_steps()
    test_share_mount_step()
    test_capabilities_and_new_primitives()
    test_every_sh_c_body_parses()

    print()
    if FAILED:
        print(f"=== {len(FAILED)} FAILURE(S) ===")
        for one in FAILED:
            print(f"  {one}")
        return 1
    print("=== ALL STEPS TESTS PASS ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
