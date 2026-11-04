"""Bake FreeBSD by giving bsdinstall a script instead of a person.

    python3 bsd/freebsd/bake.py --out out/freebsd.qcow2

HOW THE SCRIPT REACHES THE INSTALLER, AND WHY IT IS ON THE MEDIUM

bsdinstall's `script` target reads /etc/installerconfig from the environment it is
running in - the memory filesystem the installation media boots, not the installed disk
and not a second volume. There is no network fetch and no kernel argument that names a
URL. So the file has to be inside the media, and putting it there means repacking the ISO.

A SECOND ISO DOES NOT WORK, and it is worth saying why because it looks like it should:
nothing opens a second optical drive before bsdinstall runs, and the only thing that
could mount it is a script - which is the file being looked for. The media the machine
boots is the media the installer reads.

WHAT THE FIRST BOOT DOES

The media boots to its own loader, the kernel starts bsdinstall's script target, the
preamble partitions the disk and unpacks base and kernel, and then the post-install half
runs chrooted in the new system: sshd on, the docker/admin account, the toolchain. The
machine then reboots into what it just installed.

NO SCREEN IS READ. bsdinstall in script mode has no prompts - that is what the mode is
for - so the completion test is the guest exiting and the disk holding a UFS filesystem.
lib/bsdinstall.py checks the second half; the first is what -no-reboot turns into a fact.

THE KERNEL IS BOOTED FROM THE MEDIUM, not with -kernel. FreeBSD's installer kernel is on
the El Torito image and expects the mfsroot beside it; handing it over directly is a
different machine than the one the media is built for.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from lib import bsdinstall, compress, install_evidence, iso as isolib, serial, vm  # noqa: E402

FREESD_TAG = "iso-freebsd"
CONFIG_PORT = 8000


def log(message: str) -> None:
    print(f"[freebsd {time.strftime('%H:%M:%S')}] {message}", flush=True)


def build_installerconfig() -> str:
    """The preamble plus the post-install script, as one file.

    THE MARKER IS CHECKED, NOT HOPED FOR. bsd/freebsd/installerconfig carries
    @POST_INSTALL@ where the setup script goes; a file that still contains it installs a
    system with no ssh, and the next stage fails with "connection refused" against a disk
    that is otherwise fine. Better to refuse here.
    """
    template = os.path.join(HERE, "installerconfig")
    post = os.path.join(HERE, "installerconfig.sh")
    with open(template, encoding="utf-8") as fh:
        body = fh.read()
    with open(post, encoding="utf-8") as fh:
        script = fh.read()

    if "@POST_INSTALL@" not in body:
        raise SystemExit(
            f"{template} has no @POST_INSTALL@ marker, so the setup script has nowhere "
            f"to go. The preamble alone installs a FreeBSD with no sshd and no account."
        )
    return body.replace("@POST_INSTALL@", script)


def volume_label(iso_path: str) -> str:
    """The ISO's own volume label, read from its Primary Volume Descriptor.

    THE MEDIA FINDS ITSELF BY THIS STRING. FreeBSD's loader mounts its root as
    cd9660:/dev/iso9660/<label>, so a repack that renames it produces a machine that
    boots to "waiting for device ... failed with error 19" and a manual-root prompt -
    which is exactly what run 41606524055 showed.

    Sector 16 holds the PVD; the label is 32 bytes at offset 40 in it. Read rather than
    hardcoded, because the next release is a different string.
    """
    with open(iso_path, "rb") as fh:
        fh.seek(16 * 2048)
        pvd = fh.read(2048)
    if len(pvd) < 2048 or pvd[0:1] != b"\x01":
        raise SystemExit(
            f"{iso_path} has no ISO9660 Primary Volume Descriptor at sector 16, so its "
            f"volume label cannot be read - and the repack must keep that label.")
    label = pvd[40:72].decode("ascii", "replace").strip()
    if not label:
        raise SystemExit(f"{iso_path}'s volume label is empty")
    return label


def console_says_umount(ser) -> bool:
    """Has bsdinstall reached its last step? Read from the console sink it writes to.

    `umount` is the final entry in /usr/libexec/bsdinstall's script sequence. After it the
    only things left are a bsddialog --pause and a reboot, and neither touches the disk -
    so this is the last moment at which waiting tells you anything.

    Read from the sink rather than from `guest.alive()` because the machine does not
    always stop: see the note where this is called.
    """
    try:
        with open(ser.sink, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    return "Running installation step: umount" in text


def build_media(iso_path: str, work: str) -> str:
    """The installation ISO with /etc/installerconfig added to it.

    THE ISO HAS A REAL /etc, AND THAT IS THE WHOLE REPACK.

    The first version of this looked for an mfsroot inside the ISO and failed on run
    41600033829 with "no mfsroot inside ... the ISO's layout has changed". It had not
    changed - FreeBSD 15.1's disc1 simply does not use one. Its root filesystem IS the
    ISO: etc/, boot/, usr/freebsd-packages/ are all there as ordinary directories, and
    bsdinstall reads /etc/installerconfig from that tree when the media boots.

    That makes this two steps instead of four: unpack, write one file, repack. No
    makefs, no mfsroot, no second image to keep in step.

    THE PACKAGES DIRECTOR Y IS WHY THE ISO IS BIG, and it is also why nothing needs a
    network: usr/freebsd-packages/offline carries base, kernel and the rest, so bsdinstall
    installs from the medium.
    """
    import shutil

    tree = os.path.join(work, "iso")
    if os.path.exists(tree):
        shutil.rmtree(tree)
    os.makedirs(tree)

    log("unpacking the ISO")
    result = subprocess.run(["7z", "x", "-y", f"-o{tree}", iso_path],
                            capture_output=True, text=True)
    # A NON-ZERO EXIT IS NOT ALWAYS A FAILED UNPACK, and this one never is.
    #
    # FreeBSD's ISO contains symlinks that point outside their directory - etc/os-release
    # -> ../var/run/os-release, sbin/nologin -> ../usr/sbin/nologin - and 7z refuses to
    # create them:
    #
    #     ERROR: Dangerous link path was ignored : etc/os-release : ../var/run/os-release
    #
    # It then exits 1. Run 41599346958 stopped there even though the tree was complete:
    # the symlinks it skipped are inside the live system, which this never mounts.
    if "Dangerous link path was ignored" in (result.stderr or result.stdout or ""):
        log("  7z skipped symlinks pointing outside the tree; expected here")
    elif result.returncode != 0:
        raise SystemExit(
            f"could not unpack {iso_path}: {(result.stderr or result.stdout)[:400]}")

    etc = os.path.join(tree, "etc")
    if not os.path.isdir(etc):
        raise SystemExit(
            f"no etc/ in {iso_path}. bsdinstall reads /etc/installerconfig from the "
            f"medium's own root, so without it there is nowhere to put the answers.")

    target = os.path.join(etc, "installerconfig")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build_installerconfig())
    log(f"wrote etc/installerconfig ({os.path.getsize(target)} bytes)")

    # AND THE BOOT MENU IS TOLD TO RUN IT. bsdinstall with no arguments starts its menu
    # wizard; the script target is what reads the file. The media's loader can hand it a
    # kernel environment, and `bsdinstall.script` is the variable its rc reads - but the
    # simplest reliable place is the menu's own default entry, which no longer needs to
    # offer a choice.
    loader_conf = os.path.join(tree, "boot", "loader.conf")
    if os.path.exists(loader_conf):
        with open(loader_conf, "a", encoding="utf-8") as fh:
            fh.write('\n# easydiskbake: no menu, and everything on the serial port\n')
            fh.write('autoboot_delay="0"\n')
            # THE CONSOLE, AND THE REASON RUN 41602957404 SHOWED NOTHING.
            #
            # FreeBSD sends boot messages to the VGA adapter by default. qemu's serial
            # gets nothing, so the bake's log had only its own heartbeat lines -
            # "0 MiB, 120s, 240s, ... 720s" - and a bsdinstall that was working and one
            # that had died were indistinguishable for twelve minutes.
            #
            # These four lines are the documented way to move both the loader's output
            # and the kernel's console onto ttyu0. They have to be in the MEDIA's
            # loader.conf because that is read before anything on the disk exists.
            fh.write('console="comconsole"\n')
            fh.write('boot_serial="YES"\n')
            fh.write('comconsole_speed="115200"\n')
            fh.write('comconsole_port="0x3f8"\n')
        log("boot/loader.conf: autoboot, console on ttyu0")

    # rc.local IS THE HOOK, AND ITS LAST LINE HAS TO SAY `primary`.
    #
    # The media's rc.local ends:
    #
    #     if bsdinstall_multicons_disabled; then
    #         /usr/libexec/bsdinstall/startbsdinstall primary
    #     else
    #         /usr/libexec/bsdinstall/runconsoles /usr/libexec/bsdinstall/startbsdinstall %
    #     fi
    #
    # and runconsoles replaces % with the console's name - ttyu0 - before exec. So
    # startbsdinstall's $1 is "ttyu0", and its test is
    #
    #     if [ "$1" != "primary" ]; then ... msgbox "use the primary console"
    #
    # which is why /etc/installerconfig, sitting on the medium the whole time, was never
    # opened. Three runs stopped at "Please choose the appropriate terminal type".
    #
    # bsdinstall.multicons_disable WAS TRIED FIRST AND DOES NOT WORK: the function reads
    # it with `kenv -q`, and a loader.conf variable is not in the boot environment that
    # kenv reports. A local boot of the repacked media still asked for a terminal type.
    #
    # So the media's own rc.local is rewritten. rc.d/local runs it - the boot log says
    # "Starting local daemons:" - and the replacement is the branch that installs, with
    # no wrapper between the console and the installer.
    rc_local = os.path.join(tree, "etc", "rc.local")
    with open(rc_local, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#!/bin/sh\n")
        fh.write("# easydiskbake: install from /etc/installerconfig, no menu\n")
        fh.write("# rc.d/local runs this file; the media's version wraps the call in\n")
        fh.write("# runconsoles, which passes the console's name instead of 'primary'\n")
        fh.write("# and startbsdinstall then reads no answer file at all.\n")
        fh.write("TERM=vt100; export TERM\n")
        fh.write("/usr/libexec/bsdinstall/startbsdinstall primary\n")
    os.chmod(rc_local, 0o755)
    log("etc/rc.local: startbsdinstall primary, no runconsoles")

    # The repack keeps the El Torito record that makes it bootable: boot/cdboot for BIOS
    # and boot/boot1.efi for UEFI, both of which this image carries.
    #
    # THE VOLUME LABEL IS THE ORIGINAL ONE, AND RUN 41606524055 IS WHY IT HAS TO BE.
    #
    # FreeBSD's loader is built to mount its root by LABEL:
    #
    #     vfs.root.mountfrom=cd9660:/dev/iso9660/15_1_RELEASE_AMD64_CD
    #
    # so after the repack the machine said
    #
    #     mountroot: waiting for device /dev/iso9660/15_1_RELEASE_AMD64_CD...
    #     Mounting from cd9660:/dev/iso9660/15_1_RELEASE_AMD64_CD failed with error 19
    #
    # and stopped at the manual-root prompt. Error 19 is ENODEV: the device the loader
    # names does not exist, because the repack had renamed it FREESD_INSTALL. The label
    # is not a description, it is how the media finds itself.
    #
    # It is read from the source image rather than written here, so a new release with a
    # new label keeps working.
    label = volume_label(iso_path)
    log(f"volume label: {label}")
    media = os.path.join(work, "freebsd-install.iso")
    repack = subprocess.run([
        "xorriso", "-as", "mkisofs", "-o", media,
        "-b", "boot/cdboot", "-no-emul-boot", "-c", "boot/boot.catalog",
        "-eltorito-alt-boot", "-e", "boot/boot1.efi", "-no-emul-boot",
        "-J", "-R", "-V", label, tree,
    ], capture_output=True, text=True)
    if repack.returncode != 0:
        raise SystemExit(
            f"xorriso could not repack the ISO: {(repack.stderr or repack.stdout)[:400]}")
    log(f"media: {media} ({os.path.getsize(media) >> 20} MiB)")
    return media


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/freebsd.qcow2")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--disk-size", default="24G")
    p.add_argument("--timeout", type=int, default=3600)
    args = p.parse_args(argv)

    work = args.work or os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"out={args.out}")
    iso_path = os.path.join(work, "freebsd.iso")
    if not os.path.isfile(iso_path):
        log(f"pulling {FREESD_TAG} from ghcr")
        try:
            isolib.fetch_tag(FREESD_TAG, iso_path)
        except isolib.IsoError as exc:
            raise SystemExit(
                f"{FREESD_TAG} is not published: {exc}\n\n"
                f"Run iso-publish.yml for freebsd first.") from exc
    log(f"iso {os.path.getsize(iso_path) >> 20} MiB")

    media = build_media(iso_path, work)

    disk = os.path.join(work, "disk.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    # THE SERIAL IS A CHARDDEV ON A FIFO, NOT mon:stdio, SO IT CAN BE READ AND PRINTED.
    #
    # With mon:stdio the console lands in console.log through qemu's own stdout, which
    # means nothing prints it while the run happens - and a poll on the disk says "0 MiB"
    # whether bsdinstall is working or dead. lib/serial.py's drain thread prints every
    # line as it arrives, which is what makes this log readable at all.
    ser = serial.Serial(work, "serial")
    shape = vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                     serial_pipe=ser.stdin.removesuffix(".in"))
    # -boot d: TRY THE CD FIRST, EXPLICITLY. The disk is empty, so a firmware that
    # starts with the hard disk would find nothing - and this machine has no boot entry
    # to fall through to. FreeBSD happened to reach its loader anyway, because its
    # cdbr is position-dependent and whose media has one boot entry rather than two, did
    # not. Naming the order removes the difference between them.
    # -no-reboot, WHICH THE DOCSTRING ABOVE HAS NAMED SINCE THE FILE WAS WRITTEN AND
    # WHICH WAS NEVER IN THE ARGUMENT LIST.
    #
    # bsdinstall's script target ends with `reboot` once the post-install script returns,
    # and without this flag the guest restarts into the medium it is still attached to -
    # the same CD, the same installer - and starts over. `guest.alive()` then never becomes
    # false, so the wait loop runs out its whole --timeout while the disk sits unchanged.
    #
    # Run 41663564002 is that: `Running installation step: umount` was the last thing the
    # console said, and the disk held at 2057 MiB for the remaining twenty minutes. With
    # the new timeout gate that is now a clean failure instead of a published partial
    # install, but the install itself was already complete - it just never stopped.
    drives = [
        "-boot", "d",
        "-drive", f"file={media},media=cdrom,readonly=on",
        "-drive", f"file={disk},if=virtio,format=qcow2",
        "-no-reboot",
    ]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)
    try:
        guest.start()
        ser.open(timeout=60)
        # NO MP4 FOR THIS INSTALLER. bsdinstall is a text console on ttyS0, so the
        # framebuffer holds nothing but a blank screen and a cursor. The recording that was
        # uploaded proved it: 731 frames, 64 KiB, mean grey 0.08 - an entire hour of black
        # with one white pixel in the corner.
        #
        # The serial console already carries the whole install line by line, including the
        # 214 packages' progress, and lib/serial.py prints it as it arrives. A video of an
        # empty framebuffer adds no information.

        # ANSWER THE TERMINAL-TYPE PROMPT, WHICH IS NOT ABOUT THE ANSWER FILE.
        #
        # startbsdinstall's first act is
        #
        #     kbdcontrol -d >/dev/null 2>&1
        #     if [ $? -eq 0 ]; then
        #         TERM=xterm                       # a syscons console
        #     else
        #         echo "Please choose the appropriate terminal type ..."
        #         read TERM                        # <-- blocks here
        #     fi
        #
        # `kbdcontrol -d` succeeds only when syscons is present, and this machine's only
        # console is the serial port. So it asks - and WAITS - before it ever looks at
        # /etc/installerconfig. That is what every FreeBSD run has been sitting on: not a
        # wrong branch, a read that never got an answer.
        #
        # Measured locally against the repacked media: sending one carriage return took
        # it straight to
        #
        #     DEBUG: f_debug_init: ARGV=[/etc/installerconfig]
        #     DEBUG: Began Installation at Tue Nov  3 10:50:04 UTC 2026
        #
        # with the answer file being read and scriptedpart starting.
        #
        # AN EMPTY LINE TAKES THE DEFAULT, which the prompt itself states: "Console type
        # [vt100]". TERM=vt100 is already exported by rc.local.
        time.sleep(45)
        log("answering the installer's terminal-type prompt with the default")
        ser.say("", wait=10)

        log(f"waiting up to {args.timeout}s for bsdinstall to finish and reboot")
        install_started = time.time()
        deadline = install_started + args.timeout
        exited = False
        while time.time() < deadline:
            if not guest.alive():
                log("the guest exited (bsdinstall reboots when it is done)")
                exited = True
                break

            # THE CONSOLE CAN SAY THE INSTALL IS DONE EVEN WHEN THE MACHINE WILL NOT STOP.
            #
            # bsdinstall's `script` target ends with `umount`, and then startbsdinstall
            # shows a bsddialog --pause ("Installation complete! Rebooting in 10
            # seconds") before calling reboot. On this guest that dialog never appears and
            # the machine never reboots - run 41663564002 and 41667748898 both end their
            # console at `Running installation step: umount` and sit there until the
            # timeout, with the disk finished at 2056 MiB.
            #
            # So `umount` in the console is taken as the end of the install as well. It is
            # bsdinstall's own last step: after it, the only work left is a dialog and a
            # reboot, and nothing about the disk changes. Waiting for a machine to stop
            # that is only waiting to show a message is waiting on the wrong thing.
            if console_says_umount(ser):
                log("the console reached installation step 'umount' - bsdinstall is done")
                log("  the machine is not rebooting, so it is stopped here instead")
                exited = True
                break

            elapsed = int(args.timeout - (deadline - time.time()))
            if elapsed % 120 < 15:
                log(f"  {elapsed}s, disk {os.stat(disk).st_blocks * 512 >> 20} MiB")
            time.sleep(15)
    finally:
        guest.stop()
        ser.close()

    # A TIMEOUT IS A FAILURE, AND RUN 41638444422 IS THE PROOF IT WAS NOT TREATED AS ONE.
    #
    # That run's console says the install never finished:
    #
    #     13:48:42  === BSDINSTALL DID NOT FINISH IN 3600s ===
    #     13:48:44  [bsd   UFS magic at offset 364407: 19015419]
    #     13:48:48  BAKED freebsd -> out/freebsd.qcow2 (319 MiB)
    #
    # A superblock written in the first minute made the disk check pass, and the workflow
    # reported success on a partial install. Both halves are required now - see
    # lib/install_evidence.py.
    if not install_evidence.finished_cleanly(
            exited, time.time() - install_started, args.timeout, log):
        return 1

    log(f"disk is {install_evidence.disk_mib(disk)} MiB")
    if not install_evidence.above_floor(
            disk, install_evidence.floor_for("freebsd"), log, "freebsd"):
        return 1
    if not bsdinstall.check_disk_ufs(disk):
        log("=== THE DISK HOLDS NO UFS FILESYSTEM ===")
        log("  a failed bsdinstall leaves a partition table and nothing on it")
        first_error(publish_console(ser, args.out))
        return 1

    log("compressing")
    compress.compress(disk, args.out)
    log(f"BAKED freebsd -> {args.out} ({os.path.getsize(args.out) >> 20} MiB)")
    return 0


def publish_console(ser, out_path: str) -> str:
    """Copy the guest's console to where the workflow uploads it, and return the path.

    ser.sink lives in the work directory beside the disk; every workflow's artifact path
    names out/console.log. Without this the guest's own words never reached an artifact,
    so a failed run could only be explained by whatever happened to fit in the check-run's
    65535-character budget - which is how run 41607302540 came to report "exit code 1"
    and nothing else.
    """
    target = os.path.join(os.path.dirname(os.path.abspath(out_path)) or ".", "console.log")
    try:
        with open(ser.sink, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
        log(f"console: {target} ({os.path.getsize(target) >> 10} KiB)")
        return target
    except OSError as exc:
        log(f"could not copy the console to {target}: {exc}")
        return ser.sink


def first_error(path: str, context: int = 30) -> None:
    """The first error and its surroundings, not the last sixty lines."""
    try:
        with open(path, "rb") as fh:
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        log(f"(no console log at {path})")
        return
    index = None
    for i, line in enumerate(lines):
        if any(word in line for word in ("ERROR", "error:", "failed", "FAILED")):
            index = i
            break
    if index is None:
        for line in lines[-40:]:
            log(f"  [console] {line}")
        return
    for line in lines[max(0, index - 8):index + context]:
        log(f"  [console] {line}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
