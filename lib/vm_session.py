"""Start a built disk the way the disk expects to be started, and stop it again.

    import vm_session
    with vm_session.open(args.family, args.disk, work, ssh_port=2222) as guest:
        ...  # guest is up and its share is being served, or the block is never entered

WHY THIS EXISTS

Three places start a guest that was built by this repository - `lib/clean_step.py`,
`lib/boot_machine.py` and the stage scripts under `macos/15` and `windows/11` - and until now each
one decided for itself what a running disk needs. That was fine while a disk needed nothing but
qemu. It stopped being fine when the share stage began installing a root daemon that mounts
172.30.0.1 at boot:

    clean_step.py   started the disk and served NOTHING on 172.30.0.1. The daemon retried a mount
                    that could never work, KeepAlive restarted it every sixty seconds, and the
                    clean timed out after 900s with "No route to host" (run 42756290022) on a
                    disk whose sshd had answered in thirty seconds during the stage before.

That is not a missing line in clean_step.py; it is three implementations of one idea and only one
of them knowing about the share. This module is the one implementation.

WHAT "THE WAY THE DISK EXPECTS" MEANS

    the machine      macos/15/machine.py or a plain q35, exactly as the stage that built the disk
                     started it. Reused, not restated - a second description drifts.
    the share        smbd serving ~/shared on the bridge address, BEFORE the guest boots.
                     A disk carrying the mount daemon will try to mount it within seconds of
                     launchd starting, and a server that appears later is a retry, not a mount.
    sshd             waited for, because every caller's first act is to connect.

WHAT THIS IS NOT

`boot_machine.start()` COPIES the disk first, because the launch self-test must not modify the
artifact it was given. Cleaning modifies the disk in place on purpose - it is the last step before
publish. So copying is a parameter, not a policy: `copy=False` opens the disk the caller named.
"""

from __future__ import annotations

import contextlib
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


def log_to(prefix: str = "vm"):
    """A logger with the stage's name on it. Every caller prints; none of them should look alike."""
    import time

    def log(message: str) -> None:
        print(f"[{prefix} {time.strftime('%H:%M:%S')}] {message}", flush=True)

    return log


def start(family: str, disk: str, work: str, *, ssh_port: int = 2222,
          boot_timeout: int = 900, copy: bool = True, share: str | None = None,
          cpus: int = 4, mem_mb: int = 0, discard: bool = True, log=print):
    """Serve the share, boot `disk`, wait for sshd.

    Returns `(guest, reached, overlay)`: `reached` is True when sshd answered, None when the
    machine came up and never did, and `(None, None, None)` when nothing could be started at all.
    A caller that only needs to know whether to continue can test the second value; the guest is
    returned either way so the failure can be photographed before it is stopped.

    THE THIRD VALUE IS THE OVERLAY WHEN `copy=True`, and its `close()` is what proves the base was
    never written. A caller that ignores it leaks a temporary file; a caller that calls `close()`
    gets the check. `stop()` does it.

    THE SHARE GOES UP FIRST. A disk built by this repository carries a launchd job that mounts
    172.30.0.1 the moment the system starts; if the server comes up second, the daemon has already
    failed once and is in its retry path. Starting the server first makes the disk's first attempt
    its only attempt.

    `share=None` MEANS ~/shared. Passing "" means do not serve one - which is only right for a
    disk built before the share stage existed, and the caller should say so where it calls this.
    """
    import overlay as overlaymod
    import vm

    os.makedirs(work, exist_ok=True)

    # AN OVERLAY INSTEAD OF A COPY, AND THAT CHANGES WHAT STOPPING MEANS.
    #
    # `copy=True` used to `shutil.copyfile` a 15-27 GiB disk so the caller's artifact could not be
    # written to. The copy cost minutes and disk space, and it did not actually guarantee anything:
    # a run that corrupted its copy left the original alone only because they were different
    # files, which is true but expensive.
    #
    # A qcow2 backing chain is the same guarantee for the price of the clusters this run writes,
    # and it makes one more thing true that matters here: THE BASE IS OPENED READ-ONLY. So a
    # verification stage does not need a clean shutdown at all - it can be killed, and the disk it
    # read is still the disk that was published. That is why these stages can stop being careful
    # about ACPI and `shutdown -h now`: there is nothing to flush.
    #
    # `copy=False` still means "open this disk itself", which is what the clean stage wants - it
    # writes, and its output is the artifact.
    run_overlay = None
    running = disk
    if copy:
        run_overlay = overlaymod.Overlay(base=disk, work=work, name="session-overlay.qcow2")
        running = run_overlay.open()

    # THE SERVER BEFORE THE MACHINE. See the note above; this is the one ordering decision this
    # module exists to hold, and it is the one clean_step.py got wrong by omission.
    served = None
    if share is not None:
        served = os.path.expanduser(share or hostshare.DEFAULT_DIR)
        os.makedirs(served, exist_ok=True)
        if not hostshare.start(served):
            log(f"=== THE HOST COULD NOT SERVE {served} ===")
            log("  the disk carries a job that mounts it at boot; without it that job fails and")
            log("  retries. Booting anyway would test a state the disk never ships in.")
            _discard_overlay(run_overlay, log)
            return None, None, None

    guest = _start_machine(family, running, work, ssh_port, cpus, mem_mb, log,
                           discard=discard)
    if guest is None:
        _discard_overlay(run_overlay, log)
        hostshare.stop()
        return None, None, None

    import session

    log(f"waiting up to {boot_timeout}s for sshd")
    if not session.wait_for_ssh(ssh_port, boot_timeout, log, probe="echo ok"):
        log("=== THE DISK DID NOT ANSWER ON SSH ===")
        return guest, None, run_overlay

    return guest, True, run_overlay


