"""Bake MX Linux, driving its graphical installer, and record the whole thing.

    OUT=out/mx.qcow2 python3 linux/mx/bake.py

WHY OCR AT ALL

MX's minstall has an unattended mode and it does not work in any of the three forms
this project could reach:

  minstall --tui --auto --config     stops at 'Scanning disk drives' for ever
  minstall --auto --config (GUI)     reports 'Paused for required ops' on the very
                                     pane the configuration was meant to fill
  minstall-launcher                  refuses every argument, so the config is never
                                     read at all

So the GUI is driven: OCR reads the screen, QMP clicks at absolute coordinates, and
keys are typed into the account fields. Every coordinate in drive-mx.py was measured
from a frame this pipeline captured, not guessed.

WHY A SECOND BOOT

minstall installs no bootloader. The disk it produces holds a complete system and a
zeroed MBR, and the fix is grub-install in a chroot - which cannot run in the first
boot because minstall has taken the console. So the live ISO is booted a second time
with one job and nobody else using the serial line.

WHY THIS IS PYTHON NOW

The shell version fed the guest's serial console with a printf holding a command that
itself held a quoted command:

    printf 'echo demo | sudo -S sh -c "minstall > /tmp/minstall.out 2>&1; \\
            echo MINSTALL_RC=\\\\$? > /tmp/mx-rc" &\\n'

Four layers of quoting for one line, and the same shape as the four escaping failures
fix_grub.py's docstring records. Here the guest sees an ordinary Python string.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from lib import compress, iso, smoke, vm  # noqa: E402

MX_VERSION = os.environ.get("MX_VERSION", "25.3")
MX_EDITION = os.environ.get("MX_EDITION", "Xfce")
DISK_SIZE = os.environ.get("DISK_SIZE", "24G")
DRIVE_SECONDS = int(os.environ.get("DRIVE_SECONDS", "1800"))
ACCOUNT_USER = os.environ.get("ACCOUNT_USER", "docker")
ACCOUNT_PASS = os.environ.get("ACCOUNT_PASS", "admin")


def log(msg: str) -> None:
    print(f"[bake {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Serial:
    """The guest's serial console, as something that can be written to.

    qemu is given `-serial mon:stdio`, so its stdin is the guest's console. Opening a
    FIFO for writing BLOCKS until a reader appears, and a blocked open here is
    indistinguishable from a hung guest - fix_grub.py records the run that sat for
    fifteen minutes on exactly that. So the open is non-blocking and retried.
    """

    def __init__(self, path: str, timeout: float = 120.0):
        self.path = path
        self.timeout = timeout
        self.fh = None

    def open(self) -> None:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                # O_NONBLOCK on a write-only FIFO fails with ENXIO while there is no
                # reader, which is exactly the condition worth retrying.
                fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
                os.set_blocking(fd, True)
                self.fh = os.fdopen(fd, "w", buffering=1)
                return
            except OSError:
                time.sleep(1)
        raise SystemExit(f"no reader appeared on {self.path} within {self.timeout}s")

    def send(self, line: str, settle: float = 0.0) -> None:
        if self.fh is None:
            return
        self.fh.write(line + "\n")
        self.fh.flush()
        if settle:
            time.sleep(settle)


def feed_login(serial: Serial, workdir: str, disk: str) -> None:
    """Drive the live session from the serial console, before the GUI driver starts.

    Runs in a thread because the guest has to be typing while qemu starts and while
    the driver works: the sequence below is timed against the guest's own boot, not
    against the host's.
    """
    # Log in. Three cycles, not fourteen: every cycle after the first success types
    # 'demo' at a shell prompt, which runs it as a command and buries whatever
    # follows it.
    for _ in range(3):
        serial.send("demo", settle=3)
        serial.send("demo", settle=12)
    serial.send("", settle=3)
    serial.send("echo BAKE_LOGIN_OK $(id -u)", settle=4)

    # The disk has to look like a disk before the installer offers it: minstall runs
    # lsblk and parted, and a disk with no partition table has nothing for parted to
    # describe, so the drive list stays empty.
    serial.send("echo demo | sudo -S sgdisk --zap-all /dev/vda; echo ZAP_RC=$?", settle=8)

    # minstall with no arguments at all: the driver fills every pane.
    serial.send(
        'echo demo | sudo -S sh -c "minstall > /tmp/minstall.out 2>&1; '
        'echo MINSTALL_RC=$? > /tmp/mx-rc" &',
        settle=20,
    )
    serial.send("echo MINSTALL_STARTED; pgrep -a -f minstall | head -3", settle=40)


def main() -> int:
    out = os.environ.get("OUT")
    if not out:
        raise SystemExit("OUT must be set to the qcow2 to produce")

    work = os.path.dirname(os.path.abspath(out))
    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    os.chdir(work)

    def blog(msg: str) -> None:
        log(msg)
        with open(os.path.join(frames, "bake.log"), "a") as fh:
            fh.write(f"[bake {time.strftime('%H:%M:%S')}] {msg}\n")

    # --- the ISO ------------------------------------------------------------
    blog("=== ISO ===")
    image = os.path.join(work, "mx.iso")
    iso.fetch_iso("mx", image)

    # --- kernel and initrd --------------------------------------------------
    blog("=== kernel and initrd from /antiX ===")
    subprocess.run(["rm", "-rf", "x"], check=False)
    os.makedirs("x", exist_ok=True)
    subprocess.run(
        ["7z", "x", "-y", "-ox", image, "antiX/vmlinuz", "antiX/initrd.gz"],
        capture_output=True, check=False,
    )

    kernel = initrd = ""
    for root, _dirs, files in os.walk("x"):
        for name in files:
            path = os.path.join(root, name)
            if name.startswith("vmlinuz") and not kernel:
                kernel = path
            elif name.startswith("initrd") and not initrd:
                initrd = path
    blog(f"kernel: {kernel}")
    if not kernel or not initrd:
        blog("PAIR_FAIL")
        return 2
    blog(f"initrd: {initrd} ({os.path.getsize(initrd)} bytes)")

    # --- the disk -----------------------------------------------------------
    blog("=== disk ===")
    disk = os.path.join(work, "disk.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "disk.qcow2", DISK_SIZE], check=True)

    # --- the live session ---------------------------------------------------
    blog("=== the live session, with vnc and a recording ===")
    fifo = os.path.join(work, "serial.in")
    if os.path.exists(fifo):
        os.unlink(fifo)
    os.mkfifo(fifo)

    serial = Serial(fifo)
    feeder: threading.Thread | None = None

    def start_feeder() -> None:
        nonlocal feeder
        serial.open()
        feeder = threading.Thread(target=feed_login, args=(serial, work, disk), daemon=True)
        feeder.start()

    # The feeder blocks until qemu opens the other end of the FIFO, so it runs in its
    # own thread and qemu is started immediately after.
    threading.Thread(target=start_feeder, daemon=True).start()

    guest = vm.Guest(
        work,
        extra=[
            "-kernel", os.path.abspath(kernel),
            "-initrd", os.path.abspath(initrd),
            "-append", "console=ttyS0,115200n8 init=/lib/systemd/systemd",
            "-drive", f"file={os.path.abspath(image)},media=cdrom,readonly=on",
            "-drive", f"file={os.path.abspath('disk.qcow2')},if=virtio,format=qcow2",
        ],
        stdin=fifo,
        stdout=os.path.join(work, "console.log"),
    ).start()

    blog("=== waiting for vnc to answer ===")
    if not vm.wait_port("127.0.0.1", 5900, 90, "vnc"):
        blog("vnc did not answer")

    # Record every frame the guest draws, for the whole run.
    #
    # Not the VNC stream: this ffmpeg has no vnc demuxer ("Unknown input format:
    # 'vnc'"). The frames are captured over QMP instead and encoded afterwards,
    # which needs nothing the image does not already have.
    os.makedirs(os.path.join(frames, "shots"), exist_ok=True)
    snap_log = open(os.path.join(frames, "shots", "snapshots.log"), "a")
    snap = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "..", "..", "lib", "snapshots.py"),
         guest.paths.record, os.path.join(frames, "shots"),
         os.environ.get("SNAPSHOT_INTERVAL", "1"), "1500"],
        stdout=snap_log, stderr=subprocess.STDOUT,
    )
    blog(f"snapshot every {os.environ.get('SNAPSHOT_INTERVAL', '5')}s, pid {snap.pid}")

    # --- drive ---------------------------------------------------------------
    blog("=== driving the installer ===")
    driver = subprocess.run(
        [sys.executable, os.path.join(HERE, "drive-mx.py"), guest.paths.drive, frames],
        timeout=DRIVE_SECONDS, check=False,
    )
    blog(f"driver exit {driver.returncode}")

    blog("=== waiting for the installer to finish ===")
    if feeder is not None:
        feeder.join(timeout=120)

    guest.stop()
    snap.terminate()
    try:
        snap.wait(timeout=10)
    except subprocess.TimeoutExpired:
        snap.kill()
    snap_log.close()

    # --- did anything land? --------------------------------------------------
    blog("=== the disk so far ===")
    size = os.path.getsize(disk)
    blog(f"disk.qcow2 is {size} bytes")
    if size < 1_000_000_000:
        blog("=== THE DISK IS EMPTY ===")
        return 1

    # --- second boot: the bootloader ----------------------------------------
    blog("=== second boot: install the bootloader ===")
    subprocess.run(
        [sys.executable, os.path.join(HERE, "fix_grub.py"), work],
        check=False,
    )

    # --- third boot: the one that matters -----------------------------------
    #
    # Every check up to here reads the disk or reads a live session: 8 GB of
    # filesystem, a grub.cfg with 159 lines, RC_*=0 for each chroot step. None of
    # that is evidence that the installed system starts - and a recording that ends
    # after the second boot shows the INSTALLER a second time, which is exactly what
    # a viewer noticed.
    #
    # The pattern has to be something only the installed system prints. An earlier
    # version used "lightdm|login:|MX-25|MX 25" and matched after one second, because
    # GRUB's own menu says "Booting 'MX 25.3 Infinity'". GRUB's menu text is not
    # evidence that the system boots. A serial getty, or lightdm reaching its
    # greeter, is.
    blog("=== third boot: the installed system, with no ISO attached ===")
    smoke.check_disk(disk, root_fs="ext4")

    booted = smoke.boot_disk(
        disk,
        r"mx1 login:|Starting lightdm|Reached target Graphical Interface|lightdm-gtk-greeter",
        600,
        os.path.join(work, "smoke"),
        os.path.join(work, "shots-boot"),
    )
    blog("THE INSTALLED SYSTEM STARTED" if booted else "=== THE INSTALLED SYSTEM DID NOT REACH A LOGIN ===")

    # --- the recording ------------------------------------------------------
    #
    # One recording, every boot. The install happens in the first boot, the
    # bootloader in the second, and the proof in the third. The frames are
    # concatenated in capture order, so the video runs from the live ISO starting to
    # the installed system reaching its greeter.
    blog("=== encode the recording: install, bootloader, then the real boot ===")
    vm.record_many(
        os.path.join(work, "install.mp4"),
        os.path.join(frames, "shots"),
        os.path.join(work, "shots-grubfix"),
        os.path.join(work, "shots-boot"),
    )

    # --- compress -----------------------------------------------------------
    compress.compress("disk.qcow2", out)
    blog(f"BAKED mx -> {out}")
    return 0 if booted else 1


if __name__ == "__main__":
    raise SystemExit(main())
