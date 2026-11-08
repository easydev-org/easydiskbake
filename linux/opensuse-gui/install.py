"""Install openSUSE Tumbleweed from its KDE Live ISO, through YaST2's graphical installer.

    python3 linux/opensuse-gui/install.py --iso out/iso/openSUSE-...-Media.iso \
                                         --out out/opensuse-gui.qcow2

YAST2, NOT CALAMARES, AND THE DIFFERENCE COST A WRONG GUESS.

openSUSE's installer is YaST2. Calamares is not involved anywhere, and the two are not
similar: Calamares is a linear wizard with a footer, YaST2 is a single window with a
navigation list down the left that can be entered at any point. This driver was written
against a guess and corrected against a frame, which is why the docstring says so.

THE INSTALLER IS ONE DESKTOP ICON AND IT RUNS AS ROOT.

The live session's icon is

    /usr/share/applications/live-installation.desktop
    Exec=kdesu -u root /usr/sbin/start-install.sh

SO IT HAS TO BE STARTED THROUGH kdesu, AND THIS IS THE PART THAT MATTERS.

The first attempt ran `/usr/sbin/start-install.sh` from a terminal and YaST2 came up in
TEXT mode with

    Your computer does not fulfill all requirements for a graphical installation.
    There is either less than 96 MB memory or the X server could not be started.

which is YaST2's honest way of saying it had no DISPLAY it trusted. The X server was
running the whole time - it was that shell, started from a terminal that had been opened by
the driver rather than by the session, that carried no way to reach it. Double-clicking the
icon goes through kdesu and the same binary opens graphically.

TWO THINGS ON THIS WIZARD ARE NOT DEFAULTS, AND BOTH ARE CHECKED.

1. System Role HAS NO SELECTION UNTIL ONE IS CLICKED. The pane lists five roles and
   `Desktop with KDE Plasma` looks highlighted, but Continue answers

       Select one of the available roles to continue.

   until the radio button itself is clicked. The highlight is the row the mouse is over.

2. THE WEAK-PASSWORD DIALOG ANSWERS TO THE KEYBOARD, NOT TO THE MOUSE. `admin` trips
   YaST2's strength check, which asks `Really use this password?` in a modal. Clicking its
   `Yes` did nothing twice in a row - measured - while Return accepted it, because Yes is
   the default button.
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

# --- measured on a live openSUSE Tumbleweed KDE session, 1920x1080 -------------------------

INSTALL_ICON = (267, 34)
DOUBLE_CLICK_GAP = 0.08

# THE LIVE MEDIUM'S BOOT TIME, AS A NUMBER. Measured: about 180 s from qemu start to a
# clickable installer icon on this image. The wait is longer than that - see
# wait_for_live_desktop for why a reading cannot replace it.
DESKTOP_SECONDS = 240

# YAST2'S FOOTER. Back / Next (or Install) at the bottom right of its window.
NEXT = (1596, 859)
BACK = (1517, 859)

# `Online Repositories` asks whether to activate the network repositories. No: an install
# from the medium alone is reproducible, and the driver has no reason to pull an update
# stream whose contents change daily.
ONLINE_REPOS_NO = (1004, 614)

# SYSTEM ROLE'S FIRST RADIO BUTTON - the KDE Plasma row. Its x is the circle, not the label.
ROLE_KDE_PLASMA = (588, 333)

# THE LOCAL USER FORM - FOUR FIELDS, NOT THREE.
#
# `User's Full Name`, `Username`, `Password`, `Confirm Password`, measured at 1920x1080 on a
# live Tumbleweed session. THE USERNAME FIELD IS NOT OPTIONAL AND WAS MISSING FROM THIS
# DRIVER: the page draws it between the full name and the password, and leaving it empty
# disables Next - the install cannot be started at all. The three-field version came from a
# frame of an older build and was never re-measured after `Username` was added.
USER_FULL_NAME = (1106, 418)
USER_NAME = (1106, 483)
USER_PASSWORD = (1106, 548)
USER_CONFIRM = (1106, 614)

# THE FINAL BUTTON is `Install`, at the same place Next has been all along.
# THE INSTALL RUN'S OWN DURATION, AS A NUMBER - see step 8 of walk_yast2.
#
# Measured from the confirmation dialog's `Install` to YaST2's window closing: about 15
# minutes on this ISO (locally 23:35 -> 23:50, and the same span on CI). Thirty minutes is
# that with room for a slower runner.
INSTALL_SECONDS = 1800

INSTALL = (1587, 859)

# THE CONFIRMATION DIALOG'S OWN `Install`. Clicking the footer's Install opens a modal
# centred on the YaST2 window - `Confirm Installation`, about 550 px wide - whose buttons
# are `Install` and `Back`, with BACK as the dialog's default (it is drawn green). Measured
# on a live Tumbleweed KDE session at 1920x1080: the modal's Install sits at (922, 707).
CONFIRM_INSTALL = (922, 707)


def log(msg: str) -> None:
    print(f"[opensuse-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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

    def type_into(self, xy, value: str) -> None:
        Q.click(self.q, *xy)
        time.sleep(1.2)
        unsupported = keys.type_text(self.q, value, per_char=0.1)
        if unsupported:
            log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(0.8)


def wait_for_live_desktop(drv: Driver, timeout: int) -> bool:
    """Wait a MEASURED TIME for the KDE desktop, then stop waiting.

    NO OCR DECISION, AND THIS IS THE THIRD DESIGN TRIED.

    1. The first version waited for the word "Installation". The desktop came up and
       tesseract read the icon labels as `Rome` and `Uporade` - nine-pixel text under an
       icon is its worst case - so the driver waited forever on a screen that was ready.

    2. The second waited for KDE's panel clock (`AM`/`PM`). Under ocr.Screen's own psm 3
       the whole desktop reads as three words, `in | to | ome`; the clock is not among
       them. That is what run after run logged while a complete desktop was on screen.

    THE TIME IS THE MEASUREMENT. qemu starts, the live medium boots, KDE initialises and
    the installer icon is clickable - about three minutes on this image, measured on four
    local runs and every CI run that got this far. Three minutes is a number; "has the
    desktop drawn" is a question OCR cannot be relied on to answer here.

    THE BUDGET IS DOUBLED RATHER THAN TRUSTED. If the machine is slow the click lands early
    and does nothing, so the wait is 240 s where the measurement says 180 s. And the double
    click is retried once below, because a click into a desktop that is still drawing is the
    only failure this cannot see coming.
    """
    log(f"waiting {DESKTOP_SECONDS}s for the live desktop (measured: about 180s)")
    deadline = time.time() + min(DESKTOP_SECONDS, timeout)
    while time.time() < deadline:
        drv.look("boot")
        time.sleep(20)
    # THE LAST FRAME IS READ FOR THE LOG ONLY. Nothing below branches on it.
    drv.look("desktop")
    log(f"  screen after the wait: {' | '.join(w[0] for w in drv.words[:12])}")
    return True


def walk_yast2(drv: Driver, args) -> bool:
    """Click through YaST2: A MEASURED COORDINATE, THEN A MEASURED WAIT.

    NOTHING IS READ TO DECIDE ANYTHING. Every step is a number in this file and a duration;
    the screen is read afterwards and the reading goes into the log, where it is useful for
    a post-mortem and dangerous as a decision. This is the rule ubuntu-gui and kubuntu-gui
    already follow, and the reason both of them are green.

    WHAT A DETECTOR COST HERE. The first version of this driver skipped the Online
    Repositories pane and the Suggested Partitioning pane whenever its text was not read -
    so on a slow frame it silently configured a different install. Later it tested
    `if "installation settings" in drv.text()` after the Install click, which is TRUE with
    the confirmation modal correctly on screen (the pane is still drawn behind it), and
    every run "recovered" from a click that had worked by pressing Return - which on this
    installer is BACK, dismissing the confirmation. See step 6.

    YAST2'S FOOTER IS AT ONE PLACE. Measured at 1920x1080 on Welcome, System Role,
    Suggested Partitioning, Clock and Time Zone and Local User: `Next` is (1596, 859) on
    all of them, because YaST2 draws a window whose footer does not move with its content.
    """
    # 1. THE ICON, THROUGH kdesu. See the module docstring: this is what makes YaST2 come up
    #    graphically, and running the same script from a shell does not.
    #
    #    TRIED TWICE, BECAUSE A CLICK INTO A STILL-DRAWING DESKTOP DOES NOTHING AND SAYS
    #    NOTHING. The wait before this is 240 s against a measured 180, so the first attempt
    #    should land - but a desktop that answered the first double-click by merely selecting
    #    the icon looks exactly like one that ignored it, and a second attempt costs 40 s.
    drv.double_click(INSTALL_ICON, "Installation desktop icon", settle=40.0)
    drv.look("yast welcome")
    if "language" not in drv.text():
        log("YaST2 did not open on the first double-click; trying once more")
        drv.double_click(INSTALL_ICON, "Installation desktop icon (second attempt)",
                         settle=45.0)
        drv.look("yast welcome")
    if "language" not in drv.text():
        log("=== YAST2 DID NOT OPEN ===")
        log(f"  screen: {drv.text()[:200]}")
        return False

    # 2. WELCOME -> SYSTEM PROBING -> ONLINE REPOSITORIES.
    #
    #    The probe is not instant, so the wait after the first click is 30 s; the question
    #    about online repositories comes up on its own and `No` is clicked at its measured
    #    place whether or not its words were read.
    drv.click(NEXT, "Welcome: Next", settle=30.0)
    drv.look("probing")
    drv.click(ONLINE_REPOS_NO, "Online Repositories: No", settle=20.0)
    drv.look("system role")

    # 3. SYSTEM ROLE. THE RADIO BUTTON, NOT THE ROW - see the module docstring: the row is
    #    highlighted on hover and the pane starts with nothing selected, which makes Continue
    #    say `Select one of the available roles to continue.`
    log("selecting Desktop with KDE Plasma (its radio button, which starts empty)")
    drv.click(ROLE_KDE_PLASMA, "role: Desktop with KDE Plasma", settle=6.0)
    drv.look("role selected")
    drv.click(NEXT, "System Role: Next", settle=30.0)
    drv.look("partitioning")

    # 4. SUGGESTED PARTITIONING. THE SUGGESTED LAYOUT IS THE RIGHT ONE: GPT, a BIOS boot
    #    partition, btrfs for / and swap. Accepting it is one click; changing it is several
    #    screens.
    drv.click(NEXT, "Suggested Partitioning: Next", settle=25.0)
    drv.look("timezone")

    # 5. TIME ZONE. THE DEFAULT IS KEPT, exactly as on every other installer here: it is
    #    already a US zone on this cluster and the requirement is a booting disk.
    log("time zone left at the installer's default")
    drv.click(NEXT, "Clock and Time Zone: Next", settle=22.0)
    drv.look("local user")

    # 6. THE LOCAL USER - FOUR FIELDS, IN THE ORDER THE PAGE DRAWS THEM.
    drv.type_into(USER_FULL_NAME, args.user)
    drv.type_into(USER_NAME, args.user)
    drv.type_into(USER_PASSWORD, args.password)
    drv.type_into(USER_CONFIRM, args.password)
    drv.look("user filled")

    # THE WEAK-PASSWORD DIALOG TAKES THE KEYBOARD, AND ONLY THE KEYBOARD. See the module
    # docstring: clicking its `Yes` does nothing twice in a row, while Return accepts it,
    # because Yes is the default button of THAT dialog. This is the one place in this driver
    # where a key is pressed, and it is a key the dialog itself makes the accept button.
    drv.click(NEXT, "Local User: Next", settle=18.0)
    drv.look("weak password")
    Q.key(drv.q, "ret")
    time.sleep(22.0)
    drv.look("after the password dialog")

    # 7. INSTALL, AND THEN THE CONFIRMATION DIALOG'S OWN INSTALL - WHICH IS NOT THE SAME
    #    BUTTON AND IS NOT WHERE THE OTHER INSTALLERS PUT IT.
    #
    # THE FOOTER'S `Install` OPENS A MODAL, AND THE MODAL'S DEFAULT IS BACK.
    #
    # Measured on a live Tumbleweed KDE session, frame by frame:
    #
    #     click (1587, 859)   -> `Confirm Installation`, Install white at (922, 707),
    #                            Back green - the modal's default
    #     Return              -> modal gone, Installation Settings again
    #     click (922, 707)    -> Performing Installation, 24%, 2273 packages remaining
    #
    # So the modal is answered by its own button at its own measured place, and the keyboard
    # is never used near it. `Back` being the default is exactly why.
    drv.click(INSTALL, "Install", settle=15.0)
    drv.look("after Install")
    drv.click(CONFIRM_INSTALL, "the Confirm Installation dialog's Install", settle=25.0)
    drv.look("after confirm")

    # 8. A MEASURED WAIT, BECAUSE YAST2 DOES NOT STAY TO SAY IT IS DONE.
    #
    #    IT CLOSES. Measured on a local run: 23:35 the install starts, 23:41 it is at
    #    `Installing Packages... (Remaining: 1.566 GiB, ...)`, and by 23:50 the wizard WINDOW
    #    IS GONE and the screen is the live KDE desktop again - `Install`, `Home`, `Upgrade`
    #    icons, the panel clock. No `Congratulations` pane, no `Installation complete`, no
    #    prompt. Every CI run showed the same thing in its log and could not name it:
    #
    #        installing: in | to | ome
    #
    #    which is tesseract's reading of that desktop's three icon labels, once per poll,
    #    for as long as the run was allowed to continue.
    #
    #    So the completion test was one this installer can never satisfy, and a finished
    #    disk would have been thrown away at the timeout - the same failure mint-gui had
    #    for the same reason.
    #
    #    THE DURATION IS THE MEASUREMENT. From the confirmation dialog's `Install` to the
    #    wizard closing: about 15 minutes on this ISO (23:35 -> 23:50 locally; the CI runs
    #    show the same span). Thirty minutes is that with room for a slower runner.
    #
    #    THE POINTER IS CLICKED INSIDE THE LOOP to keep the session awake - a locked screen
    #    has no installer on it, and mint-gui's last frame was exactly that.
    log(f"waiting {INSTALL_SECONDS}s for the install (measured: about 15 minutes)")
    started = time.time()
    while time.time() - started < min(INSTALL_SECONDS, args.timeout):
        try:
            Q.click(drv.q, 400 + (int(time.time()) % 40), 300)
        except Exception:                          # noqa: BLE001
            pass
        drv.look("installing")
        time.sleep(20)

    log("=== THE INSTALL RUN ENDED, AS MEASURED ===")
    log("  (YaST2 closes its window when it is done - see INSTALL_SECONDS)")
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
    p.add_argument("--user", default=os.environ.get("OPENSUSE_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("OPENSUSE_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "opensuse-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    kernel = os.path.join(work, "vmlinuz")
    initrd = os.path.join(work, "initrd")
    if not (os.path.exists(kernel) and os.path.exists(initrd)):
        for member in ("boot/x86_64/loader/linux", "boot/x86_64/loader/initrd"):
            subprocess.run(["7z", "e", "-y", f"-o{work}", args.iso, member], check=True,
                           stdout=subprocess.DEVNULL)
        for produced, wanted in ((os.path.join(work, "linux"), kernel),
                                 (os.path.join(work, "initrd"), initrd)):
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
            # THE VOLUME LABEL IS THE ISO'S OWN. `rd.live.image` is dracut's half of the same
            # contract Fedora uses; without both, the initramfs cannot find its squashfs.
            "-append", "root=live:CDLABEL=openSUSE_Tumbleweed_KDE_Live rd.live.image "
                       "locale=en_US.UTF-8 quiet splash=silent systemd.show_status=yes",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "opensuse-gui"),
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
            # THE WALK IS A YAML FILE NOW - see steps/install.yaml. install.py keeps the
            # machine, the frames, the recording and the publish; the walk is here.
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
