"""Run an installer walkthrough that is written as data, not as Python.

    python3 lib/steps.py macos/15/steps/sequoia.yaml --qmp /work/drive.sock.qmp

WHY THIS EXISTS, AND WHAT IT IS NOT

There are eleven Linux GUI drivers, a GhostBSD one and a macOS one. Each is four to six
hundred lines, and roughly THREE QUARTERS OF EVERY ONE IS THE SAME THREE THINGS:

    the Driver class    wait for the QMP socket, look(), click(), type_into(), text()
    the boot/wait       poll until the installer or live desktop appears
    main()              argparse, extract the kernel, start qemu, walk, compress

Only the walk differs, and what the walk actually says is almost always the same small
vocabulary: click this point, wait this long, type this value here, and then confirm the next
pane arrived. That vocabulary is DATA. This module holds the interpreter for it and the YAML
behind it holds the measurements, so a new installer is a file rather than a copy of a file.

IT IS NOT "REPLACE ALL PYTHON WITH YAML", AND THE FRAGMENTS THAT REMAIN PROVE IT.

The measurements in those drivers are not just coordinates. They are also the reason a step
exists at all, and several of them are genuinely programs: mint's screen-saver killer types a
shell one-liner into a guest terminal, fedora retries a click with Alt+N when it does not
advance, macOS's Apple Account pane opens a popup whose menu holds the way out and needs a
second look at a confirmation sheet. Those are steps with a `python: macos/15/steps/...` escape
hatch, and the escape hatch is the point: the common case is declarative, the measured
exception stays readable, and neither is hidden inside a loop that looks like the other.

THE RULE THE WHOLE REPOSITORY ALREADY FOLLOWS, ENFORCED HERE

    THE SCREEN IS READ TO DECIDE WHETHER A STEP WORKED, NEVER WHERE TO CLICK.

That rule cost this project four separate CI runs (kubuntu three times, mint once) and it is
written into every driver as a comment. Here it is structure: `expect` is checked after a step
and only produces a verdict and a log line; `at` is always a number in the file. There is no
step type that searches the screen for a target, because a driver that can do that can be
talked into clicking a sentence - which is what run 42364250527 did.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time

REPO = os.environ.get("REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))


def log(message: str) -> None:
    print(f"[steps {time.strftime('%H:%M:%S')}] {message}", flush=True)


class StepError(RuntimeError):
    pass


# THE ENLARGEMENT A CROPPED READ GETS WHEN THE WALKTHROUGH DOES NOT SAY.
#
# 2 IS A MEASUREMENT, NOT A DEFAULT. The recovery menu's rows are about ten pixels tall at
# 1920x1080, and tesseract reading that crop at 1:1 returns nothing at all - which is
# indistinguishable from "the menu has not arrived". install.py's recovery_menu_text() enlarges
# its crop by 2 for exactly this pane; the detail pane needs 4 (at 3 the decimal point is lost
# and "268.4 MB" reads as "2684 MB"), which is why a step may ask for its own.
DEFAULT_CROP_SCALE = 2


# THE VOCABULARY. Every key a step may carry, and what it does. Anything not here is a typo
# and is refused at load time - a walkthrough that silently ignores a key it does not know is
# a walkthrough that clicks nothing and reports success, which is the failure mode every
# driver in this repository has hit at least once.
STEP_KEYS = {    "name",        # required: what the log calls this step
    "at",          # [x, y] to click
    "double",      # [x, y] to double-click (separate from `at` because the gap matters)
    "type",        # text to type after clicking `at`
    "clear",       # clear the field first (ctrl+a, backspace)
    "key",         # a qemu qcode, or a + combo: "ret", "ctrl+alt+t"
    "run",         # a shell line, typed into the guest's own terminal
    "ssh",         # a command run over ssh IN THE GUEST - see _do_action
    "require_ssh", # wait for sshd before the steps that use it
    "ssh_probe",   # the command that wait runs; 'true' is not a Windows command
    "terminal_wait",  # seconds to wait for the terminal after `run` opens it
    "line_gap",    # seconds between the lines of a multi-line `run`
    "type_gap",    # seconds between keystrokes; 0.04 suits a POSIX shell, 0.15 a Windows one
    "scp",         # ["local-file", "remote-path"] - copy a script in, before running it
    "script",      # a body written on the host, copied in, and run by path
    "script_shell",  # the interpreter that runs `script`: "cmd", "powershell", "sh"
    "script_name",   # the file name `script` is written as
    "share_serve",   # START THE HOST'S smbd for a directory - one act, not two
    "share_mount",   # the GUEST mounts what share_serve: is serving
    "share_dir",     # the host directory to share (default ~/shared)
    "share_family",  # which client the guest uses: macos, windows, linux
    "settle",      # seconds to wait after the action
    "wait",        # seconds to wait with nothing clicked
    "expect",      # words that must appear afterwards; a mismatch fails the step
    "expect_any",  # a list of word-sets, any one of which passes
    "until",       # words that end a wait loop
    "until_any",   # a list of word-sets, any one of which ends the wait
    "disk_settle", # wait until the target qcow2 stops growing, for this many quiet seconds
    "disk_min_mb", # ...but only once it holds at least this many allocated MiB
    "region",      # (x0, y0, x1, y1) to crop before reading, for `until`/`expect`
    "region_at",   # [width, height] the crop was measured at; a mismatch is an error
    "scale",       # enlargement for a cropped read; small text needs it, see DEFAULT_CROP_SCALE
    "modifier",    # the select-all modifier: "ctrl" on Linux, "meta_l" on macOS
    "until_dark",  # wait until the screen goes black - the installer's own start signal
    "timeout",     # seconds the `until` loop may run
    "poll",        # seconds between looks in the `until` loop
    "keep_alive",  # click harmlessly between polls, for a guest that blanks its own screen
    "require",     # fail the whole walkthrough if this step did not run
    "optional",    # a step that may be skipped without failing the run
    "once",        # a pane answered at most ONCE, for a pane that starts a slow process
    "repeat",      # (count, settle) for a footer button pressed the same way N times
    "sweep",       # a band of clicks at one x, stopping when the sheet opens
    "at_window",   # [x, dy] - click x at dy pixels below the window's top edge
    "at_bottom",   # [x, dy] - click x at dy pixels ABOVE the window's bottom edge
    "window_x",    # the column to scan for the window's top edge (default 500)
    "window_from", # first y to scan (default 180)
    "window_to",   # last y to scan (default 400)
    "window_brightness",  # the brightness that counts as the window (default 40)
    "wait_for_change",  # until the screen stops looking like it did before
    "change_timeout",   # seconds wait_for_change may take
    "change_settle",    # seconds between fingerprints in wait_for_change
    "when",        # this step is a PANE: it runs only if these words are on screen
    "unless",      # a sequence step that runs only if these words are NOT on screen
    "do",          # a list of actions for one pane - the pane is answered in one pass
    "end",         # a pane that means the run is over (the desktop)
    "stuck_after", # seconds of no pane matching before the walk is declared stuck
    "release",     # which release this pane's numbers were measured on
    "python",      # an escape hatch: "module:function" for a measured exception
    "python_screen",  # false when a python: step never touches the screen (ssh only)
    "press",       # for a python step: which footer control it should press by name
    # --- THE VM ITSELF -------------------------------------------------------------------
    #
    # These three are a SEQUENCE-ONLY vocabulary and they change what `run_file` returns. A
    # walkthrough that boots its own machine is not a walkthrough over a guest somebody else
    # started: it owns the machine, and the stage above it becomes "run this file".
    "boot",        # start the machine: needs family + disk; the file owns the guest
    "reboot",      # reset it through QMP and reconnect, so the steps after it still see a screen
    "shutdown",    # stop it the way the caller declared (graceful or kill)
    "family",      # for boot/shutdown: macos, windows, linux, android
                   #            `android` is Android-x86: -kernel/-initrd and two files,
                   #            so `disk` names a DIRECTORY of kernel, initrd.img and
                   #            system.efs, not one qcow2. No sshd to wait for.
    "graceful",    # for shutdown: ask the guest to close its disks first (default true)
}

# THE KEYS AN ACTION INSIDE `do` MAY CARRY. A pane's actions are the same vocabulary as a
# sequence step minus the pane machinery: they cannot wait for the screen, because the pane
# already decided that, and they cannot be a pane themselves.
ACTION_ONLY_KEYS = {"at", "double", "type", "clear", "key", "run", "ssh", "scp", "script",
                    "script_shell", "script_name", "share_mount", "share_dir",
                    "share_serve", "share_family", "keep_alive",
                    "settle", "terminal_wait", "timeout", "modifier", "type_gap"}

# Steps that wait for something to happen rather than doing something. `expect` does not apply
# to these: they already have their own condition, and a second one would be a second opinion
# about the same screen.
WAIT_STEPS = {"until", "until_any", "wait", "wait_for_change", "until_dark", "disk_settle"}

# Everything above that actually drives the guest. A step with none of these is a check, and
# a check that does nothing is reported rather than run.
ACTION_KEYS = {"at", "double", "type", "key", "run", "ssh", "scp", "script", "share_serve",
               "share_mount",
               "require_ssh", "sweep",
               "at_window", "at_bottom",
               # THE MACHINE'S OWN LIFECYCLE. A file may own its guest - see LIFECYCLE below.
               "boot", "reboot", "shutdown"}

# A STEP THAT STARTS, RESETS OR STOPS THE MACHINE. These are the only steps that act on the guest
# as a PROCESS rather than through it, and they are why `run_file` can return a guest instead of
# requiring one.
LIFECYCLE = {"boot", "reboot", "shutdown"}


class Step:
    """One step, validated. Constructed from a dict so the YAML is the only source."""

    def __init__(self, spec: dict, index: int) -> None:
        if not isinstance(spec, dict):
            raise StepError(f"step {index} is {type(spec).__name__}, not a mapping")
        unknown = set(spec) - STEP_KEYS
        if unknown:
            raise StepError(
                f"step {index} ({spec.get('name', '?')}) has unknown key(s): "
                f"{', '.join(sorted(unknown))}. Known: {', '.join(sorted(STEP_KEYS))}"
            )
        self.spec = spec
        self.index = index
        self.name = spec.get("name") or f"step {index}"
        if not spec.get("name"):
            raise StepError(f"step {index} has no name; every step is named in the log")

        # A PANE'S `do` IS A LIST OF ACTIONS, AND IT IS ANSWERED IN ONE PASS.
        #
        # THIS IS NOT AN OPTIMISATION. The account pane is three fields and a Continue, and a
        # loop that ran one action per pass left the pane on the first field forever: the
        # second pass matched the same pane again, saw `full name` still typed, and stopped.
        # A pane is a QUESTION; answering it is however many clicks the question needs.
        self.actions: list[dict] = []
        if spec.get("do"):
            if not isinstance(spec["do"], list):
                raise StepError(f"step {index} ({self.name}): `do` is a list of actions")
            for position, action in enumerate(spec["do"]):
                if not isinstance(action, dict):
                    raise StepError(
                        f"step {index} ({self.name}): action {position} is not a mapping")
                unknown = set(action) - ACTION_ONLY_KEYS
                if unknown:
                    raise StepError(
                        f"step {index} ({self.name}): action {position} has unknown key(s) "
                        f"{', '.join(sorted(unknown))}. A pane action may carry: "
                        f"{', '.join(sorted(ACTION_ONLY_KEYS))}"
                    )
                self.actions.append(action)

    def __getitem__(self, key: str):
        return self.spec.get(key)

    def __contains__(self, key: str) -> bool:
        return key in self.spec

    def get(self, key: str, default=None):
        return self.spec.get(key, default)

    @property
    def acts(self) -> bool:
        return bool(ACTION_KEYS & set(self.spec))

    @property
    def waits(self) -> bool:
        return bool(WAIT_STEPS & set(self.spec))

    @property
    def is_pane(self) -> bool:
        """A pane is a step that runs only when its own words are on screen.

        THIS IS THE OTHER SHAPE A WALKTHROUGH HAS, AND IT IS NOT A VARIATION ON THE FIRST.
        Seven of the eleven Linux drivers are sequences: the installer draws its panes in a
        fixed order, so a list of clicks is the whole truth. macOS is not - its Setup Assistant
        skips panes depending on the release, on whether the machine has a network, and on
        answers given two panes earlier, so macos/15/drive_read.py is an EVENT LOOP: look at
        the screen, run the one handler whose words appeared, look again.

        A sequence cannot express that and a loop cannot express an installer that has no
        markers. Both are here, in one file, because they describe the same kind of thing and a
        reader should be able to see which one they are looking at.

        AND IT DOES NOT NEED `when` TO BE A PANE. A pane's words are the thing it matches on,
        and `do` is how that pane is answered; a pane declared as `do` alone is one whose words
        are implied by its first action, which is the shape the Gnome first-boot wizard wants.
        `when` stays required in practice because a pane nobody can name is a pane that fires
        on the wrong screen, and this file will not guess.
        """
        return bool(self.spec.get("when"))

    def __repr__(self) -> str:
        return f"<step {self.index} {self.name!r}>"


class Walk:
    """A parsed walkthrough: its steps, its variables, and the anchors they refer to."""

    def __init__(self, document: dict, *, path: str = "") -> None:
        if not isinstance(document, dict):
            raise StepError(f"{path or 'the walkthrough'} is not a mapping")
        self.path = path
        self.document = document
        self.name = document.get("name") or "unnamed"
        self.notes = document.get("notes") or ""
        self.vars = document.get("vars") or {}
        self.resolution = document.get("resolution") or [1920, 1080]

        # ANCHORS ARE YAML ALIASES' NAMED COUSIN, AND THEY ARE WHY A FOOTER IS WRITTEN ONCE.
        # `&continue [1306, 727]` / `*continue` is YAML's own mechanism and needs nothing from
        # this module; `anchors:` here is for values that come from the environment instead -
        # the account name, the password - so they are substituted rather than aliased.
        steps = document.get("steps")
        if not isinstance(steps, list) or not steps:
            raise StepError(f"{path or 'the walkthrough'} has no steps")
        self.steps = [Step(spec, i) for i, spec in enumerate(steps)]

        # TWO SHAPES, AND A FILE IS ONE OF THEM. A file that mixes them is a file whose author
        # has not decided what the installer does, and the two shapes disagree about what
        # "next" means - so it is refused rather than run one way and silently ignore the other.
        self.panes = [s for s in self.steps if s.is_pane]
        if self.panes and len(self.panes) != len(self.steps):
            mixed = [s.name for s in self.steps if not s.is_pane][:3]
            raise StepError(
                f"{path or 'the walkthrough'} mixes panes and a sequence: "
                f"{', '.join(mixed)} have no `when`. A walkthrough is either a list of steps "
                f"run in order, or a set of panes matched against the screen; not both."
            )

    # --- substitution ---------------------------------------------------------------

    def values(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """Resolve `vars` against the environment and the caller's overrides.

        A `vars` entry is either a literal or `{from: args.user, default: docker}`, and the
        point of the second form is that the walkthrough never hard-codes the account: the
        same YAML bakes a `docker` disk and an `admin` disk, and a driver that typed a literal
        would be a driver that lies about which account is on the disk it produced.

        ONE SOURCE IS COMPUTED RATHER THAN PASSED. `args.host` is the share's server, which is
        this container's end of the bridge - a fact about the run and not about the disk, so
        writing it into the file would bake the CI network into a published image. No stage
        passes it, so it is filled in here from lib/net.py. `args.smb_user` and `args.smb_pass`
        come from lib/hostshare.py for the same reason: the share's account is created by that
        module, and a step that wrote its own would be a step that had to be kept in step.
        """
        computed = {
            "args.host": ("net", "BRIDGE_IP"),
            "args.smb_user": ("hostshare", "SMB_USER"),
            "args.smb_pass": ("hostshare", "SMB_PASSWORD"),
        }
        resolved = {}
        for key, spec in self.vars.items():
            if isinstance(spec, dict):
                source = spec.get("from") or ""
                if overrides and source in overrides:
                    resolved[key] = overrides[source]
                elif source in computed:
                    import importlib
                    module, name = computed[source]
                    resolved[key] = str(getattr(importlib.import_module(module), name))
                else:
                    resolved[key] = spec.get("default", "")
            else:
                resolved[key] = str(spec)
        return resolved

    def substitute(self, value, values: dict[str, str]):
        """`"{{ user }}"` -> `"docker"`. Only strings are touched; a list is walked."""
        if isinstance(value, str):
            def repl(match: re.Match) -> str:
                key = match.group(1).strip()
                if key not in values:
                    raise StepError(
                        f"{self.path}: {value!r} names {key!r}, which is not in vars "
                        f"({', '.join(sorted(values))})")
                return values[key]
            return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", repl, value)
        if isinstance(value, list):
            return [self.substitute(v, values) for v in value]
        if isinstance(value, dict):
            return {k: self.substitute(v, values) for k, v in value.items()}
        return value


def load(path: str) -> Walk:
    import yaml

    with open(path, encoding="utf-8") as fh:
        document = yaml.safe_load(fh)
    return Walk(document, path=path)


# --- the driver the steps drive -------------------------------------------------------
#
# THIS IS THE PART THAT WAS COPIED INTO EVERY DRIVER, AND IT IS HERE ONCE. It is deliberately
# the same interface those drivers have - look/click/type/text - so a walkthrough written
# against a driver that still exists can be walked by this without translation, and so the
# measurements in the YAML are the same numbers as in the Python it replaced.


class HeadlessDriver:
    """A driver for a walkthrough with no screen in it.

    A PURE-SSH WALKTHROUGH SHOULD NOT NEED A QMP SOCKET, and this is not a convenience: the
    stages after Remote Login are exactly the ones that act on a guest by command, and requiring
    a framebuffer to run them would mean a YAML file that could only be tested on the machine it
    was written on. It also makes the ssh half of a walkthrough runnable against any sshd - this
    file's own test does that against a plain server on the same host.

    Everything that reads the screen raises rather than returning nothing. A `when` or an
    `expect` in a file being driven headless is a mistake in the file, and a silent empty screen
    would make it look like a pane that never appeared.
    """

    def __init__(self, work: str, *, disk_path: str | None = None) -> None:
        self.work = work
        self.disk_path = disk_path
        self.qmp_path = ""
        self.q = None
        self.words: list[tuple] = []
        self.qmpmod = None

    def look(self, label: str = "look") -> list[tuple]:
        log(f"look {label}: no screen (this walkthrough has no GUI steps)")
        return []

    def resolution(self) -> tuple[int, int]:
        return (0, 0)

    def text(self) -> str:
        return ""

    def says(self, *want: str) -> bool:
        raise StepError(
            "this step asks the screen a question, but the walkthrough has no GUI steps and "
            "was started headless. A `when`/`expect` needs --qmp.")

    def log(self, message: str) -> None:
        log(f"  {message}")

    def close(self) -> None:
        pass


class Driver:
    def __init__(self, qmp_path: str, work: str, *, width: int = 1920,
                 height: int = 1080, socket_timeout: int = 180,
                 disk_path: str | None = None, q=None) -> None:
        import ocr
        import qmp as qmpmod

        self.qmpmod = qmpmod
        shots = os.path.join(work, "shots")
        os.makedirs(shots, exist_ok=True)

        deadline = time.time() + socket_timeout
        while not os.path.exists(qmp_path):
            if time.time() > deadline:
                raise StepError(f"no QMP socket at {qmp_path} after {socket_timeout}s")
            time.sleep(1)

        self.screen = ocr.Screen(qmp_path, shots, width=width, height=height, q=q)
        self.q = self.screen.q
        self.work = work
        # KEPT FOR A `python:` STEP, NOT USED HERE.
        #
        # install.py's Recovery takes both: the socket it drives and the qcow2 it watches for
        # growth. A pane that installs macOS needs the second one, because a black screen
        # cannot distinguish "copying 12 GB" from "hung" - see macos/15/steps/setup_panes.py.
        self.qmp_path = qmp_path
        self.disk_path = disk_path
        self.n = 0
        self.samples = 0
        self.ppm = None
        self.words: list[tuple] = []

    def look(self, label: str = "look") -> list[tuple]:
        self.n += 1
        ppm = self.screen.grab(f"{self.n:03d}-{label}".replace(" ", "-"))
        self.words = self.screen.text(ppm) if ppm else []
        self.ppm = ppm
        log(f"look {label}: {len(self.words)} words :: "
            f"{' | '.join(w[0] for w in self.words[:16])}")
        return self.words

    def look_at(self, region, label: str = "look", *, scale: int = DEFAULT_CROP_SCALE) -> list[tuple]:
        """Read ONE RECTANGLE of the screen, which is what makes a wait cheap and reliable.

        WHY A REGION AND NOT THE WHOLE FRAME. Three measured reasons, and the third is the one
        that decides whether a `until` works at all:

            speed        the whole 1920x1080 frame costs seconds; the title strip this asks
                         about is a twentieth of it and tesseract returns in well under a
                         second, which is the difference between a 10-second poll and a
                         1-second one.

            noise        a pane's body is full of words that belong to other steps. Waiting
                         for "disk" on the disk-utility screen matches the sidebar, the
                         toolbar and the help text; waiting for it in the strip where the
                         dialog's title is drawn matches once.

            the answer   a phrase like "Erase process is complete" is only true in the sheet
                         that reports it. Anywhere else on that screen the same words are a
                         sentence in a paragraph, and a driver that matched the sentence
                         clicks prose - which is what run 42364250527 did at (896, 424).

        `region` is (x0, y0, x1, y1) in frame pixels and the words come back with ABSOLUTE
        coordinates, because the caller may click what it found.

        SCALING IS NOT A QUALITY KNOB, IT IS WHAT MAKES SMALL TEXT READABLE. The recovery
        menu's rows are about ten pixels tall at 1920x1080, and tesseract reading the crop at
        1:1 returns nothing at all - which is indistinguishable from "the menu has not
        arrived". install.py's recovery_menu_text() has always enlarged its crop by 2 for this
        reason; a walkthrough that reads the same menu at 1:1 waits out a menu that is already
        on screen. `scale` defaults to 2 for exactly this: the smallest text any pane here
        draws.
        """
        self.n += 1
        ppm = self.screen.grab(f"{self.n:03d}-{label}".replace(" ", "-"))
        if not ppm:
            self.words = []
            self.ppm = None
            return []

        # TWO READERS, AND THE DIFFERENCE IS THE ENLARGEMENT.
        #
        # assistant.ocr_text_cropped() crops, ENLARGES and reads; it is what every stage driver
        # in this repository has measured, and the enlargement is the whole reason it works on
        # small text. It answers with one string and no coordinates, which is enough for a
        # `until`/`expect` - those ask whether words are on screen, not where.
        #
        # ocr.Screen.text_in() crops and reads at 1:1 and answers with coordinates, which a
        # caller needs only if it intends to click what it found. No step vocabulary does that
        # today - a coordinate is always a number in the file - so it is reached only when a
        # walkthrough explicitly asks for a 1:1 read.
        if scale <= 1:
            self.words = self.screen.text_in(ppm, region)
        else:
            import assistant
            text = assistant.ocr_text_cropped(ppm, tuple(region), scale=scale)
            self.words = [(word, 0, 0, 0, 0) for word in (text or "").split()]

        self.ppm = ppm
        log(f"look {label} in {tuple(region)}: {len(self.words)} words :: "
            f"{' | '.join(w[0] for w in self.words[:16])}")
        return self.words

    def text(self) -> str:
        return " ".join(w[0].lower() for w in self.words)

    def resolution(self) -> tuple[int, int]:
        """The framebuffer's size, as the Screen was told it.

        NOT ASKED OF QEMU. ocr.Screen takes width/height because qmp.py needs the geometry at
        import time to convert pixels to absolute pointer coordinates; that is the same pair of
        numbers a screenshot comes back at, so it is the honest answer.
        """
        return (self.screen.width, self.screen.height)

    def _sample(self, kind: str) -> tuple[float, int] | None:
        """One reading of the framebuffer: (mean grey, coarse hash), or None.

        BOTH FROM ONE SCREENDUMP, AND THROUGH ocr.Screen.grab(). The first version called
        `q.execute("screendump", ...)` itself and read the file straight afterwards, with a
        fixed filename - so the read raced the write AND every sample overwrote the previous
        one. `wait_for_change` then compared a fingerprint with itself and reported "the
        picture never moved" over a pane whose Continue button had visibly been clicked.

        grab() already solves both: it sleeps for the write, and a failing screendump prints
        why instead of returning nothing.
        """
        from PIL import Image

        self.samples += 1
        path = self.screen.grab(f"sample-{self.samples:04d}-{kind}".replace(" ", "-"))
        if not path:
            return None
        try:
            image = Image.open(path).convert("RGB")
            pixels = image.tobytes()
        except (OSError, ValueError) as exc:
            log(f"  cannot read {path}: {exc}")
            return None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        total = 0
        count = 0
        digest = 0
        # EVERY 97TH PIXEL over all three channels. A stride larger than any text run, and far
        # finer than "the whole screen scrolled"; install.py's measurement is where the number
        # comes from, including the run where hashing one channel made two different
        # backgrounds identical.
        for index in range(0, len(pixels) - 2, 97 * 3):
            total += pixels[index] + pixels[index + 1] + pixels[index + 2]
            count += 3
            digest = (digest * 131 + pixels[index]) & 0xFFFFFFFF
            digest = (digest * 131 + pixels[index + 1]) & 0xFFFFFFFF
            digest = (digest * 131 + pixels[index + 2]) & 0xFFFFFFFF
        return ((total / count) if count else 0.0, digest)

    def brightness(self) -> float | None:
        """The mean grey of a fresh screendump, or None when it cannot be read."""
        sample = self._sample("brightness")
        return None if sample is None else sample[0]

    def fingerprint(self) -> int | None:
        """A coarse hash of the current screen, for "did the picture change at all"."""
        sample = self._sample("fingerprint")
        return None if sample is None else sample[1]

    def log(self, message: str) -> None:
        """A step module's own line, in the same shape as this module's.

        A `python:` step is a function in another file and calls back into the driver for
        look/click/type; it also needs to say what it decided, and a step module that printed
        directly would produce a second log format inside one run.
        """
        log(f"  {message}")

    def says(self, *want: str) -> bool:
        have = {"".join(c for c in w[0].lower() if c.isalnum()) for w in self.words}
        return all("".join(c for c in x.lower() if c.isalnum()) in have for x in want)

    def click(self, xy, why: str, settle: float = 2.0) -> None:
        log(f"click {tuple(xy)} - {why}")
        self.qmpmod.click(self.q, *xy)
        time.sleep(settle)

    def double_click(self, xy, why: str, settle: float = 20.0) -> None:
        log(f"double-click {tuple(xy)} - {why}")
        x, y = xy
        Q = self.qmpmod
        Q._send(self.q, "press", [
            {"type": "abs", "data": {"axis": "x", "value": Q.to_abs(x, 1920)}},
            {"type": "abs", "data": {"axis": "y", "value": Q.to_abs(y, 1080)}},
            {"type": "btn", "data": {"down": True, "button": "left"}},
        ])
        time.sleep(0.08)
        Q._send(self.q, "release", [{"type": "btn", "data": {"down": False, "button": "left"}}])
        time.sleep(0.08)
        Q._send(self.q, "press 2", [{"type": "btn", "data": {"down": True, "button": "left"}}])
        time.sleep(0.08)
        Q._send(self.q, "release 2", [{"type": "btn", "data": {"down": False, "button": "left"}}])
        time.sleep(settle)

    def type_into(self, xy, value: str, *, clear: bool = True, per_char: float = 0.1,
                  modifier: str = "ctrl") -> None:
        """Click a field, clear it, type the value.

        `modifier` IS NOT COSMETIC. Select-all is Control+A on Linux and Command+A on macOS,
        and the wrong one does something else entirely: on macOS Control+A moves the cursor to
        the start of the line, so the old text stays, the new text is INSERTED before it, and
        the field holds both.

        That is what the YAML desktop stage did on its first run - the account form read

            Create | a | Mac | Account | The | password | you | create | here | will | be | used
            | to | log | in | to

        for ninety seconds while every later pane was answered against a form that never
        validated: `full name` became `docker` appended to whatever the installer had already
        put there, so Continue stayed disabled and the wizard never left the pane. drive_read.py
        has always used meta_l; this is the same measurement, reachable from a walkthrough as
        `modifier: meta_l`.
        """
        self.qmpmod.click(self.q, *xy)
        time.sleep(1.2)
        if clear:
            # TWICE, THEN TWENTY-FOUR BACKSPACES - drive_read.py's clear_field(). macOS ignores
            # a select-all that arrives while a field is still taking focus, and a field that
            # held a suggested name needs the backspaces when the select-all did not take.
            for _ in range(2):
                self.qmpmod.key_combo(self.q, modifier, "a")
                time.sleep(0.5)
                self.qmpmod.key(self.q, "backspace")
                time.sleep(0.4)
            for _ in range(24):
                self.qmpmod.key(self.q, "backspace")
                time.sleep(0.12)
            time.sleep(0.8)
        import keys
        unsupported = keys.type_text(self.q, value, per_char=per_char)
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(0.8)

    def key(self, spec: str, hold: float = 0.0) -> None:
        names = spec.split("+")
        if len(names) == 1:
            self.qmpmod.key(self.q, names[0])
        else:
            self.qmpmod.key_combo(self.q, *names, hold=hold)

    def press(self, name: str, times: int = 1, gap: float = 3.0) -> None:
        """Send one key `times` times, pausing `gap` between.

        SEPARATE FROM key(), because a REPEATED key is how macOS confirms a sheet: the Apple
        Account pane's `Set Up Later` link raises a sheet, and if the sheet has not drawn yet a
        Return raises it - which is what drive_read.py's own 15 branch does with
        `press("ret", gap=3.0)`. That call is spelled `key_combo`-adjacent but means "the same
        key again", and a step vocabulary that cannot say it leaves a pane unanswered.
        """
        for _ in range(times):
            self.qmpmod.key(self.q, name)
            time.sleep(gap)

    def close(self) -> None:
        try:
            self.q.close()
        except Exception:                              # noqa: BLE001
            pass

    def reconnect(self, socket_timeout: int = 300) -> None:
        """Drop the QMP connection, wait for the socket to come back, and open a new one.

        WHY A YAML FILE NEEDS THIS. A `reboot:` step asks qemu to reset the guest; qemu keeps the
        same socket, but the CONNECTION dies with the machine's session - and an OCR `Screen`
        holds one. Without this, the first step after a reboot talks to a closed socket and
        reports a guest that is not there.

        THE SOCKET FILE IS THE SIGNAL. It is removed while the machine resets and recreated when
        the monitor is listening again, so waiting on the file is waiting on the thing that
        actually has to happen. Same test Driver.__init__ makes at startup - this is that code
        made repeatable rather than a second copy of it.
        """
        self.close()
        deadline = time.time() + socket_timeout
        while not os.path.exists(self.qmp_path):
            if time.time() > deadline:
                raise StepError(
                    f"the QMP socket {self.qmp_path} did not come back within {socket_timeout}s")
            time.sleep(1)
        # AND A MOMENT FOR THE MONITOR TO ACCEPT A CLIENT. The file can exist a beat before
        # qemu is listening on it, and a connect in that window blocks on the greeting.
        time.sleep(1)
        import ocr
        # THE GEOMETRY COMES FROM THE SCREEN THAT IS BEING REPLACED, not from a constant: a
        # walkthrough may declare any resolution and the OCR crop tables are measured against it.
        self.screen = ocr.Screen(self.qmp_path, os.path.join(self.work, "shots"),
                                 width=self.screen.width, height=self.screen.height)
        self.q = self.screen.q


class DeferredDriver(Driver):
    """A Driver that connects to nothing until the file's own `boot:` step has started a machine.

    WHY THIS EXISTS. `Driver.__init__` waits for the QMP socket, because every walkthrough before
    this one was handed a running guest. A file that declares `boot:` has no socket when its
    driver is built - the first step creates it - so the wait would time out on a file that was
    about to work.

    IT SUBCLASSES Driver ON PURPOSE. Every method of Driver asks `self.screen` for something, and
    the only thing this changes is when that exists. A second, parallel class would be a second
    copy of several hundred lines of click/type/read logic that must stay identical.
    """

    def __init__(self, work: str, *, width: int = 1920, height: int = 1080,
                 disk_path: str | None = None) -> None:
        # NOT super().__init__ - THAT IS THE WAIT. Everything Drivers store, set directly.
        import ocr
        import qmp as qmpmod

        self.qmpmod = qmpmod
        self._ocr = ocr
        self.work = work
        os.makedirs(os.path.join(work, "shots"), exist_ok=True)
        self.qmp_path = ""
        self.disk_path = disk_path
        self.width = width
        self.height = height
        self.screen = None
        self.q = None
        self.n = 0
        self.samples = 0
        self.ppm = None
        self.words = []

    def reconnect(self, socket_timeout: int = 300) -> None:
        """Attach for the first time, or reattach after a reset. Both are the same act."""
        deadline = time.time() + socket_timeout
        if not self.qmp_path:
            raise StepError("a `boot:` step did not give this driver a socket")
        while not os.path.exists(self.qmp_path):
            if time.time() > deadline:
                raise StepError(
                    f"the QMP socket {self.qmp_path} did not appear within {socket_timeout}s")
            time.sleep(1)
        time.sleep(1)
        if self.q is not None:
            try:
                self.q.close()
            except Exception:                          # noqa: BLE001
                pass
        self.screen = self._ocr.Screen(self.qmp_path, os.path.join(self.work, "shots"),
                                       width=self.width, height=self.height)
        self.q = self.screen.q


class MachineSession:
    """The guest a walkthrough owns, when the file declares `boot:`.

    WHY THIS IS NOT IN lib/vm_session.py. That module answers "start a disk and hand me the
    guest"; this one answers "a YAML file is driving, so the guest's lifetime belongs to the
    file". The difference matters at the end: `vm_session.start` returns a guest the CALLER
    stops, while here the file may stop it itself with `shutdown:` and the runner must not stop
    it twice. The machine itself still comes from vm_session, so there is one description of what
    a macOS or Windows guest is.

    `boot:` MAY APPEAR ONLY ONCE. Two boots in one file would mean the second silently replaces
    the first and everything between them ran against a machine nobody stopped.
    """

    def __init__(self, family: str, disk: str, work: str, *, share=None,
                 mem_mb: int = 0, cpus: int = 4) -> None:
        self.family = family
        self.disk = disk
        self.work = work
        self.share = share
        self.mem_mb = mem_mb
        self.cpus = cpus
        self.guest = None
        self.overlay = None
        self.qmp_path = ""
        self.stopped = False

    def boot(self, family: str, *, ssh_port: int = 2222, log=print) -> None:
        if self.guest is not None:
            raise StepError(
                "this file booted a machine already; a second `boot:` would replace it without "
                "stopping it. Put the second boot in its own file.")
        import vm_session

        # ANDROID-X86'S "DISK" IS A DIRECTORY, SO THE OVERLAY HAS TO GO.
        #
        # `copy=True` wraps the disk in a qcow2 backing chain so a verification stage cannot
        # write to a published artifact. That is right for one qcow2 and wrong here: the guest
        # is handed three separate files and creates its own /data, so there is nothing to
        # overlay. Booting it with copy=False is also honest about what this stage does - it
        # starts the extracted set and photographs it, and the /data it writes lives in `work`
        # exactly like every other family's overlay does.
        android = (family or self.family) == "android"
        if android and self.share is not None:
            log("android-x86: no host share (this guest mounts no smb filesystem)")
            self.share = None

        guest, reached, overlay = vm_session.start(
            family or self.family, self.disk, self.work,
            ssh_port=ssh_port, wait_for_ssh=False, copy=not android, share=self.share,
            cpus=self.cpus, mem_mb=self.mem_mb, log=log)
        if guest is None:
            raise StepError("the machine could not be started")
        self.guest, self.overlay = guest, overlay
        self.qmp_path = guest.paths.drive
        if not reached:
            raise StepError(
                f"the machine started but never answered on ssh port {ssh_port}; "
                f"the console is in {self.work}/console.log")

    def wait_for_ssh(self, ssh_port: int, seconds: int) -> None:
        import session as sessmod
        if not sessmod.wait_for_ssh(ssh_port, seconds, log, probe="echo ok"):
            raise StepError(
                f"the machine did not come back on ssh port {ssh_port} within {seconds}s")

    def shutdown(self, *, graceful: bool = True, log=print) -> None:
        if self.guest is None or self.stopped:
            return
        import vm_session
        vm_session.stop(self.guest, acpi_first=graceful, family=self.family,
                        overlay=self.overlay, share=False, log=log)
        self.stopped = True

    def stop_if_running(self, *, log=print) -> None:
        """What `run_file` calls on the way out, so a failed file does not leave a machine up."""
        if self.guest is not None and not self.stopped:
            self.shutdown(graceful=True, log=log)


# --- the interpreter ------------------------------------------------------------------


def _expect_matches(driver: Driver, expect=None, expect_any=None) -> bool:
    if expect:
        words = expect if isinstance(expect, list) else [expect]
        return all(driver.says(w) for w in words)
    if expect_any:
        return any(all(driver.says(w) for w in group) for group in expect_any)
    return True


def _wait_for_change(driver: Driver, step: Step) -> bool:
    """Until the picture has moved away from what it was AND then stopped again.

    THE MISSING HALF IS WHY RUN 41615881004 FAILED. "Has the picture stopped moving" is the
    right question for a pane that is PAINTING, and the wrong one for a pane that is supposed
    to REPLACE another: a sheet that never closed is perfectly still, so the wait returned True
    while the licence sheet was still up and the next two clicks landed on the sheet.

    So: take a fingerprint now, wait for it to move, then wait for the new picture to settle.
    A screen that never changes times out, which is the honest answer for a click that did
    nothing. install.py's version of this is where the measurement comes from.
    """
    timeout = float(step.get("change_timeout", 120))
    settle = float(step.get("change_settle", 5))
    before = driver.fingerprint()
    if before is None:
        log(f"  {step.name}: the screen cannot be read; nothing can be compared")
        return False

    deadline = time.time() + timeout
    changed = False
    stable = 0
    last = before
    samples = 0
    while time.time() < deadline:
        time.sleep(settle)
        now = driver.fingerprint()
        if now is None:
            continue
        samples += 1
        if not changed:
            if now != before:
                changed = True
                log(f"  {step.name}: the screen changed after {samples} sample(s)")
            continue
        # SOMETHING NEW IS HERE; now wait for it to finish drawing. Two identical fingerprints
        # in a row is the settle, because a repaint that is still going changes every frame.
        if now == last:
            stable += 1
            if stable >= 1:
                log(f"  {step.name}: settled")
                return True
        else:
            stable = 0
        last = now

    # SAY WHICH OF THE TWO FAILURES IT WAS, WITH THE NUMBERS. "The picture never moved" and
    # "it moved and never settled" are different bugs, and the first real run of setup.yaml
    # reported neither - it said "no change in 120s" over a pane whose Continue button had just
    # been clicked, which could be a missed click, a wrong coordinate, or a fingerprint that
    # cannot see this particular repaint. The count is what tells them apart.
    log(f"  {step.name}: no change in {timeout:.0f}s over {samples} sample(s): "
        f"{'the picture never moved' if not changed else 'it moved and never settled'}")
    # AND WHAT THE SCREEN ACTUALLY SAYS, because a click that did nothing and a click that
    # landed on the wrong control look identical in a fingerprint. This line is the difference
    # between "the coordinate is wrong" and "the guest is not responding".
    driver.look(f"{step.name} - unchanged")
    log(f"    brightness now {driver.brightness()}")
    return False


def _run_until_dark(driver: Driver, step: Step) -> bool:
    """Until the screen is mostly black, sampled rather than OCR'd.

    THE INSTALLER'S OWN START SIGNAL. macOS agrees to the licence, takes the destination, and
    then goes BLACK while it copies - so a dark frame is not a failure here, it is the evidence
    that the click was accepted and the copy began. install.py calls the same measurement
    "brightness", and it is a mean grey over a fixed stride.

    A THRESHOLD AND NOT "zero", because a black framebuffer still has a few non-zero pixels and
    a transition through near-black is long enough to catch. install.py measured every real
    pane here at 20 or more mean grey, so 8 is far from both ends.
    """
    timeout = float(step.get("timeout", 600))
    threshold = float(step.get("dark", 8))
    poll = float(step.get("poll", 10))
    deadline = time.time() + timeout

    while time.time() < deadline:
        brightness = driver.brightness()
        if brightness is not None and brightness <= threshold:
            log(f"  {step.name}: the screen is dark ({brightness:.0f} <= {threshold:.0f})")
            return True
        time.sleep(poll)
    log(f"  {step.name}: the screen never went dark in {timeout:.0f}s")
    return False


def _read_region(driver: Driver, region, scale: int, label: str) -> list[tuple]:
    """Read one rectangle and leave the words on the driver. Nothing else is decided here."""
    driver.look_at(region, label, scale=scale)
    return driver.words


def _read(driver: Driver, step: Step, label: str = "look") -> list[tuple]:
    """One look, cropped to the step's `region` when it has one.

    THE SINGLE PLACE THE CROP IS APPLIED, so `until`, `expect` and `sweep` cannot disagree about
    whether they were reading a region or the whole screen. That disagreement is exactly the
    bug where a `until` matched a sentence in a paragraph on the other half of the frame.

    A CROP IS IN FRAME PIXELS AND THE FRAME IS NOT ALWAYS 1920x1080. The UEFI shell draws at
    1280x800 and the macOS installer at 1920x1080, so a region measured on one is empty space on
    the other - and an empty crop reads as "the words are not there", which is indistinguishable
    from "the pane has not arrived". A region therefore states the resolution it was measured
    at, and a mismatch is an error rather than a silent empty read.
    """
    region = step.get("region")
    if region:
        at = step.get("region_at")
        if at:
            width, height = driver.resolution()
            if [width, height] != list(at):
                # A REGION IS ONLY MEANINGFUL ON THE SCREEN IT WAS MEASURED ON, and the guest
                # changes size during a boot: the UEFI shell draws at 1280x800, the recovery
                # menu at 1920x1080, and lib/live.py's own log records
                #
                #     frames arrive in 3 sizes (640x480, 1280x800, 1920x1080)
                #
                # So a mismatch is not always a mistake in the file - it can be a poll that
                # landed before the mode changed. It is reported and the step reads nothing,
                # which for an `until` means "try again next poll"; the caller's timeout is
                # what decides whether the pane ever arrives at the size the file expects.
                log(f"  {step.name}: the framebuffer is {width}x{height} but this region was "
                    f"measured at {at[0]}x{at[1]}; not reading it")
                return []
        return driver.look_at(region, label, scale=int(step.get("scale", DEFAULT_CROP_SCALE)))
    return driver.look(label)


def _disk_mb(driver) -> int | None:
    """Allocated MiB in the target qcow2, or None when the walk has no disk to watch.

    st_blocks, NOT st_size: a qcow2 is sparse, so its apparent size is the virtual disk size
    from the moment it is created and never changes. The allocated blocks are what grow as
    the guest writes - the measurement macos/15/install.py's disk_allocated() has always used.
    """
    path = getattr(driver, "disk_path", None)
    if not path:
        return None
    try:
        return os.stat(path).st_blocks * 512 >> 20
    except OSError:
        return None


def _run_until(driver: Driver, step: Step) -> bool:
    """Wait until the step's `until` words are on screen, or its timeout expires.

    TWO SHAPES, BECAUSE A COMPLETION IS NOT ALWAYS ONE PHRASE. `until: [all, done]` is a
    conjunction - every word must be up. `until_any: [[all, done], [you, may, now], ...]` is a
    disjunction - the step ends on the first group that matches. The second exists for the
    sentence an installer prints when it finishes, because that sentence is TRANSLATED: the
    Calamares drivers each carry a list of completions in five languages, and what was Python
    there is a list here.

    THE KEEP-ALIVE CLICK IS AT A FIXED HARMLESS POINT, not wherever the pointer is. Several
    guests blank their own display on idle and a QMP click from outside is not the event their
    idle timer counts - mint needed a real click and ghostbsd needed the same, and both
    measured that a moving pointer alone was not enough.
    """
    if step["until_any"]:
        groups = [[g] if isinstance(g, str) else list(g) for g in step["until_any"]]
    else:
        target = step["until"]
        groups = [target if isinstance(target, list) else [target]]

    timeout = float(step.get("timeout", 1800))
    poll = float(step.get("poll", 20))
    deadline = time.time() + timeout

    while time.time() < deadline:
        _read(driver, step, step.name)
        if any(all(driver.says(w) for w in group) for group in groups):
            return True
        if step["keep_alive"]:
            try:
                driver.qmpmod.click(driver.q, 400 + (int(time.time()) % 40), 300)
            except Exception:                          # noqa: BLE001
                pass
        time.sleep(poll)
    return False


def _run_until_disk_settles(driver: Driver, step: Step) -> bool:
    """Until the target disk has stopped growing - which is the only honest "it is written".

    WHY A SCREEN SENTENCE IS NOT ENOUGH, AND THIS IMAGE IS THE PROOF. Fedora's Anaconda
    prints `Finalizing: The system configuration is complete. Finalizing installation may take
    a few moments.` while it is STILL WRITING, and it printed exactly that for forty minutes on
    run 42502633627 while the disk was being filled. A wait that ends on the sentence therefore
    declares the install finished at the moment the last files are still being copied, and the
    caller then stops the machine.

    THE DISK CAN TELL THE TWO APART. An installation writes gigabytes into the target qcow2; a
    guest that has stopped writes nothing. `st_blocks` is what grows on a sparse file, and a
    quiet interval is the answer. It is the measurement macos/15/install.py's installer_panes()
    has always used for the same reason, and its docstring says it best:

        a guess about duration cannot tell slow from stopped.

    TWO CONDITIONS, AND BOTH MATTER:

        disk_min_mb    a floor, so a disk that has not been touched yet cannot look "settled".
                       An unwritten sparse image sits at a few MiB and never moves.
        disk_settle    how long the size must not change. Long enough to cover one qcow2
                       flush, short enough not to spend minutes after the last write.

    A SCREEN THAT CHANGES IS NOT A REASON TO KEEP WAITING: this is for an installer whose last
    pane is static while it copies, so the disk replaces the screen entirely here.
    """
    timeout = float(step.get("timeout", 3600))
    poll = float(step.get("poll", 20))
    quiet_needed = float(step.get("disk_settle", 180))
    floor = int(step.get("disk_min_mb", 512))
    deadline = time.time() + timeout

    last_size: int | None = None
    quiet_since: float | None = None
    while time.time() < deadline:
        mb = _disk_mb(driver)
        if mb is None:
            log(f"  {step.name}: no disk to watch; this wait cannot decide")
            return False
        if last_size is None or mb != last_size:
            log(f"  the disk is {mb} MiB allocated")
            last_size, quiet_since = mb, time.time()
        elif mb >= floor and time.time() - quiet_since >= quiet_needed:
            log(f"  the disk has held at {mb} MiB for {quiet_needed:.0f}s - it is written")
            return True
        if step["keep_alive"]:
            try:
                driver.qmpmod.click(driver.q, 400 + (int(time.time()) % 40), 300)
            except Exception:                          # noqa: BLE001
                pass
        time.sleep(poll)
    log(f"  {step.name}: the disk never settled (last {last_size} MiB)")
    return False


def _do_action(driver: Driver, step: Step, *, ssh_port: int = 2222,
               password: str | None = None, session=None) -> None:
    """Everything a step does, in the order that makes one step one action.

    Separated from walk() because the pane loop and the sequence loop must do exactly the same
    thing to a step; two copies would be the "one fact in two places" mistake this repository
    keeps paying for.
    """
    settle = float(step.get("settle", 2.0))

    # --- THE MACHINE'S OWN LIFECYCLE -------------------------------------------------------
    #
    # A YAML FILE MAY OWN ITS GUEST. `boot:` starts it, `reboot:` resets it, `shutdown:` stops
    # it. That is what makes the whole qemu lifecycle declarable instead of living in a stage
    # script - and it is why the three are sequence-only: a pane is matched against a screen that
    # has to exist, and "start the machine" is the step that makes one.
    #
    # `session` CARRIES THE GUEST BETWEEN STEPS. It is created by `boot:` and consumed by every
    # later step that needs the socket; `run_file` owns it and stops it if the file forgets.
    if step["boot"]:
        if session is None:
            raise StepError(f"{step.name}: boot needs a session; run this file through run_file")
        family = str(step.get("family") or "macos")
        # `boot:` IS OVER WHEN QEMU IS RUNNING. Nothing else.
        #
        # AN SSHD INSIDE THE GUEST IS A DIFFERENT FACT, AND `require_ssh:` IS THE STEP THAT ASKS
        # FOR IT. Keeping the two apart is what makes a walkthrough able to install an OS (no
        # sshd exists yet) or drive a GUI (a login shell is irrelevant) - and it puts the wait
        # where a reader can see it, with its own timeout, instead of hiding it in a boot.
        session.boot(family, ssh_port=ssh_port, log=log)
        # THE DRIVER HAS TO SEE THE NEW SOCKET. A boot in the middle of a sequence replaces the
        # machine, and a Driver built before it holds a connection to a qemu that no longer runs.
        if hasattr(driver, "reconnect") and session.qmp_path:
            driver.qmp_path = session.qmp_path
            driver.reconnect()
        return

    if step["reboot"]:
        if session is None or session.guest is None:
            raise StepError(f"{step.name}: reboot needs a machine that boot: started")
        session.guest.reset()
        if hasattr(driver, "reconnect"):
            driver.reconnect()
        else:
            raise StepError(
                f"{step.name}: reboot needs a screen driver; a headless file cannot survive one")
        # `reboot:` IS OVER WHEN THE RESET IS DONE AND THE SCREEN IS BACK - the same rule as
        # `boot:`. Whether an sshd answers afterwards is `require_ssh:`'s business, and it is the
        # step after this one in every file that cares. Waiting here as well would make a
        # GUI-only walkthrough unable to reboot, and would hide the wait from the reader.
        return

    if step["shutdown"]:
        if session is None:
            raise StepError(f"{step.name}: shutdown needs a session")
        session.shutdown(graceful=bool(step.get("graceful", True)))
        return

    # THE ACCOUNT ON THIS DISK, WHEN THE FILE SAYS IT IS NOT THE USUAL ONE.
    #
    # Every image here was installed with the account it was given, so `docker` is right for
    # all of them but one: Pop!_OS' installer derives the login name from the full name and
    # writes `dockeruser`. A walkthrough that knows that says `ssh_user: dockeruser` in its
    # own `vars`, and this is where that reaches the client. `None` means "the default the
    # client already carries", so a file that does not name one is unchanged.
    login = (getattr(driver, "values", {}) or {}).get("ssh_user") or None

    # --- require_ssh: WAIT FOR THE GUEST'S OWN SSHD BEFORE USING IT -------------------
    #
    # THE STAGE THAT TURNS REMOTE LOGIN ON IS A GUI STAGE; EVERYTHING AFTER IT IS NOT. So a
    # walkthrough that has an ssh section has to wait for the daemon the previous step enabled,
    # and the wait belongs where the dependency is rather than as a `wait: 120` at the top of
    # the file - that number is the exact weakness macos/15/ssh.py's docstring records.
    if step["require_ssh"]:
        import session
        seconds = int(step.get("timeout", 600))
        # THE PROBE IS NOT `true` ON EVERY GUEST. session.wait_for_ssh's own docstring records
        # why: a Windows guest runs cmd.exe, where `true` is
        #
        #     'true' is not recognized as an internal or external command
        #
        # and run 41844808248 spent its whole fifteen-minute budget on a Windows sshd that was
        # answering perfectly. A walkthrough whose guest is not a POSIX shell says so with
        # `ssh_probe: echo ok`.
        probe = str(step.get("ssh_probe", "true"))
        log(f"  waiting up to {seconds}s for sshd on port {ssh_port}")
        if not session.wait_for_ssh(ssh_port, seconds, user=login, probe=probe,
                                    password=password):
            # A FRAME BEFORE FAILING, because the question that follows is always "what was
            # on screen". The ssh stage's first failures had nothing to look at, and the
            # answer - a lock screen, an empty terminal, a sudo prompt - was only visible in
            # a picture.
            try:
                _dump_frame(driver, step.name)
            except Exception:                      # noqa: BLE001
                pass
            if not step["optional"]:
                raise StepError(f"{step.name}: sshd did not answer in {seconds}s")
            log(f"  {step.name}: no sshd, and the step is optional")
        return

    # --- ssh: A COMMAND IN THE GUEST, ONCE sshd IS UP --------------------------------
    #
    # THIS IS THE THIRD WAY TO ACT ON A GUEST AND IT IS THE CHEAPEST. A click is a guess about
    # a coordinate; typing into a terminal is a guess about a window being focused; an ssh
    # command is the guest's own shell answering, so a step can assert on its exit status
    # instead of on a word on a screen. Every stage after Remote Login is turned on should
    # prefer this, and macos/15/ssh.py is what turns it on.
    #
    # It reuses lib/session.py rather than re-deriving the client: that module already carries
    # the two measured details - SSH_HOST is the bridge and not 127.0.0.1 (DNAT does not
    # rewrite the source, so a loopback connection is answered to the guest's own loopback and
    # hangs), and sshpass because there is no tty to prompt at.
    if step["ssh"]:
        import session
        command = str(step["ssh"])
        timeout = int(step.get("timeout", 120))
        status, output = session.ssh(ssh_port, command, timeout=timeout, user=login,
                                     password=password)
        log(f"  ssh: {command} -> {status}")
        for line in (output or "").splitlines()[:40]:
            log(f"    {line}")
        # AN EXIT STATUS IS THE VERDICT. There is no `expect` on an ssh step: the shell already
        # said whether it worked, and a word on a screen is a weaker answer than a return code.
        if status != 0 and not step["optional"]:
            raise StepError(f"{step.name}: the command exited {status}")
        return

    # --- script: A BODY WRITTEN ON THE HOST, COPIED IN, RUN BY PATH --------------------
    #
    # THIS EXISTS BECAUSE A COMMAND THAT SURVIVES ssh -> cmd -> PROGRAM IS A NARROW THING.
    # windows/11/devtools.py says it plainly: a Visual Studio install takes an --override with
    # spaces and quotes, and by the time it has been through ssh and cmd the quotes are gone.
    # Its answer - write the line to a .bat, copy it up, run it by path - is not a Windows
    # quirk, it is what any guest needs when a command is too fragile to quote.
    #
    #     - name: install the build tools
    #       script: |
    #         @echo off
    #         winget install --id Microsoft.VisualStudio.2022.BuildTools --exact ...
    #       script_shell: cmd
    #       timeout: 3600
    #
    # The body is written with CRLF endings and no BOM, because cmd.exe reads a .bat byte for
    # byte and a stray \r is a command name.
    if step["script"]:
        import session
        body = str(step["script"])
        shell = str(step.get("script_shell", "powershell"))
        name = str(step.get("script_name") or f"{step.index}-{abs(hash(body)) % 10**6}.ps1")
        if shell == "cmd" and not name.endswith((".bat", ".cmd")):
            name += ".bat"
        if shell in ("powershell", "pwsh") and not name.endswith(".ps1"):
            name += ".ps1"
        if shell == "sh" and not name.endswith(".sh"):
            name += ".sh"
        # THE BODY IS WRITTEN INTO THE STAGE'S WORK DIRECTORY, which both drivers have and
        # which is on the same filesystem as the rest of the stage's scratch. A headless
        # walkthrough has one too - lib/steps.py's HeadlessDriver takes `work` for exactly
        # this and for the frames a failing step wants to leave behind.
        work_dir = getattr(driver, "work", "") or "."
        os.makedirs(work_dir, exist_ok=True)
        local = os.path.join(work_dir, name)
        body = body.replace("\r\n", "\n").replace("\n", "\r\n") if shell == "cmd" else body
        with open(local, "w", newline="") as fh:
            fh.write(body)
        remote = f"C:/Users/{login or 'docker'}/{name}"
        if shell == "sh":
            remote = f"/tmp/{name}"
        timeout = int(step.get("timeout", 1800))
        status, output = session.scp(local, remote, port=ssh_port, timeout=300, user=login,
                                     password=password)
        if status != 0:
            log(f"  scp: {name} -> {remote} -> {status}")
            if not step["optional"]:
                raise StepError(f"{step.name}: the copy exited {status}")
            return
        if shell == "cmd":
            command = 'cmd /c "' + remote.replace("/", "\\") + '"'
        elif shell in ("powershell", "pwsh"):
            command = (f'powershell -NoProfile -ExecutionPolicy Bypass -File '
                       f'"{remote}"')
        else:
            # `bash`, NOT `sh`. The shebang inside the body is ignored by an explicit
            # interpreter argument, and these scripts use `set -uo pipefail` and `[[ ]]`
            # freely - `sh` on macOS is bash 3.2 in POSIX mode, where pipefail does not
            # exist and a failing stage of a pipeline is silently ignored. bash 3.2 itself
            # is fine; it is the mode that is not.
            command = f"bash {remote}"
        status, output = session.ssh(ssh_port, command, timeout=timeout, user=login,
                                     password=password)
        log(f"  script {name} ({shell}) -> {status}")
        for line in (output or "").splitlines()[:40]:
            log(f"    {line[:150]}")
        if status != 0 and not step["optional"]:
            raise StepError(f"{step.name}: the script exited {status}")
        return

    # --- share_serve: THE HOST STARTS SERVING, AND NOTHING ELSE ----------------------
    #
    # ONE STEP, ONE ACT. Starting smbd and mounting it in the guest used to be one step, and that
    # made a failure ambiguous: "the guest could not mount the share" was the message for a host
    # with no smbd, a credential the guest could not use, and a network that did not route - three
    # repairs, one sentence.
    #
    # Splitting them also lets a walkthrough serve the share in one stage and mount it in another,
    # which is what a stage that only proves the host side needs.
    if step["share_serve"]:
        import hostshare

        directory = str(step.get("share_dir") or hostshare.DEFAULT_DIR)
        if not hostshare.start(directory):
            if not step["optional"]:
                raise StepError(f"{step.name}: the host could not serve {directory}")
            log(f"  {step.name}: the share did not start, and the step is optional")
            return
        log(f"  serving {directory} on {hostshare.net.BRIDGE_IP}:445")
        return

    # --- share_mount: THE GUEST MOUNTS WHAT THE HOST IS SERVING -----------------------
    #
    # IT DOES NOT START THE SERVER. If nothing is serving, that is the failure and it should be
    # named as one - `share_serve:` is the step that starts it.
    #
    # TWO THINGS IN ONE STEP, because one without the other is not a fact anyone can check:
    # the host starts serving the directory, and the guest mounts it and is asked to read
    # something. A step that only started smbd would pass while a guest that cannot mount it
    # fails four steps later for a reason that looks like a broken disk.
    #
    # WHY SMB IS THE PROTOCOL FOR BOTH GUESTS. lib/hostshare.py has the long version; the
    # short one is that Windows has no 9p driver at all (virtio-win closed the request as
    # not-planned) and macOS 9p is read-only (mount_9p takes -r and nothing else). SMB is what
    # both speak natively, read and write, with nothing installed.
    #
    # THE LINUX FAMILY USES 9p, which IS its kernel's own client - see lib/hostshare.py's
    # mount_commands for the three command lines.
    if step["share_mount"]:
        import hostshare
        import session

        family = str(step.get("share_family") or "macos")
        if not hostshare.listening():
            raise StepError(
                f"{step.name}: nothing is serving on port 445; a `share_serve:` step comes first")

        command = hostshare.mount_commands(user=login or "docker",
                                          login_password=password or "admin")[family]
        timeout = int(step.get("timeout", 180))
        status, output = session.ssh(ssh_port, command, timeout=timeout, user=login,
                                    password=password)
        log(f"  mount: {command[:100]} -> {status}")
        for line in (output or "").splitlines()[:10]:
            log(f"    {line[:150]}")
        if status != 0 and not step["optional"]:
            # THE SERVER'S OWN ACCOUNT OF THE REFUSAL, WHICH IS WHERE THE ANSWER IS.
            #
            # The client says only "Authentication error". At `log level = 3` smbd records the
            # session setup - the dialect, the auth type offered, the nt status returned - and
            # that is the difference between "the password is wrong", "the account is not in the
            # passdb" and "the client never sent credentials". Without this line three runs were
            # spent guessing which.
            hostshare.log_tail()
            raise StepError(f"{step.name}: the guest could not mount the share")
        return

    # --- scp: A FILE INTO THE GUEST, BECAUSE SOME WORK IS NOT TYPEABLE -----------------
    #
    # `ssh:` can run any one command; it cannot carry a 190-line script. windows/11/devtools.py
    # has needed that since it was written - its own docstring says so ("a script is a FILE:
    # written on the host, copied with scp, run by path") - and it re-derived the client in
    # its own module. A walkthrough that could not express it would have to keep the stage in
    # Python for the sake of one copy.
    #
    #     - name: copy the compiler check in
    #       scp: [windows/11/msvc-check.ps1, C:/Users/docker/msvc-check.ps1]
    if step["scp"]:
        import session
        pair = step["scp"]
        if not isinstance(pair, list) or len(pair) != 2:
            raise StepError(f"{step.name}: `scp` is [local, remote]")
        local, remote = (str(v) for v in pair)
        if not os.path.isabs(local):
            local = os.path.join(REPO, local)
        timeout = int(step.get("timeout", 300))
        status, output = session.scp(local, remote, port=ssh_port, timeout=timeout,
                                     user=login)
        log(f"  scp: {local} -> {remote} -> {status}")
        for line in (output or "").splitlines()[:10]:
            log(f"    {line}")
        if status != 0 and not step["optional"]:
            raise StepError(f"{step.name}: the copy exited {status}")
        return

    if step["double"]:
        driver.double_click(step["double"], step.name, settle=settle)

    # --- sweep: A BAND OF CLICKS, BECAUSE A SINGLE VALUE IS A COIN TOSS ---------------
    #
    # ONE CONTROL HERE HAS A HIT AREA A FEW PIXELS TALL AND IT MOVES BY A PIXEL OR TWO as the
    # toolbar redraws. install.py's history is the evidence, and it is worth keeping:
    #
    #     (1290, 306) nothing   (1290, 296) nothing   (1290, 311) nothing
    #     (1290, 314) nothing   (1290, 309) nothing
    #     (1255, 278) the sheet OPENED
    #     (1290, 295) the sheet OPENED again
    #
    # so a fixed number is a coin toss per run and the band is the fix. Twenty-one clicks two
    # seconds apart is forty seconds; the first that lands raises the sheet, and every click
    # after it lands on the sheet's own backdrop and does nothing.
    #
    # THIS IS A PRIMITIVE AND NOT A HACK. "Sweep a band until a control responds" is the
    # honest description of what a person does with an unreliable hit area, and it belongs in
    # the vocabulary rather than being written out as twenty-one `at:` steps that a reader
    # would have to count.
    # --- at_window: A CONTROL THAT MOVES WITH ITS WINDOW -------------------------------
    #
    # Disk Utility centres its window vertically and REMEMBERS ITS HEIGHT, and both differ
    # between runs - measured on two consecutive CI runs of this same stage:
    #
    #     run 42567290197   window top y=294  height 521   Erase icon y=309
    #     run 42570035685   window top y=256  height 599   Erase icon y=271
    #
    # The icon was 15 pixels below the window's top edge in BOTH. Its x was identical to the
    # pixel (1283..1295, centre 1289) in both. So the constant that survives is the offset
    # from the window, and a fixed absolute y is wrong on whatever run has the other height -
    # which is what made this coordinate look like it moved between runs for a dozen attempts.
    #
    # The top edge is found by scanning down a column for the first pixel brighter than the
    # wallpaper's near-black, which needs no OCR and cannot be confused by a word in a dialog.
    if step["at_window"]:
        x, dy = (int(v) for v in step["at_window"])
        column = int(step.get("window_x", 500))
        region_top = int(step.get("window_from", 180))
        region_bottom = int(step.get("window_to", 400))
        threshold = int(step.get("window_brightness", 40))

        if step.get("region"):
            driver.look_at(step["region"], f"{step.name} - find the window",
                           scale=int(step.get("scale", DEFAULT_CROP_SCALE)))
        else:
            driver.look(f"{step.name} - find the window")

        top = None
        if driver.ppm:
            from PIL import Image
            try:
                image = Image.open(driver.ppm).convert("RGB")
            except (OSError, ValueError):
                image = None
            if image is not None:
                for y in range(region_top, min(region_bottom, image.height)):
                    r, g, b = image.getpixel((min(column, image.width - 1), y))
                    if (r + g + b) // 3 > threshold:
                        top = y
                        break

        if top is None:
            log(f"  {step.name}: no window edge found in column {column} "
                f"between y={region_top} and y={region_bottom}; not clicking")
            return

        click_y = top + dy
        log(f"  {step.name}: window top y={top}; clicking ({x}, {click_y})")
        driver.click((x, click_y), step.name, settle=settle)
        return

    # --- at_bottom: A FOOTER THAT MOVES WITH THE WINDOW'S HEIGHT -----------------------
    #
    # THE MIRROR OF `at_window`, AND THE NIXOS INSTALLER NEEDS THAT ONE INSTEAD.
    #
    # Calamares' window in the NixOS graphical image has a FIXED TOP (y=296) and a variable
    # BOTTOM: its branding says `windowSize: 800px,520px` with `windowExpanding: normal`, so it
    # grows to fit each pane and never moves up. Measured on run 42551013541's own recording:
    #
    #     Unfree software page   window 296..853   footer buttons centred y=839
    #     Summary page           window 296..883   footer buttons centred y=869
    #
    # - 28 pixels lower, and the walk pressed (1324, 835) on BOTH. On Summary that is 30 px
    # ABOVE the `Install` button, in empty white space, so the press did nothing, the install
    # never started, and `disk_settle` watched a qcow2 that stayed at 0 MiB for an hour.
    #
    # The constant that survives is the distance from the window's BOTTOM EDGE: the button's
    # centre is 15 px above that edge on both frames, and x is 1327. `dy` is measured UP.
    #
    # SEARCH DOWNWARD FROM THE WINDOW'S TOP, NOT UPWARD FROM THE SCREEN'S BOTTOM.
    #
    # THE FIRST VERSION SCANNED UP AND FOUND THE GNOME DOCK. `at_bottom` looked for the
    # bottom-most near-white pixel in the column, and on this desktop the bottom-most bright
    # thing is not the window at all - measured on run 42574138805's own frame (NixOS'
    # Welcome page, which is the SHORTEST one):
    #
    #     window (white panel)          296..852
    #     GNOME's dock / panel          ~1035..1060   brighter than 200
    #     wallpaper between them         (59,75,160)  brightness 98
    #
    # so the scan returned y=1045 and pressed (1327,1030) - 178 pixels BELOW the Next button,
    # on the dock. Every page that reported `window bottom y=852` in the same run was right
    # by luck: those pages are tall enough that the window's own band reaches further down.
    #
    # THE WINDOW IS THE FIRST CONTIGUOUS BRIGHT BAND BELOW ITS TOP EDGE. So this finds the
    # top the way `at_window` does, walks down while the column stays bright, and takes the
    # last bright row before the first dark gap. A dock below the window is a SECOND band,
    # and is never reached.
    if step["at_bottom"]:
        x, dy = (int(v) for v in step["at_bottom"])
        column = int(step.get("window_x", 960))
        threshold = int(step.get("window_brightness", 200))
        search_from = int(step.get("window_from", 180))
        search_to = int(step.get("window_to", 1080))

        if step.get("region"):
            driver.look_at(step["region"], f"{step.name} - find the window",
                           scale=int(step.get("scale", DEFAULT_CROP_SCALE)))
        else:
            driver.look(f"{step.name} - find the window")

        bottom = None
        top = None
        if driver.ppm:
            from PIL import Image
            try:
                image = Image.open(driver.ppm).convert("RGB")
            except (OSError, ValueError):
                image = None
            if image is not None:
                col = min(column, image.width - 1)
                limit = min(search_to, image.height)

                def bright(y: int) -> bool:
                    r, g, b = image.getpixel((col, y))
                    return (r + g + b) // 3 > threshold

                # A ROW IS "IN THE WINDOW" WHEN MOST OF ITS WIDTH IS BRIGHT.
                #
                # A single column is not enough: on the Welcome page the column through the
                # artwork passes through dark shapes, and the longest 1-column band is a
                # picture, not the window. Measured on run 42574138805's Welcome frame, over
                # the window's own x span (470..1450, every 10 px, 98 samples):
                #
                #     y=330..852   83..98 samples bright   <- the window
                #     y=853..      0 samples               <- the desktop
                #
                # so the edge is the LAST row that is mostly bright, and the rows above it
                # are the window whatever their content is.
                span = [xx for xx in range(int(step.get("window_span_from", 470)),
                                           int(step.get("window_span_to", 1450)), 10)
                        if xx < image.width]
                if span:
                    def in_window(y: int) -> bool:
                        hits = 0
                        for xx in span:
                            r, g, b = image.getpixel((xx, y))
                            if (r + g + b) // 3 > threshold:
                                hits += 1
                        return hits > len(span) * 0.6

                    rows = [y for y in range(search_from, limit) if in_window(y)]
                    if rows:
                        bottom = rows[-1]
                        top = search_from
                        if bottom - search_from < 40:
                            log(f"  {step.name}: the bright window band is only "
                                f"{bottom - search_from + 1} rows; not treating it as "
                                f"the window")
                            bottom = None

        if bottom is None:
            log(f"  {step.name}: no window band brighter than {threshold} in "
                f"column {column} between y={search_from} and {search_to}; not clicking")
            return

        click_y = bottom - dy
        log(f"  {step.name}: window {top}..{bottom}; clicking ({x}, {click_y})")
        driver.click((x, click_y), step.name, settle=settle)
        return

    if step["sweep"]:
        first, last, stride = step["sweep"]
        x = int(step.get("at")[0]) if step.get("at") else 0
        for y in range(int(first), int(last) + 1, int(stride)):
            driver.click((x, y), f"{step.name} at y={y}", settle=settle)
            if step["expect_any"] and _expect_matches(driver, None, step["expect_any"]):
                log(f"  {step.name}: responded at y={y}")
                return
        log(f"  {step.name}: swept {first}..{last} with no response")
        return

    if step["at"] and step["type"] is not None:
        driver.type_into(step["at"], str(step["type"]),
                         clear=bool(step.get("clear", True)),
                         modifier=str(step.get("modifier", "ctrl")))
        time.sleep(settle)

    # A `type:` WITH NO `at:` TYPES INTO WHATEVER ALREADY HAS THE FOCUS.
    #
    # The greeter is the case: a password box is clicked once (the step before) and then the
    # text goes into it, and clicking it again to type would be a second press that some
    # greeters read as "submit an empty password". Without this branch `type:` alone did
    # NOTHING AT ALL - the only branch that typed was `at: AND type:`, so an ssh walk that
    # logged in with `at:`/`type:`/`key:` would click the box, type nothing, and press Return
    # on an empty password.
    #
    # THE THREE BRANCHES BELOW ARE ONE CHAIN, AND SEPARATING THEM WAS A REAL BUG. It used to be
    # `if at and type: ...` followed by a new `if at is None and type: ... elif at: ...`, so a
    # step with BOTH `at` and `type` typed into the field and then clicked it again - two
    # presses where the walkthrough asked for one. The unit test caught it as
    # 'the Continue clicked once, got [(1, 1), (2, 2), (3, 3)]' over a three-field form whose
    # Continue had not been clicked yet.
    elif step["at"] is None and step["type"] is not None:
        import keys as keysmod
        # THE SAME `type_gap` AS THE `at:`+`type:` BRANCH AND THE `run:` BRANCH. Three places
        # type, and a speed that only some of them honour is a trap: windows/11/steps/ssh.yaml
        # types eleven characters here (`powershell` into the Run dialog) and a hundred into
        # the prompt, and PSReadLine drops them below ~150 ms either way.
        unsupported = keysmod.type_text(
            driver.q, str(step["type"]),
            per_char=float(step.get("type_gap", 0.1)))
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(settle)

    elif step["at"]:
        driver.click(step["at"], step.name, settle=settle)
        repeat = step["repeat"]
        if repeat:
            count, gap = int(repeat[0]), float(repeat[1])
            for _ in range(max(0, count - 1)):
                driver.click(step["at"], f"{step.name} (again)", settle=gap)

    if step["key"]:
        keys = step["key"] if isinstance(step["key"], list) else [step["key"]]
        for one in keys:
            driver.key(str(one))
            time.sleep(0.3)
        time.sleep(settle)

    if step["run"]:
        # A SHELL, TYPED INTO THE GUEST'S OWN TERMINAL. This is how mint disables a screen
        # saver that ignores synthetic clicks: the settings live in the session's dconf and the
        # keep-alive is the session's own xdotool. It is data here because the command is a
        # measurement; it would be an escape hatch if it were logic.
        #
        # MULTI-LINE, AND THAT IS NOT COSMETIC. `keys.for_text` REFUSES a newline - measured:
        #
        #     for_text('apt-get install\nopenssh')
        #     ValueError: cannot type '\n'; split the command and send it as separate lines
        #
        # so a `run:` whose text contains one used to type everything up to the newline and
        # then silently drop the rest, because `type_text` REPORTS untypable characters rather
        # than raising. The ssh stage's install is multi-line by nature (nixos-rebuild after an
        # append), so the newline is honoured here: each line is typed, then Return, then the
        # next - which is exactly what a person does at a terminal.
        # THE TERMINAL IS OPENED BY ITS OWN STEP (`key: ctrl+alt+t` or a dock click),
        # because whether it opened is the first thing to check when an install produces
        # nothing. `terminal_wait: 0` therefore means "the terminal is already up".
        #
        # THIS USED TO PRESS ctrl+alt+t UNCONDITIONALLY, which is right for a desktop whose
        # shortcut works and wrong for one where it does not: Pop!_OS' COSMIC ignores it, and
        # pressing it again after the dock had opened the terminal just typed a control
        # chord into the focusing window.
        wait = float(step.get("terminal_wait", 8.0))
        if wait > 0:
            driver.key("ctrl+alt+t")
            time.sleep(wait)
        import keys
        body = str(step["run"])
        for index, line in enumerate(body.splitlines()):
            if not line.strip():
                continue
            # 40 ms is the measured floor for a SHELL COMMAND with shift-heavy text. At 20 ms
            # the guest lost shift releases - see lib/keys.type_text, which holds each chord
            # open for `hold` seconds precisely because of this - and the command arrived as
            # `export DEBIAN_FRONTEND+NONINTERACTIVE: APT_GET UPDATE`. 40 ms leaves room for
            # the chord AND the character, and a 130-character line is still five seconds.
            # `type_gap` IS A MEASURED NUMBER PER FAMILY, NOT A PREFERENCE, AND THE DEFAULT IS
            # THE ONE THAT WAS ALREADY HERE.
            #
            # It was 0.02 - the value every existing `run:` step has been timing against, in the
            # macOS setup, desktop and ssh walkthroughs as well as the Linux ones - and this
            # change found itself rewriting it to 0.04 on the way past, which silently doubles
            # the typing time of steps that were not being changed. A Windows guest needs 0.15
            # and says so with an explicit `type_gap`; nothing else asked for slower.
            unsupported = keys.type_text(
                driver.q, line, per_char=float(step.get("type_gap", 0.02)))
            if unsupported:
                log(f"  COULD NOT TYPE {unsupported!r} on line {index + 1}")
            driver.qmpmod.key(driver.q, "ret")
            # A SHORT PAUSE BETWEEN LINES, so the shell has started the previous command
            # before the next is typed into the same terminal.
            if index < len(body.splitlines()) - 1:
                time.sleep(float(step.get("line_gap", 1.0)))
        time.sleep(settle)


def _dump_frame(driver, label: str) -> None:
    """Save one screendump beside the driver's work directory, for a post-mortem.

    NOT THE VERDICT, AND NOT READ. A stage that fails while typing into a guest fails with a
    picture: an empty terminal, a sudo prompt, a greeter nobody answered. Three ssh runs
    ended `sshd did not answer` with nothing to look at before this existed.
    """
    import lastframe
    work = getattr(driver, "work", "") or "."
    stem = os.path.join(work, "steps", "failed-" + "".join(
        c if c.isalnum() else "-" for c in label)[:40])
    os.makedirs(os.path.dirname(stem), exist_ok=True)
    ppm = stem + ".ppm"
    try:
        driver.q.execute("screendump", filename=ppm)
    except Exception:                              # noqa: BLE001
        return
    try:
        lastframe.convert(ppm, stem + ".png")
        log(f"  frame for the post-mortem: {stem}.png")
    except Exception:                              # noqa: BLE001
        pass


def _run_python(driver: Driver, step: Step, walk_path: str) -> bool:
    """Run a step's `python: module:function`, finding the module BESIDE THE WALKTHROUGH.

    WHERE `setup_panes` LIVES IS NOT WHERE PYTHON LOOKS BY DEFAULT, and the first real run of
    this failed with

        ModuleNotFoundError: No module named 'setup_panes'
        ... after 'click (1142, 586) - open disk utility'

    The module sits next to the YAML - macos/15/steps/setup_panes.py beside
    macos/15/steps/setup.yaml - which is the right place: an extension belongs with the
    walkthrough that names it, not in lib/ with the general primitives. So the walkthrough's
    own directory goes on the path for the duration of the call, and is removed afterwards so
    two walkthroughs with a module of the same name cannot shadow each other.

    A RELATIVE NAME IS ALSO ACCEPTED. `python: ./mine:fn` and `python: mine:fn` both work, so a
    file does not have to know the layout above it.
    """
    import importlib

    module_name, _, function_name = str(step["python"]).partition(":")
    if not function_name:
        raise StepError(
            f"{step.name}: python needs 'module:function', got {step['python']!r}")

    here = os.path.dirname(os.path.abspath(walk_path)) if walk_path else ""
    added = False
    if here and here not in sys.path:
        sys.path.insert(0, here)
        added = True
    try:
        # A CACHED MODULE FROM AN EARLIER STEP IS THE SAME MODULE, which is what makes a
        # module-level cache inside an extension behave the way its author wrote it.
        module = importlib.import_module(module_name.lstrip("./"))
        handler = getattr(module, function_name, None)
        if handler is None:
            raise StepError(
                f"{step.name}: {module_name} has no {function_name}; "
                f"it defines {', '.join(n for n in dir(module) if not n.startswith('_'))[:200]}")
        return bool(handler(driver, step))
    except ModuleNotFoundError as exc:
        raise StepError(
            f"{step.name}: no module {module_name!r}. Looked in {here or '(nowhere)'} and on "
            f"the path; an extension sits beside the walkthrough that names it. ({exc})"
        ) from exc
    finally:
        if added and here in sys.path:
            sys.path.remove(here)


def walk_panes(driver: Driver, steps: Walk, values: dict[str, str], *,
               ssh_port: int = 2222, password: str | None = None,
               session=None) -> tuple[bool, list[str]]:
    """The macOS shape: look at the screen, run the one pane that matches, look again.

    THE END IS A WORD, NOT A RAN OUT OF PANES. macos/15/drive_read.py ends on `finder`, which is
    the desktop's menu bar and nothing else - a version that treated "the wizard stopped
    matching" as arrival left the guest one click from finished, which is why the end condition
    is written down rather than inferred.

    `stuck_after` is the other half of that: nothing matching twice in a row means either the
    desktop (already handled) or a pane no handler knows, and the two must not be confused.
    """
    answered: list[str] = []
    stuck_after = float(steps.document.get("stuck_after", 20))

    # A PANE FILE MUST SAY HOW IT KNOWS THE FIRST PANE HAS ARRIVED.
    #
    # Without this the loop looks at a machine that is still booting, matches nothing, and
    # reports STUCK - which is what the first CI run of the desktop stage did:
    #
    #     === STUCK: no pane matched, and this is not the end ===
    #       last screen:
    #
    # over a guest whose disk had just started. macos/15/drive_read.py has the same wait for
    # the same reason: `wait_for("country", timeout=boot_timeout, poll=10, label="boot")`
    # before its loop.
    #
    # `waits_for` is the words, and `boot_timeout` how long the disk may take to get there.
    gates = steps.document.get("waits_for")
    if gates:
        # `waits_for` IS A LIST OF ALTERNATIVES, NOT ONE PHRASE.
        #
        # A desktop stage does not always start at the wizard's first page. It can resume from
        # any pane - an earlier run may have answered the first six and stopped on Terms, which
        # is exactly what a persistent volume produces. Requiring the FIRST pane then waits
        # forever on a screen that is already several panes in:
        #
        #     [steps] waiting up to 1800s for country
        #     [steps] look boot: 206 words :: Terms | and | Conditions | ...
        #     ... every twelve seconds until the timeout
        #
        # So each entry may be a phrase or a list of phrases, and ANY of them means the wizard
        # is up. The walk then starts matching panes from wherever it actually is.
        alternatives: list[list[str]] = []
        for entry in (gates if isinstance(gates, list) else [gates]):
            alternatives.append(entry if isinstance(entry, list) else [str(entry)])

        boot_timeout = float(steps.document.get("boot_timeout", 1800))
        poll = float(steps.document.get("boot_poll", 10))
        shown = " or ".join(" ".join(a) for a in alternatives)
        log(f"waiting up to {boot_timeout:.0f}s for {shown}")
        deadline = time.time() + boot_timeout
        arrived = False
        # THE LOGIN WINDOW IS A VALID PLACE TO ARRIVE, and it is not readable as a phrase.
        #
        # A macOS login window's only text is the password field's placeholder, twelve pixels
        # tall, so a whole-frame read returns nothing at all:
        #
        #     [steps] look boot: 0 words ::
        #
        # and a walk whose gate lists fifteen pane titles waits out its whole budget on a
        # finished wizard. The gate therefore checks the walkthrough's own `ends_at` crops too -
        # the same reader the end condition uses, so the two cannot disagree about what "there
        # is nothing left to answer" looks like.
        end_crops = [s for s in (steps.document.get("ends_at") or []) if isinstance(s, dict)]
        while time.time() < deadline:
            driver.look("boot")
            for words in alternatives:
                if all(driver.says(w) for w in words):
                    log(f"=== A PANE IS UP: {' '.join(words)} ===")
                    arrived = True
                    break
            if arrived:
                break
            for spec in end_crops:
                words_here = _read_region(driver, spec.get("region"),
                                          int(spec.get("scale", 3)), "boot-end")
                wanted = spec.get("words") or []
                wanted = wanted if isinstance(wanted, list) else [wanted]
                have = {"".join(c for c in w[0].lower() if c.isalnum()) for w in words_here}
                if wanted and all("".join(c for c in w.lower() if c.isalnum()) in have
                                  for w in wanted):
                    log(f"=== ARRIVED AT {' '.join(wanted)} ({spec.get('name', 'a control')}) ===")
                    arrived = True
                    break
            if arrived:
                break
            time.sleep(poll)
        if not arrived:
            log("=== NO KNOWN PANE APPEARED ===")
            log(f"  last screen: {driver.text()[:200]}")
            return False, answered
    # A PANE THAT KEEPS MATCHING MUST NOT BE ANSWERED FOREVER. macOS 26's Update pane greys
    # Continue and spins, and a loop that ran its handler every pass would click it every ten
    # seconds for the length of the run. Each pane is answered at most `repeat_max` times, and a
    # pane that hits the limit is reported rather than clicked again.
    repeat_max = int(steps.document.get("pane_limit", 3))
    counts: dict[str, int] = {}

    idle_since: float | None = None
    while True:
        driver.look("pass")
        # THE WHOLE-FRAME READING IS KEPT, because a pane with a `region` overwrites
        # driver.words with its crop. Without this, a pane that follows a cropped one is
        # matched against the CROP instead of the screen - which is what the first local run
        # did: `country or region` (no region) was tested against the 300..340 strip that the
        # account pane had just read, saw the words 'select your country or region' from the
        # COUNTRY screen's card, and never matched its own.
        whole = list(driver.words)

        ended = False
        # THE END CONDITION IS READ IN ITS OWN REGION WHEN ONE IS DECLARED, AND FOR A MACOS
        # DESKTOP IT MUST BE.
        #
        # `finder` is 12-pixel text in the menu bar. Read whole, tesseract's page segmenter
        # decides a 1920x1080 desktop has no page at all and returns
        #
        #     Empty page!!
        #
        # which is run 42383829203: its last-frame.png IS the desktop and the stage waited out
        # its whole budget because the word never appeared in a whole-frame read.
        # lib/desktop.py solves it by cropping the menu bar and enlarging it three times; this
        # is the same answer, reachable from a walkthrough as `ends_region`.
        #
        # THE PANE MATCHING BELOW STILL USES THE WHOLE FRAME. A pane's title is large text and
        # reads fine whole; only the menu bar is too small for that. So the end is asked in its
        # region FIRST, on its own read, and the loop's own `look` follows.
        ends_region = steps.document.get("ends_region")
        if ends_region:
            words_here = _read_region(driver, ends_region,
                                      int(steps.document.get("ends_region_scale", 3)), "end")
            driver.words = whole
            for spec in steps.document.get("ends") or []:
                wanted = spec if isinstance(spec, list) else [spec]
                have = {"".join(c for c in w[0].lower() if c.isalnum()) for w in words_here}
                if all("".join(c for c in w.lower() if c.isalnum()) in have for w in wanted):
                    log(f"=== THE END: {' '.join(wanted)} ===")
                    ended = True
                    break
            if ended:
                return True, answered
        for spec in steps.document.get("ends") or []:
            words = spec if isinstance(spec, list) else [spec]
            if all(driver.says(w) for w in words):
                log(f"=== THE END: {' '.join(words)} ===")
                ended = True
                break
        if ended:
            return True, answered

        # A WALKTHROUGH MAY ALSO END ON A SMALLER CONTROL THAN A TITLE OR A MENU BAR.
        #
        # macOS 15's Setup Assistant does not finish on a desktop: it finishes on the LOGIN
        # WINDOW, and there the only readable text is the password field's placeholder - twelve
        # pixels tall, in a 165x40 box. lib/desktop.py crops PASSWORD_FIELD = (880, 960, 1045,
        # 1000) at 6x for exactly this, and reads 'enter password'.
        #
        # So an end condition may carry its own crop. Without it this walk answered fifteen
        # panes correctly and then reported STUCK on a screen that read
        #
        #     Sunday, November 8   7:18   docker   [Enter Password]
        #
        # which is a finished wizard, not a pane nobody knows.
        for spec in steps.document.get("ends_at") or []:
            if not isinstance(spec, dict):
                continue
            words_here = _read_region(driver, spec.get("region"),
                                      int(spec.get("scale", 3)), "end-at")
            driver.words = whole
            wanted = spec.get("words") or []
            wanted = wanted if isinstance(wanted, list) else [wanted]
            have = {"".join(c for c in w[0].lower() if c.isalnum()) for w in words_here}
            if wanted and all("".join(c for c in w.lower() if c.isalnum()) in have
                              for w in wanted):
                log(f"=== THE END: {' '.join(wanted)} ({spec.get('name', 'a control')}) ===")
                return True, answered

        ran_one = False
        for step in steps.panes:
            spec = steps.substitute(step.spec, values)
            here = Step(dict(spec), step.index)

            condition = here["when"]
            words = condition if isinstance(condition, list) else [condition]

            # A PANE MAY SAY WHERE ITS WORDS ARE. `region` ON A PANE IS NOT DECORATION: the
            # account pane's own body contains
            #
            #     Allow computer account password to be reset with your Apple Account.
            #     You must sign in with your Apple Account in the next step to use this feature.
            #
            # which satisfies every word of the Apple Account pane's title match. Measured on
            # the CI recording's own frames: `sign in to your apple account` was TRUE for the
            # ACCOUNT pane (f003) and for the Apple Account pane (f006) alike, so the walk
            # answered one as the other. Restricted to the title band, the two are clean apart:
            #
            #     account  y=300..340  -> 'create a mac account'
            #     apple    y=420..460  -> 'sign in to your apple account'
            if here["region"]:
                _read_region(driver, here["region"],
                             int(here.get("scale", DEFAULT_CROP_SCALE)), here.name)
                matched = all(driver.says(w) for w in words)
                # PUT THE WHOLE-FRAME READING BACK, so the next pane is matched against the
                # screen rather than this crop.
                driver.words = whole
            else:
                driver.words = whole
                matched = all(driver.says(w) for w in words)
            if not matched:
                continue

            # `once` IS FOR A PANE WHOSE ANSWER TAKES LONGER THAN A PASS.
            #
            # The account pane answers by going to "Creating account..." for a minute with
            # Continue greyed out. A loop that looks again before it finishes sees the same
            # pane and runs the `do` list a second time - typing the account name into fields
            # that already hold it. Measured on a live guest: three passes, and the form was
            # unusable afterwards. `pane_limit` alone is not enough, because the pane is
            # legitimately up for a while; `once` says "I have answered this, wait it out".
            limit = 1 if here["once"] else repeat_max
            if counts.get(here.name, 0) >= limit:
                # A PANE THAT HAS BEEN ANSWERED ITS LIMIT IS TREATED AS "NOTHING MATCHED".
                #
                # It must NOT set ran_one: that flag means "a pane was answered, go round
                # again", and setting it here is what made this loop spin forever - the
                # update pane matched, was refused, and the stuck timer was never consulted
                # because the loop always took the `if ran_one: continue` branch.
                log(f"  {here.name}: answered {counts.get(here.name, 0)} time(s); "
                    f"not clicking again")
                idle_since = idle_since or time.time()
                break

            log(f"=== {here.name}")
            if here["python"]:
                log(f"=== {here.name} (python {here['python']})")
                ok = _run_python(driver, here, steps.path)
                counts[here.name] = counts.get(here.name, 0) + 1
                answered.append(here.name)
                if not ok and not here["optional"]:
                    log(f"=== {here.name} FAILED (a python step said so)")
                    return False, answered
                ran_one = True
                break

            # THE PANE IS ANSWERED IN ONE PASS: either its `do` list, or the single action set
            # written directly on the step. See Step.actions for the account pane that made
            # this necessary.
            if here.actions:
                for position, action in enumerate(here.actions):
                    _do_action(driver, Step(dict(action, name=f"{here.name} #{position + 1}"),
                                            step.index), ssh_port=ssh_port)
            else:
                _do_action(driver, here, ssh_port=ssh_port, password=password, session=session)

            counts[here.name] = counts.get(here.name, 0) + 1
            answered.append(here.name)
            ran_one = True
            idle_since = None
            break

        if ran_one:
            continue

        # NOTHING MATCHED.
        if idle_since is None:
            idle_since = time.time()
            driver.look("stuck")
            log(f"  no pane matched; last screen: {driver.text()[:160]}")
            time.sleep(10)
            continue

        if time.time() - idle_since >= stuck_after:
            log("=== STUCK: no pane matched, and this is not the end ===")
            log(f"  last screen: {driver.text()[:200]}")
            return False, answered
        time.sleep(10)


def walk(driver: Driver, steps: Walk, *, overrides: dict[str, str] | None = None,
         password: str | None = None,
         start_at: str = "", stop_before: str = "",
         ssh_port: int = 2222, session=None) -> tuple[bool, list[str]]:
    """Run the walkthrough. Returns (ok, the names of the steps that ran).

    `start_at` / `stop_before` exist because the same file describes the installer walk AND
    the first-boot walk, and a stage sometimes has to run only the second half - fedora's
    install and its GNOME first-boot wizard are two halves of one walkthrough, and a resumed
    run starts from the disk it already has.
    """
    values = steps.values(overrides)
    if steps.panes:
        return walk_panes(driver, steps, values, ssh_port=ssh_port, password=password,
                          session=session)

    ran: list[str] = []
    started = not start_at

    for step in steps.steps:
        if not started:
            if step.name == start_at:
                started = True
            else:
                continue
        if stop_before and step.name == stop_before:
            log(f"stopping before {stop_before!r} as asked")
            break

        spec = steps.substitute(step.spec, values)
        here = Step({k: v for k, v in spec.items()}, step.index)

        # --- a measured exception, and it is allowed to be Python ----------------------
        if here["python"]:
            log(f"=== {here.name} (python {here['python']})")
            ok = _run_python(driver, here, steps.path)
            ran.append(here.name)
            if not ok and not here["optional"]:
                log(f"=== {here.name} FAILED (a python step said so)")
                return False, ran
            continue

        log(f"=== {here.name}")

        # --- a step that only runs when something is ABSENT -----------------------------
        #
        # `when` makes a pane, and is refused inside a sequence because a sequence is not
        # matched against the screen. `unless` is the other shape and belongs here: it says
        # "do this ONLY IF the screen does not already show X", which is how a retry is
        # written without a conditional-that-gates-a-click in Python. Measured need:
        # opensuse's installer retry must not press the desktop icon while YaST2 is already
        # open, because a second double-click there lands on the wizard.
        if here["unless"]:
            _read(driver, here, f"{here.name} - unless")
            absent = here["unless"]
            absent = absent if isinstance(absent, list) else [absent]
            if all(driver.says(w) for w in absent):
                log(f"  {here.name}: skipped, the screen already shows {' '.join(absent)}")
                ran.append(here.name)
                continue

        # --- waits ---------------------------------------------------------------------
        if here["until_dark"]:
            if not _run_until_dark(driver, here):
                if here["optional"]:
                    log(f"  {here.name}: never went dark, and the step is optional")
                    ran.append(here.name)
                    continue
                log(f"=== {here.name}: THE SCREEN NEVER WENT DARK ===")
                log(f"  last screen: {driver.text()[:200]}")
                return False, ran
            ran.append(here.name)
            continue

        if here["wait_for_change"]:
            if not _wait_for_change(driver, here):
                if here["optional"]:
                    log(f"  {here.name}: nothing changed, and the step is optional")
                    ran.append(here.name)
                    continue
                log(f"=== {here.name}: THE SCREEN DID NOT CHANGE ===")
                log(f"  last screen: {driver.text()[:200]}")
                return False, ran
            ran.append(here.name)
            continue

        if here["until"] or here["until_any"]:
            if not _run_until(driver, here):
                if here["optional"]:
                    log(f"  {here.name}: did not appear, and the step is optional")
                    ran.append(here.name)
                    continue
                log(f"=== {here.name}: NEVER APPEARED ===")
                log(f"  last screen: {driver.text()[:200]}")
                return False, ran
            ran.append(here.name)
            continue

        if here["disk_settle"]:
            if not _run_until_disk_settles(driver, here):
                if here["optional"]:
                    log(f"  {here.name}: the disk never settled, and the step is optional")
                    ran.append(here.name)
                    continue
                return False, ran
            ran.append(here.name)
            continue

        if here["wait"]:
            seconds = float(here["wait"])
            log(f"  waiting {seconds:.0f}s, as measured")
            deadline = time.time() + seconds
            while time.time() < deadline:
                if here["keep_alive"]:
                    try:
                        driver.qmpmod.click(driver.q, 400 + (int(time.time()) % 40), 300)
                    except Exception:                  # noqa: BLE001
                        pass
                driver.look(here.name)
                time.sleep(min(20.0, max(1.0, deadline - time.time())))
            ran.append(here.name)
            continue

        # --- the action ------------------------------------------------------------------
        _do_action(driver, here, ssh_port=ssh_port, password=password, session=session)

        # --- the verdict, and it only ever produces one ---------------------------------
        #
        # THE SCREEN IS READ HERE AND NOWHERE ELSE IN THIS LOOP. A step that did not produce
        # the words it was measured to produce is a failure with the screen in the log, which
        # is what every driver here learned to do after skipping a pane silently.
        if here["expect"] or here["expect_any"]:
            _read(driver, here, f"{here.name} - after")
            matched = _expect_matches(driver, here["expect"], here["expect_any"])
            if matched:
                ran.append(here.name)
                log(f"  {here.name}: advanced")
                continue

            wanted = here["expect"] or here["expect_any"]
            if here["optional"]:
                ran.append(here.name)
                log(f"  {here.name}: expected {wanted}, did not see it, step is optional")
                continue
            log(f"=== {here.name}: DID NOT ADVANCE (expected {wanted}) ===")
            log(f"  screen: {driver.text()[:200]}")
            # THE STEP RAN AND DID NOT ADVANCE, SO IT IS NOT IN `ran`.
            #
            # `ran` is what the caller reports as answered, and a step that clicked and then
            # failed its own check is the one thing a completion count must not include - a
            # run that says "13 of 14 answered" is a run whose transcript disagrees with its
            # verdict.
            return False, ran
        else:
            ran.append(here.name)

    return True, ran


def driver_for(qmp_path: str, *, work: str = "", width: int = 1920, height: int = 1080,
               disk_path: str | None = None, ssh_port: int = 2222,
               q=None) -> "Driver":
    """A Driver another stage can hand to `walk_with`.

    WHY A STAGE PASSES ITS OWN DRIVER INSTEAD OF LETTING run_file OPEN ONE. qemu accepts ONE
    client on a QMP socket, so a stage that already holds a connection - windows/11/ssh.py does,
    through its Keyboard - cannot have a walk open a second one: it would block on the greeting
    and look like a hung guest. That is the same single-client rule the macos ssh extensions
    had to learn (`Recovery(q=...)`, `dismiss_keyboard_assistant(q=...)`).

    `q` IS THE CONNECTION THE STAGE ALREADY HOLDS, and passing it is the normal case for a
    stage that had to type before ssh existed. windows/11/ssh.py opens a Keyboard to wake the
    lock screen, and that Keyboard IS the socket - handing it in means the walk and the stage
    share one client, which is the only thing qemu permits.

    The caller owns the result and closes it; `walk_with` deliberately does not.
    """
    driver = Driver(qmp_path, work or ".", width=width, height=height, disk_path=disk_path,
                    q=q)
    return driver


def walk_with(steps: Walk, driver: "Driver", *, values: dict[str, str] | None = None,
              ssh_port: int = 2222, user: str = "docker",
              password: str = "admin", frames: str = "",
              start_at: str = "", stop_before: str = "") -> tuple[bool, list[str]]:
    """Walk `steps` against `driver` - the half of run_file that does not open a connection.

    The same split macOS needed: the stage keeps everything that starts a machine and
    publishes a disk, and the walk is the part in YAML. `run_file` is this plus a Driver of
    its own.
    """
    setattr(driver, "release", str(steps.document.get("release", "15")))
    setattr(driver, "frames_dir", frames or "")
    overrides = {"args.user": user, "args.password": password}
    if values:
        overrides.update(values)
    setattr(driver, "values", steps.values(overrides))
    # A `python:` EXTENSION NEEDS THE PORT AND THE PASSWORD, and they are not in `values`
    # because they are not walkthrough data - they are how this run was started. Attaching
    # them is the same argument as `release` and `frames_dir` above: an extension should read
    # the facts of its own run off the driver rather than re-deriving them from the
    # environment, which is how two answers to one question start.
    setattr(driver, "ssh_port", ssh_port)
    setattr(driver, "password", password)
    return walk(driver, steps, overrides=overrides, start_at=start_at,
                stop_before=stop_before, ssh_port=ssh_port, password=password)


def run_file(walkthrough: str, qmp_path: str, *, work: str = "",
             user: str = "docker", password: str = "admin",
             disk_path: str | None = None, ssh_port: int = 2222,
             start_at: str = "", stop_before: str = "",
             release: str = "15", frames: str = "",
             extra_vars: dict[str, str] | None = None,
             log_prefix: str = "", session=None,
             boot_options: dict | None = None) -> tuple[bool, list[str]]:
    """Walk a YAML file against a running guest, from Python rather than from the CLI.

    THIS IS HOW A STAGE JOINS ITS YAML WITHOUT GIVING UP WHAT IT ALREADY DOES. macos/15/bake.py
    starts the machine, records it, keeps the artifacts current and publishes a disk; the part
    that belongs in YAML is the walk in the middle. So the stage calls this with a `--steps`
    path and leaves everything else alone - the alternative would be a second entry point that
    duplicates the recording and the publishing, which is the "one fact in two places" mistake.

    `log_prefix` is the stage's own tag ([macos-install], [desktop]) because a reader looking at
    a CI log needs to know which stage a line came from, and this module's [steps] is common to
    all of them.
    """
    steps = load(walkthrough)

    here = work or os.path.dirname(os.path.abspath(walkthrough))
    os.makedirs(here, exist_ok=True)

    # A FILE THAT DECLARES `boot:` OWNS ITS MACHINE, AND THAT CHANGES THREE THINGS.
    #
    # No QMP socket is required from the caller - the file makes one. The driver cannot be built
    # before the first step runs, because the socket it needs does not exist yet. And this
    # function has to stop the guest if the file did not, because a failed walkthrough that
    # leaves a qemu running holds /dev/kvm for every later job in the container.
    #
    # `session` MAY BE PASSED IN when the caller already has a machine and only wants the file to
    # drive it - that is the old contract, unchanged.
    owns_machine = any(s["boot"] for s in steps.steps) and session is None
    if session is None and owns_machine:
        first = next(s for s in steps.steps if s["boot"])
        family = str(first.get("family") or "macos")
        opts = dict(boot_options or {})
        session = MachineSession(
            family, disk_path or "", here,
            share=opts.get("share"), mem_mb=int(opts.get("mem_mb") or 0),
            cpus=int(opts.get("cpus") or 4))

    needs_screen = bool(steps.panes) or any(
        s["at"] or s["double"] or s["expect"] or s["expect_any"] or s["until"]
        or s["until_any"] or s["disk_settle"] or s["until_dark"] or s["wait_for_change"]
        or s["sweep"] or s["when"] or s["unless"]
        # `run:` AND `key` NEED A SCREEN TOO, AND FORGETTING THEM WAS A CRASH.
        #
        # `run:` presses ctrl+alt+t to open the guest's own terminal and types into it; `key`
        # sends a keystroke. Both need the QMP connection that a Driver has and a
        # HeadlessDriver does not. The ssh stage's walk is nothing but `run:`, `require_ssh:`
        # and `ssh:`, so it was classified headless and died on the first step with
        #
        #     AttributeError: 'HeadlessDriver' object has no attribute 'key'
        #
        # - measured on run 42555862172, which is the run that found this.
        #
        # AND `python:` NEEDS ONE, WHICH IS THE SAME MISTAKE A THIRD TIME. An extension like
        # ssh_panes:enable_remote_login drives the GUI through the driver's own QMP connection;
        # classified headless it gets a driver with no socket, and the function waits out its
        # 120-second "no QMP socket at " (with an empty path) before giving up. A walkthrough
        # that delegates to an extension cannot be known to be headless.
        # AND A `python:` STEP MAY SAY IT DOES NOT. The conservative default is right - most
        # extensions drive the GUI - but macos/15/steps/dev.yaml's two extensions fetch and copy
        # a file over ssh and never look at the screen. Declaring `python_screen: false` is how
        # a pure-ssh walkthrough stops being refused a HeadlessDriver it does not need:
        #
        #     StepError: macos/15/steps/dev.yaml has GUI steps and no --qmp was given
        #
        # which run 42632192846 raised before its first step.
        or (s["python"] and s.get("python_screen") is not False) or s["run"] or s["key"]
        # `reboot:` NEEDS THE SCREEN BECAUSE IT REBUILDS IT; `boot:` AND `shutdown:` DO NOT -
        # they act on the machine, and a file of nothing but boot/shutdown/ssh is headless.
        or s["reboot"]
        for s in steps.steps)

    if needs_screen and owns_machine:
        # THE SOCKET DOES NOT EXIST YET - `boot:` MAKES IT. So the driver is created without
        # connecting and attaches when the first boot step has started the machine. A Driver
        # built the normal way here would wait out its socket_timeout for a file nothing has
        # written, which is exactly the 180 seconds this change removes.
        log("driver: deferred until the first `boot:` step has a socket")
        driver = DeferredDriver(here, width=steps.resolution[0],
                                height=steps.resolution[1], disk_path=disk_path)
    elif needs_screen:
        if not qmp_path:
            raise StepError(f"{walkthrough} has GUI steps and no --qmp was given")
        log(f"driver: qmp {qmp_path!r}")
        driver = Driver(qmp_path, here, width=steps.resolution[0],
                        height=steps.resolution[1], disk_path=disk_path)
    else:
        driver = HeadlessDriver(here, disk_path=disk_path)

    # A YAML STEP MAY NEED TO KNOW WHICH RELEASE IT IS DRIVING - the Apple Account pane is a
    # menu on 26 and a text link on 15 - and a `python:` extension reads it from the driver
    # rather than from a global.
    setattr(driver, "release", str(release))
    # THE STAGE'S FRAME DIRECTORY, for a python: step that asks the stage's own snapshotter a
    # question. NEW: None when the caller did not say, and such a step falls back to
    # <work>/frames - which is right for a standalone run and wrong for a CI stage, because
    # bake.py keeps its frames somewhere else and install.py reads them to decide whether the
    # wizard has appeared. See macos/15/steps/setup_panes.py._recovery().
    setattr(driver, "frames_dir", frames or "")
    # THE WALKTHROUGH'S OWN VALUES, FOR THE SAME REASON. A `python:` step that types into a
    # form needs the account the walk was given, and passing it through the step's own keys
    # would mean inventing a vocabulary for every field. The resolved vars are attached
    # instead, so an extension reads `driver.values["user"]` and the YAML stays data.
    setattr(driver, "values", steps.values(
        {"args.user": user, "args.password": password}))
    setattr(driver, "ssh_port", ssh_port)
    setattr(driver, "password", password)

    try:
        # `extra_vars` IS FOR A VAR THE CALLER COMPUTES, NOT FOR A FLAG. The ssh walkthrough
        # needs the base64 install script, which is per-family and cannot be written in the
        # file; everything else (`user`, `password`) already comes from the two named args.
        overrides: dict[str, str] = {"args.user": user, "args.password": password}
        if extra_vars:
            overrides.update(extra_vars)
        return walk(driver, steps,
                    overrides=overrides,
                    start_at=start_at, stop_before=stop_before, ssh_port=ssh_port,
                    password=password, session=session)
    finally:
        driver.close()
        # A MACHINE THIS FILE STARTED IS ITS RESPONSIBILITY, NOT THE CALLER'S. A failed
        # walkthrough that left qemu running would hold /dev/kvm and the forwarded ports for
        # every later job in the same container - and the caller was never told it existed.
        if owns_machine and session is not None:
            session.stop_if_running(log=log)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("walkthrough", help="the YAML file")
    p.add_argument("--qmp", default="",
                   help="the guest's QMP socket; omit for a walkthrough with no GUI steps")
    p.add_argument("--work", default=None)
    p.add_argument("--user", default="docker")
    p.add_argument("--password", default="admin")
    p.add_argument("--disk", default=None,
                   help="the target qcow2; a setup pane watches it for growth")
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--start-at", default="")
    p.add_argument("--stop-before", default="")
    # A FILE THAT DECLARES `boot:` NEEDS THE DISK AND NOT A SOCKET, so these two are the mirror
    # image of --qmp: one of them is required by whichever shape the file is.
    p.add_argument("--boot-disk", default="",
                   help="the disk a `boot:` step should start; required by such a file")
    p.add_argument("--boot-share", default=None,
                   help="host directory to serve on the bridge; omit for ~/shared")
    p.add_argument("--boot-mem", type=int, default=0)
    p.add_argument("--boot-cpus", type=int, default=4)
    p.add_argument("--list", action="store_true", help="print the steps and exit")
    p.add_argument("--check", action="store_true",
                   help="validate the file and print what it would do, without a guest")
    args = p.parse_args(argv)

    try:
        steps = load(args.walkthrough)
    except StepError as exc:
        log(f"FAILED: {exc}")
        return 1

    log(f"walkthrough {steps.name}: {len(steps.steps)} step(s), {steps.path}")
    if steps.notes:
        for line in str(steps.notes).strip().splitlines():
            log(f"  | {line}")

    if args.list or args.check:
        values = steps.values({"args.user": args.user, "args.password": args.password})
        for step in steps.steps:
            spec = steps.substitute(step.spec, values)
            keys = ", ".join(sorted(k for k in spec if k != "name"))
            log(f"  {step.index:2} {step.name:44} {keys}")
        return 0

    work = args.work or os.path.dirname(os.path.abspath(args.walkthrough))
    os.makedirs(work, exist_ok=True)
    # A FILE WITH NO PANES AND NO `at` NEEDS NO SCREEN, and starting one would make the ssh
    # half of a walkthrough untestable anywhere but a running guest. See HeadlessDriver.
    needs_screen = bool(steps.panes) or any(
        s["at"] or s["double"] or s["expect"] or s["expect_any"] or s["until"]
        or s["until_any"] or s["disk_settle"] or s["when"] or s["unless"]
        or (s["python"] and s.get("python_screen") is not False)
        or s["run"] or s["key"] for s in steps.steps)
    boots = any(s["boot"] for s in steps.steps)
    if needs_screen and not args.qmp and not boots:
        log("FAILED: this walkthrough has GUI steps; --qmp is required")
        return 1
    if boots and not args.boot_disk:
        log("FAILED: this walkthrough boots a machine; --boot-disk is required")
        return 1

    if needs_screen:
        driver = Driver(args.qmp, work,
                        width=steps.resolution[0], height=steps.resolution[1],
                        disk_path=args.disk)
    else:
        driver = HeadlessDriver(work, disk_path=args.disk)
    session = None
    if boots:
        first = next(s for s in steps.steps if s["boot"])
        session = MachineSession(
            str(first.get("family") or "macos"), args.boot_disk, work,
            share=args.boot_share, mem_mb=args.boot_mem, cpus=args.boot_cpus)
    try:
        ok, ran = walk(driver, steps,
                       overrides={"args.user": args.user, "args.password": args.password},
                       start_at=args.start_at, stop_before=args.stop_before,
                       ssh_port=args.ssh_port, session=session)
    except StepError as exc:
        # A STEP THAT FAILED IS A VERDICT, NOT A TRACEBACK.
        #
        # Every driver in this repository reports a failed step with the screen in the log and a
        # non-zero exit; a Python traceback says the interpreter broke, which is a different
        # thing and sends the reader to the wrong place. The step's own message is already the
        # useful part.
        log(f"=== {exc} ===")
        ok, ran = False, []
    finally:
        driver.close()

    if ok:
        log(f"=== WALKTHROUGH COMPLETE: {len(ran)} step(s) ran ===")
        return 0
    log(f"=== WALKTHROUGH FAILED after {len(ran)} step(s) ===")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
