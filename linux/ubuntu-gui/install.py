"""Install Ubuntu from the official desktop ISO, through the installer's own GUI.

    python3 linux/ubuntu-gui/install.py --iso out/iso/ubuntu-26.04.1-desktop-amd64.iso \
                                        --out out/ubuntu-gui.qcow2

WHY A GUI DRIVER AND NOT AN ANSWER FILE

ubuntu.yml installs from the *server* ISO, where subiquity reads an autoinstall document - its
comment says so: the server variant is "the image whose installer a file can drive". This one
starts from the desktop image, whose installer is a Flutter application. The interactive path
has no answer file, and the only way through it is the GUI.

(It does have an "Automated with autoinstall file" option, visible on the installer's own
"Type of installation" pane. That is a different feature and it is not what this file does:
the point here is to drive the graphical path, which is the one every future distro shares.)

HOW IT IS DRIVEN

The same way macos/15/drive_read.py drives the Setup Assistant: look at the screen, decide from
what is written on it, click, then look again to confirm the pane changed. Ubuntu's installer
is much easier than macOS's in one respect - every pane has a title tesseract reads cleanly
("Accessibility", "Disk setup", "Create your account"), so the driver knows where it is before
it clicks instead of hoping.

EVERY COORDINATE WAS MEASURED on a real ubuntu-26.04.1-desktop ISO at 1920x1080, by clicking it
and looking at the next frame. The panes and their answers are in PANES below, in order.

THE ONE PANE THAT CAN FAIL SILENTLY IS THE ACCOUNT FORM. A password the installer calls "Weak
password" still passes, but the form will not let Next through until every field has a green
tick beside it, so the driver checks for the ticks rather than assuming its typing landed.
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
import iso          # noqa: E402
import live         # noqa: E402
import ocr          # noqa: E402
import qmp as qmpmod  # noqa: E402
import uefi         # noqa: E402
import vm           # noqa: E402
import lastframe    # noqa: E402

# --- measured positions, 1920x1080 -------------------------------------------------------
#
# THE FOOTER IS THE SAME ON EVERY PANE EXCEPT WHERE THE BUTTON IS GREEN. "Next" and the green
# "Install" sit at the same place; that is a property of this installer's layout, not a
# convention to rely on across releases.
NEXT = (1355, 848)
INSTALL = (1355, 848)

ACCOUNT_FIELDS = {
    "name": (1139, 393),
    "hostname": (1139, 447),
    "username": (1139, 501),
    "password": (1050, 563),
    "confirm": (1139, 616),
}
REQUIRE_PASSWORD = (923, 670)      # ticked by default; cleared so the guest logs itself in

# --- the panes ---------------------------------------------------------------------------
#
# A FIXED SEQUENCE OF CLICKS, NOT A SET OF CONDITIONS TO MATCH.
#
# The first version of this file decided each pane from the words tesseract read, and it failed
# on the first CI run in exactly the way drive.py's docstring warns about:
#
#     A fixed sequence has one failure mode - being wrong about a position - and that is found
#     on the first run and fixed by changing one number. A reading driver has a failure mode
#     per pane and per wallpaper.
#
# What happened: the disk pane's detector was ("disk", "setup"), the pane actually reads
# "How do you want to install Ubuntu? Erase disk and install Ubuntu", and "setup" is not on it.
# The pane was SKIPPED - silently, because a driver that only clicks when its condition holds
# cannot tell "this pane was skipped by the installer" from "my condition is wrong" - and the
# run then waited out its whole timeout on a screen it was never going to leave.
#
# So: the sequence is positions and waits. Every step clicks where the control was measured to
# be and then waits for the next pane to settle. OCR still runs, but only to WRITE THE SCREEN
# INTO THE LOG - a diagnostic that is read when something goes wrong, never a branch.
#
# (name, clicks_before_wait, wait_seconds). The name is for the log only.
PANES = [
    ("welcome / language", 1, 12),
    ("accessibility", 1, 12),
    ("keyboard layout", 1, 12),
    ("internet connection", 1, 14),
    ("try or install", 1, 14),
    ("type of installation", 1, 14),
    ("applications", 1, 14),
    ("proprietary software", 1, 14),
    ("disk setup", 1, 20),
    ("encryption", 1, 14),
    ("create your account", 0, 0),      # handled by fill_account
    ("time zone", 1, 14),
    ("ready to install", 1, 30),        # the green Install button
]


def log(msg: str) -> None:
    print(f"[ubuntu-gui {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Installer:
    def __init__(self, qmp_path: str, work: str, *, user: str = "docker",
                 password: str = "admin") -> None:
        self.user = user
        self.password = password
        shots = os.path.join(work, "shots")
        os.makedirs(shots, exist_ok=True)
        # THE SOCKET IS WAITED FOR. guest.start() returns as soon as the process exists and
        # qemu binds its QMP socket a moment later; connecting immediately raises
        # FileNotFoundError, which is what drive_read.py did on run 42289113404 and why
        # desktop.py spent its whole timeout on an undriven guest.
        deadline = time.time() + 120
        while not os.path.exists(qmp_path):
            if time.time() > deadline:
                raise SystemExit(f"no QMP socket at {qmp_path} after 120s")
            time.sleep(1)
        self.screen = ocr.Screen(qmp_path, shots, width=1920, height=1080)
        self.q = self.screen.q
        self.work = work
        self.n = 0
        self.words: list[tuple] = []

    def look(self, label: str = "look") -> None:
        self.n += 1
        ppm = self.screen.grab(f"{self.n:03d}-{label}")
        self.words = self.screen.text(ppm) if ppm else []
        log(f"look {label}: {len(self.words)} words :: "
            f"{' | '.join(w[0] for w in self.words[:16])}")

    def says(self, *want: str) -> bool:
        have = {"".join(c for c in w[0].lower() if c.isalnum()) for w in self.words}
        return all("".join(c for c in x.lower() if c.isalnum()) in have for x in want)

    def screen_text(self) -> str:
        """Everything OCR read, as one lowercased string.

        USED ONLY TO NOTICE THAT THE SCREEN STOPPED CHANGING - never to decide where to
        click. Those are different questions and only one of them is safe: a lookup can be
        talked into clicking a word in a paragraph, which is exactly what stalled the
        sibling kubuntu-gui driver for eighty-seven minutes.
        """
        return " ".join(w[0].lower() for w in self.words)

    def click(self, xy, why: str, settle: float = 8.0) -> None:
        log(f"  click {xy} - {why}")
        qmpmod.click(self.q, *xy)
        time.sleep(settle)

    def type_into(self, xy, text: str) -> None:
        qmpmod.click(self.q, *xy)
        time.sleep(1.5)
        # CTRL+A, NOT SUPER+A. Super+A opens GNOME's application grid, which put the installer
        # behind the Activities overlay and typed the value into its search box - measured on
        # the run that wrote this file, and it cost a manual escape and a re-entry of every
        # field. QEMU's qcode for the modifier is "ctrl", not "ctrl_l": the latter is refused
        # with "Parameter 'data' does not accept value 'ctrl_l'".
        for _ in range(2):
            qmpmod.key_combo(self.q, "ctrl", "a")
            time.sleep(0.4)
            qmpmod.key(self.q, "backspace")
            time.sleep(0.3)
        for ch in text:
            qmpmod.key(self.q, "spc" if ch == " " else ch)
            time.sleep(0.3)
        time.sleep(1.0)

    def fill_account(self, user: str, password: str) -> None:
        log(f"  account form: {user} / {password}")
        self.type_into(ACCOUNT_FIELDS["name"], user)
        self.type_into(ACCOUNT_FIELDS["hostname"], "ubuntu")
        self.type_into(ACCOUNT_FIELDS["username"], user)
        self.type_into(ACCOUNT_FIELDS["password"], password)
        self.type_into(ACCOUNT_FIELDS["confirm"], password)
        # AUTOLOGIN, SO LATER STAGES CAN REACH THE DESKTOP WITHOUT A PASSWORD PROMPT. The
        # tick is on when the pane opens; clicking it clears it.
        self.click(REQUIRE_PASSWORD, "clear 'Require my password to log in'", settle=3.0)

    def wait_for_welcome(self, timeout: int) -> bool:
        log("waiting for the installer's first pane")
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.look("boot")
            if self.says("welcome", "ubuntu"):
                return True
            time.sleep(10)
        log("=== THE INSTALLER DID NOT APPEAR ===")
        log(f"  last screen: {' | '.join(w[0] for w in self.words[:24])}")
        return False

    def walk(self) -> bool:
        """Click through the sequence. Every step advances or the log says it did not.

        THE SCREEN IS READ AFTER EACH STEP AND THE READING DECIDES NOTHING. It goes into the
        log so a failed run can be diagnosed without a recording - which is what OCR is for
        now. Whether a click worked is answered by the sequence reaching its end, not by
        matching words.

        A STEP THAT DID NOT MOVE IS LOUD. The word count before and after a click is compared
        and an unchanged count is logged as a warning. The old conditional driver could skip a
        pane (its detector did not match) and still report a clean run; this cannot.
        """
        for name, clicks, wait in PANES:
            log(f"pane: {name}")
            before = len(self.words)
            if name == "create your account":
                self.fill_account(self.user, self.password)
                self.click(NEXT, "account Next", settle=12.0)
            else:
                target = INSTALL if wait >= 30 else NEXT
                why = "the green Install button" if wait >= 30 else "Next"
                for _ in range(max(1, clicks)):
                    self.click(target, f"{name} - {why}", settle=float(wait))
            self.look("after")
            if self.words and len(self.words) == before:
                log(f"  WARNING: same word count as before ({before}); "
                    f"this step may not have advanced")
        return True


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=None)
    p.add_argument("--disk-size", default="64G")
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--timeout", type=int, default=5400)
    p.add_argument("--user", default=os.environ.get("UBUNTU_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("UBUNTU_PASSWORD", "admin"))
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, "ubuntu-gui.qcow2")
    # compress.run(), NOT A BARE qemu-img, for the reason lib/compress.py's docstring gives:
    # the bake image puts the Android SDK's emulator directory ahead of /usr/bin, and its
    # qemu-img is 2.12.0, which cannot create or read what the rest of this repository
    # writes. This call and the flatten at the end were the last two bare ones in the
    # repository, and the flatten failed on CI at exactly that step.
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    # THE ISO'S OWN KERNEL AND INITRD, as every Linux bake here does: qemu hands them to the
    # guest, so the console is on ttyS0 from the first line and there is no boot menu to click.
    kernel = os.path.join(work, "vmlinuz")
    initrd = os.path.join(work, "initrd")
    if not (os.path.exists(kernel) and os.path.exists(initrd)):
        for member in ("casper/vmlinuz", "casper/initrd"):
            subprocess.run(["7z", "e", "-y", f"-o{work}", args.iso, member], check=True,
                           stdout=subprocess.DEVNULL)
        for name in ("vmlinuz", "initrd"):
            src = os.path.join(work, name)
            if os.path.exists(src):
                os.replace(src, os.path.join(work, name))
        log("kernel and initrd extracted")

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            "-kernel", kernel,
            "-initrd", initrd,
            # NOTHING HERE ANSWERS THE INSTALLER'S QUESTIONS. `---` ends qemu's own arguments
            # and the rest goes to the guest kernel; the only things passed are the console
            # and the same line twice, which is what casper expects.
            "-append", "console=ttyS0,115200n8 --- console=ttyS0,115200n8",
        ],
        # UEFI, NOT SeaBIOS. See lib/uefi.py for why every guest here boots on
        # OVMF: Garuda refuses Legacy mode outright, and the disks that the other
        # installers produce on the BIOS path are MBR/GPT with a BIOS boot
        # partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(
                cpus=args.cpus,
                mem_mb=args.mem,
                machine="q35",
                vga="std",
                vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1"),
            ),
            work, "ubuntu-gui",
        ),
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
        stdout=open(os.path.join(work, "frames", "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )
    art = live.start([frames], os.path.join(work, "install.mp4"),
                     os.path.join(work, "last-frame.png"),
                     every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "30")))

    ok = False
    try:
        drv = Installer(guest.paths.drive, work, user=args.user, password=args.password)
        if drv.wait_for_welcome(args.timeout):
            ok = drv.walk()
            # THE INSTALLER'S LAST ACT IS A REBOOT PROMPT. Waiting for it rather than for a
            # fixed time means the disk is not copied while it is still being written.
            # THE COMPLETION TEST IS THE ONE PLACE A WORD MATCH IS RIGHT.
            #
            # Every other decision here is a position and a wait, because a wrong detector
            # silently skips a pane. This one is a TERMINAL condition: the installer either
            # eventually says it is finished or it does not, and matching the words it prints
            # at the end cannot skip anything - there is nothing after it to skip.
            #
            # A TIMEOUT IS A FAILURE, NOT A SUCCESS. The old loop fell through to the publish
            # step with ok still true, so a run that never finished would have flattened and
            # pushed a half-installed disk.
            log("waiting for the installer to finish")
            finished = False
            deadline = time.time() + args.timeout
            # A SCREEN THAT HAS NOT CHANGED IN FIFTEEN MINUTES IS A DEAD RUN, NOT A SLOW ONE.
            #
            # ubuntu-gui has never stalled - this loop is the one part of it that has not
            # been proved by a failure. The sibling driver, kubuntu-gui, stalled on run
            # 42364250527 and spent eighty-seven of its ninety minutes logging one identical
            # OCR line every twenty seconds; the timeout was the only thing that ended it.
            # Finishing an install always changes the screen - a progress bar moves, a
            # package name changes, a pane changes - so a plateau that long means the
            # installer is not working and waiting longer cannot help.
            stalled_since = time.time()
            previous = ""
            while time.time() < deadline:
                drv.look("finish")
                current = drv.screen_text()
                if current != previous:
                    previous, stalled_since = current, time.time()
                if drv.says("installation", "complete") or drv.says("installed", "ready"):
                    log("=== INSTALL COMPLETE ===")
                    finished = True
                    break
                if drv.says("restart", "now"):
                    log("=== INSTALL COMPLETE (restart prompt) ===")
                    finished = True
                    break
                stalled = int(time.time() - stalled_since)
                if stalled > 900:
                    log(f"=== THE SCREEN HAS NOT CHANGED IN {stalled}s ===")
                    log("  the installer is not working; waiting longer will not change that")
                    log(f"  last screen: {current[:200]}")
                    break
                time.sleep(20)
            if not finished:
                log("=== THE INSTALLER NEVER REPORTED COMPLETION ===")
                log(f"  last screen: {' | '.join(w[0] for w in drv.words[:24])}")
                ok = False
    finally:
        try:
            shot = os.path.join(work, "final.ppm")
            client = qmpmod.QMP(guest.paths.drive)
            try:
                client.execute("screendump", filename=shot)
            finally:
                client.close()
            lastframe.convert(shot, os.path.join(work, "last-frame.png"))
        except Exception:                              # noqa: BLE001 - best effort
            pass
        guest.stop(acpi_first=False)
        snap.terminate()
        art.stop()

    if ok and os.path.exists(disk):
        # compress.compress() resolves the binary AND refuses to fall back to zlib, which
        # is why it is used here rather than a copy of its command line.
        log(f"compressing {disk} -> {args.out}")
        compress.compress(disk, args.out)
        log(f"{args.out}: {os.path.getsize(args.out) >> 20} MiB")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
