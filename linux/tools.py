"""Boot a baked Linux disk, install the toolchain over ssh, clean it and export it.

    python3 linux/tools.py --disk out/ubuntu.qcow2 --out out/ubuntu-tools.qcow2 --kind ubuntu

WHY THIS BOOTS THE DISK ITSELF

The bake ends with the guest shut down and the disk exported. There is nothing running to
ssh into, so this stage starts one - the same machine lib/clean_step.py starts, for the same
reason: a workflow step has to hand itself a disk, and a disk that is not running is the only
thing a step can receive.

WHAT IT DOES, IN ORDER

    boot          the published bake disk, no install media
    install       apt update, upgrade, then the toolchain (lib/linstools.py)
    clean         the same cleaner the bake used - an upgrade and a package install leave
                  the deb cache full, and this stage put it there
    shutdown      ACPI, with a long timeout: the cleaner trimmed the filesystem and the
                  guest is writing back its own metadata
    compress      because the disk is larger than the one it started from and the published
                  artifact should not be

THE OUTPUT IS A SEPARATE TAG, NOT AN OVERWRITE. `linux-ubuntu-tools` beside `linux-ubuntu`,
so a toolchain change can be compared with the bake it came from and a broken install does
not take the working disk with it.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))


def log(message: str) -> None:
    print(f"[tools {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the bake's published disk")
    p.add_argument("--out", required=True, help="where the toolchain disk is written")
    p.add_argument("--kind", default="ubuntu", choices=["ubuntu", "kubuntu", "freebsd"],
                   help="which package manager and which package names")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=4096,
                   help="MB; an apt upgrade unpacks a lot at once and 2 GiB fails on grub")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--skip-upgrade", action="store_true")
    p.add_argument("--skip-clean", action="store_true",
                   help="leave the debris; the workflow runs lib/clean_step.py next")
    args = p.parse_args(argv)

    if not os.path.isfile(args.disk):
        log(f"=== NO DISK AT {args.disk} ===")
        return 2

    work = os.path.abspath(args.work or os.path.join(
        os.path.dirname(os.path.abspath(args.out)), f"{args.kind}-tools-work"))
    os.makedirs(work, exist_ok=True)

    # MOVED, NOT COPIED: the disk came from ghcr, so the registry still holds it and a
    # 12 GiB copy protects nothing. See lib/working_copy.py.
    import working_copy as wc

    disk = wc.working_copy(args.disk, os.path.join(work, "disk.qcow2"), note=log)

    import vm

    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        vga="std", resolution=(1920, 1080), ssh_port=2222,
    )
    # discard=unmap so the guest's own fstrim reaches the qcow2, rather than qemu storing
    # zeros that convert has to read again.
    guest = vm.Guest(work, extra=[
        "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
    ], shape=shape, stdout=os.path.join(work, "console.log"))

    ok = False
    try:
        log("starting the disk")
        guest.start()

        status = 0
        log("=== installing the toolchain ===")
        import linstools

        status = linstools.main(
            ["--port", "2222", "--kind", args.kind,
             "--boot-timeout", str(args.boot_timeout)]
            + (["--skip-upgrade"] if args.skip_upgrade else []))

        if status != 0:
            log("=== THE TOOLCHAIN STAGE DID NOT FINISH ===")
            log("  the disk is not exported: it is not what this stage claims to produce")
            return status

        if not args.skip_clean:
            # THE CLEANER, BECAUSE THIS STAGE MADE THE MESS. An apt upgrade fills
            # /var/cache/apt/archives and /var/lib/apt/lists the same way the bake did, and
            # the disk that goes out should have neither.
            log("=== cleaning the disk this stage filled ===")
            import clean_disk

            # THE CLEANER MATCHES THE GUEST: bsdclean knows pkg and zpool, linclean knows
            # apt and fstrim, and running either against the other system's disk fails at
            # its first `df` because the command it looks for is not installed.
            kind_for_clean = "freebsd" if args.kind == "freebsd" else "linux"
            if not clean_disk.clean_running(kind_for_clean, port=2222, note=log, disk=disk):
                log("=== THE CLEAN DID NOT FINISH ===")
                return 1

        ok = True
    finally:
        # ACPI FIRST WHEN IT WORKED, AND LONG ENOUGH. The clean trimmed the filesystem
        # seconds ago; a disk killed mid-write repairs itself on every later boot.
        guest.stop(acpi_first=ok, acpi_timeout=300)

    if not ok:
        return 1

    log("compressing")
    import compress

    before = os.path.getsize(disk)
    compress.compress(disk, args.out)
    after = os.path.getsize(args.out)
    log(f"  {before >> 20} MiB -> {after >> 20} MiB")
    log(f"READY {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
