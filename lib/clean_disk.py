"""Clean a running guest over ssh, before its disk is exported.

    from lib.clean_disk import clean_running
    ok = clean_running("windows", port=2222, note=log)
    ok = clean_running("linux", port=2222, note=log)

WHY THIS IS A FUNCTION AND NOT A WORKFLOW JOB

A separate job has to hand the disk to itself through an artifact: upload 12-16 GiB, download
it again, boot it, clean it, shut it down. Every one of those steps is work that was already
done - the bake has the guest RUNNING, ssh CONNECTED, and the disk on local storage - and the
only thing a job boundary adds is two transfers of the whole disk.

So the cleaning is called from inside the stage that already has the machine up:

    windows/11/ssh.py     installs sshd, proves it answers, THEN cleans
    windows/11/devtools.py installs the toolchain, THEN cleans
    linux/*/bake.py       boots the installed disk, proves it answers, THEN cleans

The guest is stopped after, by the caller's own `guest.stop(acpi_first=True)`, because that
caller already owns the machine.

WHY THE STEPS ARE SHARED AND ONLY THE LAST ONE MATTERS

`qemu-img convert -O qcow2` only skips clusters that are ALREADY ZERO. Deleting a file marks
its blocks free and leaves the bytes where they were, so convert stores them and the qcow2
comes out the size of the disk's high-water mark. Writing zeros over the free space is what
makes those blocks zero when convert looks, and it is the same action on every system.

Everything before it is per-system - apt caches, winget downloads, nix paths - and lives in
lib/linclean.py and lib/winclean.py. This module is only the dispatch, so a caller says which
system it has and gets the right steps.
"""

from __future__ import annotations

# WHICH CLEANER, AND WHAT IT NEEDS BESIDES THE PORT.
#
#   windows   clean.ps1 is 190 lines and cannot be typed at a keyboard, so it goes up with
#             scp; the account is the one devtools.py and ssh.py both create
#   linux     the steps are one command each, sent through ssh
#
# Both take (port=, note=) and make their own connection, so this table is the whole
# difference between them.
SYSTEMS = {
    "windows": ("winclean", {"user": "vmuser"}),
    "linux": ("linclean", {}),
    # NO `user` HERE. linclean and bsdclean talk to the guest as the account every image in
    # this repository creates, and lib/session.ssh already supplies it; only winclean needs the
    # name, because it builds its own scp and ssh command lines. Passing it to a cleaner that
    # does not take it is a TypeError at the moment the clean starts - run 41811218132, after a
    # completely successful FreeBSD install.
    "freebsd": ("bsdclean", {}),
    # macOS. The note that used to sit here said it was deliberately absent: "its filesystem is
    # copy-on-write, so writing zeros over the free space allocates new blocks before the old
    # ones are released". THAT IS AN ARGUMENT AGAINST ZEROING, and it is correct - it is why
    # none of the four systems write zeros. It says nothing about DISCARD, which writes
    # nothing and is exactly what a copy-on-write filesystem can do safely.
    #
    # The four macOS stages each wrote their own `-drive ... format=qcow2,id=target` line,
    # none with discard=unmap, so every byte the guest ever wrote stayed allocated in the
    # qcow2 and the image grew from setup to dev with nothing ever shrinking it. The line is
    # now macos/15/machine.TARGET_DRIVE, in one place, and this cleaner is what uses it.
    "macos": ("macclean", {}),
}


def allocated_mib(disk: str) -> int:
    """Bytes the qcow2 actually occupies, in MiB. `qemu-img info`'s "disk size".

    THE ABSOLUTE BINARY, for the reason lib/compress.py documents: the bare name resolves to
    the Android SDK's qemu-img on this image's PATH, and that build cannot open a qcow2 whose
    clusters use compression_type=zstd - which is what every published disk is.
    """
    import json
    import subprocess

    from compress import qemu_img

    r = subprocess.run([qemu_img(), "info", "--output=json", disk],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return -1
    try:
        return (json.loads(r.stdout).get("actual-size") or 0) >> 20
    except ValueError:
        return -1


def clean_running(kind: str, *, port: int, note=print, disk: str | None = None,
                  **extra) -> bool:
    """Clean the guest on `port`. True only when every step that matters ran.

    THE CALLER OWNS THE MACHINE. This does not start or stop anything: it is called while the
    guest is up and connected, and the caller stops it afterwards with acpi_first, which is
    what makes the shutdown clean. A cleaning step that stopped its own guest would leave the
    caller holding a process it no longer controls.
    """
    if kind not in SYSTEMS:
        raise SystemExit(
            f"=== NO CLEANER FOR {kind!r} ===\n"
            f"  known: {', '.join(sorted(SYSTEMS))}\n"
            f"  adding one means writing its steps in lib/, not passing a flag"
        )

    module_name, defaults = SYSTEMS[kind]
    import importlib

    module = importlib.import_module(module_name)
    kwargs = {**defaults, **extra}

    # `disk` IS PASSED TO THE CLEANER, NOT USED HERE. The cleaners report how much the image
    # shrank as well as what the guest released, and those are different numbers: a trim can
    # release 116 GiB inside the guest and leave a qcow2 that still holds every cluster it ever
    # allocated, if the discard never reached the driver. Only the cleaner knows where its
    # messages go, so it gets the path.
    if disk:
        kwargs["disk"] = disk
    return module.clean(port=port, note=note, **kwargs)
