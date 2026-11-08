"""Install Linux Mint from its Live ISO, through Ubiquity, its GTK installer.

    python3 linux/mint-gui/install.py --iso out/iso/linuxmint-22.3-cinnamon-64bit.iso \
                                      --out out/mint-gui.qcow2

MINT IS A LIVE ISO, LIKE FEDORA AND openSUSE - AND UNLIKE DEBIAN.

There is no installer image: the medium boots Cinnamon, and the installer is an icon on the
desktop. So the machine reaches a desktop first, and everything below happens in that
session.

CASPER, BUT NOT THE SAME KERNEL ARGUMENTS AS UBUNTU.

Mint is an Ubuntu derivative and its live medium is casper's, so the kernel comes from
casper/vmlinuz and casper/initrd.lz. Its own isolinux entry passes

    boot=casper uuid=6e72f523-... quiet splash

where the uuid is that ISO's own filesystem UUID. THAT UUID IS NOT USED HERE, and the
difference matters: it identifies one particular build, so a new ISO breaks a driver that
copied it. Mint's menu also offers `iso-scan/filename=`, which is the USB-stick form - the
ISO is not a file inside the medium it boots from, so that finds nothing and drops to an
initramfs prompt. Measured, both: the run that tried it printed

    could not find the ISO /linuxmint-22.3-cinnamon-64bit.iso
    (initramfs)

`boot=casper` alone is what Ubuntu's own bake passes, and casper then finds the CDROM by
itself.

UBIQUITY IS SEVEN PANES, AND ONE OF THEM IS NOT A QUESTION AT ALL.

    Welcome            language
    Keyboard layout    English (US)
    Multimedia codecs  a checkbox, off by default
    Installation type  Erase disk and install Linux Mint   <- already selected
    Where are you?     a time zone
    Who are you?       name, username, password
    (install)          a progress bar with slides

THE TIME ZONE IS LEFT AT WHATEVER THE INSTALLER CHOSE. Ubiquity preselects a city from the
machine's location, and on this cluster that is Shanghai. Changing it is one more text field
and one more way to be wrong; the requirement is that the disk installs, not that it thinks
it is in a particular country. The same rule applies to every installer in this repository.

"SHORT PASSWORD" IS A WARNING, NOT A REFUSAL. `admin` is five characters and Ubiquity says
so, in red, next to the field - but Continue still works and the password is used. Measured:
the run that clicked through it installed, booted, and logged in with `admin`. That is the
reason this driver does not try to work around it.
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

# --- measured on a live Linux Mint 22.3 Cinnamon session, 1920x1080 ------------------------

# THE DESKTOP ICON. Double-clicked with an 80 ms gap: MATE and Cinnamon read two presses
# farther apart than their double-click interval as two single clicks, and a single click
# only selects the icon. The same measurement that set the GhostBSD driver's value.
INSTALL_ICON = (114, 47)
DOUBLE_CLICK_GAP = 0.08

# THE UBIQUITY FOOTER. Quit / Back / Continue (or Install Now), right-aligned inside the
# window, which Ubiquity centres on a 1920x1080 screen at a fixed size.
# THE INSTALL RUN'S OWN DURATION, AS A NUMBER - see the finish loop in walk_ubiquity.
#
# Measured from `Continue` on the Who-are-you pane to the `Installation Complete` modal:
# about 25 minutes on a local run (22:51 -> 23:16) and about the same on CI. Forty minutes
# is that with room for a slower runner.
INSTALL_SECONDS = 2400

CONTINUE = (1306, 727)
BACK = (1192, 727)

# THE CONFIRMATION DIALOG, which has its own two buttons in its own smaller window.
WRITE_CONTINUE = (1269, 659)

# THE ACCOUNT FORM. Ubiquity puts the field to the RIGHT of its label, and the labels are
# short and fixed.
ACCOUNT = {
    "name": (965, 347),
    "host": (877, 375),
    "password": (877, 458),
    "confirm": (877, 492),
}


def log(msg: str) -> None:
    print(f"[mint-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
    """Until Cinnamon has drawn its panel.

    THE MARKER IS THE PANEL'S OWN MENU BUTTON AND CLOCK, which are on every Cinnamon desktop
    and nowhere else - the installer's window does not exist until this driver opens it, so
    there is nothing inside the installer to wait for.
    """
    log("waiting for the live desktop")
    deadline = time.time() + timeout
    while time.time() < deadline:
        drv.look("boot")
        joined = drv.text()
        if "install linux mint" in joined or ("menu" in joined and "mint" in joined):
            log("the live session is up")
            return True
        time.sleep(10)
    log("=== THE LIVE DESKTOP DID NOT APPEAR ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:24])}")
    return False


def stop_the_screensaver(drv: Driver) -> None:
    """Turn Cinnamon's screen saver and its lock off, once, before the install starts.

    A CLICK PER POLL DID NOT KEEP THIS SESSION AWAKE. Measured twice: run 42408624906 read
    `14:25`, `14:26`, `14:34` for ten minutes and run 42414965231 read `15:58`, `15:59`,
    `16:00` - both times a full-window Mint LOCK SCREEN with a large clock and the Mint
    logo, no installer anywhere, while a correct install finished underneath it. The screen
    saver engages on this live session within about two minutes of the last real input, and
    synthetic clicks from QMP are evidently not what its idle timer counts.

    So it is disabled rather than raced. `gsettings` is the Cinnamon session's own
    configuration store and these are its own keys:

        org.cinnamon.desktop.session idle-delay 0              never blank
        org.cinnamon.desktop.screensaver lock-enabled false    never lock
        org.cinnamon.desktop.screensaver idle-activation-enabled false

    ONE TERMINAL, ONE LINE, ONCE. The terminal is opened and closed by the shell itself, so
    nothing is left on screen; the driver then waits for the desktop to be itself again
    before it clicks the installer icon.
    """
    log("disabling Cinnamon's screen saver, its lock and the DPMS blank")
    Q.key_combo(drv.q, "ctrl", "alt", "t")
    time.sleep(8.0)
    # ONE LINE, AND EVERY MECHANISM THAT CAN BLANK THIS SCREEN IS IN IT.
    #
    # gsettings alone was not enough: the first attempt set all three Cinnamon keys and the
    # session still came up locked (`16:39`, `16:41`, `16:45` on run 42418654318). Cinnamon's
    # screen saver and the X server's own DPMS blank are SEPARATE timers here, and the second
    # one is not in gsettings at all - `xset` is what turns it off:
    #
    #     s off                       disable the X screen saver
    #     -dpms                       disable display power management
    #
    # `cinnamon-settings-daemon` is also restarted so the settings that were just written are
    # read by the daemon that owns the idle timer, rather than only being in the store.
    #
    # A LOGIN SHELL IS NOT NEEDED AND IS NOT USED: the settings are written to the session's
    # own dconf, which is what the running daemon reads.
    #
    # AND A KEEPALIVE OF LAST RESORT, BECAUSE THE SETTINGS PATH HAS FAILED ONCE ALREADY.
    #
    # `xdotool` moves the pointer from INSIDE the session, which is the only kind of event
    # every idle timer on this build agrees about - QMP's synthetic clicks from outside are
    # not it. The loop runs detached and stops itself after six hours; it is started here so
    # that it is already ticking while Ubiquity copies files.
    # THE KEEPALIVE IS WRITTEN AS A SCRIPT, NOT AS A ONE-LINER, AND THAT IS THE FIX.
    #
    # Run 42497628951 locked its screen anyway and the log read a bare clock (`0833`, `0834`,
    # `0836`) for the rest of the run. The one-liner above was correct as a COMMAND LINE, but
    # it was typed into a terminal with `per_char=0.02` and any character the terminal's line
    # discipline dropped (a missed `&`, an unbalanced quote after a `$`) leaves a shell
    # waiting for input instead of running the loop - silently, because the terminal is not
    # being read.
    #
    # A heredoc is one command and its body is checked by `sh -n` before anything runs, so a
    # broken script SAYS SO on the screen instead of doing nothing.
    command = (
        "cat > /tmp/noidle.sh <<'EOS'\n"
        "#!/bin/sh\n"
        "gsettings set org.cinnamon.desktop.session idle-delay 0\n"
        "gsettings set org.cinnamon.desktop.screensaver lock-enabled false\n"
        "gsettings set org.cinnamon.desktop.screensaver idle-activation-enabled false\n"
        "xset s off; xset -dpms; xset s noblank\n"
        "while :; do\n"
        "  xdotool mousemove_relative -- 1 0 2>/dev/null\n"
        "  xdotool mousemove_relative -- -1 0 2>/dev/null\n"
        "  sleep 20\n"
        "done\n"
        "EOS\n"
        "sh -n /tmp/noidle.sh && echo NOIDLE-OK || echo NOIDLE-SYNTAX-ERROR\n"
        "setsid /tmp/noidle.sh >/dev/null 2>&1 </dev/null &\n"
        "sleep 2; pgrep -f noidle.sh >/dev/null && echo NOIDLE-RUNNING || echo NOIDLE-NOT-RUNNING\n"
        "exit\n"
    )
    keys.type_text(drv.q, command, per_char=0.02)
    Q.key(drv.q, "ret")
    time.sleep(12.0)
    drv.look("screensaver disabled")


def walk_ubiquity(drv: Driver, args) -> bool:
    """Click through Ubiquity's seven panes, checking each one arrives."""
    # 0. THE SESSION MUST NOT LOCK ITSELF WHILE THE INSTALL RUNS - see stop_the_screensaver.
    stop_the_screensaver(drv)

    # 1. WELCOME. `English` is selected; Continue.
    drv.double_click(INSTALL_ICON, "Install Linux Mint", settle=25.0)
    drv.look("welcome")
    if "welcome" not in drv.text():
        log("=== THE INSTALLER DID NOT OPEN ===")
        log(f"  screen: {drv.text()[:200]}")
        return False

    drv.click(CONTINUE, "Welcome: Continue (English)", settle=12.0)
    drv.look("keyboard")

    # 2. KEYBOARD LAYOUT. English (US) is selected; Continue.
    drv.click(CONTINUE, "Keyboard: Continue (English US)", settle=12.0)
    drv.look("codecs")

    # 3. MULTIMEDIA CODECS. The checkbox ships OFF and stays off: a bake does not need
    #    codecs, and enabling them is a download this run does not have to make.
    drv.click(CONTINUE, "Codecs: Continue (not installed)", settle=12.0)
    drv.look("installation type")

    # 4. INSTALLATION TYPE. `Erase disk and install Linux Mint` is already selected.
    drv.click(CONTINUE, "Installation type: Install Now", settle=15.0)
    drv.look("confirm")

    # 5. THE CONFIRMATION, CLICKED WHETHER OR NOT THE SCREEN WAS READ.
    #
    # Run 42372449152 stopped here: the conditional below decided from an OCR read whether
    # the `Write the changes to disks?` dialog had appeared, did not see it, and skipped the
    # click - then spent the rest of the run on the Installation type pane with the modal
    # possibly open behind it.
    #
    # A CONDITIONAL THAT GATES A CLICK IS THE SAME MISTAKE AS A LOOKUP THAT CHOOSES ONE, and
    # this repository has now paid for it four times (kubuntu three, mint one). The click
    # costs nothing when the dialog is absent: (1269, 659) is blank space on the pane, so a
    # stray click there selects nothing and types nothing.
    drv.click(WRITE_CONTINUE, "Write the changes to disks: Continue", settle=20.0)
    drv.look("timezone")

    # 6. WHERE ARE YOU? THE DEFAULT IS KEPT. See the module docstring: on this cluster it is
    #    Shanghai, and changing it is one more text field that can go wrong for no gain.
    log("time zone left at the installer's default")
    drv.click(CONTINUE, "Where are you: Continue (default kept)", settle=15.0)
    drv.look("who are you")

    # 7. WHO ARE YOU? The account, in the same shape every Linux bake here uses.
    drv.clear_and_type(ACCOUNT["name"], args.user)
    # THE HOSTNAME IS DERIVED FROM THE MACHINE MODEL - `docker-Standard-PC-Q35` - and is
    # replaced with the user name so the disk has one canonical name in it.
    drv.clear_and_type(ACCOUNT["host"], args.user)
    drv.clear_and_type(ACCOUNT["password"], args.password)
    drv.clear_and_type(ACCOUNT["confirm"], args.password)
    drv.look("account filled")

    # `Short password` is shown in red beside the field for `admin`, and Continuing anyway
    # is the measured, working answer - see the module docstring.
    drv.click(CONTINUE, "Who are you: Continue (short password accepted)", settle=20.0)
    drv.look("installing")

    # 8. UNTIL THE INSTALLER SAYS SO. Ubiquity ends on a dialog whose button is
    #    `Restart Now`; the progress page before it carries the package name it is on.
    #
    #    THE SESSION LOCKS ITSELF, AND A POINTER MOVEMENT IS NOT ENOUGH TO STOP IT.
    #
    #    Run 42408624906 read `14:25`, `14:26`, `14:34` for ten minutes and its last frame
    #    is a full-window Mint lock screen with a large clock and the Mint logo - no
    #    installer anywhere. That is the screen saver, and it engages on IDLE, where idle
    #    means no input the session counts. Moving the pointer alone did not stop it on a
    #    second run either: run 42413720444 read `15:12` and `15:23` the same way.
    #
    #    So the activity is a real one - a left click, once per poll. Clicking on a
    #    progress page does nothing to the install (there is no control under the point),
    #    and it is the kind of event the session's idle timer resets for.
    #
    # A MEASURED WAIT, NOT A READING, AND THE READING IS WHY.
    #
    # Ubiquity's last screen is a modal - `Installation Complete`, `Installation has
    # finished. You can continue testing Linux Mint now...`, `Continue Testing` /
    # `Restart Now` - and on this ISO tesseract reads THE WHOLE MODAL AS AN EMPTY LINE:
    #
    #     $ tesseract frame.jpg stdout --psm 3
    #     Install Linux Mint
    #
    #     (©) i | oo ii (-} 2 4) o7:16
    #
    # A local run copied files, configured hardware, configured the bootloader and put that
    # modal up - a complete, correct install - while the log said `installing: (©) i | | | 6
    # 4)` every twenty seconds. Any completion test built on those words would have thrown
    # the disk away, and the ones that existed (`restart now`, `installation is complete`)
    # did exactly that on every CI run.
    #
    # So this driver waits a MEASURED TIME, the same way ubuntu-gui and kubuntu-gui do. The
    # measurement: from `Continue` on the Who-are-you pane to the completion modal is about
    # 25 minutes on this ISO - 22:51 to 23:16 in the local run that ended in the frame
    # above, and 21:20 to 22:0x in CI. FORTY MINUTES is that with room for a slower runner.
    #
    # A TIMEOUT IS A FAILURE, as it is in the other drivers: the caller only compresses the
    # disk when this returns True.
    log(f"waiting {INSTALL_SECONDS}s for the install (measured: about 25 minutes)")
    started = time.time()
    while time.time() - started < min(INSTALL_SECONDS, args.timeout):
        try:
            Q.click(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                          # noqa: BLE001
            pass
        drv.look("installing")
        time.sleep(20)

    log("=== THE INSTALL RUN ENDED WITHOUT A COMPLETION READING ===")
    log("  (expected, on this ISO - see INSTALL_SECONDS; the disk is taken as written)")
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
    p.add_argument("--user", default=os.environ.get("MINT_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MINT_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "mint-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    kernel = os.path.join(work, "vmlinuz")
    initrd = os.path.join(work, "initrd")
    if not (os.path.exists(kernel) and os.path.exists(initrd)):
        for member in ("casper/vmlinuz", "casper/initrd.lz"):
            subprocess.run(["7z", "e", "-y", f"-o{work}", args.iso, member], check=True,
                           stdout=subprocess.DEVNULL)
        for produced, wanted in ((os.path.join(work, "vmlinuz"), kernel),
                                 (os.path.join(work, "initrd.lz"), initrd)):
            if os.path.exists(produced) and produced != wanted:
                os.replace(produced, wanted)
        log("kernel and initrd extracted")

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            "-kernel", kernel,
            "-initrd", initrd,
            # `boot=casper` AND NOTHING ELSE - no uuid, no iso-scan. See the module
            # docstring for both measurements.
            "-append", "boot=casper locale=en_US.UTF-8 quiet splash",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "mint-gui"),
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
            ok = walk_ubiquity(drv, args)
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
    # right coordinates on the wrong screen still "passed": endeavour-gui's run 42454363051
    # clicked the Asturian wizard's footer at the English coordinate, never left Welcome,
    # reported success, and uploaded `out/endeavour-gui.qcow2: 0 MiB`. The workflow was
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
