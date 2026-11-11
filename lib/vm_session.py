"""Start a built disk the way the disk expects to be started, and stop it again.

    import vm_session
    with vm_session.open_guest(args.family, args.disk, work, ssh_port=2222) as guest:
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
          cpus: int = 4, mem_mb: int = 0, discard: bool = True,
          boot_options: dict | None = None, log=print):
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
        # THE BRIDGE FIRST, BECAUSE smbd BINDS TO ITS ADDRESS.
        #
        # `net.up()` is what creates 172.30.0.1 on the tap, and smbd's `interfaces` line names
        # that address. Starting smbd before the address exists makes it listen on nothing, and
        # the wait then reports
        #
        #     === smbd DID NOT BIND 172.30.0.1:445 IN 10s ===
        #
        # with "waiting for connections" in samba's own log - a share that started perfectly and
        # bound to no interface. Run 42813552966 is that failure, and it cost a macOS ssh stage
        # its whole run before the first step.
        #
        # `net.up()` is idempotent (vm.Guest.start calls it too), so this is not a second setup -
        # it is the same call moved to before the thing that depends on it.
        import net as netmod

        netmod.up(ssh_port=ssh_port)
        if not hostshare.start(served):
            log(f"=== THE HOST COULD NOT SERVE {served} ===")
            log("  the disk carries a job that mounts it at boot; without it that job fails and")
            log("  retries. Booting anyway would test a state the disk never ships in.")
            _discard_overlay(run_overlay, log)
            return None, None, None

    guest = _start_machine(family, running, work, ssh_port, cpus, mem_mb, log,
                           discard=discard, boot_options=boot_options)
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


def _android_firmware(work: str, opts: dict, log):
    """OVMF's CODE image and a FRESH variable store for an android-x86 guest.

    UEFI IS PART OF WHAT `os: android` MEANS. The installer branches on it - init reads
    /sys/firmware/efi/fw_platform_size, and that is what makes it write a GPT with an ESP instead
    of an MBR - and an installed disk is started BY the firmware reading the ESP's GRUB.

    IT USED TO BE TWO REQUIRED KEYS in `boot_options` (`firmware_code`, `firmware_vars`) indexed
    with [], which made `os: android` an incomplete statement and a KeyError for every caller
    that was not the one Python stage. Naming the OS is now enough, the same way it is for
    Windows and Linux.

    THE STORE IS ALWAYS A FRESH COPY, never a leftover: a previous run's NVRAM carries boot
    entries that name a disk which may no longer have a system on it.
    """
    import uefi

    code = opts.get("firmware_code") or ""
    template = opts.get("firmware_vars_template") or ""
    if not code or not template:
        code, template = uefi.firmware_paths(code, template)
    for path in (code, template):
        if not os.path.isfile(path):
            log(f"=== UEFI FIRMWARE NOT FOUND: {path} ===")
            log("  an android-x86 guest writes a GPT with an ESP only under UEFI, and an")
            log("  installed disk is started by the firmware reading that ESP")
            return "", ""
    vars_image = os.path.join(work, "OVMF_VARS.fd")
    shutil.copyfile(template, vars_image)
    log(f"firmware: {os.path.basename(code)} + a fresh NVRAM")
    return code, vars_image


def _android_x86(disk: str, work: str, ssh_port: int, cpus: int, mem_mb: int, log,
                 options: dict | None = None):
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

    TWO CALLERS, TWO SHAPES, AND `options` IS THE DIFFERENCE.

    Without options this is the LIVE boot: three extracted files and a scratch /data, which is
    what android/blissos16/steps/launch.yaml photographs. With `options["iso"]` and `options["disk"]`
    it is the INSTALLER: the same kernel and initrd, plus the ISO as a cdrom and a blank qcow2
    as the target, and the walkthrough in steps/install.yaml answers the TUI.

    THE ISO IS AN OPTION RATHER THAN A FOURTH MEMBER because it is not unpacked - the installer
    reads /src/install.img out of it at run time, and init refuses to install unless the
    mounted source holds isolinux/ and install.img.

    THREE SHAPES, AND WHICH ONE IS DECIDED BY WHAT THE CALLER PUT IN `options`
    ---------------------------------------------------------------------------

    `options["iso"]` + `options["disk"]`   the INSTALLER: kernel and initrd out of the extracted
                                           set, the ISO as a cdrom, and a blank qcow2 as target.
                                           steps/install.yaml answers the TUI.

    neither                                the LIVE boot: the three extracted files and a scratch
                                           /data. steps/desktop.yaml photographs the launcher.

    `options["installed"]`                  the INSTALLED DISK: this is the shape that makes the
                                           stage worth having. NO -kernel, NO -initrd, NO cdrom -
                                           the firmware reads the ESP's GRUB, GRUB loads the
                                           kernel off the data partition, and Android's own init
                                           finds its own /data. A disk that needs the host to
                                           supply its kernel is not an installed system, so this
                                           branch is the only one that proves the artefact.

    THE INSTALLED SHAPE NEEDS A DRIVER THIS GUEST ACTUALLY HAS, and that is `virtio-vga` rather
    than `-vga std`. The installed system runs `HWC=drm_minigbm`, which asks for a DRM/KMS node
    (/dev/dri/card0). `-vga std` provides a framebuffer and no DRM device, so Android brings up no
    display at all and the screen stops on initrd's last line for as long as you are willing to
    wait - measured at 240 s, 600 s and 600 s again before the cause was found. `virtio-vga` is
    the real KMS driver and it is what the ISO's own menu assumes ("QEMU/KVM - Virgl").
    """
    import os
    import subprocess

    import vm

    opts = dict(options or {})
    iso = opts.get("iso") or ""

    # WHICH OF THE THREE SHAPES THIS IS, DECIDED BY WHAT THE CALLER HANDED US.
    #
    #     an ISO and a qcow2 target     the INSTALLER   -kernel/-initrd, the ISO as a cdrom,
    #                                                   steps/install.yaml answers the TUI
    #     a DIRECTORY, no ISO           the LIVE boot   the three extracted members and a scratch
    #                                                   /data; steps/desktop.yaml photographs it
    #     a FILE, no ISO                the INSTALLED DISK  no -kernel, no cdrom - the firmware
    #                                                   reads the ESP's GRUB, which loads the
    #                                                   disk's own kernel. steps/launch.yaml.
    #
    # THE SHAPE IS DECIDED FROM THE ISO AND FROM WHAT `disk` IS, and the target is always `disk`.
    #
    # `disk` IS THE DISK THIS RUN PREPARED, not the path the run was started with. A `disk:` step
    # may have made it (a blank qcow2 with a partition table) or moved the input somewhere else -
    # and THE FIRST RUN OF THE YAML-ONLY INSTALL failed exactly here, because this module still
    # read `opts["disk"]`, which held `--boot-disk`, while the prepared file was one directory
    # down:
    #
    #     qemu-system-x86_64: -drive file=/a/yt2/blissos16.qcow2,if=virtio,format=qcow2:
    #     Could not open '/a/yt2/blissos16.qcow2': No such file or directory
    #
    # It is the same statement `MachineSession.disk` already makes for every other family - the
    # session points at the file this run writes, and `boot:` reads the session.
    installer = bool(iso)
    if not installer and (opts.get("installed") or os.path.isfile(disk)):
        return _android_installed(disk, work, ssh_port, cpus, mem_mb, log, opts)

    # THE MEMBERS COME OUT OF THE ISO'S OWN DIRECTORY. `android/blissos16/fetch_iso.py --out X`
    # leaves kernel, initrd.img and system.efs beside the ISO in X, so the ISO names the set and
    # a second argument pointing at the same place would be one fact written twice.
    members = os.path.dirname(os.path.abspath(iso)) if installer else disk
    target = disk

    # THE INSTALLER SHAPE NEEDS ONLY THE KERNEL AND THE INITRD: system.efs stays inside the
    # ISO, and there is no /data to create because the target disk is partitioned by the
    # walkthrough itself.
    needed = ("kernel", "initrd.img") if installer else ("kernel", "initrd.img", "system.efs")
    for name in needed:
        path = os.path.join(members, name)
        if not os.path.isfile(path):
            log(f"=== {path} IS MISSING ===")
            log("  android/blissos16/fetch_iso.py unpacks these from the ISO")
            return None

    if installer:
        log(f"installer: {os.path.basename(iso)} -> {os.path.basename(target)}")
        # UEFI IS PART OF WHAT `os: android` MEANS, NOT AN OPTION THE CALLER MUST REMEMBER.
        #
        # The installer BRANCHES ON IT: init reads /sys/firmware/efi/fw_platform_size, and that is
        # what makes it write a GPT with an ESP instead of an MBR. Installed under SeaBIOS the
        # disk it leaves behind is a different disk, and not the one this repository tests.
        #
        # It used to be two required keys in `boot_options` (`firmware_code`, `firmware_vars`)
        # indexed with [], which made `os: android` an incomplete statement and a KeyError for any
        # caller that did not happen to be a Python stage. Naming an OS is now enough: the
        # firmware is found and a FRESH variable store is made here, which is what
        # `boot_files`/`VM_FIRMWARE_*` already give every Windows and Linux guest.
        code, vars_image = _android_firmware(work, opts, log)
        if not code:
            return None
        shape = vm.Shape(
            cpus=cpus, mem_mb=mem_mb or 4096, machine="q35", vga="std",
            ssh_port=ssh_port,
            # THE PANEL SIZE THE WALKTHROUGH WAS MEASURED AT. Every `region` in
            # steps/install.yaml is in these pixels, and a frame of any other size puts the
            # OCR crop somewhere else on the dialog - measured: a 1920x1080 frame read as
            # 1280x800 found the title bar and nothing else, and the walk was declared stuck
            # on a screen that was showing the menu.
            resolution=opts.get("resolution") or (1280, 800),
            # UEFI, BECAUSE THE INSTALLER BRANCHES ON IT. init reads
            # /sys/firmware/efi/fw_platform_size and that is what makes it write a GPT with an
            # ESP instead of an MBR; installed under SeaBIOS the disk it leaves behind is a
            # different disk, and not the one this repository tests.
            firmware_code=code,
            firmware_vars=vars_image,
            # NO `console=ttyS0`. With it the TUI moves to the serial line and the keys the
            # walkthrough sends go to a console nobody is drawing on - measured, four runs.
            # `auto_install` IS THE CALLER'S CHOICE, AND IT IS A REAL ONE.
            #
            # Without it the installer asks which partition is the ESP and which holds the
            # system, pane by pane. WITH it, `install_hd` sets `answer=Create` and
            # `auto_partition` runs straight away - which is only correct when the disk
            # ALREADY has the table this repository wrote, because then `auto_partition`'s
            # `sgdisk --zap-all` is not what runs; `set_answer_if_auto Create` routes to the
            # same path and `partition_drive` returns without touching the disk.
            #
            # Either way `console=ttyS0` stays OUT: with it the TUI moves to the serial line.
            kernel_append=(
                "root=/dev/ram0 INSTALL=1 "
                # AUTO_UPDATE IS NOT A MISTAKE AND IT IS NOT AN UPDATE. It is the SECOND half
                # of a condition in the installer that decides whether the ESP is looked for:
                #
                #     [ -z "$AUTO_INSTALL" -o -n "$AUTO_UPDATE" ] && for i in `list_disks`; do
                #         esp=`sgdisk --print /dev/$disk | grep EF00 | ...`
                #
                # AUTO_INSTALL alone makes that test FALSE, the loop never runs, `esp` stays
                # empty, "Choose EFI Boot" never appears, and Grub2 is never written into the
                # ESP. Measured on a manual walkthrough of the AUTO_INSTALL path: the disk came
                # out with a FAT32 ESP labelled EFI containing no BOOTX64.EFI at all.
                #
                # Setting AUTO_UPDATE as well makes the test TRUE, so the search runs and finds
                # the EF00 partition this stage wrote. `install_hd`'s own update branch keys on
                # `AUTO_INSTALL` starting with U, and `force` does not - so this does not turn
                # the run into an update.
                + ("AUTO_INSTALL=force AUTO_UPDATE=1 " if opts.get("auto_install") else "")
                + "HWC=drm_minigbm GRALLOC=minigbm_arcvm"
            ),
        )
        return vm.Guest(
            work,
            extra=[
                "-kernel", os.path.join(members, "kernel"),
                "-initrd", os.path.join(members, "initrd.img"),
                "-drive", f"file={iso},media=cdrom,readonly=on",
                "-drive", f"file={target},if=virtio,format=qcow2",
            ],
            shape=shape,
            stdout=os.path.join(work, "console.log"),
        ).start()

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


