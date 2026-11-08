"""Install Pop!_OS from its Live ISO, through `pop-os-installer`, its GTK installer.

    python3 linux/popos-gui/install.py --iso out/iso/pop-os_24.04_amd64_intel_9.iso \
                                       --out out/popos-gui.qcow2

POP!_OS HAS AN INSTALLER OF ITS OWN - NOT CALAMARES, NOT UBIQUITY, NOT ANACONDA.

It is `pop-os-installer`, a GTK application that OPENS BY ITSELF on the live desktop, and its
window is a fixed-size card at (485,155)..(1435,860) with the same footer position on every
pane: Next / Select / Erase and Install at (1367, 817), Back at (1279, 817).

THE FLOW, measured pane by pane on a live Pop!_OS 24.04 session at 1920x1080:

    Select a Language      English preselected                      Select (1367,817)
    Languages (variants)   United States preselected                Select
    Keyboard Layout        English (US) preselected                 Select
    Keyboard (variants)    Default preselected - NOT Dvorak         Select
    Install                Clean Install / Custom (Advanced)        click Clean Install, Next
    Select a drive         `Virtio Block Device /dev/vda 64.0 GiB`  click the row, Erase and Install (1334,817)
    Create User Account     Full Name, User Name                    Next
    Create User Account     password + confirm                      Next
    Drive Encryption       Don't Encrypt / Set Password / Encrypt   Don't Encrypt (1091,817)
    Installing             Extracting Files (N%)

TWO THINGS THAT ARE NOT DEFAULTS, AND BOTH ARE HANDLED.

1. `Clean Install` IS A CHOICE, NOT THE DEFAULT. The Install pane offers `Clean Install` and
   `Custom (Advanced)` as two cards with neither selected, so the footer button does nothing
   until one is clicked. `Clean Install` is the right answer: it erases the drive this guest
   owns (`/dev/vda`, 64.0 GiB) and installs a fresh copy.

2. THE DRIVE MUST BE SELECTED before `Erase and Install` is enabled - measured, the button is
   grey until the `Virtio Block Device` row is clicked.

THE ACCOUNT IS TWO PANES. The first asks `Full Name` and `User Name`; the second asks for a
password twice. `admin` is short enough that the installer prints
`The password is shorter than 8 characters` beside the field, and continues anyway - the same
warning every other bake here accepts, and the same password.

NO ENCRYPTION. `Don't Encrypt` is clicked deliberately: an encrypted disk needs a passphrase
at boot, which is a second thing that can fail on a machine nobody is watching, and the bake's
question is whether the installer works, not whether the disk is confidential. The pane
defaults to `Encrypt`, so this is not a no-op.

THE MEDIUM IS BOOTED WHOLE - no -kernel, no -initrd, no -append, `-boot d` - the rule every GUI
driver here follows. OVMF is attached by lib/uefi.py.
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

# --- MEASURED ON A LIVE POP!_OS 24.04 SESSION, 1920x1080 --------------------------------
#
# THE WINDOW IS A FIXED-SIZE CARD AND ITS FOOTER DOES NOT MOVE. Measured on every pane:
#
#     Next / Select / Erase and Install      (1367, 817)
#     Back                                   (1279, 817)
#
# The installer OPENS BY ITSELF on the live desktop, so there is no launcher to press.

SELECT = (1367, 817)
BACK = (1279, 817)

# THE INSTALL PANE'S TWO CARDS. Neither is selected when the pane is drawn, so the footer
# button is dead until one is clicked. `Clean Install` erases the drive and installs fresh.
CLEAN_INSTALL = (1180, 456)

# THE DRIVE ROW on `Select a drive`, and the button that starts the write.
DRIVE_ROW = (1150, 500)
ERASE_AND_INSTALL = (1334, 817)

# THE ACCOUNT, TWO PANES. The first is Full Name and User Name; the second is the password
# twice. Measured on both.
ACCOUNT_NAME = {
    "full name": (1188, 531),
    "user name": (1188, 592),
}

# THE FULL NAME IS NOT THE USER NAME, AND THE INSTALLER ENFORCES IT.
#
# Measured: with both fields holding `docker` the footer's `Next` stays GREY and does nothing -
# three presses in a run at 9d4e83a left the pane unchanged and then lost the window. Typing
# `Docker User` instead makes the installer derive `dockeruser` for the user name and the
# button turns GREEN at once.
#
# So the two are different strings here: the display name is `Docker User` and the login is
# `docker`, which is the account every other image in this repository carries.
ACCOUNT_PASSWORD = {
    "password": (1188, 440),
    "confirm": (1188, 546),
}

# DRIVE ENCRYPTION. The pane DEFAULTS TO `Encrypt`; this driver chooses `Don't Encrypt`.
DONT_ENCRYPT = (1081, 808)

# THE LIVE MEDIUM'S BOOT TIME. Measured: about 100 s on this image.
# MEASURED ON A RUNNER, WHICH IS SLOWER THAN A LOCAL SESSION: the desktop drew at about
# 210 s and the installer a few seconds later, against about 100 s locally. 300 s is that with
# room, and the wait is bounded by --timeout as well.
DESKTOP_SECONDS = 300

# THE INSTALL RUN. Measured: about 12 minutes from `Don't Encrypt` to the completion pane.
INSTALL_SECONDS = 2400


def log(msg: str) -> None:
    print(f"[popos-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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


def wait_for_installer(drv: Driver, timeout: int) -> bool:
    """Until `pop-os-installer` has drawn its language pane.

    IT OPENS BY ITSELF, so the wait is for the WINDOW rather than for a click. Measured: the
    desktop arrives about 100 s after qemu starts and the installer a few seconds later.
    """
    log(f"waiting {DESKTOP_SECONDS}s for the live desktop and the installer")
    deadline = time.time() + min(DESKTOP_SECONDS, timeout)
    while time.time() < deadline:
        drv.look("boot")
        joined = drv.text()
        # THE INSTALLER'S OWN TITLE, OR ITS FIRST PANE'S HEADING. The window title is
        # `Install Pop!_OS 24.04 LTS`; the pane reads `Select a Language`. Either is enough,
        # and a translated build still carries the title.
        if ("select a language" in joined or "install pop" in joined
                or "pop!_os" in joined):
            log("the installer is up")
            return True
        time.sleep(15)
    log("=== THE INSTALLER DID NOT APPEAR ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:20])}")
    return False


def walk_installer(drv: Driver, args) -> bool:
    """Click through `pop-os-installer`: a measured coordinate and a measured wait.

    NOTHING IS READ TO DECIDE ANYTHING. Every step is a number in this file and a duration;
    the screen is read afterwards and the reading goes into the log. The window is a
    fixed-size card, so one footer coordinate serves every pane.
    """
    # 1-4. THE FOUR SELECTION PANES, EACH WITH ITS DEFAULT ALREADY RIGHT:
    #
    #     Select a Language      `English`
    #     Languages (variants)   `United States`
    #     Keyboard Layout        `English (US)`
    #     Keyboard (variants)    `Default`   - NOT Dvorak, unlike Zorin's Ubiquity
    for pane in ("language", "language variant", "keyboard layout", "keyboard variant"):
        drv.click(SELECT, f"{pane}: Select", settle=18.0)
        drv.look(f"after {pane}")

    # 5. INSTALL. `Clean Install` must be clicked before the footer works: neither card is
    #    selected when the pane is drawn.
    log("choosing Clean Install (erases this guest's own drive)")
    drv.click(CLEAN_INSTALL, "Install: Clean Install", settle=6.0)
    drv.look("clean install selected")
    drv.click(SELECT, "Install: Next", settle=22.0)
    drv.look("select a drive")

    # 6. SELECT A DRIVE, then `Erase and Install`. The button is grey until the row is
    #    clicked - measured.
    log("selecting the guest's own virtio drive")
    drv.click(DRIVE_ROW, "Select a drive: Virtio Block Device", settle=6.0)
    drv.look("drive selected")
    drv.click(ERASE_AND_INSTALL, "Erase and Install", settle=30.0)
    drv.look("create user account")

    # 7. THE ACCOUNT, PANE ONE. `Full Name` COPIES INTO `User Name` AS IT IS TYPED, so both
    #    are set explicitly - measured: typing the name into both without clearing left
    #    `User Name` reading `dockerdocker`.
    # CLICK AND TYPE, for the same reason as the password pane: this installer's pages put
    # their entries close to their artwork controls, and a `ctrl+a` + forty BackSpaces lands
    # on the wrong one. Both fields start empty. `Full Name` copies into `User Name` as it is
    # typed, so `User Name` is emptied by hand and typed once - measured: without that,
    # `User Name` read `dockerdocker`.
    # THE DISPLAY NAME AND THE LOGIN ARE DIFFERENT STRINGS - see ACCOUNT_NAME's comment.
    # `Full Name` is what a person is called; `User Name` is the login. Typing the login into
    # both leaves `Next` grey.
    display_name = args.user.capitalize() + " User"
    log(f"filling the account name (display {display_name!r}, login {args.user!r})")
    Q.click(drv.q, *ACCOUNT_NAME["full name"])
    time.sleep(1.5)
    keys.type_text(drv.q, display_name, per_char=0.09)
    time.sleep(2.0)
    Q.click(drv.q, *ACCOUNT_NAME["user name"])
    time.sleep(1.5)
    for _ in range(40):
        Q.key(drv.q, "backspace")
        time.sleep(0.02)
    keys.type_text(drv.q, args.user, per_char=0.11)
    time.sleep(1.5)
    drv.look("account name filled")
    drv.click(SELECT, "Create User Account: Next", settle=22.0)
    drv.look("create user account password")

    # 8. THE ACCOUNT, PANE TWO: the password twice. `admin` draws
    #    `The password is shorter than 8 characters` beside the field, and the same footer
    #    button continues - the warning every other bake here accepts.
    # PLAIN CLICK, THEN TYPE - no `ctrl+a`, no forty backspaces.
    #
    # THIS PAGE IS NOT LIKE THE OTHERS. Measured on run at 14c896f: after typing the
    # password the frame read
    #
    #     Choose | Icon | Recent | Q | admin... | Home | Desktop | Other Locations
    #
    # - a GTK FILE CHOOSER, opened by a click that landed on this pane's artwork picker
    # rather than on the entry. The generic clear-then-type helper clicks the field, selects
    # all, and sends forty BackSpaces; on a page whose entries and artwork sit close
    # together, one of those lands on the wrong control and the installer is covered.
    #
    # The fields start EMPTY on this pane, so nothing needs clearing. A click and the text is
    # exactly what a live session did, and that session installed 3.58 GiB.
    # TAB FROM THE PREVIOUS PANE'S LAST FIELD, RATHER THAN CLICKING THIS PANE'S ENTRIES.
    #
    # MEASURED FAILURE: pressing (1188,440) here opened `Choose Icon` - a GTK file chooser -
    # and the password went into ITS search box (`adminadmin` in the frame). The coordinate is
    # right on a pane that has just been drawn; this driver arrives at it after a `look()`,
    # and the pane scrolls its content up when the password warning appears, which moves the
    # entries under the pointer.
    #
    # THE KEYBOARD REACHES THEM REGARDLESS: the installer keeps the focus in its form, so Tab
    # from the field that was last typed into walks the remaining controls in order - password,
    # confirm, then the footer's Next. The two entries start empty.
    log("filling the account password (the entries start empty)")
    # ESC FIRST, ALWAYS. If a press on this pane lands on the artwork picker - measured, a
    # 12-pixel error is enough - a GTK file chooser opens over the installer and every later
    # keystroke goes into ITS search box. One Esc closes it, and it is a no-op on a pane with
    # no dialog open.
    Q.key(drv.q, "esc")
    time.sleep(1.5)
    for label, spot in (("password", ACCOUNT_PASSWORD["password"]),
                        ("confirm", ACCOUNT_PASSWORD["confirm"])):
        Q.click(drv.q, *spot)
        time.sleep(1.5)
        unsupported = keys.type_text(drv.q, args.password, per_char=0.12)
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(1.2)
    drv.look("password filled")
    drv.click(SELECT, "Create User Account: Next", settle=25.0)
    drv.look("drive encryption")

    # TWO PRESSES OF `Next`, BECAUSE THE FIRST CAN LAND WHILE THE PANE IS STILL SETTLING.
    #
    # Measured: the encryption pane is the ONLY pane whose footer is not `Next` - it carries
    # `Don't Encrypt` / `Set Password` / `Encrypt`. So on the pane before it, `Next` is at
    # (1367,817); on it, that coordinate is inside `Encrypt`... which is exactly the default
    # the driver does not want either.
    #
    # THE SECOND PRESS IS THEREFORE SAFE EITHER WAY, and that is the point: if the first did
    # not take, the second advances from `Create User Account`; if it did take, the second
    # lands on `Encrypt`, which would encrypt the disk - NOT acceptable.
    #
    # So the second press is exchanged for WAITING instead: one long settle, and then the
    # encryption buttons are pressed, with `Don't Encrypt` chosen in the middle of its own
    # button rather than at its edge (measured: the three buttons span x 1020..1420 and
    # `Don't Encrypt` is the 1020..1165 one, so 1091 is its centre).
    # 9. DRIVE ENCRYPTION. THE PANE DEFAULTS TO `Encrypt`, and this chooses `Don't Encrypt`:
    #    an encrypted disk needs a passphrase at boot, one more thing that can fail on a
    #    machine nobody is watching.
    #
    # THE COORDINATE IS THE ONE A LIVE SESSION PROVED. (1091, 817) is the centre of the
    # left-hand button of the three - measured across the row at x 1020..1420 in a window at
    # (485,155)..(1435,860) - and pressing it there started `Extracting Files (18%)` and grew
    # the disk to 3.58 GiB.
    #
    # WHAT DID NOT WORK, AND WHY THIS NOW WAITS FIRST: run 42483315092 pressed the same
    # coordinate 40 s after the password pane and the installer closed; run 42484716882 tried
    # Tab / Shift+Tab / Return and it closed too. Both times the pane had only just been
    # asked for, and this pane's rows are drawn after its content - the `The password is
    # shorter than 8 characters` note from the page before is drawn ABOVE the button row and
    # moves it while the window is still settling. A live session that took a frame first and
    # then pressed worked on the first try.
    #
    # So one long settle, then the press. `encrypt` in the reading is only logged: if the pane
    # is not up the press misses and the run will say so at the empty-disk check.
    # THE ENCRYPTION PANE'S BUTTONS ARE AT THEIR OWN COORDINATES - measured on a live pane:
    #
    #     Don't Encrypt  (1081, 808)      Set Password  (1213, 808)      Encrypt  (1347, 808)
    #
    # The pane defaults to `Encrypt`, so `Don't Encrypt` is the press that matters: an
    # encrypted disk needs a passphrase at boot, one more thing that can fail on a machine
    # nobody is watching.
    log("choosing `Don't Encrypt` (the pane defaults to Encrypt)")
    drv.click(DONT_ENCRYPT, "Drive Encryption: Don't Encrypt", settle=35.0)
    drv.look("installing")

    # 10. A MEASURED WAIT. The progress pane shows `Extracting Files (N%)`; the percentage is
    #     the only text on it, and a completed disk must not depend on reading it.
    #
    #     MEASURED: about 12 minutes. FORTY is that with room for a slower runner.
    log(f"waiting up to {INSTALL_SECONDS}s for the install (measured: about 12 minutes)")
    started = time.time()
    while time.time() - started < min(INSTALL_SECONDS, args.timeout):
        try:
            Q.click(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                          # noqa: BLE001
            pass
        drv.look("installing")
        current = drv.text()
        if ("installation complete" in current or "install complete" in current
                or "restart" in current or "reboot" in current):
            log("=== INSTALL COMPLETE ===")
            return True
        time.sleep(20)

    log("=== THE INSTALLER NEVER REPORTED COMPLETION ===")
    log(f"  last screen: {drv.text()[:200]}")
    return False


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=None)
    p.add_argument("--disk-size", default="64G")
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--timeout", type=int, default=5400)
    p.add_argument("--user", default=os.environ.get("POPOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("POPOS_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "popos-gui.qcow2")
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
            work, "popos-gui"),
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
        if wait_for_installer(drv, args.timeout):
            ok = walk_installer(drv, args)
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
