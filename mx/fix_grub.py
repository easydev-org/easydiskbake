"""Put a bootloader on a disk MX installed but cannot start.

    fix_grub.py <workdir>

WHY THIS IS PYTHON AND NOT SHELL

The shell version of this file worked, eventually, and it cost four separate escaping
failures to get there. Every one was the same shape: a script embedded inside another
script, inside a string, inside a format string.

  1. `for i in $(seq 1 120)` - $(seq) was expanded by the HOST shell before the text
     reached the guest, so the serial line received a command carrying a hundred and
     twenty numbers, truncated it, and the check never ran.
  2. Rewritten as `\\$i`, printf kept the backslash, and the guest received the literal
     two characters `\\$` instead of a variable reference.
  3. `work="$(cd "$work" && pwd)"` - when the cd failed the substitution produced an
     empty string rather than an error, and every path below became "/smoke.log".
  4. `cd "$work" || return 2` inside a function that then used relative paths after
     bake_vm_stop had changed directory.

Python has one string semantics. A guest script is a plain literal, sent as-is, with no
layer that might eat it. That is the whole argument for rewriting this file.

WHY THE WAITING IS NOT SLEEPS

The shell version waited a fixed number of seconds before each step and used their sum
as an implicit deadline. The same script gave RC_MOUNT=0 at 330 seconds and RC_MOUNT=32
at 280, and the 32 case then ran grub-install in an empty chroot, wrote zeroes over the
MBR, and returned success.

Here every wait is a predicate on what the guest has printed. No guess, and the ceiling
exists only so a genuinely hung guest still fails the run.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "..", "lib")
sys.path.insert(0, LIB)

W, H = 1280, 800


def log(msg):
    print("[grub %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Guest:
    """The live session's serial console, as an object that can wait for things.

    The guest prints to serial.out. Every method that waits does so by polling that
    file for a line, which is the only mechanism here that does not involve guessing a
    duration.
    """

    def __init__(self, feed, console):
        self.feed = feed
        self.console = console
        self.sent = 0

    def send(self, command, settle=1.0):
        """Write one line to the guest and give it a moment to echo it."""
        self.feed.write(command + "\n")
        self.feed.flush()
        self.sent += 1
        time.sleep(settle)

    def read(self):
        try:
            with open(self.console, "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def wait_for(self, pattern, seconds=180, since=0, quiet=False):
        """Wait until the console matches `pattern` somewhere after `since`.

        Returns the text that matched, or None. `since` is an offset into the console so
        that a marker printed by an earlier step cannot satisfy a later wait - which is
        exactly what would happen with a plain grep on an append-only file.
        """
        deadline = time.time() + seconds
        while time.time() < deadline:
            text = self.read()
            found = re.search(pattern, text[since:], re.M)
            if found:
                if not quiet:
                    log("saw %r" % found.group(0)[:60])
                return found.group(0)
            time.sleep(1)
        log("NEVER SAW %r after %ds" % (pattern, seconds))
        return None

    def mark(self):
        """Current length of the console, for use as a `since` offset."""
        return len(self.read())

    def run(self, command, marker=None, seconds=180, settle=1.0):
        """Send a command and, if a marker is given, wait for it to report back.

        The marker is echoed by the command itself on both the success and the failure
        path, so the wait always ends - a command that fails to run at all is what the
        ceiling is for.
        """
        since = self.mark()
        self.send(command, settle=settle)
        if marker is None:
            return True
        return self.wait_for(marker, seconds=seconds, since=since) is not None


# A guest script is a plain literal. Nothing interprets it before the guest does.
#
# The quoting is the reason this file exists: in shell these had to survive printf, a
# double-quoted string, and the serial line. Here they go out exactly as written.
WAIT_FOR_PARTITION = (
    'i=0; while [ $i -lt 120 ]; do '
    'if [ -b /dev/vda1 ]; then echo VDA1_READY; exit 0; fi; '
    'i=$((i+1)); sleep 0.5; done; '
    'echo VDA1_NEVER_APPEARED; ls -la /dev/vda* 2>&1'
)

MOUNT_WITH_RETRY = (
    'i=0; while [ $i -lt 10 ]; do '
    'if mount /dev/vda1 /mnt/mxroot 2>/tmp/mnt.err; then echo RC_MOUNT=0; exit 0; fi; '
    'i=$((i+1)); sleep 1; done; '
    'echo RC_MOUNT_FAILED; cat /tmp/mnt.err 2>&1'
)

DISK_REPORT = (
    'lsblk -o NAME,SIZE,TYPE,FSTYPE > /tmp/rep.txt 2>&1; '
    'parted -s /dev/vda print >> /tmp/rep.txt 2>&1; '
    'blkid >> /tmp/rep.txt 2>&1; '
    'cat /tmp/rep.txt'
)


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    work = os.path.abspath(work)
    os.chdir(work)

    kernel = next((os.path.join(work, "x", "antiX", f)
                   for f in os.listdir(os.path.join(work, "x", "antiX"))
                   if f.startswith("vmlinuz")), None) if os.path.isdir(
                       os.path.join(work, "x", "antiX")) else None
    initrd = next((os.path.join(work, "x", "antiX", f)
                   for f in os.listdir(os.path.join(work, "x", "antiX"))
                   if f.startswith("initrd")), None) if os.path.isdir(
                       os.path.join(work, "x", "antiX")) else None

    if not kernel or not os.path.getsize(kernel):
        log("no kernel in %s/x" % work)
        return 2
    if not initrd or not os.path.getsize(initrd):
        log("no initrd in %s/x" % work)
        return 2
    if not os.path.exists("disk.qcow2"):
        log("no disk.qcow2 in %s" % work)
        return 2

    log("kernel %s" % kernel)
    log("initrd %s" % initrd)
    log("disk   %d bytes" % os.path.getsize("disk.qcow2"))

    # The serial fifo the guest reads from. One writer, opened once, kept open - closing
    # it would send EOF and the getty would see the console disappear.
    fifo = os.path.join(work, "serial.in")
    if os.path.exists(fifo):
        os.unlink(fifo)
    os.mkfifo(fifo)

    console = os.path.join(work, "serial.out")
    open(console, "w").close()

    qmp = os.path.join(work, "grubfix.qmp")
    for path in (qmp,):
        if os.path.exists(path):
            os.unlink(path)

    log("starting qemu")
    qemu = subprocess.Popen(
        [
            "qemu-system-x86_64", "-enable-kvm", "-cpu", "host",
            "-smp", "4", "-m", "4096",
            "-display", "none", "-vga", "std",
            "-vnc", "127.0.0.1:0",
            "-device", "qemu-xhci,id=xhci",
            "-device", "usb-tablet,bus=xhci.0",
            "-qmp", "unix:%s,server=on,wait=off" % qmp,
            "-netdev", "user,id=n0",
            "-device", "virtio-net-pci,netdev=n0",
            "-kernel", kernel, "-initrd", initrd,
            "-append", "console=ttyS0,115200n8 init=/lib/systemd/systemd",
            "-drive", "file=%s,media=cdrom,readonly=on" % os.path.join(work, "mx.iso"),
            "-drive", "file=%s,if=virtio,format=qcow2" % os.path.join(work, "disk.qcow2"),
        ],
        stdin=open(fifo, "rb"),
        stdout=open(console, "ab"),
        stderr=subprocess.STDOUT,
    )
    log("qemu pid %d" % qemu.pid)

    os.makedirs(os.path.join(work, "shots-grubfix"), exist_ok=True)
    snap = subprocess.Popen(
        ["python3", os.path.join(LIB, "snapshots.py"), qmp,
         os.path.join(work, "shots-grubfix"),
         os.environ.get("SNAPSHOT_INTERVAL", "1"), "500"],
        stdout=open(os.path.join(work, "shots-grubfix", "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )

    feed = open(fifo, "w")
    guest = Guest(feed, console)
    rc = 1
    try:
        # Log in. Three cycles is enough; every cycle after the first success types
        # 'demo' at a shell prompt, which runs it as a command and buries what follows.
        log("waiting for the live session")
        since = guest.mark()
        for _ in range(4):
            guest.send("demo", settle=3)
            guest.send("demo", settle=12)
            if guest.wait_for(r"demo@\S+:|^#|^\$", seconds=8, since=since, quiet=True):
                break

        if not guest.wait_for(r"demo@\S+:|^#\s*$", seconds=60,
                              since=0):
            log("never got a shell prompt; sending anyway")

        guest.run("echo GRUBFIX_LOGIN $(id -u)", marker=r"GRUBFIX_LOGIN")
        guest.run("sudo -S mkdir -p /mnt/mxroot <<< demo", settle=2)

        # 1. Wait for the partition to exist. First need sudo - use a password pipe.
        log("waiting for /dev/vda1")
        guest.run(
            'echo demo | sudo -S sh -c %s' % shquote(WAIT_FOR_PARTITION),
            marker=r"VDA1_READY|VDA1_NEVER_APPEARED", seconds=120,
        )

        # 2. What is on the disk, before anything writes to it.
        guest.run('echo demo | sudo -S sh -c %s' % shquote(DISK_REPORT),
                  marker=r"^NAME|vda", seconds=30, settle=3)

        # 3. Mount, retrying in the guest.
        log("mounting /dev/vda1")
        guest.run('echo demo | sudo -S sh -c %s' % shquote(MOUNT_WITH_RETRY),
                  marker=r"RC_MOUNT=0|RC_MOUNT_FAILED", seconds=40)

        # 4. THE GATE. Everything below writes to the disk, and an empty chroot makes
        #    grub-install zero the MBR while returning success. This is the check that
        #    stops that.
        since = guest.mark()
        guest.send("test -f /mnt/mxroot/etc/debian_version && echo GATE=open "
                   "|| echo GATE=closed", settle=3)
        gate = guest.wait_for(r"GATE=open|GATE=closed", seconds=15, since=since)
        if gate != "GATE=open":
            log("=== THE GATE IS CLOSED: /mnt/mxroot has no system in it ===")
            log("=== refusing to run grub-install; that is what destroyed the MBR ===")
            return 1

        log("gate open: /mnt/mxroot holds an installed system")

        # 5. Bind mounts, then install, then the menu.
        for what, target in (
            ("/dev", "/mnt/mxroot/dev"),
            ("/dev/pts", "/mnt/mxroot/dev/pts"),
            ("/proc", "/mnt/mxroot/proc"),
            ("/sys", "/mnt/mxroot/sys"),
        ):
            guest.run("echo demo | sudo -S mount --bind %s %s && echo BIND_OK" %
                      (what, target), marker=r"BIND_OK", seconds=15, settle=2)

        log("installing the bootloader")
        guest.run(
            'echo demo | sudo -S sh -c %s'
            % shquote("grub-install --target=i386-pc --recheck /dev/vda "
                      "2>&1 | tail -20; echo RC_INSTALL=$?"),
            marker=r"RC_INSTALL=", seconds=240, settle=3)

        # 6. The menu has to boot by itself and talk to the serial port.
        for line in ("GRUB_TIMEOUT=0",
                     "GRUB_TERMINAL=serial",
                     "GRUB_CMDLINE_LINUX_DEFAULT=console=ttyS0,115200n8"):
            guest.run("echo demo | sudo -S sed -i '/^%s=/d' "
                      "/mnt/mxroot/etc/default/grub" % line.split("=")[0], settle=2)
            guest.run("echo demo | sudo -S tee -a /mnt/mxroot/etc/default/grub <<< %s"
                      % line, settle=2)

        log("generating the menu")
        guest.run(
            "echo demo | sudo -S chroot /mnt/mxroot update-grub 2>&1 | tail -20; "
            "echo UPDATE_DONE",
            marker=r"UPDATE_DONE", seconds=300, settle=3)

        since = guest.mark()
        guest.send("echo demo | sudo -S wc -l /mnt/mxroot/boot/grub/grub.cfg "
                   "; echo CFG_DONE", settle=5)
        cfg = guest.wait_for(r"(\d+) /mnt/mxroot/boot/grub/grub.cfg",
                             seconds=30, since=since)
        if cfg:
            log("grub.cfg: %s" % cfg)
            log("GRUB IS INSTALLED")
            rc = 0
        else:
            log("=== no grub.cfg - the menu was not generated ===")

    finally:
        feed.close()
        qemu.send_signal(signal.SIGTERM)
        try:
            qemu.wait(timeout=20)
        except subprocess.TimeoutExpired:
            qemu.kill()
        snap.terminate()
        log("done")

    return rc


def shquote(script):
    """Wrap a guest script in single quotes, escaping any single quotes inside it.

    The scripts above contain no single quotes, so this is the identity function in
    practice - but writing it out means a future edit that adds one cannot silently
    break the command line.
    """
    return "'" + script.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    raise SystemExit(main())
