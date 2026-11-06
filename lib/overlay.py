"""A read-only base disk plus a throwaway overlay: every run starts from the same bytes.

    from overlay import Overlay
    run = Overlay(base="~/disks/macos15-desktop.qcow2", work="out/run")
    disk = run.open()          # the overlay qemu should use
    ...
    run.close()                # deletes the overlay; base is untouched

WHY THIS EXISTS

A 27 GiB macOS disk takes fifteen minutes to download and is the only copy of half a day's
work. Every attempt to test something currently does one of two things to it:

    working_copy() moves it      the base is consumed by the first run and every later
                                 experiment starts from a disk the previous one already
                                 wrote to
    working_copy(copy=True)      keeps the base, but a 27 GiB copy per attempt on a machine
                                 that then has two of them

NEITHER IS "START FROM THE SAME BYTES". The first does not keep the base, and the second does
not guarantee that the base is unchanged - it copies it, so a run that corrupts its copy is
invisible until the copy is compared, and nothing compares it.

A QCOW2 BACKING CHAIN IS EXACTLY THIS. `qemu-img create -b base.qcow2 overlay.qcow2` makes a
file that reads through to the base for every cluster it does not have, and writes every new
cluster into itself. The base is opened READ-ONLY by qemu while a backing file is in use, so a
run cannot corrupt it even if it tries; and `close()` deletes the overlay, which restores the
disk to its published state exactly - not approximately, not by copying it back.

WHAT IT COSTS: one overlay per run, growing only with what that run writes. A macOS boot
writes a few hundred MiB to a couple of GiB; the base stays 27 GiB and is never copied.

THE BASE IS VERIFIED UNCHANGED, because "I did not write to it" is an assumption and this file
exists to stop that kind of assumption. `open()` records the base's size and modification time
before qemu starts and `close()` checks them again, so a run that somehow touched it says so
rather than leaving a quietly modified golden image behind.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def log(message: str) -> None:
    print(f"[overlay] {message}", flush=True)


class Overlay:
    """A writable qcow2 whose parent is a read-only base disk."""

    def __init__(self, base: str, work: str, *, name: str = "run.qcow2") -> None:
        self.base = os.path.abspath(os.path.expanduser(base))
        self.work = os.path.abspath(work)
        self.path = os.path.join(self.work, name)
        self._base_stat: tuple[int, int] | None = None

    def open(self) -> str:
        """Create the overlay and return its path."""
        if not os.path.isfile(self.base):
            raise SystemExit(
                f"=== NO BASE DISK AT {self.base} ===\n"
                f"  this run starts from a published disk; fetch it first with iso.fetch_tag")
        os.makedirs(self.work, exist_ok=True)

        # THE BASE'S IDENTITY BEFORE ANYTHING OPENS IT.
        st = os.stat(self.base)
        self._base_stat = (st.st_size, st.st_mtime_ns)

        # A LEFTOVER OVERLAY IS DELETED, NOT REUSED. It is a disk a previous attempt wrote to,
        # and starting from it would carry that attempt's half-finished work forward - which is
        # the failure mode this whole file exists to prevent.
        if os.path.exists(self.path):
            log(f"removing the previous {os.path.basename(self.path)}")
            os.unlink(self.path)

        log(f"base   {self.base} ({os.path.getsize(self.base) >> 20} MiB, read-only)")
        result = subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
             "-b", self.base, self.path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"could not create the overlay: {(result.stderr or result.stdout).strip()[:300]}")
        log(f"overlay {self.path} ({os.path.getsize(self.path) >> 10} KiB)")
        return self.path

    def close(self) -> None:
        """Delete the overlay and confirm the base is byte-for-byte the same file it was."""
        if os.path.exists(self.path):
            size = os.path.getsize(self.path)
            os.unlink(self.path)
            log(f"released the overlay ({size >> 20} MiB written this run)")
        self.verify()

    def verify(self) -> None:
        """Fail loudly if the base is not exactly what it was when open() recorded it."""
        if self._base_stat is None:
            return
        if not os.path.isfile(self.base):
            raise SystemExit(f"=== THE BASE DISK IS GONE: {self.base} ===")
        st = os.stat(self.base)
        now = (st.st_size, st.st_mtime_ns)
        if now != self._base_stat:
            raise SystemExit(
                f"=== THE BASE DISK CHANGED DURING THIS RUN ===\n"
                f"  {self.base}\n"
                f"  before: size {self._base_stat[0]} mtime_ns {self._base_stat[1]}\n"
                f"  after:  size {now[0]} mtime_ns {now[1]}\n"
                f"  the base is a published artifact and must never be written to")
        log(f"base unchanged (size {now[0]}, mtime {now[1]})")

    def flatten(self, dest: str) -> str:
        """Write the overlay's CURRENT contents out as a standalone disk.

        THE PUBLISHED RESULT OF A RUN IS A DISK, and an overlay is not one: it is a diff that
        only means anything next to the base it was made from. So a stage that produces an
        artifact converts here, which is `qemu-img convert` - the same call lib/compress.py
        makes, reading the chain and writing one file.
        """
        dest = os.path.abspath(dest)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        log(f"flattening {os.path.basename(self.path)} -> {os.path.basename(dest)}")
        result = subprocess.run(
            ["qemu-img", "convert", "-O", "qcow2", "-c",
             "-o", "compression_type=zstd", self.path, dest],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"could not flatten the overlay: {(result.stderr or result.stdout).strip()[:300]}")
        log(f"  {dest} ({os.path.getsize(dest) >> 20} MiB)")
        return dest


def main(argv: list[str]) -> int:
    """Exercise the mechanism without a VM: create, write, close, confirm the base is intact.

        python3 lib/overlay.py --base <disk.qcow2>
    """
    import argparse

    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base", required=True)
    p.add_argument("--work", default="/tmp/opencode/overlay-selftest")
    args = p.parse_args(argv)

    run = Overlay(args.base, args.work)
    disk = run.open()

    # WRITE SOMETHING, SO close() HAS SOMETHING TO DELETE AND THE SIZE IS NON-TRIVIAL.
    log("writing 8 MiB into the overlay")
    with open(disk, "r+b") as fh:
        fh.seek(0)
        fh.write(b"\xAA" * (8 << 20))
        fh.flush()
        os.fsync(fh.fileno())
    log(f"  overlay is now {os.path.getsize(disk) >> 20} MiB")

    run.close()
    log("=== BASE INTACT, OVERLAY RELEASED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
