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
for path in (REPO, os.path.join(REPO, "lib")):
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
    """
    import vm

    running = os.path.join(work, "selftest-disk.qcow2")
    if os.path.exists(running):
        os.unlink(running)
    log(f"copying {os.path.basename(disk)} -> {running} (the original is not opened)")
    shutil.copyfile(disk, running)

    os.makedirs(work, exist_ok=True)
    if family == "macos":
        return _start_macos(running, work, ssh_port, log)
    if family == "windows":
        return _start_windows(running, work, ssh_port, log)
    raise SystemExit(f"no boot for {family!r}")


def _start_macos(disk: str, work: str, ssh_port: int, log):
    """OpenCore, a fresh variable store, and the target drive - what macos/15/bake.py does."""
    import machine
    import opencore
    import macos_recovery
    import vm

    release = "15"
    log("=== recovery image ===")
    macos_recovery.fetch_published(release, work)
    log("=== OpenCore, firmware, APFS driver ===")
    assets = opencore.build(release, work)

    shape = machine.shape(assets, cpus=4, mem_mb=8192)
    shape.ssh_port = ssh_port
    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            # TARGET_DRIVE IS THE VALUE, NOT THE FLAG. bake.py pairs it with "-drive" the same
            # way; passing it alone makes qemu read a filename as an option.
            "-drive", machine.TARGET_DRIVE.format(disk=disk),
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
            "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
        ],
        shape=shape,
        stdout=os.path.join(work, "console.log"),
    ).start()
    log(f"started; the QMP socket is {guest.paths.drive}")
    return guest


def _start_windows(disk: str, work: str, ssh_port: int, log):
    """OVMF with a fresh variable store, and NO TPM - see windows/11/ssh.py."""
    import vm

    firmware_code = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd")
    firmware_vars = os.path.join(work, "OVMF_VARS.fd")
    template = os.environ.get("OVMF_VARS_TEMPLATE", "/usr/share/OVMF/OVMF_VARS_4M.fd")
    shutil.copyfile(template, firmware_vars)

    shape = vm.Shape(
        cpus=4, mem_mb=6144, machine="q35",
        firmware_code=firmware_code, firmware_vars=firmware_vars,
        tpm_socket="", vga="std", ssh_port=ssh_port,
    )
    guest = vm.Guest(
        work,
        extra=["-drive", f"file={disk},if=virtio,format=qcow2"],
        shape=shape,
        stdout=os.path.join(work, "console.log"),
    ).start()
    log(f"started; the QMP socket is {guest.paths.drive}")
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
    command = hostshare.mount_commands(user=user)[family]
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
