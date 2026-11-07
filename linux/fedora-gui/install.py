"""Install Fedora Workstation from its Live ISO, through Anaconda, its GTK installer.

    python3 linux/fedora-gui/install.py --iso out/iso/Fedora-Workstation-Live-43-1.6.x86_64.iso \
                                        --out out/fedora-gui.qcow2

WHY THIS IS A LIVE ISO AND NOT AN INSTALLER

Fedora publishes no separate "installer" image any more: Workstation is a live desktop
with Anaconda on it. So the machine has to reach a GNOME session, and the installer is
started from there - from a Welcome dialog that the live session opens BY ITSELF, which is
the first thing this driver relies on and the kind of detail that is only found by running
it.

THE ISO'S OWN KERNEL, NOT THE ISO'S MENU. The same reason linux/ubuntu-gui boots casper's
kernel directly: a boot menu is a screen, and a screen is something a driver would have to
read. `root=live:CDLABEL=Fedora-WS-Live-43 rd.live.image` is what the ISO's own GRUB says,
copied here so qemu passes it instead of a person pressing Enter.

ANACONDA IS FOUR PANES, AND THE DEFAULTS ARE ALREADY RIGHT.

    Welcome             language and keyboard
    Installation method Use entire disk          <- already selected
    Storage configuration  Encrypt my data       <- already unchecked
    Review and install  Erase data and install

Nothing has to be changed on panes 2 and 3, which is why the driver clicks Next twice with
no clicks in between. That was measured, not assumed: the frames show `Use entire disk` with
its radio filled and `Encrypt my data` empty.

THE LANGUAGE HAS TO BE PINNED, AND IT IS NOT COSMETIC. The first run came up in Chinese
because Anaconda picks a locale from the session, and this repository's runner has no
locale in common with the image's first choice. A Chinese installer is not a problem for a
person and is a fatal one for a coordinate driver: the footer buttons are laid out from
their labels, so `下一个` is not at the x that `Next` is. `locale=en_US.UTF-8` on the kernel
command line fixes it before Anaconda starts.

GNOME'S FIRST-BOOT WIZARD IS PART OF THE INSTALL, NOT OF THE LIVE SESSION.

The live session has no such wizard. It belongs to the installed system, so it appears only
when the disk is booted on its own - which is the second half of this file and the thing that
proves the install. Seven panes: language, typing, privacy, time zone, third-party
repositories, account, password.
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

# --- measured on a live Fedora 43 Workstation session, 1920x1080 ---------------------------

# THE WELCOME DIALOG THE LIVE SESSION OPENS ON ITS OWN. It has two buttons; the left one
# starts Anaconda. Measured from the frame, and clicked TWICE because the first press on an
# unfocused window is taken as focus - which is the same failure the Kubuntu driver hit.
INSTALL_BUTTON = (838, 708)

# THE ANACONDA FOOTER. Back / Next / Quit, right-aligned, and the same three positions on
# every pane because Anaconda keeps the footer fixed.
NEXT = (729, 1002)
BACK = (630, 1002)
QUIT = (838, 1002)

# The Welcome pane's language list, when Anaconda's own default is not English.
LANGUAGE_EN_US = (860, 424)

# `Erase data and install` takes the width of three buttons, so it is wider than Next and
# its centre is not the same x.
ERASE_AND_INSTALL = (780, 1002)

# --- the installed system's first-boot wizard ---------------------------------------------

# ITS OWN BUTTON, AT ITS OWN POSITION. GNOME's initial-setup window is smaller than
# Anaconda's and its Next is at the top right, not the bottom right.
SETUP_NEXT = (1432, 181)

# Privacy: two switches that ship ON, and both are turned OFF before continuing. A bake is
# not a device in somebody's pocket, and a disk that reports its problems to Fedora on every
# boot is a disk whose behaviour depends on a network it may not have.
PRIVACY_LOCATION = (1209, 485)
PRIVACY_REPORTING = (1209, 601)

# The time zone map. THE CITY LIST CANNOT BE USED: searching "united states" returns
# airports, sorted with `Charlotte Amalie ... Virgin Islands` first, and the first entry is
# never the answer. Clicking the map is one action and lands on a whole region.
TIMEZONE_MAP = (590, 560)        # North America, east coast

SETUP_FINISH = (959, 709)        # "Start Using Fedora Linux"
TOUR_SKIP = (852, 756)           # the tour offer, after the wizard


def log(msg: str) -> None:
    print(f"[fedora-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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

    def click_twice(self, xy, why: str, settle: float = 12.0) -> None:
        """Click, then click again. The first may only give the window focus."""
        self.click(xy, why, settle=0.8)
        self.click(xy, f"{why} (second click)", settle=settle)

    def alt_n(self, why: str, settle: float = 12.0) -> None:
        """Alt+N, which Anaconda binds to its forward button in every language.

        A CLICK ON A TRANSLATED LABEL IS A COORDINATE THAT MOVES. The footer is laid out
        from its labels, so `Next` at x 729 is `Siguiente` at 744 and `Suivant` somewhere
        else again - and run 42373700380 lost a whole run to exactly that, having been
        measured in English and then handed a Spanish installer.
        """
        log(f"alt+n - {why}")
        Q.key_combo(self.q, "alt", "n")
        time.sleep(settle)


def wait_for_live_desktop(drv: Driver, timeout: int) -> bool:
    """Until the live session has drawn a GNOME desktop.

    THE MARKER IS THE SHELL'S OWN SEARCH BOX, which is on every GNOME desktop and nowhere
    else: `Type to search` in the overview, or the Activities button beside it. Booting a
    live session takes one to three minutes and the desktop appears well before the Welcome
    dialog does, so this waits for the desktop and not for the dialog.
    """
    log("waiting for the live desktop")
    deadline = time.time() + timeout
    while time.time() < deadline:
        drv.look("boot")
        joined = drv.text()
        if "type to search" in joined or "activities" in joined or "install fedora" in joined:
            log("the live session is up")
            return True
        time.sleep(10)
    log("=== THE LIVE DESKTOP DID NOT APPEAR ===")
    log(f"  last screen: {' | '.join(w[0] for w in drv.words[:24])}")
    return False


def open_anaconda(drv: Driver, attempts: int = 4) -> bool:
    """Open Anaconda from the live session's welcome dialog, and prove it opened.

    THE DIALOG IS NOT ANACONDA AND DOES NOT OPEN BY ITSELF.

    Run 42382690048 clicked `Install Fedora Linux...` at its measured centre (838, 708) and
    the uploaded frame shows that dialog still on screen with everything after it pointed at
    an installer that had never started: the driver's next click was Anaconda's `Next` at
    (729, 1002), which on the welcome dialog is blank background, and the run then waited for
    a pane that could not appear.

    WHY THE CLICK DID NOT TAKE is timing: the live session draws the dialog and the driver
    arrives from a fixed 230 s wait, so the first press can land on a window that is still
    opening. The click is therefore repeated, and each attempt asks the only question that
    matters - is Anaconda's own sidebar on screen - instead of assuming it.

    THE MARKER IS THE SIDEBAR, NOT THE DIALOG. `Installation method` is one of Anaconda's
    four panes and appears nowhere on the live desktop. `Welcome to Fedora Linux` is the
    DIALOG's title, which is why matching it would answer the wrong question.
    """
    for attempt in range(1, attempts + 1):
        log(f"opening Anaconda (attempt {attempt})")
        drv.click(INSTALL_BUTTON, "Install Fedora Linux...", settle=3.0)
        drv.click(INSTALL_BUTTON, "Install Fedora Linux... (second click)", settle=20.0)
        drv.look("after Install Fedora Linux")
        if "installation method" in drv.text() or "let's install" in drv.text():
            log("Anaconda is up")
            return True
        log("  Anaconda is not up yet")
    log("=== ANACONDA NEVER OPENED ===")
    log(f"  screen: {drv.text()[:200]}")
    return False


def walk_anaconda(drv: Driver, args) -> bool:
    """Click through Anaconda, checking that each pane is the one expected.

    WHY THE CHECK IS A WORD FROM THE PANE BEING REACHED. Anaconda's footer buttons do not
    move, so a click that misses them does nothing at all - which means a run can walk every
    step and never leave the first pane. ubuntu-gui's first CI run did exactly that and was
    only found from the uploaded last frame, so each step here says what it expects to see
    afterwards.
    """
    # 1. THE LIVE SESSION'S WELCOME DIALOG, and the installer behind it. See open_anaconda:
    #    the dialog is not the installer, and a run that assumes otherwise points every
    #    later click at a window that does not exist.
    if not open_anaconda(drv):
        return False

    # 2. THE LANGUAGE, AND THEN THE LIST THAT COVERS THE FOOTER.
    #
    # TWO RUNS DIED HERE AND BOTH DID SO BY TRUSTING THE SCREEN.
    #
    # 42372442386: English was already selected, so the driver never clicked the list, so
    #   nothing ever closed it - and the list is expanded by default, covering (729, 1002).
    #   The Next click landed on the list. `after welcome:` was empty.
    #
    # 42373700380: the language was SPANISH. `locale=en_US.UTF-8` on the kernel command
    #   line does not move Anaconda - it reads its locale from the live session, not from
    #   the kernel - so this run looked for English at (860, 424) and clicked a row of a
    #   Spanish list, missing it. The list stayed open and Next stayed covered.
    #
    # BOTH ARE THE SAME LESSON: the language is not part of the contract, so nothing below
    # may depend on it. The list is closed with Escape, which is what a person does and is
    # the same key in every language, and every advance is made with Alt+N - Anaconda binds
    # it to the Next button under every locale it ships.
    drv.look("anaconda welcome")

    # ESCAPE, REPEATEDLY, AND AFTER ANACONDA HAS SETTLED.
    #
    # The comment above says the list is closed with Escape; what it did not say is WHEN.
    # Run at 17:17:39 pressed Escape two seconds after `Anaconda is up`, and the list was
    # still on screen afterwards - the window had only just mapped and the key went to a
    # widget that was not ready. Three presses, a second apart, is what a person does when
    # the first one appears to do nothing, and it costs three seconds.
    for attempt in range(1, 4):
        log(f"closing the language list with Escape (attempt {attempt})")
        Q.key(drv.q, "esc")
        time.sleep(1.5)
        drv.look(f"after escape {attempt}")
        # THE LIST HAS A SEARCH BOX AT ITS TOP; the pane does not. That is the phrase to
        # look for, and it is the only use of the reading here.
        if "suggested" not in drv.text():
            break

    for pane, expected_set in (
        ("welcome", ("installation method", "metodo de instalacion", "methode d'installation")),
        ("installation method", ("storage configuration", "configuracion de almacenamiento")),
        ("storage configuration", ("review and install", "revisar e instalar")),
    ):
        # ALT+N, NOT A CLICK. The footer button is at a different x in every language -
        # `Next`, `Siguiente`, `Suivant`, `Weiter` - and Alt+N is the binding that does not
        # move. This is the same conclusion the Kubuntu driver reached and for the same
        # reason: a translated label cannot be a coordinate.
        drv.alt_n(f"{pane}: Next", settle=12.0)
        drv.look(f"after {pane}")
        text = drv.text()
        if not any(expected in text for expected in expected_set):
            log(f"=== {pane.upper()} DID NOT ADVANCE (expected one of {expected_set}) ===")
            log(f"  screen: {text[:200]}")
            return False

    # 5. THE POINT OF NO RETURN. Its label is not "Next" and its position is not Next's.
    drv.click(ERASE_AND_INSTALL, "Erase data and install", settle=20.0)
    drv.look("installing")

    # 6. UNTIL IT SAYS SO. `Successfully installed` is Anaconda's own verdict and the only
    #    end to this loop that means anything.
    log("waiting for the installer to finish")
    started = time.time()
    while time.time() - started < args.timeout:
        drv.look("installing")
        if "successfully installed" in drv.text():
            log("=== INSTALL COMPLETE ===")
            # LEAVE THE INSTALLER. The disk is written either way, but a run that stops
            # here leaves qemu showing a modal over the live desktop, and the frames are
            # the artifact that explains a failure.
            drv.click((963, 364), "Exit to live desktop", settle=10.0)
            return True
        time.sleep(20)

    log("=== THE INSTALLER NEVER REPORTED SUCCESS ===")
    log(f"  last screen: {drv.text()[:200]}")
    return False


def walk_first_boot(drv: Driver, args) -> bool:
    """GNOME's first-boot wizard, on the disk the installer wrote.

    THIS RUNS IN A DIFFERENT MACHINE. ${args} has no ISO attached by the time this is called:
    the point of the stage is that the firmware boots the disk on its own and the system it
    finds is the one Anaconda installed.
    """
    log("waiting for the installed system's first-boot wizard")
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        drv.look("first boot")
        joined = drv.text()
        if "welcome" in joined or "bienvenue" in joined or "next" in joined:
            break
        if "type to search" in joined and "next" not in joined:
            # THE DESKTOP WITHOUT THE WIZARD. Either it was already answered on an earlier
            # run of this disk, or gdm logged straight in - both mean the install is fine.
            log("the desktop is already up without a wizard")
            return True
        time.sleep(10)
    else:
        log("=== THE FIRST-BOOT WIZARD NEVER APPEARED ===")
        log(f"  last screen: {drv.text()[:200]}")
        return False

    # 1. LANGUAGE. English is preselected; Next.
    drv.click(SETUP_NEXT, "language: Next", settle=10.0)
    drv.look("typing")

    # 2. TYPING. English (US) is preselected; Next.
    drv.click(SETUP_NEXT, "typing: Next", settle=10.0)
    drv.look("privacy")

    # 3. PRIVACY. Two switches that ship ON and are turned OFF.
    drv.click(PRIVACY_LOCATION, "Location Services off", settle=2.0)
    drv.click(PRIVACY_REPORTING, "Automatic Problem Reporting off", settle=2.0)
    drv.click(SETUP_NEXT, "privacy: Next", settle=10.0)
    drv.look("timezone")

    # 4. TIME ZONE. THE MAP, NOT THE SEARCH BOX. Searching "united states" returns airports
    #    with `Charlotte Amalie ... Virgin Islands` first, and the list is long enough that
    #    picking the right row is a worse question than clicking the continent.
    drv.click(TIMEZONE_MAP, "time zone from the map", settle=6.0)
    drv.click(SETUP_NEXT, "time zone: Next", settle=12.0)
    drv.look("third party")

    # 5. THIRD-PARTY REPOSITORIES. Not enabled - see the module docstring.
    drv.click(SETUP_NEXT, "third-party repositories: Next", settle=10.0)
    drv.look("about you")

    # 6. THE ACCOUNT. The same user every Linux bake here creates.
    drv.click((830, 511), "Full Name field", settle=2.0)
    unsupported = keys.type_text(drv.q, args.user, per_char=0.1)
    if unsupported:
        log(f"  COULD NOT TYPE {unsupported!r}")
    drv.click(SETUP_NEXT, "about you: Next", settle=10.0)
    drv.look("password")

    # 7. THE PASSWORD, TWICE, then the weak-password warning is left to stand: `admin` is
    #    what every other bake here uses and a mismatch between images would be worse than
    #    a short password.
    drv.click((830, 486), "Password field", settle=2.0)
    keys.type_text(drv.q, args.password, per_char=0.12)
    drv.click((830, 629), "Confirm Password field", settle=2.0)
    keys.type_text(drv.q, args.password, per_char=0.12)
    drv.click(SETUP_NEXT, "password: Next", settle=20.0)
    drv.look("setup complete")

    if "all done" in drv.text() or "ready to be used" in drv.text():
        log("=== SETUP COMPLETE ===")
        drv.click(SETUP_FINISH, "Start Using Fedora Linux", settle=25.0)
        drv.look("desktop")
        # THE TOUR OFFER IS A DIALOG OVER THE DESKTOP, not part of setup. Skipping it is
        # what leaves a clean frame for the artifact.
        if "take tour" in drv.text() or "skip" in drv.text():
            drv.click(TOUR_SKIP, "skip the tour", settle=12.0)
            drv.look("desktop after tour")
        return True

    log("=== THE FIRST-BOOT WIZARD DID NOT COMPLETE ===")
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
    p.add_argument("--user", default=os.environ.get("FEDORA_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("FEDORA_PASSWORD", "admin"))
    p.add_argument("--skip-first-boot", action="store_true",
                   help="stop after the install; the disk is written but not booted")
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "fedora-gui.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    # THE ISO'S OWN KERNEL AND INITRD, as every Linux bake here does. Read from
    # boot/x86_64/loader, which is where Fedora's live media keeps them.
    kernel = os.path.join(work, "vmlinuz")
    initrd = os.path.join(work, "initrd")
    if not (os.path.exists(kernel) and os.path.exists(initrd)):
        for member in ("boot/x86_64/loader/linux", "boot/x86_64/loader/initrd"):
            subprocess.run(["7z", "e", "-y", f"-o{work}", args.iso, member], check=True,
                           stdout=subprocess.DEVNULL)
        # 7z writes the member under its own basename: `linux` and `initrd`.
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
            # THE VOLUME LABEL IS THE ISO'S OWN, read from its PVD. `rd.live.image` tells the
            # initramfs this is a live medium; without both, dracut cannot find its squashfs
            # and drops to a shell, which on a machine with no screen is indistinguishable
            # from a slow boot.
            "-append", "root=live:CDLABEL=Fedora-WS-Live-43 rd.live.image "
                       "locale=en_US.UTF-8 quiet rhgb",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                       vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "fedora-gui"),
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
            ok = walk_anaconda(drv, args)
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

    if not ok:
        log("=== THE INSTALL DID NOT COMPLETE ===")
        return 1

    # THE DISK IS PROVEN BY BOOTING IT ON ITS OWN. Without this the artifact is a file whose
    # contents nothing has ever read - and a failed Anaconda leaves a partition table and
    # nothing else, which is indistinguishable from a good disk by its size.
    if not args.skip_first_boot:
        log("=== BOOTING THE DISK ON ITS OWN ===")
        disk_guest = vm.Guest(
            work,
            extra=["-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap"],
            # UEFI AGAIN, AND WITH ITS OWN NVRAM. The disk was installed on the EFI path, so
            # its bootloader is on an ESP and a BIOS boot of it finds nothing to chain to.
            # The vars file is separate from the installer's (`fedora-gui` above) because the
            # installer's NVRAM holds the live medium's boot entry, not this disk's.
            shape=uefi.shape_with_uefi(
                vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                         vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
                work, "fedora-disk"),
            stdout=os.path.join(work, "console-disk.log"),
        )
        frames2 = os.path.join(work, "frames-disk")
        os.makedirs(frames2, exist_ok=True)
        disk_guest.start()
        log(f"disk guest qemu pid {disk_guest.pid}")
        snap2 = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
             disk_guest.paths.record, frames2, os.environ.get("SNAPSHOT_INTERVAL", "2")],
            stdout=open(os.path.join(frames2, "snapshots.log"), "wb"),
            stderr=subprocess.STDOUT,
        )
        try:
            drv2 = Driver(disk_guest.paths.drive, work)
            if not walk_first_boot(drv2, args):
                log("=== THE INSTALLED SYSTEM DID NOT COME UP ===")
                ok = False
        finally:
            disk_guest.stop(acpi_first=False)
            snap2.terminate()

    if ok and os.path.exists(disk):
        log(f"compressing {disk} -> {args.out}")
        compress.compress(disk, args.out)
        log(f"{args.out}: {os.path.getsize(args.out) >> 20} MiB")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
