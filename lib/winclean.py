"""Clean a Windows guest before its disk is exported, and say what was freed.

    from winclean import clean
    ok = clean(scp=scp_to, ssh_run=ssh_run, user="vmuser", note=log)

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


def _allocated_mib(disk: str) -> int:
    """Bytes the qcow2 occupies, in MiB; -1 when it cannot be read.

    THE ABSOLUTE qemu-img, for the reason lib/compress.py documents: the bare name is the
    Android SDK's copy on this image's PATH and it cannot open a disk whose clusters use
    compression_type=zstd.
    """
    import json
    import subprocess

    try:
        from compress import qemu_img

        r = subprocess.run([qemu_img(), "info", "--output=json", disk],
                           capture_output=True, text=True)
        return (json.loads(r.stdout).get("actual-size") or 0) >> 20
    except (OSError, ValueError):
        return -1


def clean(*, port: int, user: str = "vmuser", note=print, timeout: int = 1800,
          disk: str | None = None) -> bool:
    """Copy clean.ps1 into the guest, run it, and report what it freed.

    OVER SSH, LIKE EVERY OTHER GUEST IN THIS REPOSITORY. An earlier version took scp and
    ssh_run as callbacks, because it was called from inside a bake that already had a
    connection. As a stage of its own it makes its own, and the callbacks go away - the
    password and the account are the same everywhere.

    `port` is the forwarded host port.
    """
    import subprocess

    # THE FORWARD IS ON THE BRIDGE ADDRESS; see lib/net.py's OUTPUT rule.
    from session import SSH_HOST, SSH_PASSWORD
    if not os.path.isfile(SCRIPT):
        raise SystemExit(f"{SCRIPT} is missing; the disk would ship with its build traces")

    # THE IMAGE IS MEASURED TOO, AND THE TWO NUMBERS ARE DIFFERENT.
    #
    # The guest reports what the trim released; the qcow2 reports what it actually occupies.
    # A trim can release 116 GiB inside the guest and leave an image holding every cluster it
    # ever allocated, if the discard never reached the driver - and only this pair of numbers
    # says which happened.
    before = _allocated_mib(disk) if disk else -1
    note("cleaning the guest")
    remote = f"C:/Users/{user}/clean.ps1"

    # THE SCRIPT GOES UP WITH scp, because it is 190 lines and the keyboard is not an option
    # even when a keyboard is what installed sshd.
    try:
        copied = subprocess.run(
            ["sshpass", "-p", SSH_PASSWORD, "scp",
             "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-P", str(port), SCRIPT, f"{user}@{SSH_HOST}:{remote}"],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        note(f"=== CLEAN.PS1 COULD NOT BE COPIED: {exc} ===")
        return False
    if copied.returncode != 0:
        detail = (copied.stderr or copied.stdout or "").strip().splitlines()
        note(f"=== CLEAN.PS1 COULD NOT BE COPIED: {detail[-1][:200] if detail else ''} ===")
        return False

    result = subprocess.run(
        ["sshpass", "-p", SSH_PASSWORD, "ssh",
         "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "ConnectTimeout=15",
         "-p", str(port), f"{user}@{SSH_HOST}",
         f"powershell -NoProfile -ExecutionPolicy Bypass -File {remote}"],
        capture_output=True, text=True, timeout=timeout,
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
    if disk and before >= 0:
        after = _allocated_mib(disk)
        if after >= 0:
            note(f"  the image: {before} MiB -> {after} MiB ({before - after:+d})")
    return True
