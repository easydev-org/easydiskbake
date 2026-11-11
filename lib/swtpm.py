#!/usr/bin/env python3
"""A software TPM that a Windows guest can boot against, and that outlives one stage.

    from lib import swtpm as swtpmmod
    socket = swtpmmod.start(work)

WHY THIS IS IN `lib/` AND NOT IN A STAGE

`windows/11/ssh.py` and `windows/11/devtools.py` each had this function, character for character.
It is not a fact about either stage: Windows 11 will not boot without a TPM, and the disk remembers
the TPM it was installed against, so BOTH stages have to start one on the same state directory or
the second boot sees a different machine.

NO --daemon, AND THE OUTPUT IS KEPT.

With `--daemon`, swtpm forks and the parent's exit status says only that the fork worked. If the
state directory cannot be loaded, the CHILD prints the reason and dies - and the caller sees
nothing until qemu fails to talk to it:

    qemu-system-x86_64: tpm-emulator: TPM result for CMD_INIT: 0x9 operation failed
    qemu exited during the boot
    0 frames captured over 12s

which is what launch-windows reported on runs 41501928264, 41503681324 and 41516552206. Running it
in the foreground with its log captured means a bad state is reported as a bad state, by swtpm, in
its own words.

THE SOCKET IS WAITED FOR, NOT ASSUMED. swtpm creates it when it is ready to serve, and qemu
connecting a moment early is the same failure seen from the other side.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time


def log(message: str) -> None:
    print(f"[swtpm] {message}", flush=True)


def start(work: str, *, timeout: int = 30) -> str:
    """Start swtpm on `work`'s tpm state and return its socket, or "" if it would not come up."""
    state = os.path.join(work, "tpm")
    os.makedirs(state, exist_ok=True)
    sock = os.path.join(state, "swtpm.sock")
    if not shutil.which("swtpm"):
        log("=== swtpm IS NOT INSTALLED IN THIS IMAGE ===")
        log("  Windows 11 will not boot without a TPM; the installer stops at the hardware check")
        return ""

    # A STALE SOCKET FROM A PREVIOUS RUN IS NOT A RUNNING SERVER. The wait below would otherwise
    # be satisfied by a file nothing is listening on.
    try:
        os.unlink(sock)
    except OSError:
        pass

    handle = open(os.path.join(work, "swtpm.log"), "wb")
    proc = subprocess.Popen(
        ["swtpm", "socket", "--tpmstate", f"dir={state}",
         "--ctrl", f"type=unixio,path={sock}", "--tpm2",
         "--log", "level=5"],
        stdout=handle, stderr=subprocess.STDOUT,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            log("=== swtpm EXITED BEFORE IT WAS READY ===")
            try:
                with open(os.path.join(work, "swtpm.log"), errors="replace") as fh:
                    for line in fh.readlines()[-6:]:
                        log(f"    {line.rstrip()[:180]}")
            except OSError:
                pass
            return ""
        if os.path.exists(sock):
            log(f"tpm    {sock}")
            return sock
        time.sleep(0.2)
    log(f"=== swtpm DID NOT CREATE {sock} IN {timeout}s ===")
    return ""


def stop(work: str) -> None:
    """Stop swtpm on `work`'s TPM state so the state it holds reaches the disk.

    pkill on the state directory: swtpm has no stop command, and the state directory is the only
    thing that identifies this instance. A timeout is what makes the subsequent read safe -
    SIGTERM lets swtpm flush, and a state file written before the flush is one that fails to
    initialise.
    """
    marker = os.path.join(work, "tpm")
    subprocess.run(["pkill", "-f", marker], check=False)
    for _ in range(20):
        time.sleep(0.25)
        check = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        if not check.stdout.strip():
            return
    log(f"  swtpm on {marker} is still running after 5s")
