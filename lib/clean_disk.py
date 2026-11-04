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
    "windows": ("winclean", {"user": "docker"}),
    "linux": ("linclean", {}),
    "freebsd": ("bsdclean", {"user": "docker"}),
    # macOS IS DELIBERATELY ABSENT. Its filesystem is copy-on-write, so writing zeros over the
    # free space allocates new blocks before the old ones are released, and APFS snapshots hold
    # blocks a write cannot reach. "Adding it here" is not the work; measuring whether it saves
    # anything is, and that has not been done.
}


def clean_running(kind: str, *, port: int, note=print, **extra) -> bool:
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
    return module.clean(port=port, note=note, **kwargs)
