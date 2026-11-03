"""Repack an OpenBSD install ISO with a serial console and the answer file.

    media = build_media(iso_path, work)

WHY THE ISO IS REBUILT, AND WHY THAT IS THE ONLY WAY

OpenBSD's bsd.rd starts its console on pc0 - the VGA adapter - unless it is told
otherwise, and nothing on the kernel command line changes that: its boot loader reads
/etc/boot.conf from the media. So a bake that wants to TYPE at the installer has to put
`set tty com0` in that file, on the medium, before boot.

THE ALTERNATIVE PEOPLE TRY FIRST - a second FAT disk with install.conf - does not work
for this either. bsd.rd looks for its answer file on a disk it can find, but it still
prints its questions to the VGA, so a bake has no way to know which question it is
answering. The console has to move.

WHAT GOES IN

    etc/boot.conf     `set tty com0`, so the loader and bsd.rd both use the serial port
    etc/install.conf  the answers, which bsd.rd reads when it is booted as an installer

install.conf is written here as well as typed by the bake, because a person booting the
media by hand gets the same run: that is what makes the artifact reproducible.

THE VOLUME LABEL IS PRESERVED. bsd.rd does not mount its root by label the way FreeBSD's
loader does, but a repack that changes the label is a repack that changed something
nobody asked it to - and `disklabel` output and the installer's own prompts both show it.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def log(message: str) -> None:
    print(f"[openbsd-media] {message}", flush=True)


def volume_label(iso_path: str) -> str:
    """The ISO's own volume label, from its Primary Volume Descriptor.

    Read rather than assumed: a repack has no business renaming the medium, and the next
    release is a different string.
    """
    with open(iso_path, "rb") as fh:
        fh.seek(16 * 2048)
        pvd = fh.read(2048)
    if len(pvd) < 2048 or pvd[0:1] != b"\x01":
        raise SystemExit(
            f"{iso_path} has no ISO9660 Primary Volume Descriptor at sector 16, so its "
            f"volume label cannot be read")
    label = pvd[40:72].decode("ascii", "replace").strip()
    if not label:
        raise SystemExit(f"{iso_path}'s volume label is empty")
    return label


def build_media(iso_path: str, work: str, answers: str) -> str:
    """The installation ISO with a serial console and the answers inside it."""
    tree = os.path.join(work, "iso")
    if os.path.exists(tree):
        shutil.rmtree(tree)
    os.makedirs(tree)

    log("unpacking the ISO")
    result = subprocess.run(["7z", "x", "-y", f"-o{tree}", iso_path],
                            capture_output=True, text=True)
    if "Dangerous link path was ignored" in (result.stderr or result.stdout or ""):
        log("  7z skipped symlinks pointing outside the tree; expected here")
    elif result.returncode != 0:
        raise SystemExit(
            f"could not unpack {iso_path}: {(result.stderr or result.stdout)[:400]}")

    etc = os.path.join(tree, "etc")
    os.makedirs(etc, exist_ok=True)

    # THE CONSOLE. com0 at 115200 is what lib/serial.py's fifo carries.
    boot_conf = os.path.join(etc, "boot.conf")
    with open(boot_conf, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# easydiskbake: the console is the serial port, not the VGA\n")
        fh.write("set tty com0\n")
        fh.write("stty com0 115200\n")
    log("etc/boot.conf: set tty com0")

    install_conf = os.path.join(etc, "install.conf")
    with open(install_conf, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(answers)
    log(f"etc/install.conf: {len(answers.splitlines())} lines")

    label = volume_label(iso_path)
    log(f"volume label: {label}")

    # THE BOOT RECORD IS THE MEDIA'S OWN. OpenBSD's disc boots through cdbr, a 2 KB
    # first-stage block, which then loads cdboot from the ISO's root - so the El Torito
    # entry names cdbr and the catalog is the one already in the image. Both paths are
    # read from the release directory rather than written here, because the version
    # number is part of them.
    release = ""
    for entry in os.listdir(tree):
        if entry and entry[0].isdigit():
            release = entry
            break
    if not release:
        raise SystemExit(f"no release directory (8.0, 8.1, ...) at the root of {iso_path}")
    cdbr = f"{release}/amd64/cdbr"
    catalog = f"{release}/amd64/boot.catalog"
    if not os.path.exists(os.path.join(tree, cdbr)):
        raise SystemExit(f"{iso_path} has no {cdbr}; its layout changed")
    log(f"boot record: {cdbr}")

    media = os.path.join(work, "openbsd-install.iso")

    # TWO BOOT RECORDS: BIOS AND UEFI, WHICH IS WHAT THE ORIGINAL HAS.
    #
    # The release ISO carries 8.0/amd64/cdbr for BIOS and 8.0/amd64/eficdboot for UEFI.
    # A repack with only the first produces a medium that boots on a BIOS machine and
    # silently does nothing under OVMF: no kernel, no console, no error - which is what
    # run 41611273209 showed, ninety seconds of silence followed by answers typed into
    # a machine that had never started an installer.
    #
    # THE MACHINE IS Q35 WITH NO FIRMWARE PAIR, so qemu uses SeaBIOS and the BIOS path is
    # what actually runs here. The UEFI record is kept anyway: a medium that only boots
    # one way is a medium somebody will try to boot the other way, and the warning would
    # be a machine that does nothing rather than a message.
    # cdboot IS THE EL TORITO IMAGE; cdbr IS A ONE-SHOT THAT FINDS IT BY LBA.
    #
    # OpenBSD ships three pieces:
    #
    #     cdbr        a 2 KB first-stage block whose last bytes name the LBA of cdboot.
    #                 Those bytes are ZERO in the release image - read them - because the
    #                 real number is written by whoever builds the ISO, and it depends on
    #                 where cdboot lands in THAT build.
    #     cdboot      the loader itself, 90 KB, and what actually reads /etc/boot.conf.
    #     eficdboot   the UEFI equivalent.
    #
    # A repack changes every LBA, so a cdbr copied from the original points at whatever
    # now sits at the old address. The result boots to nothing and says nothing, which
    # is exactly what run 41613076306 recorded: no console output at all.
    #
    # Naming cdboot as the boot image removes the indirection: xorriso places it and
    # records where it went, so there is no stale number to be wrong.
    efi = f"{release}/amd64/eficdboot"
    boot_img = f"{release}/amd64/cdboot"
    if not os.path.exists(os.path.join(tree, boot_img)):
        boot_img = cdbr          # fall back to the chained form if cdboot is absent
    log(f"boot image: {boot_img}")
    cmd = [
        "xorriso", "-as", "mkisofs", "-o", media,
        "-b", boot_img, "-no-emul-boot", "-c", catalog,
    ]
    if os.path.exists(os.path.join(tree, efi)):
        cmd += ["-eltorito-alt-boot", "-e", efi, "-no-emul-boot"]
        log(f"uefi record: {efi}")
    cmd += ["-J", "-R", "-V", label, tree]
    repack = subprocess.run(cmd, capture_output=True, text=True)
    if repack.returncode != 0:
        raise SystemExit(
            f"xorriso could not repack the ISO: {(repack.stderr or repack.stdout)[:400]}")
    log(f"media: {media} ({os.path.getsize(media) >> 20} MiB)")
    return media
