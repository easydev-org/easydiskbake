"""Add the developer toolchain to a macOS disk that already has Remote Login.

    python3 macos/15/dev.py --disk out/macos15-ssh.qcow2 --out out/macos15-dev.qcow2

WHY THIS STAGE USES SSH AND THE EARLIER ONES DID NOT

The ssh stage could not, because sshd was what it was creating. By this point the disk
answers on a forwarded port, so everything below is ordinary remote work: no keyboard, no
coordinates, no frames to read. That is the entire reason the stages are ordered
desktop -> ssh -> dev, and putting dev before ssh would mean driving a package manager
through a GUI.

WHAT IS INSTALLED, AND WHY IT IS THIS SHORT LIST

The same question as the Windows dev stage: what does a GitHub Actions runner need to be
useful? git and the compilers come with the Xcode command line tools, which Homebrew
itself requires, so they are not listed separately. Node and Python are, in the versions
a runner picks up when it looks on PATH.

WHY HOMEBREW AND NOT THE SYSTEM PACKAGES

macOS ships no package manager. Homebrew is what the ecosystem uses, and a disk without
one cannot install anything else later - which makes it the toolchain's prerequisite
rather than one of its entries.

THE ROOT PASSWORD IS NOT SET HERE. sudo works because the desktop stage's account is an
administrator, and this stage authenticates with that account's password over ssh. If
the account were not an admin, `brew install` would fail at the first write to
/usr/local and the log would say so.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

# WHICH RELEASE THIS DISK IS. The stages in this directory are shared between
# macOS 15 and 26 - macos/26 keeps only the measurements that differ - so the
# recovery image and the OpenCore build have to be told which one they are
# working on. A constant here made macos26's desktop stage pull
# macos-recovery-sequoia, which is 15's image, and install Apple's APFS driver
# from it into a 26 boot.
RELEASE = os.environ.get("MACOS_RELEASE", "15")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
# AND THIS DIRECTORY, WHICH IS WHERE `machine`, `opencore` AND `macos_recovery` LIVE.
#
# `from lib import machine` asks lib/ for a module called machine. It is not there - it is
# macos/15/machine.py, beside this file - and the failure is
#
#     ImportError: cannot import name 'machine' from 'lib' (unknown location)
#
# which arrives AFTER the 16 GiB disk has been pulled, so the whole stage is spent on an import.
# macos/15/desktop.py carries the same note for the same reason; this file had the bug until
# run 42624293500.
sys.path.insert(0, HERE)

from lib import compress, net, smoke  # noqa: E402

SSH_USER = os.environ.get("MACOS_USER", "docker")
SSH_PASSWORD = os.environ.get("MACOS_PASSWORD", "admin")

# THE BREW RECIPE, THE PACKAGE LIST AND THE THREE FUNCTIONS THAT USED THEM lived here and
# are macos/15/steps/dev.yaml now, beside the steps that run them. What is left in this file
# is what a step cannot be: the machine, the disk, the recovery image and OpenCore.
#
# The Homebrew installer is a script fetched over https and run with /bin/bash, which is what
# its own documentation has said for years. It is idempotent: a second run says "already
# installed" and exits 0. That property is what lets the step run unconditionally.
WALK = os.path.join(HERE, "steps", "dev.yaml")


def log(message: str) -> None:
    print(f"[macos-dev {time.strftime('%H:%M:%S')}] {message}", flush=True)


def wait_for_ssh(port: int, seconds: int = 180) -> bool:
    """Wait until the port answers AND the account can actually log in.

    A BANNER IS NOT A LOGIN. sshd answers before the account is usable in some
    conditions - a locked account, a shell that is not yet registered - and finding that
    out from the first real command instead of here makes the failure look like a
    package problem.
    """
    import socket

    import session

    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((net.FORWARD_HOST, port), timeout=10) as sock:
                sock.settimeout(10)
                if not sock.recv(256).startswith(b"SSH-2.0"):
                    time.sleep(10)
                    continue
        except OSError:
            time.sleep(10)
            continue
        # session.ssh, NOT A LOCAL `run()` HELPER. This file had one - sshpass and four options,
        # rebuilt here - and it was deleted with the rest of the brew code, which left
        # `wait_for_ssh` calling a name that no longer existed:
        #
        #     NameError: name 'run' is not defined
        #
        # raised after the recovery image and OpenCore had both been fetched, so the stage spent
        # its first minute on work that could not finish. lib/session.py is the shared client and
        # takes the password explicitly, which is what the WINDOWS_ADMIN_PASSWORD/MACOS_PASSWORD
        # difference needs anyway.
        status, output = session.ssh(port, "echo DEV_SSH_OK", timeout=60,
                                     user=SSH_USER, password=SSH_PASSWORD)
        if status == 0 and "DEV_SSH_OK" in (output or ""):
            log("the account logs in")
            return True
        log(f"  sshd is up but the login failed: {(output or '').strip()[:160]}")
        time.sleep(10)
    return False


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the ssh-stage disk")
    p.add_argument("--out", default=None)
    p.add_argument("--overlay", action="store_true",
                   help="treat --disk as a READ-ONLY base and write into a throwaway qcow2 "
                        "overlay; the base is never modified. See lib/overlay.py")
    p.add_argument("--copy", action="store_true",
                   help="copy the input disk instead of moving it; the default is to "
                        "move, because the disk normally came from ghcr and the "
                        "registry protects it")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--ssh-port", type=int, default=2223)
    p.add_argument("--boot-timeout", type=int, default=900)
    # NO --clean HERE ANY MORE, AND THE ONE THAT EXISTED WAS THE WRONG KIND ANYWAY.
    #
    # It ran `dd if=/dev/zero of=/tmp/zero bs=1m` - writing zeros to fill the free space, the
    # mechanism this repository abandoned everywhere else. lib/linclean.py measured what it
    # costs: one second of fstrim released 116 GiB on the same disk, and the zeroing that
    # followed allocated clusters back. On APFS it is worse than useless, because a
    # copy-on-write filesystem allocates new blocks before the old ones are released.
    #
    # Cleaning is lib/clean_step.py's job now, as the step after this one, the same way it is
    # for bake, derive and both Windows stages. It calls lib/macclean.py, which uses
    # `diskutil apfs trim` - discard, which writes nothing.
    args = p.parse_args(argv)

    out = args.out or args.disk.replace(".qcow2", "-dev.qcow2")
    work = args.work or os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(work, exist_ok=True)

    if not shutil_which("sshpass"):
        raise SystemExit(
            "sshpass is not installed. This stage authenticates with the account's "
            "password, so it needs a client that can answer the prompt without a "
            "terminal."
        )

    log(f"disk={args.disk} out={out}")
    # MOVED, NOT COPIED - the ssh disk came from ghcr, so the registry still holds it.
    import working_copy as wc

    run_overlay = None
    if getattr(args, "overlay", False):
        import overlay as overlaymod

        run_overlay = overlaymod.Overlay(args.disk, work, name="dev-overlay.qcow2")
        disk = run_overlay.open()
    else:
        disk = wc.working_copy(args.disk, os.path.join(work, "dev.qcow2"),
                               copy=args.copy, note=log)

    import opencore
    import macos_recovery
    import machine                     # macos/15/machine.py, via the path above
    from lib import vm

    # THE RECOVERY IMAGE IS AN INPUT EVEN THOUGH THIS STAGE DOES NOT BOOT FROM IT. See
    # the longer note in ssh.py: opencore.build() extracts Apple's APFS driver out of it,
    # and a job inherits no other job's inputs. This is the last stage in the chain, so
    # without this it would fail after setup, desktop and ssh had all succeeded.
    log("=== recovery image ===")
    recovery = macos_recovery.fetch_published(RELEASE, work)
    log(f"  {recovery} ({os.path.getsize(recovery) >> 20} MiB)")

    log("=== OpenCore, firmware, APFS driver ===")
    assets = opencore.build(RELEASE, work)
    log(f"identity {assets['identity']['serial']} / {assets['identity']['mac']}")

    shape = machine.shape(assets, cpus=args.cpus, mem_mb=args.mem)
    shape.ssh_port = args.ssh_port
    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            "-drive", machine.TARGET_DRIVE.format(disk=disk),
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
            "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
        ],
        shape=shape,
        stdout=os.path.join(work, "console.log"),
    ).start()

    ok = False
    try:
        log(f"waiting up to {args.boot_timeout}s for sshd")
        if not wait_for_ssh(args.ssh_port, args.boot_timeout):
            log("=== THE DISK DID NOT ANSWER ON SSH ===")
            log("  this stage needs the ssh-stage disk, not the desktop one")
            return 1

        # THE TOOLCHAIN IS A FILE NOW: macos/15/steps/dev.yaml.
        #
        # What stood here - install Homebrew, walk PACKAGES, brew install each, verify each -
        # was a sequence of measurements and no decision. The measurements belong beside the
        # numbers they produced; the decision ("does this stage pass?") stays here.
        #
        # THE WALK RUNS OVER SSH, so it takes the headless driver lib/steps.py builds when a
        # file has no GUI steps. This stage never touches the screen: the disk arrived with a
        # working sshd.
        #
        # `disk_path` IS PASSED SO A `python:` STEP CAN SEE IT, and `work` is the stage's own
        # directory because the Xcode .xip is kept there between the fetch and the copy.
        log(f"=== the walkthrough: {os.path.relpath(WALK, REPO)} ===")
        import steps as stepslib
        ok_walk, ran = stepslib.run_file(
            WALK, qmp_path="", work=work, ssh_port=args.ssh_port,
            user=SSH_USER, password=SSH_PASSWORD, release=RELEASE,
            disk_path=disk,
        )
        log(f"{len(ran)} step(s) answered" if ok_walk
            else f"the walkthrough failed after {len(ran)} step(s)")
        if not ok_walk:
            return 1

        ok = True
        return 0
    finally:
        guest.stop(acpi_first=ok)
        if ok:
            if not smoke.check_disk(disk, root_fs="apfs"):
                log("=== THE DISK FAILED ITS CHECKS ===")
                raise SystemExit(1)
            log("compressing")
            if run_overlay is not None:
                run_overlay.flatten(out)
            else:
                compress.compress(disk, out)
            log(f"BAKED macos15 dev -> {out} ({os.path.getsize(out) >> 20} MiB)")


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
