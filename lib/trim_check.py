"""Does a guest's fstrim actually make a qcow2 smaller?

    python3 lib/trim_check.py --tag linux-ubuntu --kind linux

THE QUESTION, AND WHY IT DECIDES THE DESIGN

The cleaners write zeros over free space so that `qemu-img convert` will skip those clusters.
On a 24 GB disk that was 20 GB of writes and it worked. On a 128 GB disk it is 119 GB, and both
Linux bakes timed out at fifteen minutes - so the number that decides is not "free space" but
"how much of it has ever been written".

There is a cheaper mechanism, if it works: virtio-blk can DISCARD. The qcow2 driver that backs
the drive drops the clusters, and nothing is written at all:

    -drive ...,discard=unmap      qemu accepts discards
    fstrim /                      the guest says which blocks it has released

This measures whether that actually happens, because everything downstream depends on the
answer:

    it shrinks      the Linux and FreeBSD cleaners can stop writing zeros entirely
    it does not     the only correct alternative is `zerofree`, which reads the block bitmap
                    and zeroes only the blocks that are free AND non-zero - the wrong one,
                    which is what is in the tree now, writes the whole free space

WHAT IS MEASURED, AND ON WHAT

The disk is a COPY of the tag, so nothing published is touched. `allocated` is `qemu-img
info`'s "disk size" - the bytes the qcow2 actually occupies - and it is the number that has to
fall. Virtual size is printed beside it so the two cannot be confused, because "17% of 23070
MiB" is what a report looks like when they are.
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

SYSTEMS = {
    "linux": {"ssh_port": 2222},
    "freebsd": {"ssh_port": 2222},
}


def log(message: str) -> None:
    print(f"[trim {time.strftime('%H:%M:%S')}] {message}", flush=True)


def allocated_mib(disk: str) -> int:
    """Bytes the qcow2 actually occupies, in MiB. `qemu-img info`'s "disk size"."""
    import subprocess

    r = subprocess.run(["qemu-img", "info", "--output=json", disk],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"qemu-img info failed: {(r.stderr or r.stdout)[:300]}")
    import json

    info = json.loads(r.stdout)
    # "actual-size" is what the file is on disk; some builds only report the file size. Both
    # are the same number here because the file IS the image.
    return (info.get("actual-size") or os.path.getsize(disk)) >> 20


def virtual_gib(disk: str) -> float:
    import json
    import subprocess

    r = subprocess.run(["qemu-img", "info", "--output=json", disk],
                       capture_output=True, text=True)
    info = json.loads(r.stdout) if r.returncode == 0 else {}
    return (info.get("virtual-size") or 0) / float(1 << 30)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tag", required=True)
    p.add_argument("--kind", required=True, choices=sorted(SYSTEMS))
    p.add_argument("--work", default=None)
    p.add_argument("--artifacts", default="out")
    p.add_argument("--boot-timeout", type=int, default=600)
    p.add_argument("--fstrim-timeout", type=int, default=600)
    args = p.parse_args(argv)

    work = os.path.abspath(args.work or os.path.join(args.artifacts, "trim-work"))
    artifacts = os.path.abspath(args.artifacts)
    os.makedirs(work, exist_ok=True)
    os.makedirs(artifacts, exist_ok=True)

    verdict = os.path.join(artifacts, "trim.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)
        try:
            with open(verdict, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    # THE DISK IS A COPY. Nothing published is touched by an experiment.
    disk = os.path.join(work, "disk.qcow2")
    if not os.path.exists(disk):
        import iso

        note(f"pulling {args.tag}")
        iso.fetch_tag(args.tag, disk)

    spec = SYSTEMS[args.kind]
    before = allocated_mib(disk)
    note(f"disk {disk}")
    note(f"  virtual size:  {virtual_gib(disk):.1f} GiB")
    note(f"  allocated:     {before} MiB   <- this is the number that has to fall")

    shape = vm.Shape(cpus=2, mem_mb=2048, machine="q35", vga="std",
                     ssh_port=spec["ssh_port"])
    # THE DISCARD OPTION IS THE WHOLE EXPERIMENT. Without it a trim from the guest reaches
    # nothing and this measures the absence of a feature rather than its usefulness.
    guest = vm.Guest(work, extra=[
        "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
    ], shape=shape, stdout=os.devnull)

    try:
        guest.start()
        import session

        note("waiting for ssh")
        if not session.wait_for_ssh(spec["ssh_port"], args.boot_timeout, note):
            note("=== THE DISK DID NOT ANSWER ON SSH ===")
            return 1

        # WHAT THE GUEST THINKS IT HAS, so the trim below is over something real.
        status, out = session.ssh(spec["ssh_port"], "df -m / | awk 'NR==2 {print $3, $4}'",
                                  timeout=120)
        note(f"  used/free MB:  {(out or '').strip()}")

        note(f"running fstrim -v / (up to {args.fstrim_timeout}s)")
        started = time.time()
        status, out = session.ssh(
            spec["ssh_port"],
            f"sudo timeout {args.fstrim_timeout} fstrim -v / 2>&1",
            timeout=args.fstrim_timeout + 60,
        )
        note(f"  exit {status} after {time.time() - started:.0f}s")
        for line in (out or "").strip().splitlines()[-6:]:
            note(f"    {line[:160]}")

        note("stopping the guest")
    finally:
        guest.stop(acpi_first=True, acpi_timeout=180)

    # THE COPY IS NOT COMPRESSED, SO THE ONLY THING THAT CHANGED IS WHAT THE TRIM RELEASED.
    # A compress pass here would answer a different question - convert skips zero clusters on
    # its own - and the two must not be confused.
    after = allocated_mib(disk)
    note("")
    note(f"  allocated before: {before} MiB")
    note(f"  allocated after:  {after} MiB")
    delta = before - after
    note(f"  change:           {-delta:+d} MiB")
    note("")
    if delta > 64:
        note(f"=== fstrim WORKS: it released {delta} MiB with no writes ===")
        note("  the Linux and FreeBSD cleaners do not need to zero the free space")
        return 0
    note("=== fstrim DID NOT SHRINK THE IMAGE ===")
    note("  the discard never reached the qcow2, or this kernel/filesystem did not issue it")
    note("  the correct alternative is zerofree: it reads the block bitmap and zeroes only")
    note("  the blocks that are free AND non-zero, which is megabytes rather than gigabytes")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