def _android_installed(disk: str, work: str, ssh_port: int, cpus: int, mem_mb: int, log,
                       opts: dict):
    """Boot an INSTALLED Android-x86 disk: firmware, the ESP's GRUB, the disk's own kernel.

    THIS IS THE CLAIM THE WHOLE BLISSOS PATH EXISTS TO MAKE. The live and installer shapes are
    handed a kernel by the host through `-kernel`, so a disk they boot proves nothing about
    whether the disk is startable. Here there is no `-kernel` and no cdrom: qemu gets one drive and
    the firmware does the rest. If the ESP has no loader the guest lands in the UEFI shell, which
    is exactly the failure this shape detects.

    `bootindex=0` IS NOT DECORATION. With an empty NVRAM, OVMF's boot order is "UEFI Misc Device"
    then the NIC, and a bootable disk is NOT in it - measured:

        BdsDxe: failed to load Boot0001 "UEFI Misc Device" ... Not Found
        >>Start PXE over IPv4.   PXE-E16: No valid offer received.

    A published disk must not depend on an installer's NVRAM, so the disk is put first in the
    command line itself.

    THE GEOMETRY IS SET THROUGH `extra_args`, NOT `resolution:`, AND THAT IS NOT STYLE.
    lib/vm.py's build_args reads

        if shape.vga == "none":   pass
        elif shape.resolution:    args += ["-device", f"VGA,xres=..."]
        else:                     args += ["-vga", shape.vga]

    so ANY resolution mints a standard `VGA` device and `shape.vga="virtio"` is never reached.
    Measured: a launch that set vga="virtio" and a resolution still put `-device VGA,...` on the
    command line, Android had no DRM node, and the guest stopped on initrd's last line.
    """
    import vm

    code, vars_image = _android_firmware(work, opts, log)
    if not code:
        return None

    # NO TAP BY DEFAULT FOR THIS SHAPE. Nothing here is reached over ssh - `wait_for_ssh` is False
    # for this family - and lib/net.py raises SystemExit when dnsmasq will not start, which is a
    # dependency with nothing to do with a stage that only reads the screen over QMP. A caller
    # that wants an address says `net: "tap"` explicitly.
    net = str(opts.get("net") or "none")

    # THE PORTS THIS GUEST HAS TO BE REACHABLE ON, whichever network it is given.
    #
    # `adb_port` IS HOW A STAGE GETS IN BEFORE THERE IS AN sshd. A published Android disk has one
    # channel that exists on a fresh install - adbd - and android/blissos16/steps/ssh.yaml uses it
    # to put sshd there. It arrives from the file's `adb_port` var, defaulting to adb's own 5555.
    #
    # THE SSH PORT IS THE RUN'S, NOT 22: a Termux sshd listens on 8022 and the file names that as
    # `ssh_port:`, so forwarding 2222 -> 22 would reach nothing. Forwarding the port to itself is
    # the one rule that cannot disagree with the guest.
    adb_port = int(opts.get("adb_port") or 0)

    shape = vm.Shape(
        cpus=cpus, mem_mb=mem_mb or 8192, machine="q35",
        vga="none", net=net, ssh_port=ssh_port, adb_port=adb_port,
        firmware_code=code, firmware_vars=vars_image,
    )
    shape.extra_args = [
        "-device", "virtio-vga,xres=1280,yres=800",
    ]
    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=none,id=hd0,format=qcow2",
            "-device", "virtio-blk-pci,drive=hd0,bootindex=0",
        ],
        shape=shape,
        stdout=os.path.join(work, "console.log"),
    ).start()
    log(f"started the installed disk; the QMP socket is {guest.paths.drive}")
    return guest


