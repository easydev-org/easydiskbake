"""Bake Redox OS into a disk that answers ssh, by driving its own console.

    python3 redox/bake.py --out out/redox.qcow2

WHAT THIS SYSTEM IS, AND WHY NONE OF THE OTHER BAKES APPLY

Redox has its own kernel, its own filesystem (RedoxFS), its own shell (ion) and its own
init. There is no systemd, no `/proc/cmdline`, no apt, no answer file the installer reads.
The published `harddrive.img` is not an installer - it is a complete, bootable Redox with
Orbital and a package manager already on it.

So the shape is different from every other system here:

    ubuntu, kubuntu   boot an ISO, subiquity reads a YAML document, no screen is read
    nixos             boot an ISO, type the install at a serial shell
    freebsd           boot an ISO, bsdinstall reads a script, no screen is read
    windows, macos    boot an installer, drive it with clicks
    redox             BOOT THE SYSTEM ITSELF and change it in place from its console

THE WORK IS NOT INSTALLATION. It is making the image a gold disk: an sshd, an account this
repository can log in as, and that sshd started at boot.

WHY ROOT AND NOT `docker` WITH sudo

`user` is in the sudo group, and sudo prompts - for an empty password, which still means
pressing Return at a prompt the driver would have to notice. Root at the console needs no
sudo at all: every command below is typed once, straight into a root shell. The `docker`
account is still created, because that is the account ssh uses; it is never used for the
console session.

WHY THE CONSOLE IS THE ONLY CHANNEL

Redox's desktop image has no serial console - there is no ttyS0 in it, and the kernel
writes to the VGA text console. So there is no `lib/serial.py` here, and no ssh either:
that is the thing being installed. What is left is the keyboard through QMP and the screen
read back as a screenshot.

F2 IS HOW THE TEXT CONSOLE IS REACHED. The image boots to Orbital, a graphical login; the
console is the second virtual terminal. The documentation gives F2 for the text terminal
and F3 to return to Orbital, which is what `console()` below does.

THE SCREEN IS READ, AND THAT IS DELIBERATE

Every other bake here that reads a screen does it to find something it cannot otherwise
know, or stops rather than guess. This one reads it because it is the only channel, and the
alternative is typing blind into a console whose prompt sits wherever the boot log left it.

Each step is followed by a marker the console echoes, and a step whose marker never appears
stops the bake. It does not retry: a retry into a console that was not ready types the
command twice, and the second copy is what a later stage would have to untangle.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import keys as keylib  # noqa: E402
import qmp as qmpmod  # noqa: E402
import vm  # noqa: E402

# THE PINNED IMAGE, verified against the vendor's own SHA256SUM.
IMAGE_URL = ("https://static.redox-os.org/img/x86_64/"
             "redox_desktop_x86_64_2026-11-03_575_harddrive.img.zst")
IMAGE_SHA256 = "ff3affbba2a2737e1ac015f599cf8dc3f6df220f5620ccacf66ab17c56de491b"

# REDOX'S OWN CPU STRING. Its documentation gives this for QEMU because the kernel does not
# handle those six features, and the symptom of getting it wrong is a boot that stops with
# nothing on the screen. The probe confirmed this one works.
CPU = "Broadwell,-pcid,-x2apic,-tsc-deadline,-hle,-rtm,-invpcid"

# REDOX'S OWN ACCOUNTS, from config/base.toml in the vendor's tree:
#
#     [users.root]  password = "password"
#     [users.user]  password = ""
#
# Root is used for the console session; the `docker` account below is what ssh logs in as,
# because that is this repository's account everywhere else.
ROOT_PASSWORD = "password"

# THE TERMINAL ICON IN ORBITAL'S DOCK, MEASURED FROM A LOGGED-IN SESSION.
#
# The first value here, (1895, 1055), was measured on an ORBITAL LOGIN SCREEN, where the bar
# holds two letter placeholders and nothing else - and a login screen has no dock at all,
# because the launcher only runs inside a session. Clicking there was clicking at nothing.
#
# In a real session the dock runs along the bottom, and measured from run 41811336784's own
# frame - which is 1920x1080 - the six icons are centred at roughly
#
#     R     37     the application menu
#     [~]   91     netsurf
#     $_   115     COSMIC Terminal        <- this one
#     []   160     COSMIC Files
#     [=]  207     COSMIC Text Editor
#     [^]  255     the image viewer
#
# at y=1051. The previous value, (177, 1042), is past the terminal and lands on the file
# manager's left edge - which is why clicking it opened nothing and the frame still showed a
# bare wallpaper, with the check then reporting that the dock had not launched anything.
DOCK_TERMINAL = (115, 1051)

# THE DOCK STRIP, for dock_dark_fraction(). See that function for the measurement that
# decided these: a dock that reads 0.60 dark is a placeholder and does not take clicks.
DOCK_TOP = 0.935      # y / height: the bottom 7% of the frame
DOCK_WIDTH = 0.21     # x / width: the strip the launcher icons occupy

# HOW LIGHT THE DOCK HAS TO BE. 0.000 is a painted strip, 0.603 is a placeholder; the
# threshold sits between them with room for a theme that draws darker icons.
DOCK_READY = 0.25

# WHERE THIS BAKE LOOKS, and why not the whole frame. See ocr() for the measurement.
REGIONS = [
    ((830, 440, 1100, 650), 4, "the login box"),
    ((0, 60, 1920, 1000), 2, "the window area"),
    ((0, 1030, 1920, 1080), 4, "the bottom bar"),
]

# THE ACCOUNT THIS REPOSITORY USES EVERYWHERE ELSE.
SSH_USER = "docker"
SSH_PASSWORD = "admin"


def log(message: str) -> None:
    print(f"[redox-bake {time.strftime('%H:%M:%S')}] {message}", flush=True)


# ORBITAL IS A PICTURE, AND OCR CANNOT READ IT.
#
# The desktop's only text is the dock's labels, drawn a few pixels tall; tesseract returns
# nonsense for the whole frame - run 41807177256's log shows
#
#     waiting for the desktop: '> <r : : fr "~ fad { ls aes ae 4 : . " rs rears et v ; .'
#
# which is the wallpaper being read as letters. What separates Orbital from the login box is
# not what it says but what it IS: a wallpaper and a panel, against two dark rectangles on a
# flat background. lib/redox/launch.py measures that as mean brightness and chroma, and these
# are the same numbers - the probe recorded Orbital at 91.7/93.5 and the bootloader menu at
# 9.9/0.0.
ORBITAL_MEAN = 20.0
ORBITAL_CHROMA = 6.0


def _as_ppm(path: str) -> bytes | None:
    """The file's pixels as P6 bytes, converting anything else through ffmpeg.

    BOTH READERS BELOW PARSED A PPM AND NOTHING ELSE, and Console.shot() returns a PNG -
    which it always did, and which became the only thing it returns when the second
    screendump was removed. So run 41868427701 measured

        mean 0.0 chroma 0.0

    for forty seconds against a screen that had drawn Orbital's wallpaper, because
    `open("...png")` read the PNG header, found no "P6", and answered (0.0, 0.0). The
    login was correct and the measurement could not see it.

    Converted here rather than at each call site: there are two readers and three callers,
    and a caller that had to remember would be a fourth place to get it wrong.
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if data.startswith(b"P6"):
        return data

    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".ppm", delete=False) as tmp:
        ppm = tmp.name
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", path, "-pix_fmt", "rgb24", "-f", "image2", "-vcodec", "ppm", ppm],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        with open(ppm, "rb") as fh:
            return fh.read()
    except OSError:
        return None
    finally:
        try:
            os.unlink(ppm)
        except OSError:
            pass


