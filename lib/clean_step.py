"""Clean a local disk that is not running: boot it, clean it over ssh, stop it.

    python3 lib/clean_step.py --disk out/windows11-ssh.qcow2 --kind windows
    python3 lib/clean_step.py --disk out/windows11-ssh.qcow2 --kind windows --bootstate out/ssh-work

WHY THIS EXISTS WHEN lib/clean_disk.clean_running() DOES

Two different situations, and they are worth two entry points:

    clean_running()    called from inside a stage whose guest is ALREADY UP. No boot, no
                       shutdown - just the cleaning, over a connection that exists. This is
                       what windows/11/devtools.py and linux/*/bake.py use, because they
                       finish with the machine running.

    this script        a workflow STEP. The stage before it has exited and its guest is
                       gone, so the disk has to be started again. That boot is the price of
                       having the clean be a separate, visible, separately-failing step -
                       which is what it costs to see "clean the disk: failed" in the run
                       graph instead of a line buried in someone else's log.

THE DISK IS MODIFIED IN PLACE. This is the last step before compress and publish, so the
disk passed in IS the one that ships: cleaning a copy would mean the copy has to be moved
back afterwards, and a copy-back that failed would publish the dirty one.

THE BOOTSTATE IS OPTIONAL AND USUALLY NEEDED ON WINDOWS. The firmware variables live in
`<work>/*.bootstate.tar.gz`; without them the machine starts and finds no boot entry, which
looks like a disk that does not boot. `--bootstate` points at the directory holding them.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import vm  # noqa: E402


def log(message: str) -> None:
    print(f"[clean-step {time.strftime('%H:%M:%S')}] {message}", flush=True)


# WHAT EACH SYSTEM NEEDS TO BE STARTED AND CLEANED. Only the differences, so a fourth system
# is one entry. `clean` is passed straight to clean_disk.clean_running().
SYSTEMS = {
    "windows": {"ssh_port": 2222, "clean": "windows", "bootstate": True},
    "linux": {"ssh_port": 2222, "clean": "linux", "bootstate": False},
    "freebsd": {"ssh_port": 2222, "clean": "freebsd", "bootstate": False},
}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the qcow2 to boot, clean and keep")
    p.add_argument("--kind", required=True, choices=sorted(SYSTEMS))
    p.add_argument("--work", default=None,
                   help="where qemu writes its sockets and firmware state; defaults to the "
                        "disk's directory")
    p.add_argument("--bootstate", default=None,
                   help="a directory holding <disk>.bootstate.tar.gz; for Windows")
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--mem", type=int, default=6144)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--artifacts", default="out")
    args = p.parse_args(argv)

    if not os.path.isfile(args.disk):
        log(f"=== NO DISK AT {args.disk} ===")
        return 2

    spec = SYSTEMS[args.kind]
    work = os.path.abspath(args.work or os.path.dirname(os.path.abspath(args.disk)))
    os.makedirs(work, exist_ok=True)
    os.makedirs(args.artifacts, exist_ok=True)

    verdict = os.path.join(args.artifacts, f"clean-{args.kind}.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)
        try:
            with open(verdict, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    note(f"disk {args.disk} ({os.path.getsize(args.disk) >> 20} MiB)")

    if spec["bootstate"]:
        import bootstate

        # THE ARCHIVE HAS TO SIT BESIDE THE DISK UNDER THE DISK'S OWN NAME - that is the only
        # path restore() reads. `--bootstate` says where the artifact was downloaded to, and
        # adopt() does the rename.
        dest = bootstate.archive_path(args.disk)
        if not os.path.exists(dest):
            source_dir = args.bootstate or work
            found = [
                os.path.join(source_dir, n)
                for n in sorted(os.listdir(source_dir))
                if n.endswith(".bootstate.tar.gz")
            ] if os.path.isdir(source_dir) else []
            if not found:
                note(f"=== NO BOOTSTATE IN {source_dir} ===")
                note("  Windows boots by a boot entry stored in its firmware variables;")
                note("  without the archive the machine starts and finds nothing")
                return 2
            if len(found) > 1:
                note("=== MORE THAN ONE BOOTSTATE; REFUSING TO GUESS ===")
                for f in found:
                    note(f"  {os.path.basename(f)}")
                return 2
            bootstate.adopt(args.disk, found[0])

        note("restoring the firmware state")
        if not bootstate.restore(work, args.disk):
            note("=== THE BOOTSTATE COULD NOT BE RESTORED ===")
            return 2

    shape = vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                     resolution=(1920, 1080), ssh_port=spec["ssh_port"])
    # discard=unmap so a guest's fstrim reaches the qcow2 and drops the clusters, rather than
    # qemu storing zeros that convert has to read again.
    guest = vm.Guest(work, extra=[
        "-drive", f"file={args.disk},if=virtio,format=qcow2,discard=unmap",
    ], shape=shape, stdout=os.devnull)

    ok = False
    try:
        note("starting the disk")
        guest.start()

        import session

        note("waiting for ssh")
        if not session.wait_for_ssh(spec["ssh_port"], args.boot_timeout, note):
            note("=== THE DISK DID NOT ANSWER ON SSH ===")
            note("  it cannot be cleaned if it cannot be reached; the disk is unchanged")
            return 1

        import clean_disk

        ok = clean_disk.clean_running(spec["clean"], port=spec["ssh_port"], note=note)
        if not ok:
            note("=== THE CLEAN DID NOT FINISH ===")
            return 1
        return 0
    finally:
        # ACPI FIRST WHEN IT WORKED, AND LONG ENOUGH. The clean wrote gigabytes of zeros
        # seconds ago; a disk killed mid-write repairs itself on every later boot. 60s was
        # not enough and produced "the guest did not power off within 60s; killing it".
        guest.stop(acpi_first=ok, acpi_timeout=300)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
