"""Keep what was installed in the guest, across runs.

    python3 android/15/persist.py --save    --dir /a/android
    python3 android/15/persist.py --restore --dir /a/android

WHY THIS IS TWO FILES AND NOT ONE

/data is encrypted, and the key is NOT in /data. The guest sets up metadata encryption on
first boot:

    init: Mount point /dev/block/vdc @ /data uses metadata encryption, ...
    init: Calling: /system/bin/vdc cryptfs encryptFstab /dev/block/vdc /data false ext4 false

and the key material lands in the `metadata` partition, which lives on cache.img:

    /metadata/vold
    /metadata/password_slots

Measured, not assumed - a run that wrote a marker into /data, then had its /data overlay
folded back but NOT its cache overlay, produced this on the next boot:

    cutils: Failed to mkdir(/data/misc/vold/user_keys): No such file or directory
    vold: Failed to prepare /data/misc/vold/user_keys: No such file or directory

/data was there and unreadable. So the pair is the unit: userdata.img holds the applications
and cache.img holds the key that opens them. Saving one without the other produces a disk
that boots and looks empty.

WHY THE OVERLAY HAS TO BE FOLDED IN

The emulator never writes to the raw file it is given. It puts a qcow2 overlay beside it and
the guest writes there:

    userdata.img        8589934592 bytes, still the freshly-made filesystem
    userdata.img.qcow2   545652736 bytes, everything the guest has ever written

So `ls -la userdata.img` after a run that installed things shows the SAME size and the SAME
content as before the run. The state is entirely in the overlay, and a raw file alone loses
it. --save merges the overlay back with `qemu-img convert -O raw`, which is the only thing
that turns the overlay into a file that can be handed to the next run.

WHY NOT -data foo.qcow2

Tried: the emulator wraps whatever it is given in another overlay, so a qcow2 argument
becomes `foo.qcow2.qcow2` and the boot fails after 29 restarts. `-data` takes a raw image and
nothing else.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

# THE FILE PAIR, AND THE REASON IT IS A PAIR IS IN THE MODULE DOCSTRING.
PAIR = ("cache.img", "userdata.img")


def log(msg: str) -> None:
    print(f"[persist {__import__('time').strftime('%H:%M:%S')}] {msg}", flush=True)


def qemu_img() -> str:
    """The emulator's own qemu-img, which is the one that understands its overlays.

    NOT /usr/bin/qemu-img: the SDK puts a 2.12 build in $ANDROID_SDK_ROOT/emulator ahead on
    PATH, and it cannot read an image the newer build wrote - the same trap lib/compress.py
    documents. Asking for it by absolute path means this does not depend on PATH order.
    """
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or ""
    candidates = [
        os.path.join(sdk, "emulator", "qemu-img") if sdk else "",
        "/opt/android-sdk/emulator/qemu-img",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    found = shutil.which("qemu-img")
    if not found:
        raise SystemExit("=== no qemu-img on PATH and none in $ANDROID_SDK_ROOT/emulator ===")
    return found


def save(work: str, into: str) -> int:
    """Fold each overlay into its raw file, and copy the pair to `into`."""
    os.makedirs(into, exist_ok=True)
    qimg = qemu_img()
    saved = 0

    for name in PAIR:
        raw = os.path.join(work, name)
        overlay = raw + ".qcow2"
        if not os.path.isfile(raw):
            log(f"{name}: NOT PRESENT in {work}; nothing to save")
            continue

        if os.path.isfile(overlay):
            # CONVERT THE OVERLAY, NOT THE RAW. The overlay is a complete image with the raw
            # as its backing file, so converting it produces the merged result.
            merged = raw + ".merged"
            log(f"{name}: folding {os.path.getsize(overlay) >> 20} MiB of overlay")
            result = subprocess.run([qimg, "convert", "-O", "raw", overlay, merged],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                log(f"{name}: qemu-img failed: {(result.stderr or result.stdout)[:300]}")
                return 1
            os.replace(merged, raw)
            os.unlink(overlay)
        else:
            log(f"{name}: no overlay; the raw file is already the whole story")

        # SHRINK WHAT IS EMPTY, WITHOUT TOUCHING WHAT IS NOT.
        #
        # A 64 GiB sparse file copies as 64 GiB through most tools, and `cp` fills the holes
        # unless it is told not to. copy_file_range with a sparse-aware copy is what keeps
        # the saved pair at its real size - a few hundred MiB - instead of 64.
        dest = os.path.join(into, name)
        log(f"{name}: {os.path.getsize(raw) >> 30} GiB virtual, "
            f"{du(raw) >> 20} MiB on disk -> {dest}")
        sparse_copy(raw, dest)
        saved += 1

    if not saved:
        log("=== NOTHING SAVED ===")
        return 1

    # THE PAIR IS USELESS SPLIT, so the save is refused rather than half-done. See the
    # module docstring: /data cannot be opened without the key in cache.img.
    missing = [n for n in PAIR if not os.path.isfile(os.path.join(into, n))]
    if missing:
        log(f"=== INCOMPLETE PAIR: {', '.join(missing)} missing from {into} ===")
        log("  /data is encrypted and its key is on cache.img; one without the other")
        log("  boots to a device with nothing installed")
        return 1

    log(f"saved {', '.join(PAIR)} -> {into}")
    log("restore with --restore, or point mkdisks.py --out at that directory")
    return 0


def restore(work: str, into: str) -> int:
    """Copy the saved pair back into a working directory, replacing what is there."""
    os.makedirs(work, exist_ok=True)
    for name in PAIR:
        source = os.path.join(into, name)
        if not os.path.isfile(source):
            log(f"=== NO {name} IN {into} ===")
            return 1
    for name in PAIR:
        source = os.path.join(into, name)
        dest = os.path.join(work, name)
        # THE OVERLAY BESIDE THE OLD RAW HAS TO GO. It refers to the previous raw file by
        # path; leaving it in place means the next boot reads the old state and silently
        # ignores the one that was just restored.
        for stale in (dest + ".qcow2", dest + ".merged"):
            if os.path.exists(stale):
                os.unlink(stale)
        log(f"{name}: {du(source) >> 20} MiB -> {dest}")
        sparse_copy(source, dest)
    log(f"restored {', '.join(PAIR)} into {work}")
    return 0


def du(path: str) -> int:
    """Bytes actually allocated, which is what a sparse file costs."""
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return 0


def sparse_copy(src: str, dest: str) -> None:
    """Copy preserving holes, so a 64 GiB image costs what it uses.

    `shutil.copyfile` reads and writes every byte, and a write of zeros to a fresh file
    allocates blocks - so a copy of a mostly-empty 64 GiB image becomes 64 GiB of real
    disk. `cp --sparse=always` punches the holes back instead.

    THE FIRST VERSION OF THIS WAS A PYTHON LOOP, AND IT TOOK FIVE MINUTES. It read 1 MiB
    at a time and `seek`ed over runs of zeros, which is correct and was measured at 1877
    MiB in 300 seconds - about 6 MiB/s, because every seek is a syscall and the block
    never gets written. `cp --sparse=always` is the same operation in coreutils, and it is
    the reason this function is three lines instead of thirty.
    """
    if os.path.exists(dest):
        os.unlink(dest)
    result = subprocess.run(["cp", "--sparse=always", src, dest],
                            capture_output=True, text=True)
    if result.returncode != 0:
        # cp IS THE FAST PATH, NOT THE ONLY ONE. A system without coreutils still needs to
        # be able to save a disk, so the portable version stays as a fallback rather than
        # turning a missing utility into an unrecoverable run.
        log(f"cp --sparse failed ({(result.stderr or '').strip()[:120]}); using the loop")
        _sparse_copy_loop(src, dest)


def _sparse_copy_loop(src: str, dest: str) -> None:
    """The portable version: seek over runs of zeros instead of writing them."""
    if os.path.exists(dest):
        os.unlink(dest)
    with open(src, "rb") as fh_in, open(dest, "wb") as fh_out:
        while True:
            block = fh_in.read(1 << 20)
            if not block:
                break
            if not any(block):
                fh_out.seek(len(block), os.SEEK_CUR)
            else:
                fh_out.write(block)
        # The final truncate is what makes the file the right SIZE when the tail is a hole;
        # without it the last seek leaves the file short.
        fh_out.truncate(os.path.getsize(src))


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dir", default="/a/android",
                   help="the directory mkdisks.py wrote its images into")
    p.add_argument("--save", action="store_true",
                   help="fold the overlays in and copy cache.img + userdata.img aside")
    p.add_argument("--restore", action="store_true",
                   help="copy a saved pair back into --dir")
    p.add_argument("--into", default="",
                   help="where the pair is saved to or restored from; "
                        "default <dir>/persist")
    a = p.parse_args(argv)

    if not a.save and not a.restore:
        p.error("one of --save or --restore is required")
    into = a.into or os.path.join(os.path.abspath(a.dir), "persist")

    if a.save:
        return save(os.path.abspath(a.dir), into)
    return restore(os.path.abspath(a.dir), into)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
