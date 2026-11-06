"""Bake Ubuntu by answering subiquity's autoinstall, with no screen at all.

    python3 linux/ubuntu/bake.py --out out/ubuntu.qcow2

THE SAME MECHANISM AS NIXOS, FOR AN OPPOSITE REASON

NixOS has no answer file and is typed at a console. Ubuntu has one, and it is the whole
installation: subiquity fetches a YAML document over the network at boot and does
everything in it. So both bakes boot the ISO's own kernel with -kernel/-initrd - which is
how the kernel reaches the config server before anything else is up - and neither one
looks at a screen.

WHY -kernel/-initrd AND NOT THE ISO'S BOOT MENU

The config is found from `ds=nocloud-net;s=http://{net.BRIDGE_IP}:PORT/` on the kernel command
line. Getting that onto the command line through the ISO's own GRUB means typing it at
the GRUB prompt, which is a screen this would then have to read. Booting the ISO's kernel
directly puts it there in the qemu arguments.

The kernel and initrd come OUT OF THE ISO, and they have to: Ubuntu's installer ramdisk
knows how to fetch and mount the ISO, and an ordinary kernel does not. Read from
casper/vmlinuz and casper/initrd, which is where every Ubuntu live ISO puts them.

WHY THE COMPLETION TEST IS THE DISK, NOT THE SCREEN

subiquity prints its progress to a console that scrolls, and the last thing it does is
reboot. `-no-reboot` makes qemu exit on that reboot, which is a fact rather than a
picture. The disk is then checked for an ext4 root - see lib/smoke.py - because a
partition table with no filesystem on it is what a failed install leaves behind.

WHY THERE IS NO "UBUNTU DESKTOP" BY DEFAULT

This installs whatever the autoinstall says, and the flavours differ only in that
document. linux-kubuntu/ does the same thing with a KDE one; the machinery is here so the
next flavour does not re-derive it.
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

from lib import compress, install_evidence, iso, serial, session, smoke, vm  # noqa: E402
import net  # noqa: E402 - the one place the guest's gateway is named

CONFIG_PORT = 8000
UBUNTU_TAG = "iso-ubuntu"

# The seed directory name is arbitrary but its URL must end in a slash - subiquity
# appends "user-data" and "meta-data" to whatever it is given, and a URL without the
# trailing slash gets them concatenated onto the last segment.
SEED = "autoinstall/"


def log(message: str) -> None:
    print(f"[ubuntu {time.strftime('%H:%M:%S')}] {message}", flush=True)


def extract_from_iso(iso_path: str, member: str, dest: str) -> str:
    """Pull one file out of the ISO by its path inside the image.

    7z matches nothing when the pattern contains slashes, so the candidate is found by
    basename and then chosen by full suffix - the same lesson linux/nixos/bake.py
    records, and the same helper shape.
    """
    leaf = os.path.basename(member)
    result = subprocess.run(
        ["7z", "x", "-y", "-r", f"-o{dest}", iso_path, leaf],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"7z could not extract {leaf} from {iso_path}: "
            f"{(result.stderr or result.stdout).strip()[:300]}")

    wanted = member.lstrip("/")
    found = []
    for root, _dirs, files in os.walk(dest):
        for name in files:
            path = os.path.join(root, name)
            if path.replace("\\", "/").endswith(wanted):
                found.append(path)
    if len(found) == 1:
        return found[0]
    raise SystemExit(
        f"{member} matched {len(found)} files under {dest}. The ISO's layout has "
        f"changed, or the basename is not unique there.")


def serve(work: str, flavour: str) -> subprocess.Popen:
    """Serve the seed: user-data (the autoinstall document) and meta-data.

    META-DATA MUST EXIST EVEN IF IT IS EMPTY. cloud-init reports "no instance-id" and
    drops to the interactive installer without it, which on a bake with no screen looks
    exactly like a hang.
    """
    seed = os.path.join(work, "srv", SEED)
    os.makedirs(seed, exist_ok=True)

    src = os.path.join(HERE, f"user-data-{flavour}.yaml")
    if not os.path.isfile(src):
        raise SystemExit(f"no {src}")
    with open(src, encoding="utf-8") as fh:
        document = fh.read()
    with open(os.path.join(seed, "user-data"), "w", encoding="utf-8") as fh:
        fh.write(document)
    with open(os.path.join(seed, "meta-data"), "w", encoding="utf-8") as fh:
        fh.write("instance-id: easydiskbake\n")

    return subprocess.Popen(
        [sys.executable, "-m", "http.server", str(CONFIG_PORT), "--bind", "0.0.0.0"],
        cwd=os.path.join(work, "srv"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def boot_installed(work: str, args, disk: str) -> bool:
    """Boot the disk the installer wrote, with no medium attached, and watch it.

    NO CDROM. That is what makes this a test of the disk rather than of the installer:
    the machine is the system it wrote, and the firmware's own boot order.

    IT WATCHES FOR A BOUNDED TIME AND THEN REPORTS WHAT THE FRAMES SHOW. The desktop
    arrival test is deliberately NOT here yet - a colour test has to be measured against
    a real GNOME and a real KDE screen, and guessing one from a Windows desktop is the
    mistake this repository keeps recording. This boot's job is to produce the recording
    and the frames that measurement will be taken from.
    """
    shape = vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std")
    guest = vm.Guest(work, extra=["-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap"],
                     shape=shape, stdout=os.devnull)
    shots2 = os.path.join(work, "frames2")
    os.makedirs(shots2, exist_ok=True)
    try:
        guest.start()
        # NO RECORDING HERE. See the note at the installer's own capture below.
        # THE DESKTOP IS VERIFIED BY ITS WAYLAND SESSION, NOT BY ITS PIXELS.
        #
        # This used to watch for the whole of --timeout and then return True regardless -
        # "the watch window ended; the frames say what it was showing". Two consequences,
        # both bad: every bake spent an hour here doing nothing, and nothing was ever
        # verified. Run 41638400412 sat at "2640s since the disk was started" with a
        # desktop that had been up for most of it.
        #
        # So this logs in and asks the running system, exactly as linux/nixos/launch.py
        # does: an active Wayland session whose Class is not a greeter, with the desktop's
        # compositor running and serving a WAYLAND_DISPLAY socket. A picture cannot answer
        # that - it cannot tell a compositor that does not draw on the emulated adapter
        # from one that never started.
        arrived = session.wait_for_wayland(
            args.ssh_port, args.compositor, args.timeout, log,
            ssh_seconds=min(args.timeout, 600))
        if not arrived:
            return False

        return True
    finally:
        guest.stop()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/ubuntu.qcow2")
    p.add_argument("--flavour", default="server",
                   help="which user-data-<flavour>.yaml to install; server for now")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--disk-size", default="128G")
    # ONE NUMBER, TWO PHASES. The installer gets this, and so does the boot of the
    # installed disk that follows it - see boot_installed(). The job's ceiling in the
    # workflow is the sum of both plus the encode.
    p.add_argument("--timeout", type=int, default=3600,
                   help="seconds EACH phase may run: the install, then the second boot")
    # THE SECOND BOOT IS JUDGED OVER SSH, SO THE FORWARDED PORT AND THE COMPOSITOR'S NAME
    # ARE INPUTS. lib/vm.py forwards 2222 -> 22 by default; the compositor is which binary
    # proves the session is up, and it differs per flavour:
    #     ubuntu    gnome-shell, the GNOME session's compositor
    #     kubuntu   kwin_wayland (NOT plasmashell - kwin owns the display server and is
    #               the process that fails when there is no DRM node)
    p.add_argument("--ssh-port", type=int, default=2222,
                   help="host port forwarded to the guest's 22")
    p.add_argument("--compositor", default="gnome-shell",
                   help="the process that proves the Wayland session is running")
    args = p.parse_args(argv)

    work = args.work or os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"out={args.out} flavour={args.flavour}")
    iso_path = os.path.join(work, "ubuntu.iso")
    if not os.path.isfile(iso_path):
        log(f"pulling {UBUNTU_TAG} from ghcr")
        try:
            iso.fetch_tag(UBUNTU_TAG, iso_path)
        except iso.IsoError as exc:
            raise SystemExit(
                f"{UBUNTU_TAG} is not published: {exc}\n\n"
                f"Run artifacts-publish.yml for ubuntu first.") from exc
    log(f"iso {os.path.getsize(iso_path) >> 20} MiB")

    tree = os.path.join(work, "iso")
    kernel = extract_from_iso(iso_path, "casper/vmlinuz", tree)
    initrd = extract_from_iso(iso_path, "casper/initrd", tree)
    log(f"kernel {kernel}")
    log(f"initrd {initrd}")

    disk = os.path.join(work, "disk.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    server = serve(work, args.flavour)
    seed_url = f"http://{net.BRIDGE_IP}:{CONFIG_PORT}/{SEED}"
    log(f"seed served at {seed_url}")

    # THE KERNEL COMMAND LINE, and each part of it is load-bearing:
    #
    #   autoinstall             subiquity, do an unattended install
    #   ds=nocloud-net;s=URL    where the seed is. NO QUOTES AROUND IT - see below.
    #   ip=dhcp                 bring the network up before cloud-init looks for it
    #   console=ttyS0,115200n8  so the guest's output reaches the serial log at all
    #
    # NO QUOTES AROUND ds=, AND THAT IS THE FIX FOR RUNS 41602915388 AND 41606512682.
    #
    # Every example of this on the internet writes
    #
    #     -append 'autoinstall ds=nocloud-net;s=http://host:port/'
    #
    # where the quote belongs to the SHELL - it tells bash to keep the semicolon inside
    # one argument. This append is already one argv element handed straight to qemu, with
    # no shell anywhere, so quoting it put literal quotation marks into /proc/cmdline:
    #
    #     Command line: autoinstall "ds=nocloud-net;s=http://the bridge address:8000/autoinstall/" ...
    #
    # cloud-init splits that on spaces and gets the token `"ds=nocloud-net;s=http://...`
    # starting with a quotation mark, so it recognises no datasource, runs all three of
    # its stages, finds nothing to do, and the disk stays at 0 MiB for as long as the
    # bake watches it.
    #
    # NO `---` EITHER, AND NO cloud-config-url. The media's own menu entry is
    #
    #     linux /casper/vmlinuz ---
    #
    # with nothing after the separator, because `---` there is casper's end-of-kernel-
    # arguments marker and the autoinstall parameters are meant to be typed in front of
    # it by whoever boots with them. cloud-config-url=/dev/null belongs to a PXE setup
    # that fetches the ISO over the network; with a local cdrom it suppresses the very
    # lookup this needs.
    append = (
        "autoinstall "
        f"ds=nocloud-net;s={seed_url} "
        "ip=dhcp console=ttyS0,115200n8"
    )

    # A SERIAL PIPE, SO THE SECOND BOOT CAN BE DRIVEN OVER SSH AND ITS SESSION READ.
    #
    # The installer stage already prints to console=ttyS0 through qemu's stdio, which is
    # enough for subiquity. The second boot needs more: the desktop is verified by logging
    # in and asking logind whether a Wayland session exists - the same verdict
    # linux/nixos/launch.py uses, and for the same reason. A pixel test cannot tell a
    # compositor that does not draw on the emulated adapter from one that failed to start.
    #
    # The chardev is on a fifo because lib/serial.py owns the guest's console for this
    # bake; qemu's stdout then carries only warnings. See `stdout=` on the Guest below.
    ser = serial.Serial(work, "serial")
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        vga="std",
        kernel_append=append,
        serial_pipe=ser.stdin.removesuffix(".in"),
        # THE ISO IS STILL ATTACHED. The autoinstall document names no package source;
        # subiquity installs from the medium it booted from, and it finds that medium by
        # a filename inside it. Booting the kernel directly does not remove the need for
        # the ISO to be present.
    )
    # -no-reboot IS WHAT MAKES "THE INSTALL FINISHED" A FACT INSTEAD OF A TIMEOUT.
    #
    # The autoinstall document ends with `shutdown: reboot`, and subiquity does exactly
    # that. Without this flag the guest restarts into the installer it is still attached
    # to - the same -kernel, the same initrd, the same ISO - and runs the install again,
    # and again. `guest.alive()` then never becomes false, so the loop below waits out
    # the whole of --timeout and exits through its else branch:
    #
    #     === THE INSTALLER DID NOT FINISH IN 3600s ===
    #       the installer is still running; not booting the disk
    #
    # and boot_installed() never runs, so the second boot and the desktop are never
    # recorded. Run 41629132156 is that: the disk stopped growing at 19,584 MiB - the
    # install was COMPLETE - and the bake still had twenty minutes of its budget left
    # when nothing further could happen.
    #
    # The docstring at the top of this file has described this flag since it was written.
    # The argument itself was never in the list, which is why the description of the
    # completion test and the completion test disagreed.
    drives = [
        "-kernel", kernel,
        "-initrd", initrd,
        "-drive", f"file={iso_path},media=cdrom,readonly=on",
        "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
        "-no-reboot",
    ]
    # THE CONSOLE IS A CHARDDEV ON A FIFO, so qemu's own stdout carries only warnings.
    # lib/serial.py reads the guest's side into out/serial.out.log, which the workflow
    # uploads - see `stdout=os.devnull` and the note in linux/nixos/derive.py, where
    # pointing stdout at a log produced an empty file that looked like a silent guest.
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)
    try:
        guest.start()
        # OPENED AFTER QEMU, because a fifo has no writer until qemu starts reading.
        ser.open(timeout=60)

        # THE GUEST IS A SCREEN AS WELL AS A CONSOLE, so it is recorded. A text installer
        # still draws a framebuffer - subiquity's console is on tty1 as well as ttyS0 -
        # and a cancelled run's last frame is what says which pane it was on.
        shots = os.path.join(work, "frames")
        os.makedirs(shots, exist_ok=True)
        # NO MP4 FOR THIS INSTALLER. A subiquity install has no GUI to record.
        #
        # The framebuffer shows a text console: `console=ttyS0` moved the kernel's console
        # to the serial port, and what reaches the VGA adapter is a mirror of that text at
        # 80x25. The recordings that were uploaded proved it - 12 MiB of
        # `curtin command in-target` scrolling past at 720x400, every character smeared by
        # the yuv420p encode into something nobody could read.
        #
        # The serial console already carries all of it, line by line, and lib/serial.py
        # prints those lines as they arrive. So the video added nothing but time and 12 MiB
        # of artifact per run, and the proof that the disk works is the ssh session in
        # boot_installed() - not a picture.

        # TWO PHASES, BECAUSE "THE INSTALLER EXITED" IS NOT "THE DISK BOOTS".
        #
        # subiquity's `shutdown: reboot` ends this machine, so the guest exiting means
        # the INSTALL is done - not that the system it wrote comes up. Publishing on that
        # alone is publishing a disk nobody has seen start.
        #
        # So the bake runs for the install, then starts the installed disk and watches it
        # arrive. That second boot is also what the recording needs: run 41613617754's
        # video is 29 seconds of a twenty-minute install, and the desktop - the thing
        # worth looking at - was never in it.
        log(f"waiting up to {args.timeout}s for the installer to finish and reboot")
        install_started = time.time()
        deadline = install_started + args.timeout
        exited = False
        while time.time() < deadline:
            if not guest.alive():
                log("the guest exited (the installer finished)")
                exited = True
                break
            elapsed = int(time.time() - deadline + args.timeout)
            if elapsed % 120 < 15:
                log(f"  {elapsed}s, disk {os.stat(disk).st_blocks * 512 >> 20} MiB")
            time.sleep(15)

        # A TIMEOUT IS A FAILURE, NOT A WARNING FOLLOWED BY A PUBLISH.
        #
        # See lib/install_evidence.py: this loop used to fall through on the clock and
        # carry on to the disk check, which passes on a partition table alone.
        if not install_evidence.finished_cleanly(
                exited, time.time() - install_started, args.timeout, log):
            return 1

        if guest.alive():
            log("  the installer is still running; not booting the disk")
            return 1
        else:
            # THE INSTALLED SYSTEM, WITH NO MEDIUM ATTACHED. This is the only boot that
            # tests the disk, and it is where the desktop appears.
            log("=== the installed system: booting the disk alone ===")
            arrived = boot_installed(work, args, disk)
            if not arrived:
                # A DISK THAT DOES NOT COME UP IS NOT A GOLD DISK, AND RUN 41665165958 WAS
                # PUBLISHED ANYWAY.
                #
                # That run's second boot said
                #
                #     === SSH DID NOT ANSWER IN 600s ===
                #       last output: Permission denied, please try again.
                #
                # and the bake still compressed the disk and the workflow reported
                # success, because `arrived` was only logged. The whole point of the
                # second boot is to refuse a disk nobody has seen start.
                log("=== THE INSTALLED SYSTEM DID NOT COME UP; nothing is published ===")
                return 1
            log("the installed system answers on ssh")

    finally:
        # ACPI FIRST, AND LONG ENOUGH: the clean wrote gigabytes of zeros seconds ago and a
        # disk killed mid-write repairs itself on every later boot. The return value is not
        # threaded here because the caller has already decided - this only decides how the
        # machine goes down.
        guest.stop(acpi_first=True, acpi_timeout=300)
        # AFTER guest.stop(), NOT BEFORE. Closing the writer first tells qemu its console
        # is gone, and a guest mid-shutdown then loses the line that says why.
        ser.close()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    log(f"disk is {install_evidence.disk_mib(disk)} MiB")
    if not install_evidence.above_floor(
            disk, install_evidence.floor_for("ubuntu"), log, "ubuntu"):
        return 1
    if not smoke.check_disk(disk, root_fs="ext4"):
        log("=== THE DISK FAILED ITS CHECKS; refusing to produce an artifact ===")
        return 1

    log("compressing")
    compress.compress(disk, args.out)
    log(f"BAKED ubuntu {args.flavour} -> {args.out} "
        f"({os.path.getsize(args.out) >> 20} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