def _parse_ppm(data: bytes | None) -> tuple[int, int, bytes] | None:
    """(width, height, pixels) from P6 bytes."""
    if not data:
        return None
    import io

    fh = io.BytesIO(data)
    try:
        if fh.readline().strip() != b"P6":
            return None
        line = fh.readline()
        while line.startswith(b"#"):
            line = fh.readline()
        width, height = (int(v) for v in line.split())
        fh.readline()
        pixels = fh.read(width * height * 3)
    except (ValueError, OSError):
        return None
    if not pixels:
        return None
    return width, height, pixels


def frame_stats(path: str) -> tuple[float, float]:
    """Mean grey and mean channel spread of a frame, in any format ffmpeg reads."""
    parsed = _parse_ppm(_as_ppm(path))
    if parsed is None:
        return (0.0, 0.0)
    _, _, pixels = parsed
    total = spread = count = 0
    for i in range(0, len(pixels) - 2, 97 * 3):
        r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
        total += r + g + b
        spread += max(r, g, b) - min(r, g, b)
        count += 1
    if not count:
        return (0.0, 0.0)
    return (total / (count * 3), spread / count)


def dark_window_fraction(ppm: str) -> float:
    """Fraction of the frame that is a dark window, 0..1.

    THE TERMINAL'S PROMPT IS TOO SMALL TO READ, so opening it is measured instead. COSMIC
    Terminal opens as a large dark rectangle over a blue wallpaper, and the difference between
    "the desktop" and "the desktop with a terminal on it" is tens of percent of the frame going
    from light blue to near-black. OCR cannot see that; a histogram can.

    Run 41811336784 waited 90 s for `$`, `#` or `user` and got

        '> <r : : fr "~ fad { ls aes ae 4 : . " rs rears et v ; .'

    - the wallpaper, again - while the terminal was open the whole time.
    """
    parsed = _parse_ppm(_as_ppm(ppm))
    if parsed is None:
        return 0.0
    width, height, pixels = parsed
    dark = total = 0
    for i in range(0, len(pixels), 61 * 3):
        r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
        if r + g + b < 120:
            dark += 1
        total += 1
    return dark / total if total else 0.0


def dock_dark_fraction(path: str) -> float:
    """How much of the DOCK STRIP is dark, 0..1.

    THE DOCK IS NOT PART OF THE DESKTOP TEST, and this is the difference between a dock that
    can be clicked and one that cannot. Measured on two redox bakes whose every other number
    matched:

        41874326761   terminal opened on the first click    dock dark 0.000
        41882033727   six clicks, nothing                   dock dark 0.603

    Both had passed "the desktop is up" - the wallpaper is bright and colourful, and the
    brightness test fires on it long before the panel finishes. In the second the dock was
    still a dark placeholder: the icons were drawn, but the strip had not been painted, and
    nothing answered the pointer.

    The dock occupies the bottom seventy pixels. Read from y=1010 rather than 1080 because
    the icons' own dark backgrounds sit in the top half of it, and the test wants the strip
    around them.
    """
    parsed = _parse_ppm(_as_ppm(path))
    if parsed is None:
        return 1.0
    width, height, pixels = parsed
    # THE REGION IS A FRACTION OF THE FRAME, not a pixel rectangle. A recording is 640x480
    # and a screendump is 1920x1080, and a measurement given in pixels is silently empty on
    # whichever one it was not written for - `range(1010, 480)` is no iterations at all, and
    # that reads as "entirely dark" rather than as "not measured".
    top = int(height * DOCK_TOP)
    right = int(width * DOCK_WIDTH)
    dark = total = 0
    for y in range(top, height):
        for x in range(0, right):
            i = (y * width + x) * 3
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            total += 1
            if r + g + b < 120:
                dark += 1
    return dark / total if total else 1.0


def ocr(path: str) -> str:
    """The text on a screenshot, lower-cased, read from the places this needs to watch.

    THE WHOLE FRAME DOES NOT WORK HERE, AND THAT IS WHY RUN 41783947010 SAT FOR TWELVE
    MINUTES. Measured against that run's own last frame:

        tesseract last-frame.png              -> ""          (nothing at all)
        tesseract 3x-upscaled last-frame.png  -> ""          (nothing at all)
        tesseract crop(830,450,1090,640) x4   -> "Username: user Password: Login"

    Orbital draws its login box a few hundred pixels wide in a 1920x1080 frame, and at that
    size the glyphs are below what page segmentation will call words. So the marker this
    bake was waiting for - "username" - could never appear in the text, and the wait could
    never return. It was not slow; it was waiting for a string the reader could not produce.

    lib/assistant.py already carries the answer, and its comment is the same lesson from
    macOS's Disk Utility pane: a whole screendump loses the small text and a crop found by
    its rectangle keeps it.

    THREE REGIONS, because three things have to be read on this machine:

        the login box      who is on screen, and whether it is Orbital's login at all
        the bottom bar     the launcher, once logged in
        a wide middle band the terminal, which can be anywhere in the window area

    All of them are concatenated: a caller that says "wait for '#'" does not care which
    region the '#' came from.
    """
    try:
        import assistant
    except Exception as exc:  # noqa: BLE001
        log(f"  (ocr unavailable: {exc})")
        return ""

    parts = []
    for box, scale, what in REGIONS:
        try:
            text = assistant.ocr_text_cropped(path, box, scale=scale)
        except Exception as exc:  # noqa: BLE001
            log(f"  (ocr of {what} failed: {exc})")
            continue
        if text:
            parts.append(text)

    # THE WHOLE FRAME AS WELL, LAST. It costs one more pass and catches anything large
    # enough to read - a dialog title, a boot message, an error nobody expected.
    whole = assistant.ocr_text(path)
    if whole:
        parts.append(whole)
    return " ".join(parts)


