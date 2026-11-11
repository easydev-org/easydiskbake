"""The one channel an Android guest has before anything else is installed: adb.

    adb: "id"
    adb_shell: /system/bin/sh          # optional
    timeout: 120

WHY THIS IS NOT `ssh:`

`ssh:` is how every other guest in this repository is reached, and it is the right channel for
them because their installers put sshd on the disk. Android does not: the guest's remote access
is adbd on 5555, and there is no sshd until a stage puts one there - which is a chicken-and-egg
that `ssh:` cannot express and `run:` (typing into a terminal over the framebuffer) solves only
by being blind to the result.

SO `adb:` IS TO ANDROID WHAT `ssh:` IS TO LINUX. Same shape: one command, and its exit status is
the verdict. The difference is the transport, and it is a fact about the guest rather than a
preference:

    adb shell  ->  adbd  ->  /system/bin/sh     no sshd, no key, no password

`adb root` FIRST, AND THE FILE SAYS WHETHER IT NEEDS IT. Measured on the installed BlissOS
16.9.7 disk (Android 13, x86_64):

    $ adb shell id
    uid=2000(shell) ... context=u:r:shell:s0

    $ adb root
    restarting adbd as root
    $ adb shell id
    uid=0(root) gid=0(root) ... context=u:r:su:s0

That is a real root shell - `context=u:r:su:s0`, not merely uid 0 - and it is the reason an
Android stage needs no `su` binary. Measured on the same disk: `/system/bin/su` does not exist
at all ("su: inaccessible or not found"), so the Termux-and-su approach that once existed here
could not have worked for exactly this reason.

`adb root:` IS A STEP OF ITS OWN RATHER THAN A FLAG ON EVERY COMMAND. It restarts adbd, which
drops any existing connection, so it has to happen once and be followed by a reconnect - doing
it implicitly would make the first command after it fail for a reason that looks like a broken
guest.

WHY THE ADB BINARY IS AN ABSOLUTE PATH

The bake image used to carry the Android SDK on PATH, and the emulator's own qemu-img (2.12, no
zstd) shadowed /usr/bin for every platform's bake. The SDK is gone from the image now, and adb
is not installed at all - so this module looks for it in the two places that can have it and says
so plainly when neither does, rather than resolving a bare `adb` that might be a different
version from the guest's adbd.
"""

from __future__ import annotations

import os
import shutil
import subprocess

# WHERE adb CAN BE. The bake image has neither, because Android no longer needs an SDK in it -
# a stage that wants adb installs android-tools-adb, or runs where the host provides it. The
# second path is the one the SDK's own package manager writes, kept because a developer machine
# that already had the SDK should not have to install a second copy.
CANDIDATES = (
    "/opt/android-sdk/platform-tools/adb",
    "/usr/bin/adb",
    "/usr/local/bin/adb",
)


def binary() -> str:
    """The adb to use, or a sentence naming every place that was looked in."""
    for path in CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    found = shutil.which("adb")
    if found:
        return found
    raise RuntimeError(
        "no adb on this machine. It is not in the bake image - Android stages that need it "
        "install it (android-tools-adb) or run where the runner provides it. Looked in: "
        + ", ".join(CANDIDATES))


def run(*args: str, timeout: int = 120, serial: str = "") -> tuple[int, str]:
    """One adb command. Returns (exit status, combined output)."""
    argv = [binary()]
    if serial:
        argv += ["-s", serial]
    argv += list(args)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"adb timed out after {timeout}s: {' '.join(args)}"
    except OSError as exc:
        return 127, f"adb could not be run: {exc}"
    return done.returncode, (done.stdout + done.stderr).strip()


def connect(port: int, *, timeout: int = 60) -> tuple[int, str]:
    """Attach to a guest's adbd on a forwarded TCP port and wait until it is listed as a device.

    THE CONNECT IS NOT ENOUGH AND THE DEVICE LIST IS THE PROOF. `adb connect` answers "connected
    to ..." for a socket that accepts and then says nothing - measured on a guest whose adbd was
    still starting - so the wait below is what decides, and it is the same reason
    lib/session.wait_for_ssh runs a command instead of reading a banner.
    """
    serial = f"127.0.0.1:{port}"
    status, output = run("connect", serial, timeout=timeout)
    if status != 0:
        return status, output
    import time
    deadline = time.time() + timeout
    last = output
    while time.time() < deadline:
        status, listing = run("devices", timeout=30)
        last = listing
        for line in listing.splitlines():
            if line.startswith(serial) and line.rstrip().endswith("device"):
                return 0, listing
        time.sleep(3)
    return 1, f"adb never listed {serial} as a device:\n{last}"


def as_root(serial: str, *, timeout: int = 60) -> tuple[int, str]:
    """`adb root`, then wait for the daemon to come back before anything else runs.

    THE RESTART IS THE WHOLE DIFFICULTY. `adb root` answers "restarting adbd as root" and kills
    the daemon that said it, so the connection is gone before the reply is read - and the client
    then reports the device as `offline` for a few seconds while adbd re-executes. Measured on the
    first YAML run of this stage, which treated that window as a failure:

        adb: unable to connect for root: closed
        id -> adb: device offline

    So the wait is on the DEVICE LIST plus a live `shell id`, and the old connection is dropped
    first with `disconnect` - a client that still believes it is attached reports the stale state
    rather than opening a new one.
    """
    import time

    run("disconnect", serial, timeout=30)
    time.sleep(1)
    run("connect", serial, timeout=30)
    time.sleep(2)

    status, output = run("root", timeout=timeout, serial=serial)
    # `adb root` CAN ANSWER NON-ZERO ON A BUILD WHOSE adbd IS ALREADY ROOT, which is not a failure.
    # The proof below is the uid, not this status.
    if status != 0 and "already" not in (output or "").lower() and "restarting" not in (output or "").lower():
        print(f"  adb root said: {output[:200]}", flush=True)

    deadline = time.time() + max(timeout, 60)
    last = output
    while time.time() < deadline:
        status, listing = run("devices", timeout=30)
        attached = any(line.startswith(serial) and line.rstrip().endswith("device")
                       for line in listing.splitlines())
        if attached:
            status, who = shell(serial, "id", timeout=30)
            last = who
            if status == 0 and "uid=0(" in who:
                return 0, who
        time.sleep(3)
    return 1, f"adbd did not come back as root ({last[:200]})"


def shell(serial: str, command: str, *, timeout: int = 120,
          as_root_user: bool = False) -> tuple[int, str]:
    """Run one command in the guest. The exit status is adbd's, which is the shell's."""
    argv = ["shell"]
    if as_root_user:
        # `su 0 sh -c ...` IS NOT AVAILABLE HERE, and this is the measured reason it is not
        # offered: the installed BlissOS disk has no /system/bin/su at all. Root is obtained by
        # `adb root`, which restarts adbd AS root - so a command issued after it already runs as
        # uid 0 and needs no wrapper. The flag exists only to refuse the assumption loudly.
        raise RuntimeError(
            "there is no `su` on this guest: obtain root with `adb root:` before the command, "
            "which restarts adbd as uid 0 (measured on BlissOS 16.9.7: context=u:r:su:s0)")
    argv.append(command)
    return run(*argv, timeout=timeout, serial=serial)