def start_on(disk: str, work: str, *, family: str, assets: dict | None = None,
             ssh_port: int = 2222,
             cpus: int = 4, mem_mb: int = 0, share: str | None = None, log=print):
    """Start a guest on a disk that already exists - an overlay a caller made.

    `assets` IS ONLY FOR macOS. It is the OpenCore build, and a stage that already made one
    passes it here so the recovery image is not fetched twice. Every other family builds its
    machine from `family` alone, so the parameter is optional: making it required meant an
    overlay was a macOS-only capability, which it is not - `lib/overlay.py` is a qcow2 backing
    chain and has nothing to do with OpenCore.

    WHY THIS IS SEPARATE FROM start(). `start()` decides whether to make an overlay; a stage that
    has already made one (macos/15/ssh.py, because it chooses between an overlay and a working
    copy before the walkthrough runs) must not have a second one made on top. It also already
    has the OpenCore assets, and rebuilding them would fetch the recovery image again.

    THE SHARE IS STARTED HERE FOR THE SAME REASON `start()` STARTS IT: the disk carries a launchd
    job that mounts it the moment the system comes up, so a server that appears second has already
    been missed. `share=""` means do not serve one.

    Returns `(guest, reached)`, and NOT an overlay: the caller owns that one and closes it.
    """
    import session

    served = None
    if share != "":
        served = os.path.expanduser(share or hostshare.DEFAULT_DIR)
        os.makedirs(served, exist_ok=True)
        # THE BRIDGE FIRST, for the same reason as start(): smbd binds to net.BRIDGE_IP and
        # fails with "waiting for connections" against an address that does not exist yet.
        import net as netmod

        netmod.up(ssh_port=ssh_port)
        if not hostshare.start(served):
            log(f"=== THE HOST COULD NOT SERVE {served} ===")
            return None, None

    # THE ASSETS GO IN ONLY WHEN THERE ARE ANY. `boot_options={"assets": None}` would be a key
    # that says "a macOS build exists and is empty", which `_start_machine` reads as "the caller
    # meant to pass one" - the same shape as the bug that made a caller's boot_options
    # half-effective.
    options = {"assets": assets} if assets else {}
    guest = _start_machine(family, disk, work, ssh_port, cpus, mem_mb, log,
                           discard=True, boot_options=options)
    if guest is None:
        if served:
            hostshare.stop()
        return None, None
    log(f"started on the caller's overlay; qmp is {guest.paths.drive}")
    if not session.wait_for_ssh(ssh_port, 1, log, probe="echo ok"):
        # ONE SECOND, NOT A TIMEOUT: the caller runs `require_ssh:` itself when it wants a shell,
        # and this disk has no sshd until its own walkthrough turns Remote Login on.
        log("  (no sshd yet - the walkthrough turns it on)")
    return guest, True


