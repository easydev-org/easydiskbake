"""Put Autounattend.xml where Windows setup will find it.

    from winiso import answer_iso
    answer_iso("windows/11/answer", "boot.iso")   # a few hundred KB

WHY NOT REBUILD THE WINDOWS ISO

The obvious approach is to add \\Autounattend.xml to the install ISO's root and
rebuild it. That is what dockur/windows does, and it costs a full remaster of 7.2 GiB -
xorriso cannot add a file to an ISO in place, so the whole image is read and written
again.

The first version of this module tried the short form of it (`-indev` + `-outdev` plus
one `-map`) and produced a 448 KB ISO: a boot record with the answer file and none of
the installer's files. qemu started it happily, found nothing to boot, and the bake
then sat at the arrival check until it was cancelled. The size was logged, which is
the only reason it was caught as quickly as it was.

WINDOWS DOES NOT REQUIRE THE FILE ON THE INSTALL MEDIUM

Setup looks for \\Autounattend.xml at the root of ANY removable drive it can see. The
install ISO is one CD; an answer CD is another. So a second, tiny ISO is enough, and
it is:

    a few hundred KB written instead of 7.2 GiB
    seconds instead of minutes
    no dependency on El Torito being reproduced correctly

This is the documented way to supply an answer file for a read-only install source,
not a trick. And the failure it removes is larger than the one it adds: a remastered
install ISO that silently lost a boot record is a class of bug this cannot have.

THE DRIVERS GO ON THE SAME DISC, IN $WinPEDriver$

This is the part that stopped the first full run for fifteen minutes at a dialog nobody
was watching. The system disk is virtio-blk and windowsPE ships no virtio driver, so
setup's drive list is empty. Attaching the virtio-win CD is not enough: setup loads a
storage driver from it only when told to, and the answer file has no DriverPaths.

`$WinPEDriver$` is the mechanism that needs no configuration - setup scans for a
directory of that name on removable media and loads the drivers under it. It is what
dockur/windows does (src/install.sh::addDrivers), except that project builds a FAT32
image, mounts it as disk 0, and therefore has to number the virtio disk 1. Putting the
drivers on a second CD keeps the virtio disk at index 0, which is what the answer file
already says.

WHAT IT DOES NOT DO

No $OEM$ tree. Everything a guest-side SetupComplete.cmd would do is either in the
answer file already or checked from the host - see windows/README.md. A guest that
stages no script is a guest with one fewer way to fail.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

# Setup looks for this name, not for a configured path: it is the directory that makes
# a virtio disk visible during windowsPE.
WINPE_DRIVER_DIR = "$WinPEDriver$"

# What windowsPE needs to see the disk, plus what the installed system wants for
# network and ballooning. The virtio-win ISO carries a tree per OS version; these are
# the directories that hold a loadable .inf for each.
WINPE_DRIVERS = ["viostor", "vioscsi", "NetKVM", "Balloon"]


def log(msg: str) -> None:
    print(f"[iso] {msg}", flush=True)


class IsoError(RuntimeError):
    pass


def build_args(out: str, stage: str, label: str) -> list[str]:
    """The xorriso argv. Pure, so the flags can be asserted without running it.

    That matters because a wrong flag is a FAILURE at run time only: the first
    version passed `-rock on`, which xorriso does not have - Rock Ridge is produced by
    default - and the bake died with exit 5 ninety seconds in, after downloading
    7.2 GiB. A test over this list costs nothing.
    """
    return [
        "xorriso", "-outdev", out,
        "-volid", label,
        # Joliet lets Windows read long filenames. There is no "-rock" command: Rock
        # Ridge is on by default, and asking for it is an error rather than a no-op.
        "-joliet", "on",
        # -map on a directory inserts its whole subtree; the destination "/" puts the
        # file at the image root, which is where setup looks.
        "-map", stage, "/",
        "-commit",
    ]


def extract_drivers(virtio_iso: str, stage: str) -> list[str]:
    """Copy the windowsPE-loadable drivers out of the virtio-win ISO into `stage`.

    The ISO's layout is

        viostor/w11/amd64/*.inf  *.sys  *.cat
        vioscsi/w11/amd64/...
        NetKVM/w11/amd64/...

    and each driver's files are moved up to `stage/<driver>/` so the tree is one level
    shallower. Setup recurses, but a single flat directory of conflicting `*.inf` names
    does not work, so the per-driver directory stays.

    7z rather than a mount: reading an ISO as an archive needs no privileges, which is
    the same reason lib/wim.py uses it.
    """
    if not os.path.isfile(virtio_iso):
        raise IsoError(f"no virtio-win ISO at {virtio_iso}")

    if not shutil.which("7z"):
        raise IsoError("7z is not installed; it is in the bake image (see Containerfile)")

    found: list[str] = []
    for driver in WINPE_DRIVERS:
        for version_dir in ("w11", "w10", "2k19"):
            member = f"{driver}/{version_dir}/amd64"
            result = subprocess.run(
                ["7z", "x", "-y", f"-o{stage}", virtio_iso, f"{member}/*"],
                capture_output=True, text=True,
            )
            source = os.path.join(stage, *member.split("/"))
            if result.returncode != 0 or not os.path.isdir(source) or not os.listdir(source):
                continue

            target = os.path.join(stage, driver)
            os.makedirs(target, exist_ok=True)
            for entry in os.listdir(source):
                shutil.move(os.path.join(source, entry), os.path.join(target, entry))
            # The empty version directories 7z created are not wanted in the image.
            shutil.rmtree(os.path.join(stage, driver, version_dir), ignore_errors=True)
            found.append(driver)
            break

    if not found:
        raise IsoError(
            f"no drivers were extracted from {virtio_iso}.\n"
            f"Looked for {', '.join(WINPE_DRIVERS)} under w11/w10/2k19 amd64.\n"
            f"Without viostor, windowsPE cannot see the virtio disk and setup stops "
            f"at a dialog nobody clicks."
        )

    log(f"drivers: {', '.join(found)}")
    return found


def answer_iso(answer_dir: str, out: str, *, label: str = "ANSWER",
               drivers: str | None = None) -> str:
    """Build a small ISO holding Autounattend.xml and, optionally, the drivers.

    The volume label matters more than it looks: setup scans removable media, and a
    label of ANSWER makes the bake's own log readable when more than one CD is
    attached. It does not affect which file is found.
    """
    source = os.path.join(answer_dir, "Autounattend.xml")
    if not os.path.isfile(source):
        raise IsoError(f"no Autounattend.xml in {answer_dir}")

    if not shutil.which("xorriso"):
        raise IsoError("xorriso is not installed; it is in the bake image (see Containerfile)")

    stage = tempfile.mkdtemp(prefix="answeriso-")
    try:
        shutil.copyfile(source, os.path.join(stage, "Autounattend.xml"))

        if drivers:
            winpe = os.path.join(stage, WINPE_DRIVER_DIR)
            os.makedirs(winpe, exist_ok=True)
            extract_drivers(drivers, winpe)

        if os.path.exists(out):
            os.unlink(out)

        log(f"building the answer ISO -> {out}")
        result = subprocess.run(build_args(out, stage, label), capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if line.strip():
                log(line)
        if result.returncode != 0:
            raise IsoError(
                f"xorriso failed ({result.returncode}):\n"
                f"{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
            )
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise IsoError("xorriso produced no output")

    _verify(out, want_drivers=bool(drivers))
    log(f"ok: {os.path.getsize(out)} bytes, Autounattend.xml at the root")
    return out


def _verify(path: str, *, want_drivers: bool = False) -> None:
    """The files have to be at the root, and the image has to be non-trivial.

    A mistake here means setup waits at its first pane for the whole timeout, which
    is the most expensive possible way to learn it. Both checks are sub-second.
    """
    if os.path.getsize(path) < 2048:
        raise IsoError(
            f"the answer ISO is {os.path.getsize(path)} bytes; "
            f"an ISO with one file cannot be that small"
        )

    listing = subprocess.run(
        ["xorriso", "-indev", path, "-find", "/", "-maxdepth", "2", "-exec", "lsdl"],
        capture_output=True, text=True,
    )
    text = listing.stdout + listing.stderr

    if "Autounattend" not in text:
        raise IsoError(
            "the answer ISO does not contain /Autounattend.xml at its root; "
            "setup would wait at the first pane. Listing was:\n" + text[:800]
        )

    if want_drivers and "WinPEDriver" not in text:
        raise IsoError(
            f"the answer ISO has no {WINPE_DRIVER_DIR} directory, so windowsPE will "
            f"list no disks and setup stops at a dialog nobody clicks.\n"
            f"Listing was:\n{text[:800]}"
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        raise SystemExit("usage: winiso.py <answer-dir> <out.iso> [virtio-win.iso]")
    answer_iso(sys.argv[1], sys.argv[2],
               drivers=sys.argv[3] if len(sys.argv) > 3 else None)
