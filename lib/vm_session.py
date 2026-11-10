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
          boot_timeout: int = 900, wait_for_ssh: bool = True, copy: bool = True,
          share: str | None = None,
          cpus: int = 4, mem_mb: int = 0, discard: bool = True, log=print):
    """Serve the share, boot `disk`, and - IF ASKED - wait for sshd.

    `wait_for_ssh=False` IS FOR A WALKTHROUGH THAT DRIVES THE SCREEN. A stage that installs an OS
    has no sshd until the OS is installed; a stage that photographs a desktop may have one but
    does not need it; and one that is about to type into a GUI has to start looking before any
    login shell exists. Requiring sshd before the first step is a stage that cannot start.

    The caller that wants ssh says so, and `require_ssh:` is the step that does it - so the wait
    is visible in the walkthrough where it belongs, with its own timeout, instead of hidden inside
    the boot.

    Returns `(guest, reached, overlay)`: `reached` is True when sshd answered (or when nobody
    asked), None when the machine came up and never did, and `(None, None, None)` when nothing
    could be started at all. A caller that only needs to know whether to continue can test the
    second value; the guest is returned either way so the failure can be photographed.

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
    #
    # `share=""` MEANS "DO NOT SERVE ONE", AND THE OLD TEST GOT THAT BACKWARDS. It read
    # `if share is not None:` and then `share or DEFAULT_DIR`, so the empty string - the value
    # this module's own docstring names as "do not serve" - fell through to the DEFAULT
    # directory and started an smbd. Measured: an Android-x86 guest with no cifs client still
    # paid 10 seconds for smbd and then failed the boot on
    #
    #     === smbd DID NOT BIND 172.30.0.1:445 IN 10s ===
    #
    # which is the right message for a macOS disk that needs the share and the wrong failure
    # for a guest that never asked for one. None still means "serve ~/shared", which is what
    # every existing caller passes when it omits the argument.
    served = None
    if share != "":
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

    # ONE PLACE DECIDES WHETHER TO WAIT, AND IT IS THE CALLER.
    #
    # `wait_for_ssh=False` is not a shortcut - it is a different contract: the machine is up
    # when qemu is running, and whether an sshd answers inside it is a property of the DISK.
    # A stage that installs an OS looks at a disk that has none yet; a stage that photographs
    # an Android desktop is looking at a guest whose remote access is adb, not ssh.
    #
    # `require_ssh:` is the step that asks, so the wait lives in the walkthrough where it can
    # be read, with its own timeout, instead of being hidden inside boot.
    if not wait_for_ssh:
        log("not waiting for sshd; the walkthrough asks when it needs it")
        return guest, True, run_overlay

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


def _android_x86(disk: str, work: str, ssh_port: int, cpus: int, mem_mb: int, log):
    """Start an Android-x86 guest: -kernel, -initrd, and two disks.

    WHY THIS IS NOT A `-drive` BOOT LIKE THE OTHERS

    Every other family here boots a disk that carries its own bootloader, so qemu gets a
    drive and the firmware does the rest. Android-x86 does not work that way: its initrd
    finds the system image, mounts it, and mounts a writable ext4 for /data. Booting its
    disk through SeaBIOS gets nowhere, because the published image is not a partitioned
    disk with a bootloader - it is a set of files.

    `disk` IS A DIRECTORY HERE, NOT A FILE. The same three names mkdisks.py had to
    synthesise for the goldfish path, except these come out of the ISO:

        kernel       a real bzImage (HdrS at 0x1F1, boot flag 55aa, protocol 2.15) -
                     unlike kernel-ranchu, which is why stock qemu can load this one
        initrd.img   Android-x86's init
        system.efs   EROFS, attached read-only as /dev/vda

    AND /data IS CREATED BESIDE THEM, because it is not in the ISO: a live boot keeps
    /data on tmpfs and loses everything at power-off.

    SRC= MUST BE EMPTY. Measured: pinning it starts a 137-dot detection scan that does not
    finish inside a boot budget, while empty auto-detect finds /dev/vda in about a second.
    DATA= MUST BE PINNED, or the initrd treats the run as a live CD.

    THERE IS NO sshd AND THIS DOES NOT WAIT FOR ONE. Android's remote access is adb on
    5555; the desktop is the verdict. `wait_for_ssh` is therefore False for this family in
    vm_session.start() - see the caller.
    """
    import os
    import subprocess

    import vm

    for name in ("kernel", "initrd.img", "system.efs"):
        path = os.path.join(disk, name)
        if not os.path.isfile(path):
            log(f"=== {path} IS MISSING ===")
            log("  android/blissos16/fetch_iso.py unpacks these three from the ISO")
            return None

    data = os.path.join(work, "data.img")
    if not os.path.exists(data) or os.path.getsize(data) == 0:
        log(f"creating {data} (16 GiB ext4 for /data)")
        with open(data, "wb") as fh:
            fh.truncate(16 << 30)
        made = subprocess.run(["mkfs.ext4", "-q", "-F", data], capture_output=True, text=True)
        if made.returncode != 0:
            log(f"=== mkfs.ext4 FAILED: {made.stderr[-400:]} ===")
            return None

    shape = vm.Shape(
        cpus=cpus, mem_mb=mem_mb or 4096, machine="q35",
        # A FRAMEBUFFER, BECAUSE THE HWC DRIVER EXPECTS ONE. HWC=drm_minigbm is told to use
        # DRM/KMS, and a guest with no graphics device draws nothing - which reads exactly
        # like a boot that failed.
        vga="std", ssh_port=ssh_port,
        # THE COMMAND LINE GOES THROUGH Shape, NOT THE ARGUMENT VECTOR, so the one place
        # that knows how to spell a kernel command line stays the one place.
        kernel_append=(
            "root=/dev/ram0 console=ttyS0 quiet "
            "SRC= DATA=/dev/vdb "
            "HWC=drm_minigbm GRALLOC=minigbm_arcvm"
        ),
    )
    guest = vm.Guest(
        work,
        extra=[
            "-kernel", os.path.join(disk, "kernel"),
            "-initrd", os.path.join(disk, "initrd.img"),
            "-drive", f"file={os.path.join(disk, 'system.efs')},if=virtio,format=raw,readonly=on",
            "-drive", f"file={data},if=virtio,format=raw",
        ],
        shape=shape,
        stdout=os.path.join(work, "console.log"),
    ).start()
    log(f"started android-x86; the QMP socket is {guest.paths.drive}")
    return guest


def _start_machine(family: str, disk: str, work: str, ssh_port: int, cpus: int,
                   mem_mb: int, log, discard: bool = True, qga: bool = True):
    """The machine each family boots on - reused from the stage that built the disk.

    `qga` GIVES THE GUEST A CHANNEL ITS AGENT CAN ANSWER ON, and that is what makes a graceful
    shutdown possible at all on macOS. It costs one device and one socket and it is harmless on a
    disk with no agent installed: the port exists, nothing listens, and the caller falls back.
    """
    import vm

    # ANDROID-X86 IS NOT A DISK WITH A BOOTLOADER. It is handed a kernel and an initrd, and
    # the initrd assembles the root itself. See _android_x86() for why that is the only way
    # this guest can start at all.
    if family == "android":
        return _android_x86(disk, work, ssh_port, cpus, mem_mb, log)

    # THE AGENT'S SOCKET, MADE BEFORE THE MACHINE SO QEMU CAN BIND IT.
    #
    # MACOS GETS ISA SERIAL AND EVERYONE ELSE VIRTIO-SERIAL. That is a property of the guest-side
    # agent, not of qemu: Apple's own agent never loads under QEMU, and the third-party one that
    # does moved to ISA in v2.5.0. See lib/vm.py's Shape.qga_transport.
    qga_socket = os.path.join(work, "qga.sock") if qga else ""
    if qga_socket and os.path.exists(qga_socket):
        os.unlink(qga_socket)
    qga_transport = "isa" if family == "macos" else "virtio"

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
        shape.qga_socket = qga_socket
        shape.qga_transport = qga_transport
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
        qga_socket=qga_socket, qga_transport=qga_transport,
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
# THE THREE WAYS TO ASK A GUEST TO CLOSE ITS DISKS, IN THE ORDER THEY ARE TRIED.
#
#   1. guest agent     a request to a process inside the guest that calls the system's own
#                      shutdown. Needs no ssh and no network, and works before sshd exists.
#   2. ssh command     `shutdown -h now` over the connection the stage already has.
#   3. ACPI            the virtual power button. Windows honours it; macOS answers with a dialog
#                      and waits (run 42762244256: five minutes, then `killing it`).
#
# WHY THE AGENT IS FIRST. It is the same shutdown path as (2) - the guest's own - but it survives
# the two things that break (2): a guest whose sshd never came up, and a guest whose network is
# gone. It is also the only one of the three that works during a stage that has not set up ssh yet.
SHUTDOWN_COMMAND = {
    # ONE LINE: the ssh channel takes a command, not a script, so the newline printf needs is
    # written as an escape inside its format rather than as a line break here.
    "macos": "printf '%s\\n' '{password}' | sudo -S -p '' shutdown -h now",
}


def _shutdown_via_agent(socket_path: str, guest, log=print, timeout: int = 120) -> bool:
    """Ask the agent to power the guest down and wait for qemu to exit. True if it did.

    THE REPLY IS EXPECTED TO DIE. The guest powers off while its agent is still writing the
    answer, so the connection dropping here is the success path, not a failure - see lib/qga.py.
    """
    import time
    try:
        import qga
    except ImportError as exc:
        log(f"  no qga module ({exc}); falling back")
        return False
    try:
        agent = qga.Agent(socket_path, connect_timeout=5.0)
    except qga.AgentError as exc:
        log(f"  the agent did not answer ({exc}); falling back")
        return False
    try:
        if not agent.wait(timeout=30):
            log("  the agent socket exists but nothing answers on it; falling back")
            return False
        log("  asking the guest agent to power down")
        agent.shutdown("powerdown")
    finally:
        agent.close()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if guest.proc.poll() is not None:
            guest.proc.wait()
            log("  the guest powered off")
            return True
        time.sleep(1)
    log(f"  the guest did not power off within {timeout}s after the agent asked")
    return False


def stop(guest, *, acpi_first: bool = True, family: str = "", password: str = "admin",
         share: bool = True, overlay=None, method: str = "graceful", log=print) -> None:
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
        elif method == "kill":
            log("killing the guest")
            guest.stop(acpi_first=False)
        elif method == "guest_agent":
            # ONE METHOD, AND IT IS THE ONLY ONE TRIED. A caller that asks for the agent gets the
            # agent or an error - not a quiet slide to ssh, which is what made the old chain
            # unreadable in a log.
            socket_path = getattr(getattr(guest, "shape", None), "qga_socket", "")
            if not _shutdown_via_agent(socket_path, guest, log):
                raise RuntimeError(
                    f"the guest agent did not power the machine down (socket {socket_path!r})")
        elif method == "ssh":
            command = SHUTDOWN_COMMAND.get(family)
            if not command:
                raise RuntimeError(f"no ssh shutdown command is known for {family!r}")
            if not guest.shutdown_command(command.format(password=password), STOP_TIMEOUT,
                                          password=password):
                raise RuntimeError("`shutdown -h now` did not stop the guest in time")
        elif method == "acpi":
            guest.stop(acpi_first=True, acpi_timeout=STOP_TIMEOUT)
        else:
            # THE OLD BEHAVIOUR, KEPT FOR A CALLER THAT HAS NOT BEEN UPDATED: try the careful
            # paths in order. It exists because lib/clean_step.py and the stage scripts call this
            # without naming a method, and turning their shutdown into a hard failure is a change
            # they have not asked for. New walkthroughs name one.
            done = False
            socket_path = getattr(getattr(guest, "shape", None), "qga_socket", "")
            if socket_path:
                done = _shutdown_via_agent(socket_path, guest, log)
            if not done:
                command = SHUTDOWN_COMMAND.get(family)
                if command:
                    done = guest.shutdown_command(command.format(password=password),
                                                  STOP_TIMEOUT, password=password)
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
