"""Bake a minimal NixOS base disk, by typing nixos-install at its serial console.

    python3 linux/nixos/bake.py --out out/nixos-base.qcow2

THE ONE LINUX HERE THAT IS INSTALLED AT A SHELL.

Every other distribution in this repository is driven through the screen: MX's minstall
and Mint's ubiquity are wizards, and linux/mx clicks them at measured coordinates. NixOS
has no such installer. It has `nixos-install`, and the documented way to install from a
live medium is to partition the disk yourself and run it. So the bake does the same
thing, with the commands written to the serial console instead of typed at a keyboard.

WHERE THIS COMES FROM. easysandbox-vm's bake/nixos/bake-nixos-minimal.sh has installed
NixOS this way for months, and three of its findings are load-bearing here:

    THE KERNEL IS READ FROM grub.cfg, not chosen by name. The ISO carries more than one
    kernel - boot/nix/store/<hash>-linux-6.18.59/bzImage and one for 7.3 - so any rule
    that picks "the newest" or "the first found" pairs a kernel with the wrong initrd.
    The image states the pairing in its own menu entry, and that line is what this uses.

    THE WHOLE INSTALL IS ONE COMMAND LINE ENDING IN poweroff. Install, report the exit
    status, sync, shut down. Nothing waits for the host to notice a marker and inject
    another keystroke - two attempts at that left a finished install sitting until the
    timeout.

    THE RESULT IS THE GUEST'S EXIT STATUS, not a phrase in the log. A failed install
    prints plenty of text; INSTALL_RC= is the only line that says whether it worked.

WHAT THIS DOES NOT DO

It does not install a desktop. The desktops are derived from this disk by
linux/nixos/derive.py, which boots it and runs nixos-rebuild - because partition, format
and unpack are the same for every flavour and nixos-install pays that cost each time.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from lib import compress, iso, serial, smoke, vm  # noqa: E402

NIXOS_TAG = "iso-nixos"
CONFIG_NAME = "configuration.nix"
CONFIG_PORT = 8000


def log(message: str) -> None:
    print(f"[nixos {time.strftime('%H:%M:%S')}] {message}", flush=True)


def boot_entry_from_grub(iso_path: str) -> tuple[str, str]:
    """The kernel and initrd paths the ISO's own menu entry names.

    THE PAIRING IS THE IMAGE'S CLAIM, NOT AN INFERENCE. grub.cfg's entry is one line:

        linux /boot/nix/store/<hash>-linux-X/bzImage init=... \\
              initrd=/boot/nix/store/<hash>-initrd-linux-X/initrd ...

    so the kernel is the argument after `linux` and the initrd is the one after
    `initrd=`, and they come from the same line. easysandbox-vm records what happens
    without this: a hardcoded 6.18.57 matched nothing, and taking "whichever bzImage
    find returned first" paired 6.18.59 with another build's initrd.
    """
    cfg = subprocess.run(
        ["7z", "e", "-so", iso_path, "EFI/BOOT/grub.cfg"],
        capture_output=True, text=True,
    )
    if cfg.returncode != 0 or not cfg.stdout:
        raise SystemExit(
            f"could not read EFI/BOOT/grub.cfg from {iso_path}: "
            f"{(cfg.stderr or '').strip()[:200]}"
        )
    for line in cfg.stdout.splitlines():
        line = line.strip()
        if not line.startswith("linux "):
            continue
        parts = line.split()
        kernel = parts[1]
        initrd = ""
        for part in parts:
            if part.startswith("initrd="):
                initrd = part[len("initrd="):]
                break
        if kernel and initrd:
            return kernel, initrd
    raise SystemExit(
        f"no 'linux ... initrd=...' line in {iso_path}'s grub.cfg; the image's layout "
        f"changed and this needs looking at rather than guessing"
    )


def extract(iso_path: str, wanted: str, dest: str) -> str:
    """Pull one file out of the ISO by path suffix, and return where it landed."""
    subprocess.run(["7z", "x", "-y", "-r", f"-o{dest}", iso_path, wanted],
                   capture_output=True)
    for root, _dirs, files in os.walk(dest):
        for name in files:
            path = os.path.join(root, name)
            if path.replace("\\", "/").endswith(wanted.replace("\\", "/")):
                return path
    raise SystemExit(
        f"{wanted} is named by the ISO's menu entry but was not in the extracted tree "
        f"under {dest}"
    )


def partition_script() -> list[tuple[str, float]]:
    """The commands, and how long each needs, measured from a working run.

    ONE LINE PER STEP, AND A WAIT THAT IS THE COMMAND'S OWN TIME. There is nothing to
    poll: the console is a character stream, and waiting for a prompt string would be
    reading the screen for a verdict in text form. easysandbox-vm's numbers are used
    unchanged because the guest and the disk are the same shape.

    PARTITION, NOT THE BARE DISK. grub-install refuses to embed core.img in the MBR gap
    when a filesystem starts at sector 0 ("will not proceed with blocklists"), so
    parted leaves the first MiB free and the root is addressed by LABEL - a UUID only
    exists at install time and would pin the disk to one bake.
    """
    return [
        ("echo BAKE_BEGIN", 3),
        ("curl -fsS -o /tmp/c.nix http://10.0.2.2:%d/%s && wc -l /tmp/c.nix"
         % (CONFIG_PORT, CONFIG_NAME), 8),
        ("sudo sgdisk --zap-all /dev/vda >/dev/null 2>&1 || true", 2),
        ("sudo parted -s /dev/vda mklabel msdos", 3),
        ("sudo parted -s /dev/vda mkpart primary ext4 1MiB 100%", 3),
        ("sudo partprobe /dev/vda 2>/dev/null || true", 3),
        ("sudo mkfs.ext4 -F -L nixos /dev/vda1", 15),
        ("sudo mount /dev/vda1 /mnt", 5),
        ("sudo mkdir -p /mnt/etc/nixos", 3),
        ("sudo cp /tmp/c.nix /mnt/etc/nixos/configuration.nix", 3),
        # THE INSTALL, THE STATUS, THE SYNC AND THE SHUTDOWN IN ONE LINE. The
        # semicolons matter: a failure in nixos-install must still print INSTALL_RC
        # and still power off, or the run sits until the outer timeout with a finished
        # or failed install nobody can see.
        ("sudo nixos-install --root /mnt --no-root-password --no-channel-copy 2>&1; "
         "echo INSTALL_RC=$?; sync; sudo poweroff -f", 30),
    ]


def serve_config(work: str, release: str) -> subprocess.Popen:
    """Serve configuration.nix to the guest over qemu's user network.

    WHY HTTP AND NOT THE SERIAL. The file is a hundred lines of Nix with quotes, braces
    and newlines. `type_text` cannot send a newline and a heredoc over a serial line
    needs every `$` and quote escaped twice. qemu's user netdev puts the host at
    10.0.2.2, so the guest curls the file - which is what easysandbox-vm does and the
    reason its bake needs no network beyond that.
    """
    srv = os.path.join(work, "srv")
    os.makedirs(srv, exist_ok=True)
    src = os.path.join(HERE, f"configuration-{release}.nix")
    if not os.path.isfile(src):
        raise SystemExit(f"no {src}")
    with open(src, "rb") as fh:
        body = fh.read()
    with open(os.path.join(srv, CONFIG_NAME), "wb") as fh:
        fh.write(body)
    return subprocess.Popen(
        [sys.executable, "-m", "http.server", str(CONFIG_PORT), "--bind", "0.0.0.0"],
        cwd=srv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/nixos-base.qcow2")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=6144)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--disk-size", default="20G")
    p.add_argument("--timeout", type=int, default=3600,
                   help="seconds the whole guest may run")
    args = p.parse_args(argv)

    work = args.work or os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"out={args.out}")
    iso_path = os.path.join(work, "nixos.iso")
    if not os.path.isfile(iso_path):
        log(f"pulling {NIXOS_TAG} from ghcr")
        try:
            iso.fetch_tag(NIXOS_TAG, iso_path)
        except iso.IsoError as exc:
            raise SystemExit(
                f"{NIXOS_TAG} is not published: {exc}\n\n"
                f"Run publish-iso.yml for nixos first. Nothing here downloads from a "
                f"mirror at run time - see lib/iso.py."
            ) from exc
    log(f"iso {os.path.getsize(iso_path) >> 20} MiB")

    kernel_path, initrd_path = boot_entry_from_grub(iso_path)
    log(f"the ISO's menu entry names kernel={kernel_path}")
    log(f"                                  initrd={initrd_path}")

    tree = os.path.join(work, "x")
    kernel = extract(iso_path, kernel_path.lstrip("/boot/"), tree)
    initrd = extract(iso_path, initrd_path.lstrip("/boot/"), tree)
    log(f"kernel {kernel}")
    log(f"initrd {initrd}")

    disk = os.path.join(work, "disk.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    subprocess.run(["qemu-img", "create", "-f", "qcow2", disk, args.disk_size],
                   check=True)
    log(f"disk created: {args.disk_size}")

    server = serve_config(work, "minimal")
    log(f"configuration served on :{CONFIG_PORT} (the guest reaches it at 10.0.2.2)")

    # THE SERIAL IS A PAIR OF FIFOS - see lib/serial.py for why, and for why this is
    # the only bake here that writes to a console instead of looking at a screen.
    ser = serial.Serial(work, "serial")
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        firmware_code="", firmware_vars="",
        vga="std", cpu="", smp_topology="",
        serial_pipe=ser.stdin.removesuffix(".in"),
        kernel_append=(
            "edd=off nohibernate root=fstab loglevel=4 console=ttyS0,115200n8"
        ),
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
        ser.open(timeout=60)
        # 115 seconds for the live medium to reach a root shell. Measured: the ISO's
        # own boot takes a little under two minutes before it prints the prompt.
        log("waiting 115s for the live medium to reach a shell")
        time.sleep(115)
        for line, wait in partition_script():
            ser.say(line, wait=wait)
        ser.close()

        log(f"waiting up to {args.timeout}s for the guest to power itself off")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if not guest.alive():
                log("the guest powered off")
                break
            time.sleep(10)
        else:
            log(f"=== THE GUEST DID NOT POWER OFF IN {args.timeout}s ===")
    finally:
        guest.stop()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    console = os.path.join(work, "console.log")
    rc = install_status(console)
    log(f"nixos-install exit status: {rc if rc is not None else '<never returned>'}")
    if rc != 0:
        log("=== THE INSTALL FAILED ===")
        first_error(console)
        return 1

    if not smoke.check_disk(disk, root_fs="ext4"):
        log("=== THE DISK FAILED ITS CHECKS; refusing to produce an artifact ===")
        return 1

    log("compressing")
    compress.compress(disk, args.out)
    log(f"BAKED nixos-base -> {args.out} ({os.path.getsize(args.out) >> 20} MiB)")
    return 0


def install_status(console: str) -> int | None:
    """The guest's own exit status, as it printed it.

    THE LAST ONE, because a retried install prints the line twice and it is the second
    that decided. None means the line never appeared, which is itself a failure - the
    command ends with it unconditionally.
    """
    try:
        with open(console, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = re.findall(r"INSTALL_RC=(\d+)", text)
    return int(found[-1]) if found else None


def first_error(console: str, context: int = 25) -> None:
    """Print the FIRST error with its surroundings.

    Not the last sixty lines: a Nix assertion or a failed mount sits near the top, and
    the tail is only the prompt coming back. Same reasoning as easysandbox-vm's
    bake_assert_installed.
    """
    try:
        with open(console, "rb") as fh:
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        log(f"(no console log at {console})")
        return
    index = None
    for i, line in enumerate(lines):
        if re.search(r"error:|ERROR|Traceback|failed", line):
            index = i
            break
    if index is None:
        for line in lines[-40:]:
            log(f"  [console] {line}")
        return
    start = max(0, index - 6)
    for line in lines[start:start + context + 6]:
        log(f"  [console] {line}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
