"""Derive a desktop NixOS disk from an installed base, by running nixos-rebuild.

    python3 linux/nixos/derive.py --base out/nixos-base.qcow2 --desktop kde \\
                                  --out out/nixos-kde.qcow2

WHY A SECOND STEP RATHER THAN ANOTHER INSTALL

`nixos-install` partitions, formats, extracts the kernel and every package in the base
closure before it touches the desktop, and that cost is identical for each flavour. So
the base is installed once (bake.py) and each desktop is derived from it, which is
easysandbox-vm's structure and the reason its six desktops share one five-minute install.

THE TWO STEPS DIFFER IN A WAY THAT MATTERS FOR FAILURE

`nixos-install` reads the packages out of the ISO's own store and needs no network.
`nixos-rebuild` downloads the desktop closure from cache.nixos.org. A bake that cannot
reach the cache fails HERE and not in the base - which is why the guest script checks the
cache first and says NET_FAIL rather than letting a twenty-minute rebuild die on a
download.

WHY THE BASE IS COPIED, NOT USED IN PLACE

The artifact this repository publishes is the base. Booting it read-write and rebuilding
would modify it, so the next derive would start from a disk that already carries somebody
else's desktop. `cp --reflink=auto` costs nothing on a filesystem that supports it and a
byte copy on one that does not - the same trade lib/ already makes for the Windows disks.

WHY nixos-rebuild BOOT AND NOT switch

`boot` builds the generation and makes it the default for the next boot, without trying
to activate a desktop session inside the throwaway installer VM. `switch` would start
gdm or sddm on a framebuffer nobody is looking at, and the disk being written is the
thing that matters.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from lib import compress, serial, smoke, vm  # noqa: E402

# The six desktops easysandbox-vm derives, and the display manager each needs. Kept as a
# table rather than free text so a typo fails here instead of at nix evaluation inside a
# guest, where the error costs twenty minutes to reach.
DESKTOPS = ("labwc", "gnome", "kde", "cosmic", "hyprland", "miracle")

CONFIG_PORT = 8000

# THE SMALLEST DISK THIS STAGE WILL RUN IN, IN GiB. Every disk in this repository is 128G,
# so this is a floor rather than a target: the resize below only happens if a base somehow
# arrives smaller than this, and a desktop closure needs the room when it does.
MIN_DISK_GIB = 24


def log(message: str) -> None:
    print(f"[derive {time.strftime('%H:%M:%S')}] {message}", flush=True)


def serve(work: str, desktop: str, *, variant: str = "desktop") -> subprocess.Popen:
    """Serve the nix files the guest needs.

    THE FILES ARE SERVED AS A SET, NOT AS ONE. configuration-desktop.nix IMPORTS
    ./configuration-minimal.nix by a relative path, and configuration-dev.nix imports the
    desktop one - so the whole chain has to be present or nixos-rebuild fails with "path
    /etc/nixos/configuration-minimal.nix does not exist", and the import is what keeps the
    filesystems, boot loader and users the base was installed with. A config that did not
    import them would produce a disk that cannot boot.

        --variant desktop    configuration.nix          = desktop
                             configuration-minimal.nix  = minimal
        --variant dev        configuration.nix          = dev
                             configuration-desktop.nix  = desktop
                             configuration-minimal.nix  = minimal

    DEV IMPORTS THE DESKTOP rather than the minimal config, because a dev disk is a desktop
    with a toolchain on it. Serving the minimal one under the desktop name would build a
    headless image and call it a desktop.
    """
    srv = os.path.join(work, "srv")
    os.makedirs(srv, exist_ok=True)

    def render(name: str, out_name: str) -> None:
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            text = fh.read().replace("@DESKTOP@", desktop)
        with open(os.path.join(srv, out_name), "w", encoding="utf-8") as fh:
            fh.write(text)

    # the minimal config is always needed: both branches import it, directly or through the
    # desktop one
    shutil.copyfile(os.path.join(HERE, "configuration-minimal.nix"),
                    os.path.join(srv, "configuration-minimal.nix"))

    if variant == "dev":
        render("configuration-desktop.nix", "configuration-desktop.nix")
        render("configuration-dev.nix", "configuration.nix")
    else:
        render("configuration-desktop.nix", "configuration.nix")

    return subprocess.Popen(
        [sys.executable, "-m", "http.server", str(CONFIG_PORT), "--bind", "0.0.0.0"],
        cwd=srv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def guest_script() -> str:
    """The commands the guest runs, as one line each.

    ONE LINE PER COMMAND, NOT A HEREDOC. lib/serial.py writes to a console and a
    multi-line file cannot be typed at one - the same limitation that made easysandbox-vm
    serve its guest script over HTTP. So the script is fetched too, and what is typed is
    three commands: fetch it, run it, and let it power the machine off.

    THE SCRIPT ITSELF IS BUILT HERE, not in a shell, so there is exactly one place the
    quoting lives.
    """
    return f"""#!/bin/sh
