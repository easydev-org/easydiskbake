"""Start one already-built disk, for a test that must not touch the published artifact.

    python3 lib/launch_selftest.py --family macos --disk out/macos15-dev.qcow2

WHY THIS IS A SEPARATE MODULE FROM lib/launch_windows.py

`launch_windows.py` is a REPORT: it starts a disk, photographs it, and writes `launch.txt` for a
job to publish. That is the right shape for "does this disk boot", and the wrong shape for "run
a script in it and read the answer" - one produces evidence for a person, the other asserts.

So this is the smaller thing both families need: start, log in, mount, and hand the guest back.
The two families genuinely differ in all three, and the differences are the ones the stages
already measured, so they are reused rather than re-derived:

    start     macOS boots with OpenCore + a fresh variable store; Windows with OVMF + no TPM.
              Both are what `bake.py`/`ssh.py` already do, so this calls the same builders.
    login     macOS shows a login window after the Setup Assistant; Windows autologons from
              the answer file and may have blanked its screen. Different problems, different
              fixes - `macos/15/ssh.unlock` and `windows/11/ssh.wake_from_lock_screen`.
    mount     SMB to the bridge address; the command differs per family (lib/hostshare.py).

NOTHING HERE PUBLISHES. The disk is opened read-write in the work directory and thrown away;
the caller's `--disk` is copied first unless it is already a work copy.
"""

from __future__ import annotations

import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# `machine`, `opencore` AND `macos_recovery` ARE IN macos/15, NOT IN lib/. See the note in
# macos/15/dev.py: asking lib for `machine` fails, and the failure arrives after a 16 GiB pull.
for path in (REPO, os.path.join(REPO, "lib"), os.path.join(REPO, "macos", "15")):
    if path not in sys.path:
        sys.path.insert(0, path)

import hostshare  # noqa: E402


def start(family: str, disk: str, work: str, *, ssh_port: int = 2222,
          boot_timeout: int = 900, log=print):
    """Boot `disk` from a COPY, so the artifact the caller named is never modified.

    THE COPY IS NOT PARANOIA. This test logs in and mounts a share; both write to the disk. A
    launch that started the original would leave a machine whose `~/hello.txt` and whose
    launchd/Scheduled-Task state are whatever the test did - and the caller's disk is the thing
    a later stage or a person might publish.

    THE MACHINE AND THE SHARE BOTH COME FROM lib/vm_session.py NOW. This used to build them
    itself, and so did lib/clean_step.py, and so did the stage scripts - three descriptions of one
    idea, of which only this one knew the disk expects a share on the bridge. That is how the
    clean ended up booting a disk whose daemon was mounting a server nobody was running.

    `share=""` because the CALLER serves it: launch_selftest drops its probe into the directory
    first, and a server started here would be serving it before the file existed.
    """
    import vm_session

    guest, reached = vm_session.start(
        family, disk, work, ssh_port=ssh_port, boot_timeout=boot_timeout,
        copy=True, share="", log=log)
    if guest is None or not reached:
        return None
    return guest


def login(family: str, guest, user: str, password: str, *, log=print) -> bool:
    """Get to a session, because the launcher fires at login and not before."""
    if family == "macos":
        # THE SSH STAGE'S OWN UNLOCK. It knows the password field's rectangle and how to
        # dismiss the keyboard assistant that comes up with the first session.
        import importlib.util

        path = os.path.join(REPO, "macos", "15", "ssh.py")
        spec = importlib.util.spec_from_file_location("macos_ssh_stage", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        log("waiting for ssh before typing at the login window")
        if not _wait_port(guest.shape.ssh_port, 600, log):
            log("=== sshd NEVER ANSWERED; THE DISK MAY NOT BE THE SSH-STAGE ONE ===")
            return False
        module.unlock(guest)
        module.dismiss_keyboard_assistant(guest)
        return True

    # WINDOWS AUTOLOGONS from the answer file, so there is no password to type. What it does
    # is blank the screen after a minute, and a blank screen is not a session - the keyboard
    # wake is what puts the shell back.
    log("waiting for ssh before waking the desktop")
    if not _wait_port(guest.shape.ssh_port, 900, log):
        log("=== sshd NEVER ANSWERED ===")
        return False
    import importlib.util

    path = os.path.join(REPO, "windows", "11", "ssh.py")
    spec = importlib.util.spec_from_file_location("windows_ssh_stage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    kb = module.Keyboard(guest.paths.drive, guest.work or ".")
    try:
        module.wake_from_lock_screen(kb)
    finally:
        kb.close()
    return True


def _wait_port(port: int, seconds: int, log) -> bool:
    import session

    deadline = __import__("time").time() + seconds
    while __import__("time").time() < deadline:
        if session.wait_for_ssh(port, 10, note=lambda m: None, probe="true"):
            return True
        if session.wait_for_ssh(port, 10, note=lambda m: None, probe="echo ok"):
            return True
    return False


def mount(family: str, guest, user: str, password: str, *, log=print) -> bool:
    """Mount ~/shared in the guest, with the command lib/hostshare.py names for this family."""
    import session

    port = guest.shape.ssh_port
    if not hostshare.listening():
        log("=== THE HOST IS NOT SERVING THE SHARE ===")
        return False
    command = hostshare.mount_commands(user=user, login_password=password)[family]
    status, output = session.ssh(port, command, timeout=180, user=user, password=password)
    log(f"mount -> {status}")
    for line in (output or "").splitlines()[:8]:
        log(f"    {line[:150]}")
    return status == 0


def stop(guest, *, acpi_first: bool = True) -> None:
    try:
        guest.stop(acpi_first=acpi_first)
    except Exception as exc:                                # noqa: BLE001
        print(f"[selftest] stop: {exc}", flush=True)
