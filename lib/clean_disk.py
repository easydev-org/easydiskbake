"""Clean a published disk as a job of its own: boot it, clean it over ssh, publish it.

    python3 lib/clean_disk.py --tag windows-ssh-20261103-104941 --out-kind windows-ssh
    python3 lib/clean_disk.py --tag linux-ubuntu --kind linux --out-kind linux-ubuntu

WHY THIS IS A STAGE AND NOT A STEP INSIDE THE BAKES

It was a step inside them first, and that had three problems the shape could not solve:

    every bake grew its own copy of "boot, ssh, clean, shut down", and each copy was the
    place a different bug lived - Windows reached the guest with sshpass, Linux had a script
    fetched from a seed server, NixOS typed commands at a serial console

    a bake that cleaned could not be re-run without re-installing, so a cleaning bug cost
    an hour of macOS installation instead of ten minutes of booting

    the cleaning was invisible in the artifact history - a disk published by a bake looked
    exactly like one that had been through a clean, because the clean was a line in a log
    rather than a stage with its own tag

So this runs on a disk that is ALREADY PUBLISHED. It pulls the tag, boots it with no
installer attached, logs in, cleans, shuts down through ACPI, and publishes the result.
Everything it needs is in the tag; nothing is passed between jobs except that tag.

THE BOOTSTATE IS PART OF THE DISK.

Windows and macOS carry firmware variables that have to travel with the disk - without them
the machine does not find its boot entry. Those live in a separate artifact, so this takes
`--bootstate-run` as an optional second input and restores it before booting. For Linux and
BSD there is none, and the flag is simply not passed.

WHAT "CLEAN" MEANS ON EACH SYSTEM

The single thing they share is the last step: write zeros over the free space, because
`qemu-img convert -O qcow2` only skips clusters that are already zero. Everything before that
is what creates the free space, and it is per-system - see lib/linclean.py and
lib/winclean.py. This module is the part that boots, connects and commits, so neither of
those has to know how a machine is started.
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
    print(f"[clean {time.strftime('%H:%M:%S')}] {message}", flush=True)


# HOW EACH SYSTEM IS BOOTED AND CLEANED. The three things that differ, in one table, so a
# fourth system is one entry rather than a fourth copy of the flow.
#
#   accel_cpu   the cpu string qemu is given; Redox needs its own because its kernel does not
#               handle six features, the others take the host's
#   wait        what proves the guest is up, before anything is run on it
#   clean       the module and callable that does the work over ssh
SYSTEMS = {
    "windows": {
        "ssh_port": 2222,
        "bootstate": True,
        "clean": ("winclean", "clean", {"user": "docker"}),
    },
    "linux": {
        "ssh_port": 2222,
        "bootstate": False,
        "clean": ("linclean", "clean", {}),
    },
}


def pull(tag: str, work: str, note) -> str:
    """Fetch the disk for `tag` from ghcr, resuming. Returns its path."""
    import iso

    dest = os.path.join(work, "disk.qcow2")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        note(f"disk already here: {dest} ({os.path.getsize(dest) >> 20} MiB)")
        return dest
    note(f"pulling {tag}")
    return iso.fetch_tag(tag, dest)


def restore_bootstate(work: str, disk: str, run_id: str, note) -> bool:
    """Put the firmware variables back where qemu will look for them.

    A MISSING ARCHIVE IS A FAILURE WHEN ONE WAS ASKED FOR. Windows boots by a boot entry that
    lives in the vars file; without it the firmware starts, finds nothing, and the run looks
    like a disk that does not boot.
    """
    import bootstate

    archive = os.path.join(work, f"{os.path.basename(disk)}.bootstate.tar.gz")
    os.makedirs(work, exist_ok=True)

    if not os.path.exists(archive):
        note(f"=== NO BOOTSTATE AT {archive} ===")
        note(f"  run {run_id} was given, so one was expected. Download the artifact named")
        note("  <disk>.bootstate.tar.gz from that run into the work directory first.")
        return False

    note(f"restoring {os.path.basename(archive)} into {work}")
    return bootstate.restore(work, disk)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tag", required=True,
                   help="the published disk to clean, e.g. windows-ssh-20261103-104941")
    p.add_argument("--kind", required=True, choices=sorted(SYSTEMS),
                   help="which system this disk is")
    p.add_argument("--out", required=True, help="the cleaned qcow2 to write")
    p.add_argument("--work", default=None)
    p.add_argument("--artifacts", default="out")
    p.add_argument("--bootstate-run", default="",
                   help="run id holding the bootstate artifact, for Windows and macOS")
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--mem", type=int, default=6144)
    p.add_argument("--cpus", type=int, default=4)
    args = p.parse_args(argv)

    work = os.path.abspath(args.work or os.path.join(args.artifacts, "clean-work"))
    artifacts = os.path.abspath(args.artifacts)
    os.makedirs(work, exist_ok=True)
    os.makedirs(artifacts, exist_ok=True)

    verdict = os.path.join(artifacts, "clean.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)
        try:
            with open(verdict, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    spec = SYSTEMS[args.kind]

    disk = pull(args.tag, work, note)
    note(f"disk {disk} ({os.path.getsize(disk) >> 20} MiB)")

    if spec["bootstate"]:
        if not args.bootstate_run:
            note("=== THIS SYSTEM NEEDS ITS BOOTSTATE AND NONE WAS GIVEN ===")
            note(f"  pass --bootstate-run with the run that published {args.tag}")
            return 2
        if not restore_bootstate(work, disk, args.bootstate_run, note):
            return 1
    elif args.bootstate_run:
        note(f"  {args.kind} needs no bootstate; ignoring --bootstate-run")

    shape = vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                     resolution=(1920, 1080), ssh_port=spec["ssh_port"])
    # NO CDROM AND NO INSTALLER. That is what makes this a test of the disk.
    guest = vm.Guest(work, extra=["-drive", f"file={disk},if=virtio,format=qcow2"],
                     shape=shape, stdout=os.devnull)

    ok = False
    try:
        guest.start()
        note("waiting for the guest to answer on ssh")
        import session

        if not session.wait_for_ssh(spec["ssh_port"], args.boot_timeout, note):
            note("=== THE GUEST DID NOT ANSWER ON SSH ===")
            note("  it cannot be cleaned if it cannot be reached; the disk is unchanged")
            return 1

        module_name, function_name, extra = spec["clean"]
        clean_module = __import__(module_name)
        clean = getattr(clean_module, function_name)
        # THE CALL IS THE SAME SHAPE FOR EVERY SYSTEM: port and note, plus whatever the table
        # says that system's cleaner needs. Both cleaners were written to accept exactly this
        # rather than a connection, so adding a system is one table entry.
        if not clean(port=spec["ssh_port"], note=note, **extra):
            note("=== THE CLEAN DID NOT FINISH; nothing is published ===")
            return 1

        ok = True
    finally:
        # ACPI FIRST WHEN IT WORKED, because the cleaning wrote to the filesystem and a
        # powered-off mid-write disk is one that repairs itself on every later boot.
        guest.stop(acpi_first=ok)

    if not ok:
        return 1

    # THE SAME PRESSURE, IN THE SAME ORDER: boot, clean, shut down, then compress.
    #
    # Compressing the UNCOMPRESSED disk here rather than before is the whole point of the
    # zeroing: qemu-img convert only skips clusters that are already zero, and the guest has
    # just made them zero. A copy that skipped this would convert to the disk's high-water
    # mark and look exactly like a clean.
    note("compressing")
    import compress

    compress.compress(disk, args.out)
    note(f"CLEANED {args.tag} -> {args.out} ({os.path.getsize(args.out) >> 20} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
