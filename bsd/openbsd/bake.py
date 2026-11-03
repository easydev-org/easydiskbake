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

from lib import bsdinstall, compress, iso as isolib, live as livelib, serial, vm  # noqa: E402

OPENBSD_TAG = "iso-openbsd"


def log(message: str) -> None:
    print(f"[openbsd {time.strftime('%H:%M:%S')}] {message}", flush=True)


def answers() -> list[tuple[str, float]]:
    """The installer's questions, in order, with the time each answer needs.

    THE ORDER IS THE INSTALLER'S, not a script's idea of one. OpenBSD asks:
    keyboard, hostname, network, sshd, which sets, the disk, the layout, the sets again,
    root password, timezone. Anything out of order puts an answer into the wrong read().

    EVERY VALUE HERE HAS A REASON:

        (blank keyboard)      the default is us, which is what the image boots with
        openbsd               the hostname; short because it appears in every prompt
        vio0                  OpenBSD's virtio-net name on this qemu machine
        dhcp                  lib/vm.py's user-mode network is a DHCP server
        sshd=yes              installed AND enabled, which is the whole point of a stage
                              that has to be reachable
        (blank sets)          the default, which on a one-disk machine is everything
        sd0                   the only disk; the cdrom is not a candidate
        (blank layout)        the default auto layout, whole disk
        (blank, again)        "ready to install" - the set list is shown twice
        admin / admin         root password, twice
        UTC                   so the disk behaves the same wherever it boots
    """
    return [
        ("", 3),            # keyboard layout
        ("openbsd", 3),     # system hostname
        ("vio0", 3),        # which interface to configure
        ("dhcp", 25),       # addressing; DHCP takes a moment to answer
        ("", 3),            # IPv6: no
        ("", 3),            # DNS: the resolver the DHCP lease named
        ("sshd=yes", 3),    # start sshd at boot
        ("", 3),            # which sets: all of them
        ("sd0", 5),         # the disk to install to
        ("", 5),            # disk layout: the default, whole disk
        ("", 5),            # ready to install? the set list is confirmed here
        ("", 60),           # the sets are unpacked
        ("admin", 3),       # root password
        ("admin", 3),       # and again
        ("UTC", 3),         # timezone
        ("", 5),            # done - the installer reboots
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

    disk = os.path.join(work, "disk.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    subprocess.run(["qemu-img", "create", "-f", "qcow2", disk, args.disk_size],
                   check=True)
    log(f"disk created: {args.disk_size}")

    ser = serial.Serial(work, "serial")
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
        # THE SERIAL IS A CHARDDEV, not mon:stdio, because this bake WRITES to it.
        # OpenBSD's installer is on tty00 by default and every answer goes through here.
        serial_pipe=ser.stdin.removesuffix(".in"),
    )
    drives = [
        "-drive", f"file={iso_path},media=cdrom,readonly=on",
        "-drive", f"file={disk},if=virtio,format=qcow2",
    ]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)
    try:
        guest.start()
        ser.open(timeout=60)

        shots = os.path.join(work, "frames")
        os.makedirs(shots, exist_ok=True)
        snap = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
             guest.paths.record, shots,
             os.environ.get("SNAPSHOT_INTERVAL", "5"), "2000"],
            stdout=open(os.path.join(shots, "snapshots.log"), "wb"),
            stderr=subprocess.STDOUT,
        )
        live = livelib.start(
            [shots], os.path.join(work, "install.mp4"),
            os.path.join(work, "last-frame.png"),
            every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "20")),
        )

        log(f"waiting {args.boot_seconds}s for the installer's console")
        time.sleep(args.boot_seconds)

        log("=== answering the installer ===")
        for line, wait in answers():
            ser.say(line, wait=wait)
        ser.close()

        log(f"waiting up to {args.timeout}s for the installer to finish")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if not guest.alive():
                log("the guest exited (the installer reboots when it is done)")
                break
            time.sleep(15)
        else:
            log(f"=== THE INSTALLER DID NOT FINISH IN {args.timeout}s ===")
    finally:
        guest.stop()
        snap.terminate()
        try:
            snap.wait(timeout=5)
        except subprocess.TimeoutExpired:
            snap.kill()
        live.stop(final=True)

    if not bsdinstall.check_disk_ufs(disk):
        log("=== THE DISK HOLDS NO FFS FILESYSTEM ===")
        log("  the answers landed somewhere other than the questions")
        first_error(ser.sink)
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