class Console:
    """The VGA text console: type through QMP, read back as a screenshot."""

    def __init__(self, guest: vm.Guest, shots: str):
        self.guest = guest
        self.shots = shots
        self.n = 0
        os.makedirs(shots, exist_ok=True)
        # The last frame that OCR read, so a failure can quote it.
        self.last_text = ""
    def qmp(self):
        """A QMP connection, waiting for the socket to exist.

        THE SAME WAIT AS macos/15/drive.py AND windows/11/ssh.py, and it is here for the
        same reason: vm.Guest.start() returns as soon as the process exists and qemu binds
        its sockets a moment later. Connecting immediately raises

            FileNotFoundError: [Errno 2] No such file or directory
            self.sock.connect(path)

        which is what this bake's first local run did, at the very first screenshot - after
        the image had been verified, converted and booted, so it looked late when it was a
        race at the start. Redox is the third system in this repository to hit it, which is
        why the wait is a method here rather than a comment in each caller.
        """
        deadline = time.time() + 120
        path = self.guest.paths.drive
        while not os.path.exists(path):
            if time.time() > deadline:
                raise SystemExit(
                    f"no QMP socket at {path} after 120s; qemu either failed to start "
                    f"or was given a different work directory"
                )
            time.sleep(1)
        return qmpmod.QMP(path)

    def shot(self, label: str) -> str:
        """A fresh screenshot, taken through the QMP connection snapshots.py is NOT using.

        THERE IS EXACTLY ONE screendump IN THIS VM, AND IT BELONGS TO snapshots.py.
        The recording is made by a separate process that screendumps once a second into
        `work/frames`, and this class was ALSO screendumping - a second QMP client against
        the same qmp socket, into the same directory. Run 41861406726 shows what that
        produced:

            [redox-bake 15:52:03]  43s waiting for host key: '= | ... pkg_ok=$?'
            [redox-bake 15:52:49]  89s waiting for host key: '= | ... pkg_ok=$?'   (identical)

        five times, byte for byte, while the screen underneath was showing `KEYGEN_RC=0` -
        which the final frame proves, and which this module's own ocr() reads out of that
        frame correctly. The screenshots this loop was taking were not the screen.

        It also raced snapshots.py's file names: the log carries

            [lastframe] ffmpeg failed: Error opening input file .../frame-0217.ppm

        because snapshots.py had already converted that frame to JPEG and deleted the PPM -
        the frames are JPEG now, to stop eleven gigabytes of them filling a runner - and the
        live encoder was still looking for the old name.

        SO THE FRAME IS COPIED FROM THE RECORDING'S DIRECTORY INSTEAD. That directory is
        written by the one process that owns the screendump, it is where the pictures that
        end up in the video come from, and a copy of the newest one is exactly the screen.
        The label is ignored: it only ever named the step for a human reading filenames.
        """
        import lastframe

        newest = lastframe.newest_frame(self.shots)
        if not newest:
            # Nothing has been captured yet - the recording process has not started, or the
            # guest has not drawn. An empty string is what text() already handles.
            return ""
        self.n += 1
        safe = re.sub(r"[^a-z0-9]+", "-", label.lower())[:40].strip("-") or "step"
        # KEEP THE SOURCE'S EXTENSION: a JPEG copied to a .ppm name is the kind of thing
        # that works until a tool trusts the name, and lib/lastframe.convert() takes either.
        ext = os.path.splitext(newest)[1]
        png = os.path.join(self.shots, f"rx-{self.n:03d}-{safe}.png")
        return png if lastframe.convert(newest, png) else ""

    def text(self, label: str) -> str:
        png = self.shot(label)
        if png and os.path.exists(png):
            self.last_text = ocr(png)
            return self.last_text
        return ""

    def type(self, text: str, *, settle: float = 0.5) -> None:
        """Type `text` and press Return. Newlines are refused by lib/keys.

        A CONNECTION PER LINE, as on the runs that worked. lib/snapshots.py holds the record
        socket and this uses the drive socket, so there is no contention to avoid - and the
        persistent connection introduced while chasing the pointer is gone with the rest of
        that attempt.
        """
        c = self.qmp()
        try:
            for kind, code in keylib.for_text(text):
                if kind == "key":
                    qmpmod.key(c, code)
                else:
                    qmpmod.key_combo(c, *code)
                time.sleep(0.03)
            qmpmod.key(c, "ret")
        finally:
            c.close()
        time.sleep(settle)

    def click(self, x: int, y: int, *, why: str = "", settle: float = 1.5) -> None:
        """A left click at an absolute position, then a frame.

        THIS IS THE ONE THAT WORKS, AND THE PARTS OF IT THAT LOOK REDUNDANT ARE NOT.
        Run 41874326761 opened a terminal on its first click with this exact sequence:
        move, 0.3s, press, 0.15s, release. Run 41875783970 did the same.

        Everything tried since - sending the axes and the press in one event list, walking a
        relative pointer from a saturated corner, hovering first - failed, on the same
        coordinate, against the same image. Whatever the theory, this ordering is the one
        with two successes behind it, so it is the one that ships.

        The connection is still opened and closed per click, as it was on both of those runs.
        """
        c = self.qmp()
        try:
            qmpmod.click(c, x, y)
        finally:
            c.close()
        log(f"  click ({x}, {y})" + (f" {why}" if why else ""))
        time.sleep(settle)

    def key_combo(self, *qcodes: str, settle: float = 0.5,
                  hold: float = 0.15) -> None:
        """Press several keys together, e.g. meta+t or ctrl+alt+f1.

        `hold` makes the modifier stay down between two requests - see lib/qmp.py. Redox's
        super marker is set while meta is held, so a chord delivered in one message never
        produces one.
        """
        c = self.qmp()
        try:
            qmpmod.key_combo(c, *qcodes, hold=hold)
        finally:
            c.close()
        time.sleep(settle)

    def key(self, qcode: str, *, settle: float = 0.5) -> None:
        c = self.qmp()
        try:
            qmpmod.key(c, qcode)
        finally:
            c.close()
        time.sleep(settle)

    def wait_for(self, markers: list[str], seconds: int, what: str) -> str:
        """Wait until one of `markers` is on screen. Returns the one that matched, or "".

        A LIST, NOT ONE STRING, because the console's prompt is not known until it is
        seen: `root:~#`, `root:~$` and a bare `#` are all plausible, and refusing to
        accept any of them would fail a bake that had already succeeded.
        """
        deadline = time.time() + seconds
        started = time.time()
        last_report = 0.0
        while time.time() < deadline:
            text = self.text(what)

            # SAY SOMETHING EVERY 30s, AND SAY WHAT IS ON SCREEN.
            #
            # This loop had no output at all until it matched or gave up, and run 41783947010
            # therefore sat at "=== orbital ===" for twelve minutes with nothing to read.
            # A wait that is silent is indistinguishable from a hang, and the log is the only
            # thing a person watching CI has.
            #
            # What it prints is the first 160 characters of the last frame the OCR read: that
            # is enough to tell "still the boot log", "Orbital's login", and "an error nobody
            # expected" apart without opening the artifact.
            elapsed = time.time() - started
            if elapsed - last_report >= 30:
                last_report = elapsed
                shown = (self.last_text or "")[:160].replace("\n", " ")
                log(f"  {elapsed:.0f}s waiting for {what}: {shown!r}")

            for marker in markers:
                if marker in text:
                    log(f"  {what}: {marker!r} after {time.time() - started:.0f}s")
                    return marker
            time.sleep(4)
        log(f"  === {what}: NONE OF {markers} IN {seconds}s ===")
        log(f"  the screen said: {self.last_text[:300]!r}")
        return ""


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--image", default=None, help="a .img.zst or .img; pulled if absent")
    p.add_argument("--out", default="out/redox.qcow2")
    p.add_argument("--work", default=None)
    p.add_argument("--artifacts", default="out")
    p.add_argument("--user", default=SSH_USER)
    p.add_argument("--password", default=SSH_PASSWORD)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--mem", type=int, default=2048)
    p.add_argument("--cpus", type=int, default=2)
    p.add_argument("--keep-running", action="store_true",
                   help="do not stop the guest; for looking at a failed run by hand")
    args = p.parse_args(argv)

    work = os.path.abspath(args.work or os.path.join(args.artifacts, "redox-work"))
    artifacts = os.path.abspath(args.artifacts)
    os.makedirs(work, exist_ok=True)
    os.makedirs(artifacts, exist_ok=True)

    verdict = os.path.join(artifacts, "bake.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)
        try:
            with open(verdict, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    # THE IMAGE. Decompressed once; the raw file is what the disk is made from.
    import iso as isolib

    src = args.image
    if not src:
        src = os.path.join(work, "redox.img.zst")
        if not os.path.exists(src):
            note(f"pulling {IMAGE_URL.rsplit('/', 1)[-1]}")
            isolib.fetch_url(IMAGE_URL, src, sha256=IMAGE_SHA256)
    raw = src[:-4] if src.endswith(".zst") else src
    if not os.path.exists(raw) or os.path.getsize(raw) == 0:
        note(f"decompressing {os.path.basename(src)}")
        r = subprocess.run(["zstd", "-d", "-f", "-o", raw, src],
                           capture_output=True, text=True)
        if r.returncode != 0:
            note(f"=== zstd FAILED ===\n{(r.stderr or '').strip()[:300]}")
            return 1
    note(f"image {raw} ({os.path.getsize(raw) >> 20} MiB)")

    # THE DISK IS A COPY, AND IT HAS TO BE.
    #
    # The bake writes to it - an account, a package, an init script - so it cannot be the
    # release file, and `snapshot=on` is not enough because the result is the artifact. A
    # qcow2 is also what every other stage here expects.
    disk = os.path.join(work, "redox.qcow2")
    if not os.path.exists(disk):
        note(f"making a qcow2 of {os.path.basename(raw)}")
        r = subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2", raw, disk],
                           capture_output=True, text=True)
        if r.returncode != 0:
            note(f"=== qemu-img FAILED ===\n{(r.stderr or '').strip()[:300]}")
            return 1
    note(f"disk {disk} ({os.path.getsize(disk) >> 20} MiB)")

    def drive() -> bool:
        """Everything that has to happen inside the guest.

        Returns True only when the whole chain passed. Every early exit is a
        `return False` rather than a raise, because the caller has to stop the
        guest and keep the recording either way - and a traceback through a
        finally block is how a run loses the frames that explain it.
        """
        shots = os.path.join(work, "frames")
        # THE FRAME DIRECTORY HAS TO EXIST BEFORE snapshots.py IS STARTED, because its stdout
        # goes to a file inside it. This was
        #
        #     FileNotFoundError: .../out/redox-work/frames/snapshots.log
        #
        # on the first real run - Console() creates the directory too, but it is constructed
        # after this Popen. Creating it here is the fix, and the Console's makedirs stays
        # because a directory can be removed between the two.
        os.makedirs(shots, exist_ok=True)
        # A PS/2 MOUSE AS WELL AS THE USB TABLET, AND THE REASON IS THE POINTER.
        #
        # vm.Shape always attaches `usb-tablet,bus=xhci.0`, and Redox's usbhid driver does not
        # bind it: run 41894870375 clicked (115, 1051) six times and the cursor in the
        # recording is still the white arrow at (0, 0) in the 76-second frame, after every
        # one of them. QMP accepted each input-send-event - so nothing raised, and nothing
        # moved.
        #
        # Redox ships ps2d (its boot-process documentation lists "ps2d loads PS/2 handling
        # into /scheme/serio"), and q35 has a PS/2 controller. A PS/2 mouse reports RELATIVE
        # motion, which Redox does handle - and which needs no absolute axis to be understood.
        # `-device usb-tablet` is left in place because other guests in this repository use
        # it and the extra device is inert here.
        shape = vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", cpu=CPU,
                         vga="std", resolution=(1920, 1080))
        drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
        guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.path.join(work, "qemu.log"))

        import live as livelib

        snap = None
        living = None
        try:
            guest.start()
            snap = subprocess.Popen(
                [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
                 guest.paths.record, shots,
                 os.environ.get("SNAPSHOT_INTERVAL", "1")],
                stdout=open(os.path.join(shots, "snapshots.log"), "wb"),
                stderr=subprocess.STDOUT,
            )
            living = livelib.start(
                [shots], os.path.join(artifacts, "install.mp4"),
                os.path.join(artifacts, "last-frame.png"),
                every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "20")),
            )
            con = Console(guest, shots)

            # 1. THE BOOTLOADER MENU. It waits for a key, which is what the first probe found.
            note("=== the bootloader menu ===")
            if not con.wait_for(["redox"], min(args.boot_timeout, 300), "bootloader"):
                note("  the bootloader never drew; see last-frame.png")
                return False
            con.key("ret", settle=2.0)

            # 2. ORBITAL'S LOGIN BOX, AND THEN THE LOGIN ITSELF.
            #
            # THE LOGIN IS A STEP, AND IT WAS MISSING. Run 41803873067 read the login box as
            # "a terminal is open" - the box contains the word "user", which is what the check
            # was looking for - and then typed `su` into a form that was still waiting:
            #
            #     Username: [userpassword]      <- user, then su, then password
            #     Password: [|]
            #
            # Every later stage worked on the login box: "password" was found because the label
            # says Password, and the wait for a root shell never ended because no shell had
            # ever started.
            #
            # The box comes with `user` already typed and an empty password, so Return IS the
            # login. Orbital draws the desktop after it, and the dock this needs next only
            # exists once that has happened.
            note("=== orbital's login box ===")
            started_orbital = time.time()
            if not con.wait_for(["username"], args.boot_timeout, "orbital login box"):
                note("=== ORBITAL'S LOGIN BOX NEVER DREW ===")
                return False

            note("  logging in as user (empty password)")
            con.key("ret", settle=5.0)


            # THE DESKTOP IS A PICTURE, SO IT IS MEASURED, NOT READ.
            #
            # This waited for the words "redox", "orbital" or "user" and tesseract returned
            # wallpaper noise instead:
            #
            #     waiting for the desktop: '> <r : : fr "~ fad { ls aes ae 4 : . " rs rears'
            #
            # Orbital's only text is the dock's labels, drawn a few pixels tall. What
            # distinguishes it from the login box is a wallpaper and a panel - brightness and
            # colour - which is exactly what the probe measured and what redox/launch.py
            # already uses.
            note("  waiting for Orbital to draw")
            deadline = time.time() + 120
            while time.time() < deadline:
                png = con.shot("desktop")
                if png:
                    mean, chroma = frame_stats(png)
                    note(f"    mean {mean:.1f} chroma {chroma:.1f}")
                    if mean >= ORBITAL_MEAN and chroma >= ORBITAL_CHROMA:
                        note("  the desktop is up")
                        break
                time.sleep(4)
            else:
                note("=== THE LOGIN DID NOT REACH A DESKTOP ===")
                note("  the frame beside this message is either the login box again - a wrong")
                note("  password - or a blank screen, which is a session that failed to start")
                return False

            # THE WALLPAPER IS NOT THE DESKTOP. Let Orbital finish before touching it.
            #
            # The brightness test fires on the wallpaper, which is painted before the panel
            # and the dock subscribe to input. Run 41871160859 clicked one second after this
            # point and the dock never reacted. Ten seconds is what the desktop took to
            # settle in the run that got its terminal open.
            # WAIT FOR THE DOCK ITSELF, NOT JUST THE WALLPAPER.
            #
            # "The desktop is up" fires on the wallpaper, which is painted well before the
            # taskbar is a control rather than a picture. Measured across three bakes whose
            # timings were otherwise identical:
            #
            #     41874326761   dock dark 0.000   the first click opened a terminal
            #     41882033727   dock dark 0.603   six clicks, nothing at all
            #
            # So the thing to wait for is the strip turning light. A dark dock is a
            # placeholder; it is drawn, and it answers nothing.
            # WAIT FOR THE DOCK TO SETTLE, THEN TRY THE ONLY TWO WAYS IN.
            #
            # Orbital's dock is clickable on some boots and not others - 41874326761 and
            # 41875783970 opened a terminal on the first click, and every run since has been
            # silent - and the difference is not in the frames. So both routes are tried in
            # the order of how much they are known to work, and whichever produces a shell is
            # used. Everything after this point is typed either way.
            log("  letting Orbital finish drawing its panel and dock")
            time.sleep(int(os.environ.get("DESKTOP_SETTLE_SECONDS", "15")))

            tip = None
            got_shell = False

            # --- 1. SUPER+T. The launcher binds it to the terminal, and it needs no pointer.
            #
            # orbutils/launcher/src/main.rs handles the super-key events directly:
            #
            #     if event.code >= 0x1000_0000 {          // the "super event" hack
            #         super_event.code -= 0x1000_0000;
            #         match key_event.scancode {
            #             orbclient::K_B => bar.spawn("netsurf-fb"),
            #             orbclient::K_F => bar.spawn("cosmic-files"),
            #             orbclient::K_T => bar.spawn("cosmic-term"),     // <- this one
            #         }
            #     }
            #
            # So Super+T opens the terminal through the launcher's own code path, with no
            # cursor position involved. That matters because the dock has opened a terminal in
            # six of eleven bakes and not the other five, with byte-identical frames at the
            # moment of the click - and the keyboard has never failed to reach this guest.
            note("=== a terminal, with Super+T ===")
            # BOTH SUPER KEYS, AND THEN BOTH AGAIN. The launcher's handler is fed by a
            # `code >= 0x1000_0000` marker that the kernel sets while a meta key is held, so
            # whether the left or the right one produces it depends on the keymap the image
            # was built with - and nothing here can ask. Trying both costs two keystrokes.
            for meta in ("meta_l", "meta_r"):
                con.key_combo(meta, "t", settle=2.0)
                time.sleep(4)
                png = con.shot(f"super-t-{meta}")
                if not png:
                    continue
                now = dark_window_fraction(png)
                note(f"    {meta}+t: dark fraction {now:.2f}")
                if now >= 0.55:
                    got_shell = True
                    note(f"  {meta}+T opened a terminal")
                    break
            if not got_shell:
                note("  Super+T opened nothing")

            # --- 1b. THE DOCK, IF Super+T DID NOT. --------------------------------------
            if not got_shell:
                note("=== a terminal, from the dock ===")
                baseline = con.shot("terminal-before")
                before = dark_window_fraction(baseline) if baseline else 0.0
                note(f"  dark fraction before: {before:.2f}")
                # TEN ATTEMPTS, BECAUSE THE OUTCOME IS A COIN FLIP AND EACH ONE COSTS SECONDS.
                #
                # Five bakes this session behaved like this:
                #
                #     1bf4adb  f45c29f  1a79330  a77bd3f    opened a terminal on attempt 1
                #     366b38e  41912935659                  opened nothing, ten seconds at a time
                #
                # with byte-identical frames at the moment of the click and identical timing - a
                # slow boot was the guess, and the second of those runs waited thirty extra
                # seconds and still opened nothing. Nothing measurable distinguishes them.
                #
                # Four in five, repeated ten times, is a failure rate of one in ten million; three
                # attempts is one in a hundred and twenty-five, which is what the last two runs
                # drew from.
                for attempt in range(1, 11):
                    con.click(*DOCK_TERMINAL,
                              why=f"the terminal icon in the dock (attempt {attempt} of 10)",
                              settle=1.0)
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        time.sleep(2)
                        png = con.shot(f"terminal-{attempt}")
                        if not png:
                            continue
                        now = dark_window_fraction(png)
                        note(f"    dark fraction {now:.2f}")
                        # 15 points of the frame turning dark is a window, not a redraw: the
                        # wallpaper does not change when a terminal opens over it.
                        if now - before >= 0.15:
                            got_shell = True
                            break
                    if got_shell:
                        break

            # --- 2. A TEXT CONSOLE, BY KEYBOARD. The keyboard has never failed. ----------
            #
            # The image has no getty on VT 1 or 2 - both were tried on run 41902659023 and
            # neither produced "login:" - but the switch is kept because it costs one
            # keystroke per VT and turns "no way in" into "a shell".
            if not got_shell:
                note("=== a text console, by keyboard ===")
                for vt in ("f1", "f2"):
                    # THE QCODES ARE `ctrl` AND `alt`, NOT `ctrl_l` AND `alt_l`. QEMU's
                    # QKeyCode enumeration is 'shift', 'shift_r', 'alt', 'alt_r', 'ctrl',
                    # 'ctrl_r', ... - only the right-hand modifiers take a suffix - and its
                    # ctrl-alt-delete example sends "ctrl", "alt", "delete".
                    con.key_combo("ctrl", "alt", vt, settle=1.0)
                    time.sleep(5)
                    if con.wait_for(["login:"], 30, f"a text console on {vt.upper()}"):
                        note(f"  a text console is on {vt.upper()}")
                        con.type("user", settle=4.0)
                        con.key("ret", settle=6.0)
                        if con.wait_for(["password", "user:~$", "user:~#"], 60, "a prompt"):
                            con.key("ret", settle=2.0)
                            if con.wait_for(["user:~$", "user:~#"], 30, "a shell"):
                                tip = "a text console"
                                got_shell = True
                                break
                    note(f"  no login prompt on {vt.upper()}")

            if not got_shell:
                note("=== NO WAY INTO THIS GUEST ===")
                note("  the dock opened nothing in three clicks and there is no getty on")
                note("  VT 1 or 2; the frame beside this message is the desktop")
                return False
            if tip:
                note(f"  a shell on {tip}")
            else:
                note("  a terminal is open")

            # 4. ROOT, AND WHY NOT `user`.
            #
            # `user` is in the sudo group but sudo prompts - for an empty password, which still
            # means pressing Return at a prompt the driver would have to notice and answer.
            # `su` into root needs the password ONCE, typed deliberately, and every command
            # below then needs nothing.
            # 4. ROOT, AND WHY THE SCREEN IS NOT CONSULTED AGAIN.
            #
            # Everything above this point failed at least once because a check read the screen
            # and the screen had something else on it: the login box's "user" for a terminal
            # prompt, the wallpaper for a desktop, the account form's label for a password
            # prompt. Typing into a terminal and asking tesseract what happened repeats that
            # mistake - the text is a few pixels tall on a dark background, which is the exact
            # case OCR cannot do.
            #
            # So this types a fixed sequence with fixed waits and does not look. What decides
            # whether it worked is the step AFTER it: the sshd it installs either answers from
            # outside or it does not, and that answer is a TCP banner rather than an opinion
            # about a picture.
            #
            # `su` prompts once for root's password ("password"). Everything typed before that
            # prompt appears is buffered by the terminal, not lost, which is why one wait and
            # then the password is enough.
            note("=== root ===")
            con.type("su", settle=3.0)
            con.type(ROOT_PASSWORD, settle=5.0)

            # A SANITY CHECK THAT READS THE PROMPT, NOT THE NUMBER.
            #
            # This used to wait for the string "0" on screen, on the reasoning that `id -u`
            # prints one digit and one digit is the only thing OCR can still manage on a dark
            # terminal. It cannot: run 41819642279's last frame shows, plainly enough to read
            # by eye,
            #
            #     user:~$ su
            #     password:
            #     root:~# id -u
            #     0
            #     root:~#
            #
            # - a root shell that had already worked - and the check failed anyway with
            #
            #     === id -u printed 0: NONE OF ['0'] IN 60s ===
            #
            # because the single glyph is a few pixels and tesseract returned the surrounding
            # noise instead. `root:` IS read out of the same frame; the prompt is rendered at
            # the same size as the rest of the text and there is a whole word of it.
            #
            # So the marker is the prompt. `id -u` still runs, because the prompt appearing
            # after it is what proves the command finished in a root shell rather than that
            # the word "root" was already somewhere on the screen - it is in the title bar,
            # which is why the marker is `root:~#` and not `root`.
            con.type("id -u", settle=3.0)
            # 300 SECONDS, NOT 60. This is the same reader lag the other waits were widened
            # for, and it has now cost a run: 41922820567 had a terminal open on screen with
            # `su` typed into it, and the 60-second window closed while the OCR was still
            # reading the wallpaper behind the window.
            marker = con.wait_for(["root:~#", "root:~$", "root:/root#"],
                                  300, "a root prompt")
            if not marker:
                note("=== su DID NOT GIVE A ROOT SHELL ===")
                note("  no `root:~#` prompt appeared after `id -u`; the frame beside this")
                note("  message is the terminal")
                return False
            note(f"  a root shell ({marker!r})")

            # 5. THE ACCOUNT. TWO FILES, AND THEY ANSWER DIFFERENT QUESTIONS.
            #
            #     /etc/users.toml   what login, sudo and pkg read - an ini-style list of
            #                       [users.<name>] tables with password/uid/gid/shell
            #     /etc/passwd       what every libc account lookup reads, getpwnam included;
            #                       one entry per line, `;`-separated, see the note below
            #
            # This wrote only the first for a long time, and the comment here said there was no
            # second. There is, and sshd's privilege-separation lookup uses it.
            #
            # THE FILE IS BUILT WITH A HEREDOC because the console has no clipboard and the
            # content has quotes and newlines. `ion` handles them the way sh does.
            note("=== the account ===")
            # ION HAS NO HEREDOC. THE FILE IS BUILT ONE LINE AT A TIME.
            #
            # The previous version typed
            #
            #     cat > /etc/users.toml << EOF
            #     [users.root]
            #     ...
            #     EOF
            #
            # and the frame from run 41817639644 shows what ion did with it:
            #
            #     root:~# shell = "/usr/bin/ion"
            #     ion: command not found: shell
            #     root:~# [groups.sudo]
            #     ion: command not found: groups.sudo
            #     root:~# EOF
            #     ion: command not found: EOF
            #
            # Every line was executed as a command. `USERS_WRITTEN=0` then passed because the
            # redirection itself succeeded - the file exists and is empty.
            #
            # So the first line truncates with a single-quoted redirect and the rest append.
            # Single quotes around each line, because the TOML has double quotes in it.
            users = [
                "[users.root]",
                'password = "password"',
                "uid = 0",
                "gid = 0",
                'shell = "/usr/bin/ion"',
                "",
                f"[users.{args.user}]",
                f'password = "{args.password}"',
                "uid = 1000",
                "gid = 1000",
                'shell = "/usr/bin/ion"',
                "",
                "[groups.sudo]",
                "gid = 1",
                f'members = ["{args.user}"]',
                "",
                # THE PRIVILEGE-SEPARATION ACCOUNT, IN THE SAME WRITE AS THE OTHERS.
                #
                # sshd refuses to start without it - "Privilege separation user sshd does not
                # exist", exit 255 - and adding it afterwards, with its own append and its own
                # wait, produced a file that reported SSHDUSER_OK=1 on the same screen as the
                # same error. A TOML parser stops at the first table it cannot read, so a file
                # assembled by two separate appends is one chance more than this needs.
                "[users.sshd]",
                'password = "*"',
                "uid = 22",
                "gid = 22",
                'shell = "/usr/bin/ion"',
            ]
            for n, line in enumerate(users):
                op = ">" if n == 0 else ">>"
                con.type(f"echo '{line}' {op} /etc/users.toml", settle=0.25)

            # `grep -c` PANICS ON THIS SYSTEM. The frame has
            #
            #     RELIBC PANIC: panicked at src/start.rs:188:18:
            #     failed to initialize FILETABLE: Function not implemented
            #
            # so the check that the account landed is `wc -l`, which reports a number.
            #
            # THE EXIT STATUS IS READ, NOT THE NUMBER. Waiting for "15" on screen is the same
            # mistake as waiting for "0" above: a two-digit count at terminal size is a few
            # pixels, and the tesseract pass that could not read `0` cannot be relied on to
            # read `15` either. `test` compares the count inside the guest, so what comes back
            # is `USERS_OK=0` - seven characters at the size the prompt is read at.
            con.type(f"test \"$(wc -l < /etc/users.toml)\" -eq {len(users)} ; echo USERS_OK=$?",
                     settle=2.0)
            if not con.wait_for(["users_ok=0"], 300,
                                f"users.toml has {len(users)} lines"):
                note("=== /etc/users.toml DOES NOT HAVE THE EXPECTED LINES ===")
                note(f"  expected {len(users)}; the frame beside this message shows what it has")
                return False
            note("  the account is written")

            # /etc/passwd, BECAUSE THAT IS WHAT getpwnam() READS - NOT users.toml.
            #
            # Run 41914834762 printed its users.toml, and the frame can be read without OCR:
            # the file is complete and correct, sshd's section included -
            #
            #     [users.sshd]
            #     password = "*"
            #     uid = 22
            #     gid = 22
            #     shell = "/usr/bin/ion"
            #
            # - and sshd still answered "Privilege separation user sshd does not exist",
            # SSHD_RC=255.
            #
            # relibc's pwd implementation is why. src/header/pwd/mod.rs:
            #
            #     let Ok(file) = File::open(c"/etc/passwd".into(), fcntl::O_RDONLY) else {
            #         return Err(Cause::Other);
            #     };
            #
            # and on Redox the field separator is `;` (SEPARATOR is b':' on Linux and b';'
            # under #[cfg(target_os = "redox")]), one entry per line, in the order
            #
            #     name;uid;gid;gecos;dir;shell
            #
            # `users.toml` is what login and pkg read; `passwd` is what every libc account
            # lookup reads. sshd uses getpwnam, so it has been looking in a file this bake
            # never wrote.
            passwd_entries = [
                "root;0;0;root;/root;/usr/bin/ion",
                f"{args.user};1000;1000;{args.user};/home/{args.user};/usr/bin/ion",
                "sshd;22;22;sshd;/var/empty;/usr/bin/ion",
            ]
            for n, line in enumerate(passwd_entries):
                op = ">" if n == 0 else ">>"
                con.type(f"echo '{line}' {op} /etc/passwd", settle=0.25)
            con.type(f"test \"$(wc -l < /etc/passwd)\" -eq {len(passwd_entries)} ; echo PASSWD_OK=$?",
                     settle=2.0)
            if not con.wait_for(["passwd_ok=0"], 300, "/etc/passwd"):
                note("=== /etc/passwd DOES NOT HAVE THE EXPECTED LINES ===")
                return False
            note("  /etc/passwd is written")

            # 6. OPENSSH. Not in the desktop image; installed from the vendor's package server,
            #    which the image already points at (/etc/pkg.d/50_redox).
            note("=== openssh ===")
            # `--yes` IS A LONG OPTION AND THERE IS NO `-y`. Redox's pkg is a Rust program
            # (redox-os/pkgutils, clap-derived), and clap rejects the short form outright:
            #
            #     root:~# pkg install -y openssh ; echo PKG_RC=$?
            #     error: unexpected argument '-y' found
            #       tip: to pass '-y' as a value, use '-- -y'
            #     Usage: pkg install [OPTIONS] [PACKAGES]...
            #     PKG_RC=2
            #
            # which is run 41859057018's last frame, after this line had been changed TO `-y`
            # on the reasoning that pkg asks
            #
            #     Proceed with this transaction? [Y/n]:
            #
            # and that something was needed to answer it. The prompt was real; the flag was
            # from a different pkg. From pkg-cli's Cli struct, the global flags are `--curl`,
            # `--yes`, `--nocheck` and `--user`, and the comment on `--yes` is "Always reply
            # with yes even with interactive tty".
            #
            # `--yes` GOES BEFORE THE SUBCOMMAND, because it is declared
            # `global = true` on the top-level Cli rather than on Install - so
            # `pkg install openssh --yes` also parses, and `pkg --yes install openssh` is the
            # form the declaration describes. This uses that one.
            con.type("pkg --yes install openssh ; test -f /usr/bin/sshd ; echo PKG_OK=$?",
                     settle=2.0)
            # `pkg_rc=` MATCHED THE ECHO OF THE COMMAND LINE ITSELF.
            #
            # The line above types `... ; echo PKG_RC=$?`, the terminal echoes it back, and
            # the OCR reads the echo - so the wait returned after 13 seconds, "successfully",
            # for a pkg that had exited 2. The frame from run 41859057018 shows both: the
            # echoed command near the top and `PKG_RC=2` eight lines below.
            #
            # What is waited for now is a value that the echo cannot contain: the expansion
            # of a success test rather than the text of the command that produced it.
            if not con.wait_for(["pkg_ok=0"], 900, "pkg install openssh"):
                note("=== pkg COULD NOT INSTALL openssh ===")
                note("  it needs the network; qemu's user-mode net is 10.0.2.x and the image")
                note("  ships /etc/net/ip = 10.0.2.15, so a failure here is the package server")
                return False
            note("  openssh installed")

            # 7. THE HOST KEY. sshd refuses to start without one, and it is generated once, on
            #    the disk, not at every boot.
            note("=== the host key ===")
            con.type("ssh-keygen -A ; echo KEYGEN_RC=$?")
            if not con.wait_for(["keygen_rc=0"], 600, "host key"):
                note("=== ssh-keygen -A FAILED ===")
                return False

            # 8. THE STARTUP SCRIPT. The desktop variant has no systemd and no rustysd;
            #    services are numbered files in /usr/lib/init.d/.
            #
            # /etc/init.d DOES NOT EXIST ON THIS SYSTEM, and every redox bake so far has
            # failed here believing it did. Run 41875783970's last frame reads:
            #
            #     root:~# echo '#!/usr/bin/ion' > /etc/init.d/90_sshd
            #     ion: pipeline execution error: failed to redirect stdout to file
            #          '/etc/init.d/90_sshd': No such file or directory (os error 2)
            #     root:~# chmod +x /etc/init.d/90_sshd ; echo CHMOD_RC=$?
            #     chmod: cannot access '/etc/init.d/90_sshd': No such file or directory
            #     CHMOD_RC=1
            #
            # - two redirections that failed and a chmod on a file that was never written.
            # The directory is a Debian convention; Redox's own boot-process documentation
            # puts the filesystem init's configuration in /usr/lib/init.d, and that is what
            # is on the image.
            #
            # `90_` sorts after the network and the console, which sshd needs. The directory
            # is created first because `mkdir -p` on an existing one is silent and the
            # redirection is what fails loudly when it is not there.
            note("=== the init.d entry ===")
            # ONE LINE, ONE MARKER, BECAUSE THE READER LAGS THE TYPING.
            #
            # Run 41904865449 typed `mkdir -p /usr/lib/init.d ; echo MKDIR_RC=$?` and then
            # waited 120 seconds for `mkdir_rc=0`. What the screen was showing, at 30s, 64s
            # and 106s, was the echo lines from /etc/users.toml - four commands earlier. A
            # wait_for() pass screenshots and reads three crops plus the whole frame with
            # tesseract; that is several seconds per pass, and every pass sees a screen
            # further behind the keystrokes that have queued behind it.
            #
            # So the whole init.d step goes in as ONE command line with ONE marker at the end.
            # Nothing is checked in the middle, and the marker cannot be reached unless the
            # mkdir, both echoes and the chmod all succeeded - `&&` rather than `;`, so a
            # failure anywhere leaves INITD_OK unset instead of printing a misleading number.
            #
            # /usr/lib/init.d, NOT /etc/init.d: the latter is a Debian convention and does not
            # exist here (run 41875783970's frame shows the redirection failing with
            # "No such file or directory (os error 2)").
            con.type(
                "mkdir -p /usr/lib/init.d "
                "&& echo '#!/usr/bin/ion' > /usr/lib/init.d/90_sshd "
                "&& echo '/usr/bin/sshd -D &' >> /usr/lib/init.d/90_sshd "
                "&& chmod +x /usr/lib/init.d/90_sshd "
                "&& echo INITD_OK=1",
                settle=2.0)
            if not con.wait_for(["initd_ok=1"], 600, "the init.d entry"):
                note("=== THE init.d ENTRY COULD NOT BE WRITTEN ===")
                note("  mkdir, both echoes and the chmod are joined by && on one line; the")
                note("  frame beside this message shows which of them failed")
                return False
            note("  the init.d entry is written and executable")

            # 9. START IT NOW AND PROVE IT LISTENS. The init.d entry only matters at the next
            #    boot; this run has to verify the daemon before the disk is published.
            note("=== starting sshd ===")
            # THE SAME ECHO FAULT AS `pkg_rc=`, AND FIXED THE SAME WAY. `/usr/bin/sshd ; echo
            # SSHD_RC=$?` is echoed by the terminal before it runs, and `SSHD_RC=$?` in that
            # echo does not contain a digit - so a marker of `sshd_rc=` matched the echo, and a
            # daemon that never started would have passed. What is matched is a value with a
            # digit in it, which only the expansion produces.
            # THE CHROOT DIRECTORY, WHICH IS THE NEXT CHECK IN sshd's OWN STARTUP.
            #
            # The /etc/passwd fix moved the failure on, and the frame from run 41916585870
            # shows exactly one line further into sshd.c:
            #
            #     Missing privilege separation directory: /var/empty
            #     SSHD_RC=255
            #
            # which is `if ((stat(_PATH_PRIVSEP_CHROOT_DIR, &sb) == -1) || !S_ISDIR(...))` -
            # the check immediately after the account lookup that used to fail. A directory is
            # all it wants; sshd chroots the unprivileged half of each connection into it.
            #
            # ONE LINE WITH ONE MARKER, as with the other file work: every wait_for() pass
            # reads the screen several seconds behind, so a step split into several waits falls
            # further behind with each one.
            con.type("mkdir -p /var/empty && chmod 755 /var/empty && echo VAREMPTY_OK=1",
                     settle=2.0)
            if not con.wait_for(["varempty_ok=1"], 300, "/var/empty"):
                note("=== /var/empty COULD NOT BE CREATED ===")
                return False
            note("  /var/empty exists")

            con.type("/usr/bin/sshd ; echo SSHD_RC=$?")
            if not con.wait_for(["sshd_rc=0"], 600, "sshd started"):
                note("=== sshd WOULD NOT START ===")
                note("  `sshd -d` prints why; the frame beside this message is the console")
                return False

            # 10. THE ACCOUNT CAN REACH IT, from the guest's own network - the same address
            #     qemu forwards to, so this proves the port rather than just the process.
            #
            # `-y` IS NOT NEEDED AND `true` IS NOT A REDOX COMMAND. The probe is `echo`, which
            # ion has; and the check is the exit status of the whole ssh, so an auth failure
            # and a missing binary are the same answer here - which is correct, because both
            # mean the account cannot log in.
            note("=== ssh from the guest to itself ===")
            # THE ADDRESS HAS TO BE QUOTED, OR ion READS IT AS A VARIABLE.
            #
            # Run 41924785986 reached this with sshd running (SSHD_RC=0) and the frame shows
            # what happened instead:
            #
            #     root:~# ssh ... docker@10.0.2.15 echo ok ; echo SSHTEST=$?
            #     ion: expansion error: Variable "10" does not exist
            #
            # ion expands `@10` - the `@` starts a variable name in this shell - so the
            # command never ran and the probe could not have succeeded. The redirection
            # operators wrote a literal file for the same reason in an earlier run.
            #
            # Single quotes around the destination, which is how a shell keeps its hands off
            # an argument.
            con.type(f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                     f"'{args.user}@10.0.2.15' echo ok ; echo SSHTEST=$?")
            if not con.wait_for(["sshtest=0"], 300, "ssh login"):
                note("=== sshd IS UP BUT THE ACCOUNT CANNOT LOG IN ===")
                note("  a password prompt that never accepts an empty line is the usual cause,")
                note("  and `ssh -v` on the frame beside this message says which check failed")
                return False

            # 11. CLEAN, SO THE PUBLISHED DISK DOES NOT CARRY THIS RUN.
            note("=== cleaning ===")
            con.type("rm -f /root/.ssh/known_hosts ; echo CLEAN_RC=$?")
            con.wait_for(["clean_rc=0"], 60, "cleanup")

            note("=== BAKED ===")
            return True
        finally:
            if not args.keep_running:
                try:
                    guest.stop()
                except Exception as exc:  # noqa: BLE001
                    note(f"  the guest did not stop cleanly: {exc}")
            if snap is not None:
                snap.terminate()
                try:
                    snap.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    snap.kill()
            if living is not None:
                living.stop(final=True)

    ok = drive()
    if not ok:
        note("=== THE BAKE DID NOT FINISH; the disk is not published ===")
        note("  install.mp4 and last-frame.png are in the artifacts directory")
        return 1

    note("compressing")
    import compress

    compress.compress(disk, args.out)
    note(f"BAKED redox -> {args.out} ({os.path.getsize(args.out) >> 20} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
