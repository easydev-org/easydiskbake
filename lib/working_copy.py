"""Put a disk where a stage needs it, without copying it when a copy protects nothing.

    from lib.working_copy import working_copy
    disk = working_copy(args.disk, os.path.join(work, "desktop.qcow2"), note=log)

THE COPY THAT WAS THERE, AND WHY IT WAS POINTLESS

Every stage that starts from a finished disk copied it into its work directory first:

    # A COPY, so a failed wizard leaves the setup-stage disk intact for the next attempt.
    cp --reflink=auto out/macos15-setup.qcow2 out/desktop.qcow2

That comment assumed the local file is the only copy. In this repository it never is: the
stages that run after a bake pull their disk from ghcr, so the original is in the registry and
is not touched by anything that happens locally. If a stage damages its working disk, re-running
the job pulls a fresh one - which is what `Re-run failed jobs` does.

On btrfs or XFS `--reflink=auto` makes the copy free, so this cost nothing and nobody noticed.
On ext4 - which is what the GitHub runner uses - it is a real 28 GiB copy for macOS and 12 GiB
for Windows, five minutes and two minutes respectively, on every run, to protect a file that is
already somewhere else.

WHEN A COPY IS STILL RIGHT

    the disk was baked IN THIS RUN and not published yet
    the disk is the output of an earlier stage in the same job

Then it really is the only copy, and `--copy` keeps the behaviour. The caller knows which case
it is; this function does not guess, because guessing wrong either wastes minutes or destroys
the only copy of an hour's work.

WHAT MOVING COSTS

A failed stage that moved its input needs the input again, and it comes from ghcr. That is a
pull of the same size as the copy it saved - but only on the failure path, which is rare, while
the copy was paid on every run.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def working_copy(source: str, dest: str, *, copy: bool = False, note=print) -> str:
    """Make `source` available at `dest` and return the path to use.

    THE DEFAULT IS TO MOVE. `source` and `dest` are normally the same directory, so a rename is
    instant and leaves no second copy of a 28 GiB file on a 14 GiB-free runner.

    `copy=True` keeps the original, for the case where it really is the only one - the caller
    decides, because only the caller knows whether the disk is published.
    """
    if not os.path.isfile(source):
        raise SystemExit(f"no disk at {source}")

    dest = os.path.abspath(dest)
    source = os.path.abspath(source)
    if source == dest:
        note(f"using {dest} in place")
        return dest

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    # A LEFTOVER FROM AN EARLIER RUN IS REMOVED, not reused: it is a disk this stage previously
    # wrote, and starting from it would carry that run's half-finished work forward.
    if os.path.exists(dest):
        note(f"removing the previous {os.path.basename(dest)}")
        os.unlink(dest)

    if copy:
        note(f"copying {os.path.basename(source)} -> {os.path.basename(dest)}")
        # --reflink=auto is free where the filesystem supports it and a real copy where it does
        # not; either way the source survives.
        r = subprocess.run(["cp", "--reflink=auto", source, dest], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(
                f"could not copy the disk: {(r.stderr or r.stdout).strip()[:300]}")
        return dest

    # MOVE. Same filesystem, so this is a rename; a different filesystem falls back to copy and
    # delete, which is still no worse than what it replaces.
    try:
        os.replace(source, dest)
        note(f"moved {os.path.basename(source)} -> {os.path.basename(dest)}")
    except OSError as exc:
        note(f"could not rename ({exc}); falling back to copy")
        shutil.copyfile(source, dest)
        os.unlink(source)
    return dest