echo DERIVE_BEGIN
if curl -fsS -o /dev/null -m 30 https://cache.nixos.org/nix-cache-info; then
  echo NET_OK
else
  echo NET_FAIL; echo DERIVE_RC=1; sync; poweroff -f
fi
curl -fsS -o /etc/nixos/configuration.nix http://10.0.2.2:{CONFIG_PORT}/configuration.nix || {{ echo CONFIG_FAIL; echo DERIVE_RC=1; sync; poweroff -f; }}
curl -fsS -o /etc/nixos/configuration-minimal.nix http://10.0.2.2:{CONFIG_PORT}/configuration-minimal.nix || {{ echo MINIMAL_FAIL; echo DERIVE_RC=1; sync; poweroff -f; }}
# THE DESKTOP FILE IS OPTIONAL AND IS ASKED FOR SEPARATELY. The dev variant imports it and
# the desktop one does not use it, so a 404 here is not a failure - but a silent skip would
# leave configuration.nix importing a file that is not there, and nixos-rebuild says so with
# a path error a long way from the cause.
curl -fsS -o /etc/nixos/configuration-desktop.nix http://10.0.2.2:{CONFIG_PORT}/configuration-desktop.nix 2>/dev/null || rm -f /etc/nixos/configuration-desktop.nix
echo GOT_CONFIG
export NIX_PATH="nixpkgs=https://github.com/NixOS/nixpkgs/archive/nixos-unstable.tar.gz:nixos-config=/etc/nixos/configuration.nix"
nixos-rebuild boot
rc=$?
echo REBUILD_RC=$rc
if [ $rc -ne 0 ]; then echo DERIVE_RC=$rc; sync; poweroff -f; fi
nix-collect-garbage -d
echo GC_DONE
echo DERIVE_RC=0
sync
poweroff -f
"""


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base", required=True, help="the installed base qcow2")
    p.add_argument("--desktop", required=True, choices=DESKTOPS)
    p.add_argument("--variant", default="desktop", choices=["desktop", "dev"],
                   help="desktop derives a desktop from the base; dev adds the toolchain "
                        "and the VM settings on top of one")
    p.add_argument("--out", default=None)
    p.add_argument("--copy", action="store_true",
                   help="copy the input disk instead of moving it; the default is to "
                        "move, because the disk normally came from ghcr and the "
                        "registry protects it")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=6144)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--timeout", type=int, default=3600)
    args = p.parse_args(argv)

    out = args.out or f"out/nixos-{args.desktop}.qcow2"
    work = args.work or os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"derive {args.desktop} from {args.base}")
    if not os.path.isfile(args.base):
        raise SystemExit(f"no base disk at {args.base}")

    # MOVED, NOT COPIED - the base disk came from ghcr, so the registry still holds it.
    import working_copy as wc

    disk = wc.working_copy(args.base, os.path.join(work, "work.qcow2"),
                           copy=args.copy, note=log)
    # A desktop closure is larger than the base, and 20G was sized for the base alone.
    #
    # THE ABSOLUTE BINARY, NOT THE BARE NAME, AND THE ERROR IS PRINTED.
    #
    # This is the same shadowing lib/compress.py documents at length: the bake image
    # puts the Android SDK ahead of /usr/bin on PATH, and its qemu-img cannot read a
    # qcow2 with compression_type=zstd - which is what every disk this repository
    # publishes is. Run 41612614733 died here with
    #
    #     qemu-img resize ... returned non-zero exit status 1
    #
    # and nothing else, because capture_output swallowed the sentence that said why.
    # compress.qemu_img() already resolves this correctly, so it is used rather than
    # re-derived.
    #
    # GROWING ONLY, AND THE BASE IS NOW ALREADY BIGGER THAN 24G.
    #
    # Every disk in this repository became 128G, so this unconditional `resize 24G` is a
    # SHRINK - and qemu-img refuses one without --shrink, with a warning that says exactly
    # what is at stake:
    #
    #     qemu-img: Use the --shrink option to perform a shrink operation.
    #     qemu-img: warning: Shrinking an image will delete all data beyond the
    #                     shrunken image's end.
    #
    # `--shrink` is the wrong answer: it would truncate the base's own filesystem. The right
    # one is not to resize at all, because the disk is already large enough - so this asks
    # the image its virtual size and only grows when it is under the floor.
    import compress

    digest = subprocess.run(
        [compress.qemu_img(), "info", "--output=json", disk],
        capture_output=True, text=True,
    )
    if digest.returncode != 0:
        raise SystemExit(
            f"could not read {disk}: {(digest.stderr or digest.stdout).strip()[:400]}")
    import json

    virtual_gib = (json.loads(digest.stdout).get("virtual-size") or 0) / float(1 << 30)
    if virtual_gib < MIN_DISK_GIB:
        log(f"growing the disk from {virtual_gib:.0f}G to {MIN_DISK_GIB}G")
        resized = subprocess.run(
            [compress.qemu_img(), "resize", disk, f"{MIN_DISK_GIB}G"],
            capture_output=True, text=True,
        )
        if resized.returncode != 0:
            raise SystemExit(
                f"could not grow {disk} to {MIN_DISK_GIB}G: "
                f"{(resized.stderr or resized.stdout).strip()[:400]}")
    else:
        log(f"the disk is {virtual_gib:.0f}G; nothing to grow")

    server = serve(work, args.desktop, variant=args.variant)
    # THE GUEST SCRIPT IS SERVED, NOT TYPED - see guest_script().
    srv = os.path.join(work, "srv")
    with open(os.path.join(srv, "derive-guest.sh"), "w", encoding="utf-8") as fh:
        fh.write(guest_script())
    log(f"configuration and guest script served on :{CONFIG_PORT}")

    ser = serial.Serial(work, "serial")
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        firmware_code="", firmware_vars="",
        vga="std", serial_pipe=ser.stdin.removesuffix(".in"),
    )
    drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
    # stdout IS /dev/null: this guest's console is a chardev on a fifo (Shape.serial_pipe),
    # so qemu's own stdout carries only warnings. The guest's words are in ser.sink, and
    # pointing stdout at derive.log made an empty file that looked like a silent guest -
    # then last_status() read that empty file and reported "exit status: <never returned>"
    # for a run whose last line was DERIVE_RC=0.
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)
    try:
        guest.start()
        ser.open(timeout=60)
        # 120 seconds for an installed disk to reach a login prompt. There is no desktop
        # to start on the base, so this is kernel and userspace and nothing else.
        log("waiting 120s for the installed base to boot")
        time.sleep(120)
        ser.say(
            f"curl -fsS -o /tmp/derive.sh http://10.0.2.2:{CONFIG_PORT}/derive-guest.sh "
            f"&& echo GOT_SCRIPT || echo SCRIPT_FAIL", wait=8)
        ser.say("", wait=1)
        ser.say("sudo -i sh /tmp/derive.sh", wait=20)
        # THE SERIAL STAYS OPEN UNTIL THE GUEST IS GONE. This is the same mistake the
        # base bake made and run 41617665357 shows its cost: closing the writer after the
        # last command gave qemu EOF on its serial chardev, the console stopped mid-line
        # ("[console] c"), and the run then watched a machine nobody could see for forty
        # minutes. ser.close() belongs in the finally block, after guest.stop().

        log(f"waiting up to {args.timeout}s for the rebuild")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if not guest.alive():
                log("the guest powered off")
                break
            time.sleep(15)
        else:
            log(f"=== THE GUEST DID NOT POWER OFF IN {args.timeout}s ===")
    finally:
        guest.stop()
        # CLOSED AFTER THE MACHINE, not before it: closing the writer earlier sends EOF
        # down qemu's serial chardev, which qemu treats as the console going away.
        ser.close()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    # THE CONSOLE IS ser.sink, NOT derive.log - see the note where the guest is created.
    log_path = ser.sink
    rc = last_status(log_path)
    log(f"derive exit status: {rc if rc is not None else '<never returned>'}")
    if rc != 0:
        log("=== THE DERIVE FAILED ===")
        first_error(log_path)
        return 1

    if not smoke.check_disk(disk, root_fs="ext4"):
        log("=== THE DISK FAILED ITS CHECKS ===")
        return 1

    log("compressing")
    compress.compress(disk, out)
    log(f"DERIVED {args.desktop} -> {out} ({os.path.getsize(out) >> 20} MiB)")
    return 0


def last_status(path: str) -> int | None:
    """DERIVE_RC from the guest, taking the LAST one printed.

    The guest prints DERIVE_RC exactly once per outcome, but a console that replayed a
    previous run's tail would put an older one first - and it is the final value that
    decided this run.
    """
    import re

    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = re.findall(r"DERIVE_RC=(\d+)", text)
    return int(found[-1]) if found else None


def first_error(path: str, context: int = 30) -> None:
    """The first error and its surroundings, not the last sixty lines."""
    import re

    try:
        with open(path, "rb") as fh:
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        log(f"(no log at {path})")
        return
    index = None
    for i, line in enumerate(lines):
        if re.search(r"error:|ERROR|Traceback|failed", line):
            index = i
            break
    if index is None:
        for line in lines[-40:]:
            log(f"  [guest] {line}")
        return
    for line in lines[max(0, index - 8):index + context]:
        log(f"  [guest] {line}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