def _discard_overlay(run_overlay, log) -> None:
    """Throw away an overlay when the guest never started, so a failure leaves no stray file."""
    if run_overlay is None:
        return
    try:
        run_overlay.close()
    except SystemExit as exc:                               # the base was written to
        log(f"  {exc}")


def _start_machine(family: str, disk: str, work: str, ssh_port: int, cpus: int,
                   mem_mb: int, log, discard: bool = True):
    """The machine each family boots on - reused from the stage that built the disk."""
    import vm

    if family == "macos":
        import machine
        import opencore
        import macos_recovery

        log("=== recovery image ===")
        macos_recovery.fetch_published("15", work)
        log("=== OpenCore, firmware, APFS driver ===")
        assets = opencore.build("15", work)
        shape = machine.shape(assets, cpus=cpus, mem_mb=mem_mb or 8192)
        shape.ssh_port = ssh_port
        guest = vm.Guest(
            work,
            extra=[
                "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
                "-device", "virtio-blk-pci,drive=oc,bootindex=0",
                # TARGET_DRIVE IS THE VALUE, NOT THE FLAG. bake.py pairs it with "-drive" the
                # same way; passing it alone makes qemu read a filename as an option.
                #
                # `discard=False` FOR A DISK THIS PROCESS DID NOT WRITE, and the difference is
                # not cosmetic. `discard=unmap` frees clusters as the guest TRIMs them, which is
                # right for a stage that is finished writing and wrong for anything that is still
                # reading a compressed image: a zstd qcow2 keeps compressed clusters, and handing
                # blocks back while the guest boots one is how the kernel ends up dead at
                #
                #     #[EB|LOG:HANDOFF TO XNU] _
                #
                # with no panic (run 42778795463 - the clean stage, on the artifact the share
                # stage had just compressed). machine.py already documents this trade for the
                # installer: TARGET_DRIVE_PLAIN exists because the writing stage must not discard.
                "-drive", (machine.TARGET_DRIVE if discard
                           else machine.TARGET_DRIVE_PLAIN).format(disk=disk),
                "-device", "virtio-blk-pci,drive=target,bootindex=1",
                "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
            ],
            shape=shape,
            stdout=os.path.join(work, "console.log"),
        ).start()
        log(f"started; the QMP socket is {guest.paths.drive}")
        return guest

    firmware_code = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd")
    template = os.environ.get("OVMF_VARS_TEMPLATE", "/usr/share/OVMF/OVMF_VARS_4M.fd")
    # SAY SO BEFORE QEMU DOES, AND SAY IT AS FATAL. A Windows guest on SeaBIOS does not boot, and
    # the symptom is a 900-second wait ending in "Connection timed out during banner exchange" -
    # qemu's user-mode network accepts the forwarded port even when nothing is listening, so a
    # firmware problem reads like an sshd problem. That confusion cost this repository a run
    # (42048455475's notes), which is why the check is here and not left to qemu's stderr.
    for path in (firmware_code, template):
        if not os.path.isfile(path):
            log(f"=== UEFI FIRMWARE NOT FOUND: {path} ===")
            log("  Windows does not boot on SeaBIOS; this is fatal, not a warning")
            return None
    firmware_vars = os.path.join(work, "OVMF_VARS.fd")
    # ALWAYS A FRESH COPY, never a leftover: the point is a machine with no boot entries.
    shutil.copyfile(template, firmware_vars)

    shape = vm.Shape(
        cpus=cpus, mem_mb=mem_mb or 6144, machine="q35",
        firmware_code=firmware_code, firmware_vars=firmware_vars,
        tpm_socket="", vga="std", ssh_port=ssh_port,
    )
    guest = vm.Guest(
        work,
        extra=["-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap"],
        shape=shape,
        stdout=os.path.join(work, "console.log"),
    ).start()
    log(f"started; the QMP socket is {guest.paths.drive}")
    return guest


