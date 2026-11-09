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
    "terminal_wait",  # seconds to wait for the terminal after `run` opens it
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
    "press",       # for a python step: which footer control it should press by name
}

# THE KEYS AN ACTION INSIDE `do` MAY CARRY. A pane's actions are the same vocabulary as a
# sequence step minus the pane machinery: they cannot wait for the screen, because the pane
# already decided that, and they cannot be a pane themselves.
ACTION_ONLY_KEYS = {"at", "double", "type", "clear", "key", "run", "ssh", "keep_alive",
                    "settle", "terminal_wait", "timeout", "modifier"}

# Steps that wait for something to happen rather than doing something. `expect` does not apply
# to these: they already have their own condition, and a second one would be a second opinion
# about the same screen.
WAIT_STEPS = {"until", "until_any", "wait", "wait_for_change", "until_dark", "disk_settle"}

# Everything above that actually drives the guest. A step with none of these is a check, and
# a check that does nothing is reported rather than run.
ACTION_KEYS = {"at", "double", "type", "key", "run", "ssh", "require_ssh", "sweep"}


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
        """
        resolved = {}
        for key, spec in self.vars.items():
            if isinstance(spec, dict):
                source = spec.get("from") or ""
                if overrides and source in overrides:
                    resolved[key] = overrides[source]
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
                 disk_path: str | None = None) -> None:
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

        self.screen = ocr.Screen(qmp_path, shots, width=width, height=height)
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


def _do_action(driver: Driver, step: Step, *, ssh_port: int = 2222) -> None:
    """Everything a step does, in the order that makes one step one action.

    Separated from walk() because the pane loop and the sequence loop must do exactly the same
    thing to a step; two copies would be the "one fact in two places" mistake this repository
    keeps paying for.
    """
    settle = float(step.get("settle", 2.0))

    # --- require_ssh: WAIT FOR THE GUEST'S OWN SSHD BEFORE USING IT -------------------
    #
    # THE STAGE THAT TURNS REMOTE LOGIN ON IS A GUI STAGE; EVERYTHING AFTER IT IS NOT. So a
    # walkthrough that has an ssh section has to wait for the daemon the previous step enabled,
    # and the wait belongs where the dependency is rather than as a `wait: 120` at the top of
    # the file - that number is the exact weakness macos/15/ssh.py's docstring records.
    if step["require_ssh"]:
        import session
        seconds = int(step.get("timeout", 600))
        log(f"  waiting up to {seconds}s for sshd on port {ssh_port}")
        if not session.wait_for_ssh(ssh_port, seconds):
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
        status, output = session.ssh(ssh_port, command, timeout=timeout)
        log(f"  ssh: {command} -> {status}")
        for line in (output or "").splitlines()[:40]:
            log(f"    {line}")
        # AN EXIT STATUS IS THE VERDICT. There is no `expect` on an ssh step: the shell already
        # said whether it worked, and a word on a screen is a weaker answer than a return code.
        if status != 0 and not step["optional"]:
            raise StepError(f"{step.name}: the command exited {status}")
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
        # A SHELL LINE, TYPED INTO THE GUEST'S OWN TERMINAL. This is how mint disables a screen
        # saver that ignores synthetic clicks: the settings live in the session's dconf and the
        # keep-alive is the session's own xdotool. It is data here because the command is a
        # measurement; it would be an escape hatch if it were logic.
        driver.key("ctrl+alt+t")
        time.sleep(float(step.get("terminal_wait", 8.0)))
        import keys
        keys.type_text(driver.q, str(step["run"]), per_char=0.02)
        driver.qmpmod.key(driver.q, "ret")
        time.sleep(settle)


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
               ssh_port: int = 2222) -> tuple[bool, list[str]]:
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
        while time.time() < deadline:
            driver.look("boot")
            for words in alternatives:
                if all(driver.says(w) for w in words):
                    log(f"=== A PANE IS UP: {' '.join(words)} ===")
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
                _do_action(driver, here, ssh_port=ssh_port)

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
         start_at: str = "", stop_before: str = "",
         ssh_port: int = 2222) -> tuple[bool, list[str]]:
    """Run the walkthrough. Returns (ok, the names of the steps that ran).

    `start_at` / `stop_before` exist because the same file describes the installer walk AND
    the first-boot walk, and a stage sometimes has to run only the second half - fedora's
    install and its GNOME first-boot wizard are two halves of one walkthrough, and a resumed
    run starts from the disk it already has.
    """
    values = steps.values(overrides)
    if steps.panes:
        return walk_panes(driver, steps, values, ssh_port=ssh_port)

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
        _do_action(driver, here, ssh_port=ssh_port)

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


def run_file(walkthrough: str, qmp_path: str, *, work: str = "",
             user: str = "docker", password: str = "admin",
             disk_path: str | None = None, ssh_port: int = 2222,
             start_at: str = "", stop_before: str = "",
             release: str = "15", frames: str = "",
             extra_vars: dict[str, str] | None = None,
             log_prefix: str = "") -> tuple[bool, list[str]]:
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

    needs_screen = bool(steps.panes) or any(
        s["at"] or s["double"] or s["expect"] or s["expect_any"] or s["until"]
        or s["until_any"] or s["disk_settle"] or s["until_dark"] or s["wait_for_change"] or s["sweep"] or s["when"]
        or s["unless"]
        for s in steps.steps)

    if needs_screen:
        if not qmp_path:
            raise StepError(f"{walkthrough} has GUI steps and no --qmp was given")
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

    try:
        # `extra_vars` IS FOR A VAR THE CALLER COMPUTES, NOT FOR A FLAG. The ssh walkthrough
        # needs the base64 install script, which is per-family and cannot be written in the
        # file; everything else (`user`, `password`) already comes from the two named args.
        overrides: dict[str, str] = {"args.user": user, "args.password": password}
        if extra_vars:
            overrides.update(extra_vars)
        return walk(driver, steps,
                    overrides=overrides,
                    start_at=start_at, stop_before=stop_before, ssh_port=ssh_port)
    finally:
        driver.close()


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
        or s["until_any"] or s["disk_settle"] or s["when"] or s["unless"] for s in steps.steps)
    if needs_screen and not args.qmp:
        log("FAILED: this walkthrough has GUI steps; --qmp is required")
        return 1

    if needs_screen:
        driver = Driver(args.qmp, work,
                        width=steps.resolution[0], height=steps.resolution[1],
                        disk_path=args.disk)
    else:
        driver = HeadlessDriver(work, disk_path=args.disk)
    try:
        ok, ran = walk(driver, steps,
                       overrides={"args.user": args.user, "args.password": args.password},
                       start_at=args.start_at, stop_before=args.stop_before,
                       ssh_port=args.ssh_port)
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
