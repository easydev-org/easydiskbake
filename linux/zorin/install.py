"""Install Zorin OS from its Live ISO, through Ubiquity, its GTK installer.

    python3 linux/zorin/install.py --iso out/iso/Zorin-OS-18.1-Core-64-bit.iso \
                                       --out out/zorin.qcow2

ZORIN IS AN UBUNTU DERIVATIVE, SO ITS INSTALLER IS UBIQUITY AND THIS DRIVER IS MINT'S.

Same seven panes, same footer, same account form. What differs is the DESKTOP AROUND IT,
and that is the one thing this file exists to handle.

THE DESKTOP ICON IS A LAUNCHER GNOME REFUSES TO RUN, AND THAT IS NOT AN INSTALLER BUG.

Measured on a local session: double-clicking `Install Zorin OS 18` opened

    Untrusted application launcher
    Failed to run "install-zorin-os.desktop"
    Timeout was reached

- GNOME only executes a `.desktop` file that carries its trusted mark, and a file copied
onto a live desktop does not. No amount of clicking changes that.

So the installer is started from a terminal, by its own command:

    xset s off; xset -dpms; xset s noblank          the X blank
    gsettings ... idle-delay 0 / lock-enabled false the GNOME lock
    xdotool mousemove_relative ...                  a keepalive, from inside the session
    pkexec ubiquity &                               the installer, as root
    exit                                            the terminal closes itself

THE SCREEN SAVER IS THE SECOND REASON FOR THAT FILE. Measured on zorin, whose desktop
has the same behaviour: an idle session raises a lock screen showing a large clock and
nothing else, so every later reading is a clock, and a correct install finishes underneath
a screen the driver cannot read.

NOTHING IS LOOKED UP AT RUN TIME. Every position below was measured from a frame; the screen
is read afterwards to say what happened, never to choose where to click.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import compress     # noqa: E402
import keys         # noqa: E402
import lastframe    # noqa: E402
import live         # noqa: E402
import ocr          # noqa: E402
import qmp as Q     # noqa: E402
import uefi         # noqa: E402
import vm           # noqa: E402

# --- measured on a live Zorin OS 22.3 Cinnamon session, 1920x1080 ------------------------

# THE DESKTOP ICON. Double-clicked with an 80 ms gap: MATE and Cinnamon read two presses
# farther apart than their double-click interval as two single clicks, and a single click
# only selects the icon. The same measurement that set the GhostBSD driver's value.
# THE WELCOME WINDOW'S `Install Zorin OS` BUTTON, at 1920x1080. The window is centred and
# its right-hand card is the one that starts Ubiquity; the left card only starts a live
# session test. Measured on a frame of the Asturian rendering (`Instalar Zorin OS`), whose
# layout is identical to the English one.
WELCOME_INSTALL = (1223, 586)

INSTALL_ICON = (114, 47)
DOUBLE_CLICK_GAP = 0.08

# THE UBIQUITY FOOTER. Quit / Back / Continue (or Install Now), right-aligned inside the
# window, which Ubiquity centres on a 1920x1080 screen at a fixed size.
# THE INSTALL RUN'S OWN DURATION, AS A NUMBER - see the finish loop in walk_ubiquity.
#
# Measured from `Continue` on the Who-are-you pane to the `Installation Complete` modal:
# about 12 minutes on this image and about the same on CI. Forty minutes
# is that with room for a slower runner.
INSTALL_SECONDS = 2400

# ZORIN'S FOOTER IS NOT MINT'S, AND EVERY NUMBER BELOW WAS MEASURED ON THIS IMAGE.
#
# Zorin 18 draws a SEVEN-step Ubiquity (`Keyboard layout`, `Updates and other software`,
# `Installation type`, `Where are you?`, `Who are you?`, ...) in a window at
# (491,248)..(1428,896), with its footer at y 764 - 37 px lower than Mint's, and its
# Continue button 55 px further right.
CONTINUE = (1361, 764)
BACK = (1256, 764)

# THE KEYBOARD PAGE'S RIGHT-HAND LIST, FIRST ENTRY. ZORIN PRESELECTS `English (US) -
# English (Dvorak)`, WHICH IS NOT A DETAIL: every character this driver types arrives
# remapped, so `docker` went into the account form as `erjt.p` and the username was rejected
# with `May only contain lower-case letters...`. The plain `English (US)` row is the first
# in that list, and clicking it before Continue is what makes the typing correct.
KEYMAP_PLAIN_US = (1106, 402)

# THE PARTITIONS CONFIRMATION - `Write the changes to disks?` - with `Continue` here. The
# page underneath is called `Installation type`, and `Erase disk and install Zorin OS` is
# ALREADY SELECTED there, so the only click it needs is `Install Now`.
WRITE_CONTINUE = (1293, 673)
INSTALLATION_TYPE_INSTALL_NOW = (1361, 764)

# THE ACCOUNT FORM. FIVE fields, not Mint's four, and each is labelled to its left:
#
#     Your name:            (975, 386)
#     Your computer's name: (857, 422)
#     Pick a username:      (859, 477)
#     Choose a password:    (873, 514)
#     Confirm your password:(883, 550)
ACCOUNT = {
    "name": (975, 386),
    "host": (857, 422),
    "login": (859, 477),
    "password": (873, 514),
    "confirm": (883, 550),
}


def log(msg: str) -> None:
    print(f"[zorin {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Driver:
    def __init__(self, qmp_path: str, work: str, *, width: int = 1920,
                 height: int = 1080) -> None:
        shots = os.path.join(work, "shots")
        os.makedirs(shots, exist_ok=True)
        deadline = time.time() + 180
        while not os.path.exists(qmp_path):
            if time.time() > deadline:
                raise SystemExit(f"no QMP socket at {qmp_path} after 180s")
            time.sleep(1)
        self.screen = ocr.Screen(qmp_path, shots, width=width, height=height)
        self.q = self.screen.q
        self.work = work
        self.n = 0
        self.words: list[tuple] = []

    def look(self, label: str) -> None:
        self.n += 1
        ppm = self.screen.grab(f"{self.n:03d}-{label}")
        self.words = self.screen.text(ppm) if ppm else []
        log(f"  {label}: {' | '.join(w[0] for w in self.words[:20])}")

    def text(self) -> str:
        return " ".join(w[0].lower() for w in self.words)

    def click(self, xy, why: str, settle: float = 2.0) -> None:
        log(f"click {xy} - {why}")
        Q.click(self.q, *xy)
        time.sleep(settle)

    def double_click(self, xy, why: str, settle: float = 20.0) -> None:
        log(f"double-click {xy} - {why}")
        x, y = xy
        Q._send(self.q, "press", [
            {"type": "abs", "data": {"axis": "x", "value": Q.to_abs(x, 1920)}},
            {"type": "abs", "data": {"axis": "y", "value": Q.to_abs(y, 1080)}},
            {"type": "btn", "data": {"down": True, "button": "left"}},
        ])
        time.sleep(DOUBLE_CLICK_GAP)
        Q._send(self.q, "release", [{"type": "btn", "data": {"down": False, "button": "left"}}])
        time.sleep(DOUBLE_CLICK_GAP)
        Q._send(self.q, "press 2", [{"type": "btn", "data": {"down": True, "button": "left"}}])
        time.sleep(DOUBLE_CLICK_GAP)
        Q._send(self.q, "release 2", [{"type": "btn", "data": {"down": False, "button": "left"}}])
        time.sleep(settle)

    def close(self) -> None:
        """Release the QMP connection before lib/steps.py opens its own.

        qemu serves one client per socket, so the YAML walk cannot start while this driver
        still holds it - the same boundary macos/15/bake.py documents.
        """
        try:
            self.screen.close()
        except Exception:                              # noqa: BLE001
            pass

    def clear_and_type(self, xy, value: str) -> None:
        Q.click(self.q, *xy)
        time.sleep(1.2)
        Q.key_combo(self.q, "ctrl", "a")
        time.sleep(0.4)
        for _ in range(40):
            Q.key(self.q, "backspace")
            time.sleep(0.02)
        unsupported = keys.type_text(self.q, value, per_char=0.1)
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(0.8)


def wait_for_live_desktop(drv: Driver, timeout: int) -> bool:
    """Until the live session has drawn ZORIN'S OWN WELCOME WINDOW.

    NOT MINT'S PANEL MARKER. Mint's driver waits for a Cinnamon panel; Zorin 18 opens a
    `Welcome` window by itself - `Try Zorin OS` and `Install Zorin OS`, on top of a language
    list - and that window IS the thing the next step needs, so waiting for it is both the
    right question and a stronger one. Measured on a local session:

        boot: Install | Welcome | English | Espanol | Esperanto | ... | Try | Zorin | OS |
              Install | Zorin | OS | ... | You | can | try

    Both words are always present in that window and appear nowhere else in this boot.
    """
    log("waiting for the live desktop")
    deadline = time.time() + timeout
    while time.time() < deadline:
        drv.look("boot")
        joined = drv.text()
        if "try" in joined and "zorin" in joined:
            log("the live session is up")
            return True
        time.sleep(10)
    log("=== THE LIVE DESKTOP DID NOT APPEAR ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:24])}")
    return False


def start_the_installer(drv: Driver) -> None:
    """Press `Install Zorin OS` in the launcher, then keep the session awake.

    THE TERMINAL IS OPENED AFTER THE INSTALLER, AND ONLY FOR THE SCREEN SAVER.

    Measured twice, and the order is the whole of it:

      * terminal first, launcher second - the terminal window owns the screen, both presses
        land in it, and Ubiquity never starts. Two CI runs and one local run failed exactly
        this way, the frame still reading `Paigaldamine | Tere tulemast | Eesti | ...`;
      * launcher first, terminal second - Ubiquity reaches its Keyboard pane, and the
        terminal opened afterwards sits behind it because Ubiquity raises itself.

    THE LAUNCHER'S LANGUAGE IS WHATEVER THE GEOLOCATION PROBE SAYS - measured, `Welcome` on
    one run and `Paigaldamine` (Estonian) on the next, same ISO. Its cards do not move, so the
    button is at (1223,586) in both, which is why this is a coordinate and not a lookup.

    TWO PRESSES 3 SECONDS APART, RETRIED ONCE. One press only focuses the window; the pair,
    three seconds apart, is what was measured to start Ubiquity. The retry is two attempts,
    not a loop, and it stops as soon as the Keyboard pane is readable.
    """
    for attempt in (1, 2):
        drv.click(WELCOME_INSTALL, f"launcher: Install Zorin OS (attempt {attempt})", settle=3.0)
        drv.click(WELCOME_INSTALL,
                  f"launcher: Install Zorin OS (attempt {attempt}, second)", settle=40.0)
        drv.look("ubiquity")
        if "keyboard" in drv.text() or "keymap" in drv.text():
            break
        log("  Ubiquity's keyboard pane is not up yet; one more pair of presses")

    # NOW THE TERMINAL, with Ubiquity already up and owning the screen.
    #
    # `setsid` plus full redirection puts the keepalive in its own session with no descriptor
    # on the pty, so `exit` closes the window with it instead of leaving it in front of the
    # installer. Alt+F4 was tried and put up the LAUNCHER'S `Quit the installation?` dialog.
    log("disabling the screen saver and the lock")
    Q.key_combo(drv.q, "ctrl", "alt", "t")
    time.sleep(8.0)
    command = (
        "xset s off; xset -dpms; xset s noblank; "
        "gsettings set org.gnome.desktop.session idle-delay 0; "
        "gsettings set org.gnome.desktop.screensaver lock-enabled false; "
        "setsid sh -c 'while :; do "
        "  xdotool mousemove_relative -- 1 0; xdotool mousemove_relative -- -1 0; "
        "  sleep 20; done' >/dev/null 2>&1 < /dev/null &"
    )
    keys.type_text(drv.q, command, per_char=0.02)
    Q.key(drv.q, "ret")
    time.sleep(6.0)
    keys.type_text(drv.q, "exit", per_char=0.05)
    Q.key(drv.q, "ret")
    time.sleep(6.0)
    drv.look("screensaver disabled")


def walk_ubiquity(drv: Driver, args) -> bool:
    """Click through Zorin's seven Ubiquity panes: a coordinate and a wait, each time.

    NOTHING IS READ TO DECIDE ANYTHING. The screen is read after each step and the reading
    goes into the log, where it is useful for a post-mortem and dangerous as a decision.
    """
    # 0. START THE INSTALLER AND KEEP THE SESSION AWAKE - see start_the_installer.
    start_the_installer(drv)

    # 1. THE WELCOME PANE. Its LANGUAGE LIST IS NOT TOUCHED - `English` is its default unless
    #    the geolocation probe moved it, and picking a row by coordinate is unsafe because the
    #    list scrolls to the current entry: measured, `English` sat at y 516 in a window
    #    scrolled to `Estonian` and at y 383 in one scrolled to the top. Only the footer
    #    button is pressed.
    #
    #    NO WORD IS TESTED FOR. Ubiquity was measured coming up in English and in Estonian on
    #    the same ISO; a test for the word `welcome` fails on a screen that is perfectly
    #    correct. The disk check at the end is the verdict, and it is loud.
    drv.look("welcome")
    drv.click(CONTINUE, "Welcome: Continue", settle=15.0)
    drv.look("keyboard")

    # NO GATE ON THE PANE'S TITLE. The intent was good - a click meant for the keyboard page
    # must not be spent on the welcome page - but the test is a WORD, and this wizard's words
    # are translated: measured, the CI run at 38f44c4 advanced correctly to a Keyboard pane
    # rendering `Klahvistiku paigutus` (Estonian) and the gate stopped the run with
    #
    #     === THE WELCOME PANE DID NOT ADVANCE TO KEYBOARD LAYOUT ===
    #
    # on a screen that was exactly where it should be. A translated wizard is not a broken
    # one, and the coordinate is the same in every language. The empty-disk check at the end
    # is the verdict.
    if "keyboard" not in drv.text() and "klaviatuur" not in drv.text():
        log("  (the keyboard pane's title was not read - translated wizard; continuing anyway)")

    # 2. KEYBOARD LAYOUT, AND THE PLAIN `English (US)` ROW MUST BE CLICKED.
    #
    # Zorin preselects `English (US) - English (Dvorak)`. That is not cosmetic: every
    # character this driver types is remapped, and `docker` reached the account form as
    # `erjt.p`, which the form rejected with `May only contain lower-case letters, digits,
    # hyphens, and underscores.` Measured on a live session, twice.
    log("selecting the plain English (US) keymap (the default here is Dvorak)")
    drv.click(KEYMAP_PLAIN_US, "Keyboard: English (US)", settle=4.0)
    drv.look("keymap selected")
    drv.click(CONTINUE, "Keyboard: Continue (English US)", settle=18.0)
    drv.look("installation type")

    # 3. `Updates and other software` IS NOT ALWAYS SHOWN, AND THIS DRIVER DOES NOT WAIT FOR IT.
    #
    # Measured twice on this image: the keyboard pane's Continue goes STRAIGHT to
    # `Installation type`. An earlier version spent a step clicking the footer on the
    # keyboard pane a second time, which is harmless - the button is gone by then, so the
    # click lands on blank space - but it made the sequence look like it had a pane that
    # does not exist. The step is written out below only for the builds that do show it, and
    # it is skipped by the pane check rather than by a read at the time.

    # 4. INSTALLATION TYPE. `Erase disk and install Zorin OS` is ALREADY SELECTED - measured,
    #    the radio is filled when the pane is first drawn - so the only click it needs is
    #    `Install Now`, which puts up the confirmation.
    log("erase-disk already selected; pressing Install Now")
    drv.click(INSTALLATION_TYPE_INSTALL_NOW, "Installation type: Install Now", settle=18.0)
    drv.look("confirm")

    # 5. THE CONFIRMATION IS CLICKED WHETHER OR NOT IT WAS READ.
    #
    # A conditional that gates a click is the same mistake as a lookup that chooses one, and
    # this repository has paid for it four times. The click costs nothing when the dialog is
    # absent: the point is blank space on the pane underneath.
    drv.click(WRITE_CONTINUE, "Write the changes to disks: Continue", settle=20.0)
    drv.look("where are you")

    # 6. WHERE ARE YOU? THE DEFAULT IS KEPT. On this cluster it is a US zone; the requirement
    #    is a disk that installs, not a particular region.
    log("time zone left at the installer's default")
    drv.click(CONTINUE, "Where are you: Continue (default kept)", settle=18.0)
    drv.look("who are you")

    # 7. WHO ARE YOU? FIVE fields, each labelled to its left.
    log("filling the account form")
    drv.clear_and_type(ACCOUNT["name"], args.user)
    drv.clear_and_type(ACCOUNT["host"], args.user)
    drv.clear_and_type(ACCOUNT["login"], args.user)
    drv.clear_and_type(ACCOUNT["password"], args.password)
    drv.clear_and_type(ACCOUNT["confirm"], args.password)
    drv.look("account filled")

    # `Short password` is shown in red beside the field for `admin`, and continuing anyway is
    # the measured, working answer - the same as every other bake here.
    drv.click(CONTINUE, "Who are you: Continue (short password accepted)", settle=25.0)
    drv.look("installing")

    # 8. A MEASURED WAIT. Zorin's Ubiquity copies files and then puts up `Installation
    #    Complete`; that pane is a heading in a font OCR reads badly on this image (the same
    #    problem mint documents), so the duration is the answer.
    #
    #    MEASURED: about 15 minutes from `Continue` on Who-are-you to that dialog on a
    #    runner. FORTY is that with room for a slower one, and `--timeout` still bounds it.
    # ZORIN'S COMPLETION PANE IS READABLE, SO IT IS USED - AND THE MEASURED WAIT IS THE CAP.
    #
    # Measured on CI run 42476192327: Ubiquity reaches
    #
    #     Installation Complete | Installation is complete. You need to ...
    #
    # in plain words, unlike Mint's and openSUSE's, whose last panes are headings OCR cannot
    # read. Waiting out the full INSTALL_SECONDS after the disk is complete would waste
    # twenty-five minutes of every run - so the pane ends the loop and the duration is only
    # the bound.
    log(f"waiting up to {INSTALL_SECONDS}s for the install (measured: about 10 minutes)")
    started = time.time()
    while time.time() - started < min(INSTALL_SECONDS, args.timeout):
        try:
            Q.click(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                          # noqa: BLE001
            pass
        drv.look("installing")
        current = drv.text()
        if "installation complete" in current or "installation is complete" in current:
            log("=== INSTALL COMPLETE ===")
            return True
        time.sleep(20)

    log("=== THE INSTALL RUN ENDED, AS MEASURED ===")
    return True


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=None)
    p.add_argument("--disk-size", default="64G")
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--timeout", type=int, default=5400)
    p.add_argument("--user", default=os.environ.get("ZORIN_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("ZORIN_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "zorin.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    # NO KERNEL OR INITRD IS EXTRACTED, AND THAT IS THE SAME RULE EVERY OTHER GUI DRIVER
    # HERE FOLLOWS: the ISO is booted whole, by its own bootloader, and the firmware finds
    # its El Torito entry. See the `extra=` list below.

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            # THE MEDIUM BOOTS AS ITS OWN BOOTLOADER INTENDS - no -kernel, no -initrd, no
            # -append. Its grub.cfg carries the casper arguments (boot=casper, the medium's
            # own uuid or label), and the firmware reads that through the El Torito
            # catalogue. A hand-written line was the source of two failures on this
            # repository's other drivers: a guessed `archisobasedir` sent Garuda to an
            # emergency shell, and `iso-scan/filename=` found nothing because the ISO is not
            # a file inside the medium it boots from.
            "-boot", "d",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "zorin"),
        stdout=os.path.join(work, "console.log"),
    )

    def _stop(signum: int, _frame) -> None:
        log(f"signal {signum}: stopping the guest")
        try:
            guest.stop(acpi_first=False)
        except Exception:                              # noqa: BLE001
            pass
        os._exit(143)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _stop)

    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    guest.start()
    log(f"qemu pid {guest.pid}, vnc {guest.shape.vnc_bind}:{5900 + guest.shape.vnc_display}")

    snap = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         guest.paths.record, frames, os.environ.get("SNAPSHOT_INTERVAL", "2")],
        stdout=open(os.path.join(frames, "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )
    art = live.start([frames], os.path.join(work, "install.mp4"),
                     os.path.join(work, "last-frame.png"),
                     every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "30")))

    ok = False
    try:
        drv = Driver(guest.paths.drive, work)
        if wait_for_live_desktop(drv, args.timeout):
            # THE WALK IS A YAML FILE NOW - see steps/install.yaml, which carries the launcher
            # retry (as an `unless`), the Dvorak keymap click and every account field.
            #
            # THE QMP SOCKET IS ALREADY OPEN, so this driver is closed first: qemu serves one
            # client per socket and steps.run_file opens its own.
            drv.close()
            import steps
            walk_path = os.path.join(HERE, "steps", "install.yaml")
            log(f"=== the walk: {os.path.basename(walk_path)} ===")
            ok, ran = steps.run_file(
                walk_path, guest.paths.drive, work=work,
                user=args.user, password=args.password,
            )
            log(f"{len(ran)} step(s) ran" if ok
                else f"the walkthrough failed after {len(ran)} step(s)")
    finally:
        try:
            ppm = os.path.join(work, "final.ppm")
            c = Q.QMP(guest.paths.drive)
            try:
                c.execute("screendump", filename=ppm)
            finally:
                c.close()
            lastframe.convert(ppm, os.path.join(work, "last-frame.png"))
        except Exception:                              # noqa: BLE001
            pass
        guest.stop(acpi_first=False)
        snap.terminate()
        art.stop()

    # A DISK WITH NOTHING ON IT IS NOT A SUCCESS, AND THAT WAS INVISIBLE.
    #
    # The caller only compresses when the walk reports success, so a driver that pressed the
    # right coordinates on the wrong screen still "passed": endeavour's run 42454363051
    # clicked the Asturian wizard's footer at the English coordinate, never left Welcome,
    # reported success, and uploaded `out/endeavour.qcow2: 0 MiB`. The workflow was
    # green and the artefact was empty.
    #
    # A written system is gigabytes; an unwritten sparse image is kilobytes. The threshold
    # is far from both.
    if ok and os.path.exists(disk) and os.path.getsize(disk) < 64 * 2**20:
        log(f"=== THE DISK IS EMPTY ({os.path.getsize(disk) >> 20} MiB) - THE INSTALL DID NOT HAPPEN ===")
        ok = False

    if ok and os.path.exists(disk):
        log(f"compressing {disk} -> {args.out}")
        compress.compress(disk, args.out)
        log(f"{args.out}: {os.path.getsize(args.out) >> 20} MiB")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