# LONG ENOUGH FOR macOS TO ACTUALLY SHUT DOWN.
#
# The default is sixty seconds and macOS does not finish in sixty: it writes its logs, unmounts
# the share and closes APFS. What happens when the wait expires is `kill`, and the disk then needs
# to replay a journal on the next boot - which is why a clean from a killed disk sat at "waiting
# for sshd" for minutes while the stage that produced it had booted in thirty seconds. Measured on
# run 42759838906's predecessor:
#
#     [vm] asking the guest to shut down
#     [vm] the guest did not power off within 60s; killing it
#
# lib/clean_step.py already passed 300 for exactly this reason; it was the only caller that did.
STOP_TIMEOUT = 300

# WHAT EACH FAMILY HAS TO BE TOLD, OVER SSH, TO ACTUALLY SHUT DOWN.
#
# macOS DOES NOT SHUT DOWN ON AN ACPI POWER BUTTON. It receives `system_powerdown` and puts up the
# "Restart / Sleep / Cancel / Shut Down" dialog, then waits - so the request is not a shutdown, it
# is a request to ASK, and nothing answers a dialog on a headless guest. Measured: five minutes of
# waiting and then `killing it` (run 42762244256), which is what leaves a journal for the next
# boot to replay.
#
# Windows and Linux both honour ACPI, so they need no command.
SHUTDOWN_COMMAND = {
    # ONE LINE: the ssh channel takes a command, not a script, so the newline printf needs is
    # written as an escape inside its format rather than as a line break here.
    "macos": "printf '%s\\n' '{password}' | sudo -S -p '' shutdown -h now",
}


def stop(guest, *, acpi_first: bool = True, family: str = "", password: str = "admin",
         share: bool = True, overlay=None, log=print) -> None:
    """Stop the guest, close its overlay, and stop the share.

    A GUEST ON AN OVERLAY IS KILLED, NOT ASKED NICELY, AND THAT IS THE POINT OF THE OVERLAY.
    Everything a graceful shutdown protects - filesystem consistency, an unmounted volume, a
    journal that does not need replaying - applies to the disk the guest writes to. When that disk
    is a throwaway overlay whose parent is opened read-only, a kill destroys nothing: the parent
    is still the published artifact, byte for byte, and the overlay is deleted either way.

    So `overlay` being set skips the shutdown conversation entirely. It is also what makes these
    stages predictable: `shutdown -h now` and ACPI both have failure modes here (macOS ignores the
    power button; a guest under load can take longer than any timeout), and none of them can
    damage something that is read-only.

    WITHOUT an overlay the guest owns the real disk and gets the careful path: ssh `shutdown -h
    now` first, ACPI as the fallback, kill only if both fail.
    """
    try:
        if overlay is not None:
            log("the guest is on an overlay; killing it rather than waiting for a shutdown")
            guest.stop(acpi_first=False)
        else:
            command = SHUTDOWN_COMMAND.get(family)
            done = False
            if command:
                done = guest.shutdown_command(command.format(password=password), STOP_TIMEOUT,
                                              password=password)
            if not done:
                guest.stop(acpi_first=acpi_first, acpi_timeout=STOP_TIMEOUT)
    except Exception as exc:                                # noqa: BLE001
        log(f"stop: {exc}")
    finally:
        if overlay is not None:
            # close() DELETES THE OVERLAY AND CHECKS THE BASE. A base that changed is a hard
            # error, not a warning: it is a published artifact.
            overlay.close()
        if share:
            hostshare.stop()


@contextlib.contextmanager
def open(family: str, disk: str, work: str, *, ssh_port: int = 2222,
         boot_timeout: int = 900, copy: bool = True, share: str | None = None,
         cpus: int = 4, mem_mb: int = 0, password: str = "admin", log=print):
    """`start()` as a context manager. Stops the guest and the share on the way out."""
    guest, reached, run_overlay = start(
        family, disk, work, ssh_port=ssh_port, boot_timeout=boot_timeout,
        copy=copy, share=share, cpus=cpus, mem_mb=mem_mb, log=log)
    try:
        yield guest if reached else None
    finally:
        if guest is not None:
            stop(guest, family=family, password=password, overlay=run_overlay, log=log)
        else:
            hostshare.stop()