def _start_machine(family: str, disk: str, work: str, ssh_port: int, cpus: int,
                   mem_mb: int, log, discard: bool = True, qga: bool = True,
                   boot_options: dict | None = None):
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
        return _android_x86(disk, work, ssh_port, cpus, mem_mb, log,
                            options=boot_options)

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

        # THE OPENCORE ASSETS MAY ARRIVE FROM THE CALLER. A stage that already built them (the
        # ssh stage does, because it needs the identity and the boot drive for the overlay it
        # decides about) should not make the walkthrough fetch and rebuild the same thing.
        assets = (boot_options or {}).get("assets")
        if not assets:
            import opencore
            import macos_recovery

            log("=== recovery image ===")
            macos_recovery.fetch_published("15", work)
            log("=== OpenCore, firmware, APFS driver ===")
            assets = opencore.build("15", work)

        # THE RECOVERY IMAGE AS A USB DEVICE, WHICH IS ONLY THE SETUP STAGE.
        #
        # macOS installs FROM removable media - the disk it installs TO is not the disk it boots
        # FROM - so the setup stage hands the guest the recovery image on a usb-storage device.
        # Every later stage boots a system that is already installed and has no recovery volume
        # at all, which is why this is a boot option and not part of what a macOS guest always
        # is: an extra USB disk is one more thing the guest can pick in Disk Utility, and
        # `choose the target disk by capacity` reads the sidebar.
        recovery = (boot_options or {}).get("recovery") or ""
        if recovery and not os.path.isfile(recovery):
            log(f"=== NO RECOVERY IMAGE AT {recovery} ===")
            return None

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
            ] + ([
                # THE INSTALLER, ON A USB DEVICE, WHEN THE CALLER ASKED FOR ONE. No bootindex:
                # usb-storage has none, and the firmware must never boot it - OpenCore hands
                # over to it only after the boot image has run.
                "-drive", f"file={recovery},media=disk,if=none,id=rec,readonly=on",
                "-device", "usb-storage,bus=xhci.0,drive=rec",
            ] if recovery else []),
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

    # A TPM, WHEN THE CALLER ASKED FOR ONE. Windows 11 refuses to install without it and the
    # failure is the installer's own dialog, which a stage cannot read. `boot_options["tpm"]`
    # is the statement; the socket is started here so a stage that says "this guest has a TPM"
    # does not also have to know what swtpm is.
    # `boot_options` IS OPTIONAL ON THIS FUNCTION AND EVERY CALLER THAT OMITS IT PASSES None -
    # `clean_step.py` is one, and it is the one that caught this: the clean booted the disk the
    # ssh stage had just published and crashed with
    #
    #     AttributeError: 'NoneType' object has no attribute 'get'
    #
    # on `boot_options.get("tpm")`. The parameter has a `| None = None` default, so the body has
    # to treat None as "no options" rather than assume the default was dict.
    boot_options = boot_options or {}
    # WHY A CALLER MAY PIN THE CPU MODEL, AND WHICH DISK NEEDS IT
    #
    # `-cpu host` EXPOSES WHATEVER THE RUNNER IS, so the SAME DISK BEHAVES DIFFERENTLY ON
    # DIFFERENT RUNNERS. cachyos compiles its packages for a microarchitecture level and the init
    # it installs executes those instructions - so a disk built on an Intel Xeon with AVX-512 and
    # booted on an AMD EPYC without it dies in the first second with
    #
    #     KERNEL PANIC!
    #     Attempted to kill init! exitcode=0x00000004
    #
    # 4 IS SIGILL. That comment was in this repository since 5063caf, which reproduced it on the
    # same disk with `-cpu qemu64` and proved the disk was fine by booting it with `-cpu host` -
    # and then the packet came back on a runner whose host was DIFFERENT, which is the case that
    # commit could not see from one machine.
    #
    # A FIXED MODEL IS THE ANSWER, NOT A NEWER ONE. Pinning `Skylake-Client` (or any x86-64-v3
    # model) makes the guest identical on every runner, so a disk either runs everywhere or fails
    # on the first run instead of on some other machine weeks later.
    tpm_socket = ""
    if boot_options.get("tpm"):
        import swtpm as swtpmmod
        tpm_socket = swtpmmod.start(work) or ""
        if not tpm_socket:
            log("=== A TPM WAS ASKED FOR AND swtpm DID NOT START ===")
            log("  Windows 11 will stop at the hardware check with a dialog nobody can click")
    # `cpu` IS A PROPERTY OF THE DISK'S PACKAGES, NOT OF THIS RUNNER - see the note below.
    cpu_model = str(boot_options.get("cpu") or "")
    if cpu_model:
        log(f"  cpu: {cpu_model} (the disk's packages were built for a fixed microarchitecture)")
    shape = vm.Shape(
        cpus=cpus, mem_mb=mem_mb or 6144, machine="q35",
        cpu=cpu_model,
        firmware_code=firmware_code, firmware_vars=firmware_vars,
        tpm_socket=tpm_socket, vga="std", ssh_port=ssh_port,
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

    # LINUX AND BSD ARE THE SAME COMMAND, AND THEY WERE MISSING - which made `os: linux` an
    # incomplete statement even though its `boot:` worked. `shutdown: ssh` on any of the twelve
    # Linux distributions, or on GhostBSD, failed with
    #
    #     no ssh shutdown command is known for 'linux'
    #
    # - a sentence about this table, not about the guest. Every one of them has `sudo` and every
    # one of them has `shutdown -h now`: systemd, SysVinit, and FreeBSD's own /sbin/shutdown all
    # accept it.
    #
    # GHOSTBSD NEEDS `-p`, WHICH IS NOT A COSMETIC DIFFERENCE. FreeBSD's `shutdown -h now` halts
    # the kernel and stops there, leaving the machine at "The operating system has halted" - qemu
    # never exits, and a stage waiting for the process waits forever. `-p` asks the firmware to
    # power off after the halt, which is what a VM needs and what ACPI would have done.
    "linux": "printf '%s\\n' '{password}' | sudo -S -p '' shutdown -h now",
    "bsd": "printf '%s\\n' '{password}' | sudo -S -p '' shutdown -p now",
    # `freebsd` IS THE NAME THE CLEAN STEP USES and `ghostbsd` is what the disk is called. Both
    # resolve to the same command, because it is a fact about the kernel rather than about the
    # distribution.
    "freebsd": "printf '%s\\n' '{password}' | sudo -S -p '' shutdown -p now",
    "ghostbsd": "printf '%s\\n' '{password}' | sudo -S -p '' shutdown -p now",
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
        # AND THEN ACTUALLY STOP IT. THIS `except` USED TO LOG AND FALL THROUGH TO `finally`,
        # which closes the overlay and the share and NEVER TOUCHES QEMU - so a shutdown that
        # timed out left the process running, holding a write lock on the disk the NEXT STEP was
        # about to read. Run 42897536838 is that, exactly:
        #
        #     20:46:17  [vm]  Connection to 172.30.0.1 closed by remote host.
        #     20:51:17  [vm]  the guest did not shut down within 300s
        #     20:51:17  [macos-vmconf] stop: `shutdown -h now` did not stop the guest in time
        #     20:51:17  [disk] qemu-io: can't open device out/dev.qcow2:
        #                      Failed to get "write" lock
        #     20:51:17  === THE DISK WILL NOT BOOT ===
        #
        # The disk was fine. The lock was qemu's, and the 20 walkthrough steps before this had
        # all passed. `guest.stop()` is idempotent - it collects a process that exited on its own
        # and SIGTERMs/SIGKILLs one that did not - so the failure path gets the same call as the
        # success path rather than a reason to skip it.
        if overlay is None:
            try:
                guest.stop(acpi_first=False)
            except Exception as kill_exc:                   # noqa: BLE001
                log(f"stop: could not kill the guest either ({kill_exc})")
    finally:
        if overlay is not None:
            # close() DELETES THE OVERLAY AND CHECKS THE BASE. A base that changed is a hard
            # error, not a warning: it is a published artifact.
            overlay.close()
        if share:
            hostshare.stop()


# THIS FUNCTION USED TO BE CALLED `open`, AND THAT SHADOWED THE BUILTIN FOR THE WHOLE MODULE.
#
# A module-level `def open(...)` binds the name everywhere in the file, not only after the def -
# so `_android_x86`'s `with open(data, "wb") as fh:` (the /data image it creates for a live
# Android-x86 boot) resolved to THIS context manager:
#
#     TypeError: open() missing 1 required positional argument: 'work'
#
# measured on `family: android` with the three extracted members present. Nothing in the tree
# called this function - it is documented in the module docstring and nowhere else - so the
# shadowing was invisible until the live-boot path was actually run. Renamed rather than
# qualified, because a name that hides a builtin in a file that uses the builtin is a trap.
@contextlib.contextmanager
def open_guest(family: str, disk: str, work: str, *, ssh_port: int = 2222,
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
