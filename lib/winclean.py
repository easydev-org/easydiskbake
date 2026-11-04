"""Clean a Windows guest before its disk is exported, and say what was freed.

    from winclean import clean
    ok = clean(scp=scp_to, ssh_run=ssh_run, user="docker", note=log)

WHY THIS IS IN lib/ AND NOT windows/11/

Two stages need it and they reach the guest differently:

    windows/11/ssh.py        drives the guest with the keyboard - sshd is what that stage
                             just installed, and a stage that cleaned by using ssh would be
                             testing the thing it is about to publish with itself
    windows/11/devtools.py   is already over ssh, because the toolchain is installed there

The cleaning is identical, and it is the part that decides the size of every artifact
published after it. A second copy would be a second place for "zero the free space" to be
forgotten, and that failure is invisible: the disk cleans correctly and still converts to
the same size.

WHY ZEROING IS THE STEP THAT MATTERS

`qemu-img convert -O qcow2` only skips clusters that are already zero. Deleting a file in
NTFS marks its clusters free and leaves the bytes where they were, so convert stores them
and the qcow2 comes out the same size as before. A file grown to fill the free space with
zeros and then deleted is what makes those clusters genuinely zero when convert looks.

Everything else in clean.ps1 - installers, caches, temp, history - is what CREATES the free
space. This module runs that, then reports what was freed, so a run that reclaimed nothing
is visible rather than merely quiet.

THE TWO CALLBACKS ARE PASSED, NOT IMPORTED

scp and ssh are four lines each in both callers and they differ in how they reach the
machine. Taking them as arguments keeps this module from knowing which stage is calling, and
means a caller cannot accidentally get a different guest's connection.
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(HERE, "windows", "11", "clean.ps1")

# THE MARKER clean.ps1 PRINTS WHEN IT FINISHES. Silence is not success: a script that died
# half-way leaves the disk carrying whatever it had not removed yet.
DONE = "CLEAN_DONE"


def clean(*, scp, ssh_run, user: str, note=print, timeout: int = 1800) -> bool:
    """Copy clean.ps1 into the guest, run it, and report what it freed.

    `scp(local, remote)` puts a file in the guest.
    `ssh_run(command, timeout=...)` runs a command there and returns .stdout/.stderr.
    """
    if not os.path.isfile(SCRIPT):
        raise SystemExit(f"{SCRIPT} is missing; the disk would ship with its build traces")

    note("cleaning the guest")
    remote = f"C:/Users/{user}/clean.ps1"

    try:
        scp(SCRIPT, remote)
    except (OSError, RuntimeError) as exc:
        note(f"=== CLEAN.PS1 COULD NOT BE COPIED: {exc} ===")
        return False

    result = ssh_run(
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {remote}",
        timeout=timeout,
    )
    out = (result.stdout or "") + (result.stderr or "")

    summary = []
    for line in out.splitlines():
        text = line.strip()
        if not text or text.startswith("Warning: Permanently added"):
            continue
        note(f"  {text[:150]}")
        if text.startswith(("used before:", "used after:", "freed:")):
            summary.append(text)

    if DONE not in out:
        note("=== clean.ps1 DID NOT REPORT COMPLETION ===")
        note("  the disk still carries whatever the script had not removed yet")
        return False

    note("clean complete" + (" — " + ", ".join(summary) if summary else ""))
    return True
