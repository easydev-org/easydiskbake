"""Bake Kubuntu by answering subiquity's autoinstall, with no screen at all.

    python3 linux/kubuntu/bake.py --out out/kubuntu.qcow2

THE SAME MECHANISM AS NIXOS, FOR AN OPPOSITE REASON

NixOS has no answer file and is typed at a console. Ubuntu has one, and it is the whole
installation: subiquity fetches a YAML document over the network at boot and does
everything in it. So both bakes boot the ISO's own kernel with -kernel/-initrd - which is
how the kernel reaches the config server before anything else is up - and neither one
looks at a screen.

WHY -kernel/-initrd AND NOT THE ISO'S BOOT MENU

The config is found from `ds=nocloud-net;s=http://10.0.2.2:PORT/` on the kernel command
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

from lib import compress, iso, live as livelib, smoke, vm  # noqa: E402

CONFIG_PORT = 8000
KUBUNTU_TAG = "iso-ubuntu"

# The seed directory name is arbitrary but its URL must end in a slash - subiquity
# appends "user-data" and "meta-data" to whatever it is given, and a URL without the
# trailing slash gets them concatenated onto the last segment.
SEED = "autoinstall/"


def log(message: str) -> None:
    print(f"[kubuntu {time.strftime('%H:%M:%S')}] {message}", flush=True)


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


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/kubuntu.qcow2")
    p.add_argument("--flavour", default="kde",
                   help="which user-data-<flavour>.yaml to install; server for now")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--disk-size", default="24G")
    p.add_argument("--timeout", type=int, default=3600)
    args = p.parse_args(argv)

    work = args.work or os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"out={args.out} flavour={args.flavour}")
    iso_path = os.path.join(work, "ubuntu.iso")
    if not os.path.isfile(iso_path):
        log(f"pulling {KUBUNTU_TAG} from ghcr")
        try:
            iso.fetch_tag(KUBUNTU_TAG, iso_path)
        except iso.IsoError as exc:
            raise SystemExit(
                f"{KUBUNTU_TAG} is not published: {exc}\n\n"
                f"Run iso-publish.yml for ubuntu first.") from exc
    log(f"iso {os.path.getsize(iso_path) >> 20} MiB")

    tree = os.path.join(work, "iso")
    kernel = extract_from_iso(iso_path, "casper/vmlinuz", tree)
    initrd = extract_from_iso(iso_path, "casper/initrd", tree)
    log(f"kernel {kernel}")
    log(f"initrd {initrd}")

    disk = os.path.join(work, "disk.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    subprocess.run(["qemu-img", "create", "-f", "qcow2", disk, args.disk_size],
                   check=True)
    log(f"disk created: {args.disk_size}")

    server = serve(work, args.flavour)
    seed_url = f"http://10.0.2.2:{CONFIG_PORT}/{SEED}"
    log(f"seed served at {seed_url}")

    # THE KERNEL COMMAND LINE, and each part of it is load-bearing:
    #
    #   autoinstall             subiquity, do an unattended install
    #   ds=nocloud-net;s=URL    where the seed is. The QUOTES matter: without them the
    #                           semicolon ends the argument and everything after it is
    #                           dropped, which reads later as "the seed was not found".
    #   ip=dhcp                 bring the network up before cloud-init looks for it
    #   cloud-config-url=/dev/null  stops the installer re-reading the ISO as a cloud
    #                           config source, which it otherwise does and which costs
    #                           minutes of the same work twice
    #   console=ttyS0,115200n8  so the guest's output reaches the serial log at all
    #
    # NO `---`, AND THAT IS THE FIX FOR RUN 41602915388. Its kernel said:
    #
    #     Unknown kernel command line parameters
    #       "autoinstall --- ds=nocloud-net;s=http://10.0.2.2:8000/autoinstall/ ..."
    #
    # `---` separates arguments the kernel handles from ones it passes to user space, and
    # putting it BEFORE cloud-init's own variables meant cloud-init saw a command line
    # whose first token was the separator. All three of its stages then ran and found
    # nothing to do - the log shows cloud-config.service finishing with no seed fetch and
    # no install, and the disk stayed at 0 MiB for six minutes.
    #
    # Nothing here needs the separator: every variable above is one the kernel or
    # cloud-init reads from /proc/cmdline, which is where they have to be to be seen.
    append = (
        "autoinstall "
        f'"ds=nocloud-net;s={seed_url}" '
        "ip=dhcp cloud-config-url=/dev/null "
        "console=ttyS0,115200n8"
    )

    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        vga="std",
        kernel_append=append,
        # THE ISO IS STILL ATTACHED. The autoinstall document names no package source;
        # subiquity installs from the medium it booted from, and it finds that medium by
        # a filename inside it. Booting the kernel directly does not remove the need for
        # the ISO to be present.
    )
    drives = [
        "-kernel", kernel,
        "-initrd", initrd,
        "-drive", f"file={iso_path},media=cdrom,readonly=on",
        "-drive", f"file={disk},if=virtio,format=qcow2",
    ]
    guest = vm.Guest(work, extra=drives, shape=shape,
                     stdout=os.path.join(work, "console.log"))
    try:
        guest.start()

        # THE GUEST IS A SCREEN AS WELL AS A CONSOLE, so it is recorded. A text installer
        # still draws a framebuffer - subiquity's console is on tty1 as well as ttyS0 -
        # and a cancelled run's last frame is what says which pane it was on.
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

        log(f"waiting up to {args.timeout}s for the installer to finish and reboot")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if not guest.alive():
                log("the guest exited (the installer rebooted)")
                break
            elapsed = int(time.time() - deadline + args.timeout)
            if elapsed % 120 < 15:
                log(f"  {elapsed}s, disk {os.stat(disk).st_blocks * 512 >> 20} MiB")
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
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    log(f"disk is {os.stat(disk).st_blocks * 512 >> 20} MiB")
    if not smoke.check_disk(disk, root_fs="ext4"):
        log("=== THE DISK FAILED ITS CHECKS; refusing to produce an artifact ===")
        return 1

    log("compressing")
    compress.compress(disk, args.out)
    log(f"BAKED kubuntu {args.flavour} -> {args.out} "
        f"({os.path.getsize(args.out) >> 20} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
