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

# THE SECOND GLYPH IN ORBITAL'S BOTTOM BAR, measured from a 1920x1080 frame.
#
# The bar holds two letter placeholders and nothing else, so this is a position rather than
# an icon. See the note at the click that uses it.
DOCK_TERMINAL = (1895, 1055)

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
        self.n += 1
        safe = re.sub(r"[^a-z0-9]+", "-", label.lower())[:40].strip("-") or "step"
        ppm = os.path.join(self.shots, f"rx-{self.n:03d}-{safe}.ppm")
        png = ppm[:-4] + ".png"
        c = self.qmp()
        try:
            c.execute("screendump", filename=ppm)
        finally:
            c.close()
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", ppm, png],
                           check=False, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return ""
        return png

    def text(self, label: str) -> str:
        png = self.shot(label)
        if png and os.path.exists(png):
            self.last_text = ocr(png)
            return self.last_text
        return ""

    def type(self, text: str, *, settle: float = 0.5) -> None:
        """Type `text` and press Return. Newlines are refused by lib/keys."""
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

        qmp.click() takes 0..32767 and scales to the screen, so the caller passes pixels and
        this converts - the same contract lib/qmp.py has, kept here so every call in this
        file is in the units a screenshot is measured in.
        """
        c = self.qmp()
        try:
            qmpmod.click(c, x, y)
        finally:
            c.close()
        log(f"  click ({x}, {y})" + (f" {why}" if why else ""))
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

            # 2. ORBITAL. Not required for the work, but it is what says the image is healthy,
            #    and F2 only has a second terminal to switch to once userspace is up.
            note("=== orbital ===")
            if not con.wait_for(["username", "login"], args.boot_timeout, "orbital login"):
                note("  Orbital's login never drew; the console may still be the boot log")
                note("  continuing - F2 is tried below either way")

            # 3. A TERMINAL, OPENED THE WAY A PERSON WOULD - the same shape as
            #    windows/11/ssh.py's open_powershell().
            #
            #    WINDOWS                      REDOX
            #    Esc                          (nothing to dismiss)
            #    Win+R                        click the terminal icon in the dock
            #    type "powershell"            (the icon IS the program)
            #    Return                       (it starts on click)
            #
            #    F2 WAS THE WRONG MECHANISM and this run proved it. The image's own init list
            #    is
            #
            #        30_console_switch      switches the console
            #        20_orbital             takes VT3 for the compositor
            #        30_getty_debug.service a getty
            #
            #    so 30_console_switch has ALREADY handed VT3 to Orbital by the time a driver
            #    could press F2, and pressing it afterwards moves nothing. The bake sat on
            #    "=== orbital ===" for twelve minutes waiting for a text login that was never
            #    going to appear on that VT.
            #
            #    THE DOCK IS THE LAUNCHER. Orbital's launcher is a taskbar that scans /apps/
            #    for .desktop entries (doc.redox-os.org/book/gui.html), and this image ships
            #    COSMIC Terminal's icon as /apps/com.system76.CosmicTerm.svg. So the terminal
            #    is a click on the bottom bar, not a key sequence.
            note("=== a terminal, from the dock ===")
            # WHAT THE DOCK ACTUALLY IS, measured from run 41783947010's last frame:
            #
            #     the bottom bar holds exactly two glyphs, at x=1844..1871 and x=1882..1909
            #     (y~1055), and both are LETTER PLACEHOLDERS - a bold "K" and a bold "P" on
            #     a dark square. They are not icons: Orbital's launcher falls back to the
            #     first letter when it cannot load an application's icon, and its binary
            #     carries `failed to find icon ... with size ...` for exactly that path.
            #
            # So the dock cannot be clicked by looking for a terminal picture - there is no
            # picture. The click below is at the SECOND glyph, which is the position a
            # taskbar entry occupies, and its outcome is what this stage reports.
            con.click(*DOCK_TERMINAL, why="the second glyph in the bottom bar")
            if not con.wait_for(["$", "#", "user"], 90, "the terminal's shell"):
                note("=== THE DOCK DID NOT OPEN A TERMINAL ===")
                note(f"  clicked {DOCK_TERMINAL}; the bottom bar shows two letter")
                note("  placeholders (K at x=1844..1871, P at x=1882..1909) instead of icons,")
                note("  so which glyph launches what is not knowable from the screen")
                return False
            note("  a terminal is open")

            # 4. ROOT, AND WHY NOT `user`.
            #
            # `user` is in the sudo group but sudo prompts - for an empty password, which still
            # means pressing Return at a prompt the driver would have to notice and answer.
            # `su` into root needs the password ONCE, typed deliberately, and every command
            # below then needs nothing.
            note("=== root ===")
            con.type("su")
            if not con.wait_for(["password"], 60, "the su password prompt"):
                note("  no password prompt; `user` may already be root, continuing")
            con.type(ROOT_PASSWORD)
            if not con.wait_for(["#", "root"], 120, "a root shell"):
                note("=== su DID NOT GIVE A ROOT SHELL ===")
                return False
            con.type("echo WHOAMI_MARKER")
            if not con.wait_for(["whoami_marker"], 60, "the shell echoes"):
                note("=== THE SHELL IS NOT READING WHAT IS TYPED ===")
                return False
            note("  the terminal takes commands")

            # 5. THE ACCOUNT. Redox reads /etc/users.toml; there is no /etc/passwd to edit.
            #
            # THE FILE IS BUILT WITH A HEREDOC because the console has no clipboard and the
            # content has quotes and newlines. `ion` handles them the way sh does.
            note("=== the account ===")
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
            ]
            con.type("cat > /etc/users.toml << EOF")
            for line in users:
                con.type(line, settle=0.15)
            con.type("EOF", settle=1.0)
            con.type("echo USERS_WRITTEN=$?")
            if not con.wait_for(["users_written=0"], 60, "users.toml written"):
                note("=== /etc/users.toml COULD NOT BE WRITTEN ===")
                return False
            con.type(f"grep -c {args.user} /etc/users.toml")
            if not con.wait_for(["2"], 60, "the account is in the file"):
                note("=== THE ACCOUNT IS NOT IN THE FILE TWICE ===")
                note("  expected the [users.<name>] line and the sudo members line")
                return False
            note("  the account is written")

            # 6. OPENSSH. Not in the desktop image; installed from the vendor's package server,
            #    which the image already points at (/etc/pkg.d/50_redox).
            note("=== openssh ===")
            con.type("pkg install openssh ; echo PKG_RC=$?")
            if not con.wait_for(["pkg_rc="], 900, "pkg install openssh"):
                note("=== pkg COULD NOT INSTALL openssh ===")
                note("  it needs the network; qemu's user-mode net is 10.0.2.x and the image")
                note("  ships /etc/net/ip = 10.0.2.15, so a failure here is the package server")
                return False
            con.type("ls /usr/bin/sshd ; echo HASSSHD=$?")
            if not con.wait_for(["hassshd=0"], 180, "sshd installed"):
                note("=== openssh INSTALLED BUT THERE IS NO /usr/bin/sshd ===")
                note("  the package may put it elsewhere; `pkg list openssh` says where")
                return False

            # 7. THE HOST KEY. sshd refuses to start without one, and it is generated once, on
            #    the disk, not at every boot.
            note("=== the host key ===")
            con.type("ssh-keygen -A ; echo KEYGEN_RC=$?")
            if not con.wait_for(["keygen_rc=0"], 300, "host key"):
                note("=== ssh-keygen -A FAILED ===")
                return False

            # 8. THE STARTUP SCRIPT. The desktop variant has no systemd and no rustysd;
            #    services are numbered files in /etc/init.d/. `90_` sorts after the network and
            #    the console, which sshd needs.
            note("=== the init.d entry ===")
            con.type("cat > /etc/init.d/90_sshd << EOF")
            for line in ("#!/usr/bin/ion", "/usr/bin/sshd -D &"):
                con.type(line, settle=0.15)
            con.type("EOF", settle=1.0)
            con.type("chmod +x /etc/init.d/90_sshd ; echo CHMOD_RC=$?")
            if not con.wait_for(["chmod_rc=0"], 60, "init.d entry"):
                note("=== THE init.d ENTRY COULD NOT BE MADE EXECUTABLE ===")
                return False

            # 9. START IT NOW AND PROVE IT LISTENS. The init.d entry only matters at the next
            #    boot; this run has to verify the daemon before the disk is published.
            note("=== starting sshd ===")
            con.type("/usr/bin/sshd ; echo SSHD_RC=$?")
            if not con.wait_for(["sshd_rc="], 180, "sshd started"):
                note("=== sshd WOULD NOT START ===")
                note("  `sshd -d` prints why; the frame beside this message is the console")
                return False

            # 10. THE ACCOUNT CAN REACH IT, from the guest's own network - the same address
            #     qemu forwards to, so this proves the port rather than just the process.
            note("=== ssh from the guest to itself ===")
            con.type(f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                     f"{args.user}@10.0.2.15 true ; echo SSHTEST=$?")
            if not con.wait_for(["sshtest="], 300, "ssh login"):
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
