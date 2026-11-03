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

from lib import bsdinstall, compress, iso as isolib, live as livelib, vm  # noqa: E402

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


def build_media(iso_path: str, work: str) -> str:
    """The installation ISO with /etc/installerconfig inside it.

    TWO STEPS, BECAUSE THE FILE IS NOT ON THE ISO'S FILE SYSTEM.

    Nothing on the ISO is at /etc/installerconfig - the live environment's /etc is a
    memory filesystem built at boot from the mfsroot image inside the ISO. So the mfsroot
    is unpacked, the file added, the image rebuilt, and only then is the ISO repacked with
    the new mfsroot in place of the old one.

    THE REBUILD IS udfcreate AND THE REPACK IS xorriso, and both are in the bake image.
    """
    tree = os.path.join(work, "iso")
    if os.path.exists(tree):
        import shutil
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
    # It then exits 1. Run 41599346958 stopped there, before a single file had been looked
    # at, and the tree it had written was complete for every purpose this bake has: the
    # mfsroot is a regular file, and the symlinks it skipped are inside the LIVE system's
    # filesystem, which this never mounts.
    #
    # So the judgement is whether the tree holds what is needed, not what 7z returned.
    # Anything else it complains about still shows up as a missing mfsroot below.
    if "Dangerous link path was ignored" in (result.stderr or result.stdout or ""):
        log("  7z skipped symlinks pointing outside the tree; that is expected here")
    elif result.returncode != 0:
        raise SystemExit(
            f"could not unpack {iso_path}: {(result.stderr or result.stdout)[:400]}")

    # The mfsroot is the last stage of the live environment; its name is stable across
    # releases because the loader refers to it by that name.
    mfsroot = None
    for root, _dirs, files in os.walk(tree):
        for name in files:
            if name == "mfsroot":
                mfsroot = os.path.join(root, name)
                break
        if mfsroot:
            break
    if not mfsroot:
        raise SystemExit(
            f"no mfsroot inside {iso_path}. Its /etc is built from that image, so "
            f"/etc/installerconfig cannot be added without it - and the ISO's layout has "
            f"changed from what this expects.")

    log(f"mfsroot: {os.path.relpath(mfsroot, tree)}")

    # ADD THE FILE TO THE MFSROOT. It is a plain UFS image, which 7z can open read-only.
    mtree = os.path.join(work, "mfs")
    if os.path.exists(mtree):
        import shutil
        shutil.rmtree(mtree)
    os.makedirs(mtree)
    unpack = subprocess.run(["7z", "x", "-y", f"-o{mtree}", mfsroot],
                            capture_output=True, text=True)
    if unpack.returncode != 0:
        raise SystemExit(
            f"could not unpack the mfsroot: {(unpack.stderr or unpack.stdout)[:300]}")

    etc = os.path.join(mtree, "etc")
    os.makedirs(etc, exist_ok=True)
    target = os.path.join(etc, "installerconfig")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build_installerconfig())
    log(f"wrote {os.path.relpath(target, work)}")

    # Repack the mfsroot. Compression is off: the media's own mfsroot is not compressed
    # and the loader does not expect it to be.
    rebuilt = os.path.join(work, "mfsroot.new")
    made = subprocess.run(["makefs", "-t", "ffs", "-o", "version=2", rebuilt, mtree],
                          capture_output=True, text=True)
    if made.returncode != 0:
        raise SystemExit(
            f"makefs could not rebuild the mfsroot: {(made.stderr or made.stdout)[:300]}")
    os.replace(rebuilt, mfsroot)
    log(f"rebuilt the mfsroot ({os.path.getsize(mfsroot) >> 20} MiB)")

    # And the ISO, keeping the El Torito record that makes it bootable.
    media = os.path.join(work, "freebsd-install.iso")
    repack = subprocess.run([
        "xorriso", "-as", "mkisofs", "-o", media,
        "-b", "boot/cdboot", "-no-emul-boot", "-c", "boot/boot.catalog",
        "-J", "-R", "-V", "FREESD_INSTALL", tree,
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
    subprocess.run(["qemu-img", "create", "-f", "qcow2", disk, args.disk_size],
                   check=True)
    log(f"disk created: {args.disk_size}")

    shape = vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std")
    drives = [
        "-drive", f"file={media},media=cdrom,readonly=on",
        "-drive", f"file={disk},if=virtio,format=qcow2",
    ]
    guest = vm.Guest(work, extra=drives, shape=shape,
                     stdout=os.path.join(work, "console.log"))
    try:
        guest.start()
        shots = os.path.join(work, "frames")
        os.makedirs(shots, exist_ok=True)
        snap = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
             guest.paths.record, shots,
             os.environ.get("SNAPSHOT_INTERVAL", "5"), "2000"],
            stdout=open(os.path.join(shots, "snapshots.log"), "wb"),
            stderr=subprocess.STDOUT,
        )
        live = livelib.start(
            [shots], os.path.join(work, "install.mp4"),
            os.path.join(work, "last-frame.png"),
            every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "20")),
        )

        log(f"waiting up to {args.timeout}s for bsdinstall to finish and reboot")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if not guest.alive():
                log("the guest exited (bsdinstall reboots when it is done)")
                break
            elapsed = int(args.timeout - (deadline - time.time()))
            if elapsed % 120 < 15:
                log(f"  {elapsed}s, disk {os.stat(disk).st_blocks * 512 >> 20} MiB")
            time.sleep(15)
        else:
            log(f"=== BSDINSTALL DID NOT FINISH IN {args.timeout}s ===")
    finally:
        guest.stop()
        snap.terminate()
        try:
            snap.wait(timeout=5)
        except subprocess.TimeoutExpired:
            snap.kill()
        live.stop(final=True)

    log(f"disk is {os.stat(disk).st_blocks * 512 >> 20} MiB")
    if not bsdinstall.check_disk_ufs(disk):
        log("=== THE DISK HOLDS NO UFS FILESYSTEM ===")
        log("  a failed bsdinstall leaves a partition table and nothing on it")
        first_error(os.path.join(work, "console.log"))
        return 1

    log("compressing")
    compress.compress(disk, args.out)
    log(f"BAKED freebsd -> {args.out} ({os.path.getsize(args.out) >> 20} MiB)")
    return 0


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
