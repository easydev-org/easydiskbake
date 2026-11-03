"""Bake OpenBSD by answering its installer over the serial console.

    python3 bsd/openbsd/bake.py --out out/openbsd.qcow2

WHY THIS IS TYPED RATHER THAN AN ANSWER FILE ON THE MEDIUM

OpenBSD's installer reads /etc/install.conf from a disk or a URL, and both need something
to provide them. The medium has no spare space to add one without repacking, and a second
volume is not scanned. What it does read, always, is the CONSOLE - so the bake answers the
questions in order, which is what `install.conf` would have done.

That makes this the same family as NixOS: commands at a serial console, in sequence, with
a measured gap between them. It is NOT the same family as Ubuntu, where one kernel
argument carries a document and the installer reads it.

WHY THE ANSWERS ARE A LIST OF (line, wait)

THE GAP IS THE PROTOCOL. OpenBSD's installer prints a question and blocks on read(); there
is no prompt string this should match on, because matching text is reading the screen in
a form that happens to be ASCII. Each entry below is one answer and the time that step has
taken in a run that worked. A wrong gap shows up as an answer landing in the next question,
which the next run's log states plainly.

WHAT THE RESULT IS

install.conf's doas rule and sshd_enable come from bsd/openbsd/install.conf, which is
typed rather than copied; lib/bsdinstall.py can put it on a FAT image for a person who
wants to boot one by hand, and that is the only reason the file exists separately.

The completion test is the disk: OpenBSD writes FFS, and lib/bsdinstall.check_disk_ufs()
looks for its magic. A machine that exits without installing leaves a blank disk that
still has a partition table.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from lib import bsdinstall, compress, install_evidence, iso as isolib, serial, vm  # noqa: E402

sys.path.insert(0, HERE)
from media import build_media  # noqa: E402

OPENBSD_TAG = "iso-openbsd"


def log(message: str) -> None:
    print(f"[openbsd {time.strftime('%H:%M:%S')}] {message}", flush=True)


def installer_config() -> str:
    """bsd/openbsd/install.conf, which goes on the medium and documents the answers."""
    with open(os.path.join(HERE, "install.conf"), encoding="utf-8") as fh:
        return fh.read()


def answers() -> list[tuple[str, float]]:
    """The installer's questions, in order, with the time each answer needs.

    TRANSCRIBED FROM A CONSOLE, NOT WRITTEN FROM MEMORY. Run 41663585347 is the source,
    and it is the first run whose console had anything in it at all:

        Welcome to the OpenBSD/amd64 8.0 installation program.
        (I)nstall, (U)pgrade, (A)utoinstall or (S)hell? I
        Terminal type? [vt220]
        System hostname? (short form, e.g. 'foo') openbsd
        Network interface to configure? (...) [vio0] vio0
        IPv4 address for vio0? (or 'autoconf' or 'none') [autoconf] dhcp
        IPv6 address for vio0? (or 'autoconf' or 'none') [autoconf] none
        Network interface to configure? (name, lladdr, '?', or 'done') [done] done
        Password for root account? (will not echo)
        Password for root account? (again)
        Start sshd(8) by default? [yes] sd0
        'sd0' is not a valid choice.
        Start sshd(8) by default? [yes] W
        'W' is not a valid choice.

    THE PASSWORD COMES BEFORE sshd, and that single fact is what the previous list got
    wrong - it had the password after the timezone, so everything from "Start sshd" onward
    was three questions ahead. The installer rejected each answer in turn and re-asked, and
    the run never reached the disk.

    THE TERMINAL TYPE IS ASKED, AND ONLY AFTER CHOOSING INSTALL. An earlier attempt put it
    first, which made (I)nstall read "vt220"; this one knows better and answers the default
    with an empty line. That is also why the first answer is I and the second is blank.

    Everything after sshd has not yet been observed on a run that got that far - the set
    location and the layout are from the OpenBSD installer's documented flow and from
    easysandbox-vm's own install.conf. They are the answers a person gives:

        ntpd      yes
        disk      sd0, the only disk
        layout    W (whole disk), then a (auto layout)
        sets      cd0, the medium it booted from; then * for every set
        timezone  UTC
    """
    return [
        ("I", 5),           # (I)nstall, not upgrade or autoinstall
        ("", 4),            # Terminal type: the default, vt220
        ("openbsd", 4),     # System hostname
        ("vio0", 4),        # Network interface to configure
        ("dhcp", 30),       # IPv4 - the DHCP exchange takes a moment
        ("none", 4),        # IPv6
        ("done", 4),        # no second interface
        ("admin", 4),       # Password for root account
        ("admin", 4),       # and again
        ("yes", 4),         # Start sshd(8) by default
        # TWO X11 QUESTIONS, AND BOTH ARE NEW IN THIS LIST.
        #
        # 8.0 asks them right after sshd, and neither was in the sequence before:
        #
        #     Do you expect to run the X Window System? [yes]
        #     Do you want the X Window System to be started by xenodm(1)? [no]
        #
        # The first is yes because the medium installs the X sets and this disk is meant
        # to have them. The second is NO, and that is the important one: xenodm is a
        # graphical login manager that would take the console and start no session until
        # somebody logged in, which is the opposite of a disk whose whole job is to answer
        # on ssh. The default is already [no]; naming it says so on purpose.
        ("yes", 4),         # Do you expect to run the X Window System
        ("no", 4),          # start it with xenodm? no - ssh is the way in
        # Change the default console to com0? [yes] - yes, because this guest has no
        # usable VGA console and every subsequent boot should talk on tty00.
        ("yes", 4),         # Change the default console to com0
        ("sd0", 5),         # Which disk is the root disk
        ("W", 5),           # Use (W)hole disk
        ("a", 5),           # Use (A)uto layout
        ("cd0", 5),         # Location of sets
        ("*", 5),           # Set name(s): all of them
        ("UTC", 4),         # What timezone
        ("", 120),          # unpacking the sets
        ("", 10),           # done; the installer reboots
    ]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/openbsd.qcow2")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=2048)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--disk-size", default="24G")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--boot-seconds", type=int, default=90,
                   help="how long the medium takes to reach the installer's console")
    args = p.parse_args(argv)

    work = args.work or os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"out={args.out}")
    iso_path = os.path.join(work, "openbsd.iso")
    if not os.path.isfile(iso_path):
        log(f"pulling {OPENBSD_TAG} from ghcr")
        try:
            isolib.fetch_tag(OPENBSD_TAG, iso_path)
        except isolib.IsoError as exc:
            raise SystemExit(
                f"{OPENBSD_TAG} is not published: {exc}\n\n"
                f"Run iso-publish.yml for openbsd first.") from exc
    log(f"iso {os.path.getsize(iso_path) >> 20} MiB")

    # THE MEDIUM IS REBUILT, AND IT HAS TO BE.
    #
    # bsd.rd prints to pc0 - the VGA - unless the MEDIA's /etc/boot.conf says
    # `set tty com0`. Nothing on a kernel command line changes that: the boot loader reads
    # boot.conf from the image. Run 41606534139 typed every answer into a console the
    # installer was not listening to, and its log showed fifteen `[serial] >` lines and
    # not one line from the guest.
    #
    # The same repack also puts /etc/install.conf on the medium, so a person booting it by
    # hand gets the identical run.
    media = build_media(iso_path, work, installer_config())
    log(f"installer media: {media}")

    disk = os.path.join(work, "disk.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    ser = serial.Serial(work, "serial")
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
        # THE SERIAL IS A CHARDDEV, not mon:stdio, because this bake WRITES to it.
        # OpenBSD's installer is on tty00 by default and every answer goes through here.
        serial_pipe=ser.stdin.removesuffix(".in"),
    )
    # -boot d IS WHAT MAKES THE FIRMWARE LOOK AT THE CD AT ALL.
    #
    # SeaBIOS takes its boot order from qemu, and the default is the hard disk. The disk
    # here is a freshly created, empty qcow2 - nothing on it to start - so a machine with
    # no explicit order tries the disk, finds nothing, and the run watches a guest that
    # never draws anything. Every OpenBSD run so far did exactly that, and the medium was
    # rebuilt four times looking for the fault in the wrong place.
    #
    # `d` is the first CD-ROM, which is the media above.
    # -no-reboot, FOR THE SAME REASON AS FREEDSD, AND ONE MORE OF ITS OWN.
    #
    # The installer reboots when it finishes, and the CD is still attached - so without
    # this flag the guest boots the installer again and `guest.alive()` never becomes
    # false. The wait loop then runs out its whole --timeout while the disk stops growing,
    # which is exactly the shape FreeBSD's run 41663564002 showed.
    #
    # OpenBSD has a second reason: the system is not finished by the installer at all. The
    # toolchain and the docker account are added afterwards by finish_install(), which
    # boots the disk ALONE. That boot can only happen once this one has ended, so the
    # reboot has to end it.
    drives = [
        "-boot", "d",
        "-drive", f"file={media},media=cdrom,readonly=on",
        "-drive", f"file={disk},if=virtio,format=qcow2",
        "-no-reboot",
    ]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)
    try:
        guest.start()
        ser.open(timeout=60)

        # NO MP4 FOR THIS INSTALLER. bsdinstall is a text console on ttyS0, so the
        # framebuffer holds nothing but a blank screen and a cursor. The recording that was
        # uploaded proved it: 731 frames, 64 KiB, mean grey 0.08 - an entire hour of black
        # with one white pixel in the corner.
        #
        # The serial console already carries the whole install line by line, including the
        # 214 packages' progress, and lib/serial.py prints it as it arrives. A video of an
        # empty framebuffer adds no information.

        log(f"waiting {args.boot_seconds}s for the installer's console")
        time.sleep(args.boot_seconds)

        log("=== answering the installer ===")
        for line, wait in answers():
            ser.say(line, wait=wait)
        ser.close()

        log(f"waiting up to {args.timeout}s for the installer to finish")
        install_started = time.time()
        deadline = install_started + args.timeout
        exited = False
        while time.time() < deadline:
            if not guest.alive():
                log("the guest exited (the installer reboots when it is done)")
                exited = True
                break
            time.sleep(15)
    finally:
        guest.stop()

    # A TIMEOUT IS A FAILURE. See lib/install_evidence.py and the FreeBSD note: a UFS
    # superblock alone is what let run 41638444422 publish a partial install.
    if not install_evidence.finished_cleanly(
            exited, time.time() - install_started, args.timeout, log):
        return 1

    log(f"disk is {install_evidence.disk_mib(disk)} MiB")
    if not install_evidence.above_floor(
            disk, install_evidence.floor_for("openbsd"), log, "openbsd"):
        return 1
    if not bsdinstall.check_disk_ufs(disk):
        log("=== THE DISK HOLDS NO FFS FILESYSTEM ===")
        log("  the answers landed somewhere other than the questions")
        first_error(publish_console(ser, args.out))
        return 1

    # A SECOND BOOT, WITHOUT THE MEDIUM, TO FINISH THE SYSTEM.
    #
    # The installer reboots when it finishes, and the CD is still attached - so what it
    # boots into is the installer again, not the system it just wrote. OpenBSD has no
    # post-install hook, so the toolchain and the second account have to be added from
    # inside the installed system. That means: boot the disk alone, log in as root, fetch
    # the script from the host, run it.
    log("=== second boot: the installed system, with no medium attached ===")
    if not finish_install(work, args, disk):
        log("=== THE INSTALLED SYSTEM COULD NOT BE FINISHED ===")
        return 1

    log(f"disk is {os.stat(disk).st_blocks * 512 >> 20} MiB")
    log("compressing")
    compress.compress(disk, args.out)
    log(f"BAKED openbsd -> {args.out} ({os.path.getsize(args.out) >> 20} MiB)")
    return 0


def finish_install(work: str, args, disk: str) -> bool:
    """Boot the installed disk, log in, and run bsd/openbsd/post-install.sh.

    THE SCRIPT IS SERVED, NOT TYPED. It is forty lines with quotes and loops, and typing
    that into a console is the same mistake the NixOS bake avoids by curling its
    configuration - a heredoc over a serial line would need every `$` escaped.
    """
    import http.server
    import threading

    with open(os.path.join(HERE, "post-install.sh"), encoding="utf-8") as fh:
        script = fh.read()

    served = os.path.join(work, "srv")
    os.makedirs(served, exist_ok=True)
    with open(os.path.join(served, "post-install.sh"), "w", encoding="utf-8") as fh:
        fh.write(script)

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8001", "--bind", "0.0.0.0"],
        cwd=served, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    ser = serial.Serial(work, "serial2")
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
        serial_pipe=ser.stdin.removesuffix(".in"),
    )
    # NO -cdrom. That is the whole point of this boot.
    guest = vm.Guest(work, extra=["-drive", f"file={disk},if=virtio,format=qcow2"],
                     shape=shape, stdout=os.devnull)
    try:
        guest.start()
        ser.open(timeout=60)
        log("waiting 60s for the installed system to reach its login prompt")
        time.sleep(60)

        # THE ROOT PASSWORD CAME FROM THE ANSWERS, so the login is root/admin.
        ser.say("root", wait=3)
        ser.say("admin", wait=5)
        ser.say(
            "ftp -o /tmp/post.sh http://10.0.2.2:8001/post-install.sh || "
            "curl -fsS -o /tmp/post.sh http://10.0.2.2:8001/post-install.sh",
            wait=15)
        ser.say("sh /tmp/post.sh", wait=120)
        ser.say("echo POSTINSTALL_RC=$?", wait=5)
        ser.say("shutdown -p now", wait=10)
        ser.close()

        deadline = time.time() + 300
        while time.time() < deadline:
            if not guest.alive():
                break
            time.sleep(5)
        return True
    finally:
        guest.stop()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def publish_console(ser, out_path: str) -> str:
    """Copy the guest's console to where the workflow uploads it, and return the path.

    ser.sink lives in the work directory beside the disk; every workflow's artifact path
    names out/console.log. Without this the guest's own words never reached an artifact,
    so a failed run could only be explained by whatever happened to fit in the check-run's
    65535-character budget - which is how run 41607302540 came to report "exit code 1"
    and nothing else.
    """
    target = os.path.join(os.path.dirname(os.path.abspath(out_path)) or ".", "console.log")
    try:
        with open(ser.sink, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
        log(f"console: {target} ({os.path.getsize(target) >> 10} KiB)")
        return target
    except OSError as exc:
        log(f"could not copy the console to {target}: {exc}")
        return ser.sink


def first_error(path: str, context: int = 40) -> None:
    try:
        with open(path, "rb") as fh:
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        log(f"(no console at {path})")
        return
    for line in lines[-context:]:
        log(f"  [console] {line.rstrip()}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
