"""Clean a Linux guest over ssh, before its disk is exported.

    from lib.linclean import clean
    ok = clean(port=2222, note=log)

WHY OVER SSH AND NOT INSIDE THE INSTALLER

The first version of this put a shell script in the autoinstall's late-commands, fetched from
the seed server, and ran it with `curtin in-target`. It worked, and it was wrong for three
reasons:

    it only existed for the distributions that use an answer file, so NixOS got a second
    implementation of the same idea

    it cleaned BEFORE the disk had ever booted, so it could not see what the first boot
    writes - and there is a lot of that

    it put a file the repository owns onto every disk it built, which is the thing the
    clean step exists to prevent

`sshpass` is how every other stage in this repository reaches a guest, and every Linux bake
already logs in after installing: `boot_installed` in linux/*/bake.py asks the running system
for a Wayland session. Cleaning is one more thing to do while that connection is open.

EVERY STEP REPORTS WHAT IT RECLAIMED, AND THAT IS NOT DECORATION

The first version printed `apt archives: ok` and nothing else, and a run then reported

    compressing ... 4002 MiB (17% of 23070 MiB)

which says the image is 17% of the disk. It is 17% of the PARTITION - 20 GiB of virtual
capacity, most of which was never written - and the number made a clean that removed 200 MB
look like one that removed 16 GB. A metric wrong in a plausible direction is worse than none,
so each step is measured against `df` before and after.

THE TRIM IS THE STEP THAT MATTERS, AND IT IS NOT THE ONE THIS MODULE USED TO DO

Deleting a file marks its blocks free and leaves the bytes where they were, so `qemu-img
convert` still stores them and the image keeps the size of its high-water mark. The old answer
was to write zeros over the whole free space, which works and which this module did for years.

It stopped being usable when the disks became 128 GB. Measured in one clean, run 41824878061:

    12:10:25  discarding the free blocks (fstrim, up to 120s)
    12:10:26  /: 116.3 GiB (124887408640 bytes) trimmed      <- one second
    12:11:15  writing 9129 MB of zeros over the free space   <- forty-nine seconds

One second released 116 GiB, and the forty-nine that followed allocated 9 GB of clusters back,
because qemu stores zeros like any other data. The image came out at 12.33 GiB against 4 GiB
before any of it ran.

`fstrim` is the mechanism: virtio-blk discards, the qcow2 driver drops the clusters, nothing is
written. A filesystem that cannot discard needs `zerofree` - block bitmap, free AND non-zero -
and that is the fallback this now names rather than a blanket dd.
"""

from __future__ import annotations

# THE COMMANDS, IN ORDER, AS ONE STATEMENT EACH.
#
# Each is sent through `ssh <host> "..."`, which is a shell on the guest - so each is a complete
# command line, not a fragment. They are separate so the report can name which one freed what.
#
# A STEP IS TOLERANT OF "ALREADY GONE". `rm -rf` on a path that does not exist is success; what
# is not tolerated is `rm` failing to remove something that is still there, which is why each
# command ends with `true` only where a daemon may be holding a file - see "caches".
STEPS: list[tuple[str, str]] = [
    # THE DEB CACHE IS USUALLY THE BIGGEST SINGLE ITEM after a desktop install: every package
    # that was fetched stays in /var/cache/apt/archives. `autoclean` only removes superseded
    # ones, and a fresh install has almost none of those.
    ("apt archives",
     "sudo apt-get clean"),
    ("apt lists",
     "sudo rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin"),

    # journald is capped by its config, not its content, and a fresh install still writes tens
    # of megabytes. One day is kept so a support question has something to read.
    ("journal rotate",
     "sudo journalctl --rotate"),
    ("journal vacuum",
     "sudo journalctl --vacuum-time=1d"),
    ("logs",
     "sudo rm -rf /var/log/installer /var/log/apt/term.log* /var/log/*.gz /var/log/*.1"),
    ("large logs",
     "sudo find /var/log -type f -size +1M -exec truncate -s 0 {} + 2>/dev/null; true"),

    ("temp",
     "sudo rm -rf /tmp/* /var/tmp/* /var/crash/*"),

    # A CACHE THAT IS BEING WRITTEN IS NOT A FAILURE.
    #
    # Run 41789800329 stopped here:
    #
    #     caches: exit 1 — rm: cannot remove '/home/docker/.cache/mesa_shader_cache':
    #     Directory not empty
    #
    # which is rm racing the desktop's own Mesa daemon: it recreated an entry between rm
    # reading the directory and removing it. The cache was emptied; one directory was left
    # holding a file written microseconds later. The second pass catches most of it, and the
    # trailing `true` says the remainder is not this step's verdict - the free space at the
    # end is.
    ("caches",
     "sudo rm -rf /root/.cache/* /home/*/.cache/* 2>/dev/null; "
     "sudo find /root/.cache /home/*/.cache -mindepth 1 -maxdepth 1 "
     "-exec rm -rf {} + 2>/dev/null; true"),

    # The installer's own record describes how the machine was built to whoever boots it.
    ("installer record",
     "sudo rm -rf /var/log/installer /var/lib/ubiquity"),

    ("history",
     "sudo rm -f /root/.bash_history /home/*/.bash_history /home/*/.zsh_history "
     "/home/*/.python_history"),

    # The entries this pipeline wrote when it logged in.
    ("ssh traces",
     "sudo rm -f /root/.ssh/known_hosts /home/*/.ssh/known_hosts"),
]

# USED MiB ON /, ONE NUMBER. Every measuring point goes through this, so the before/after pairs
# are always the same quantity - `df`'s Used column excludes reserved blocks, which is the
# number that has to go down for a smaller image to come out.
USED = "df -m / | awk 'NR==2 {print $3}'"

def _used(port: int) -> int | None:
    """Used MiB on /, or None if the guest stopped answering.

    THE ONE MEASURING POINT, so a before/after pair is always the same quantity. `df`'s Used
    column excludes reserved blocks, and that is the number that has to fall for a smaller
    image to come out.

    IT WAS DELETED IN A REWRITE AND NOT PUT BACK. The rewrite replaced everything from the
    zeroing block onward, and this function sat inside it - so every clean failed with

        NameError: name '_used' is not defined

    at its first line, after a successful install and a successful boot. Both Linux and
    FreeBSD bakes reported it the same way.
    """
    from session import ssh

    status, out = ssh(port, USED, timeout=120)
    if status != 0:
        return None
    lines = [x.strip() for x in (out or "").splitlines() if x.strip().isdigit()]
    return int(lines[-1]) if lines else None


# NO ZEROING. THE FILESYSTEM RELEASES ITS OWN BLOCKS.
#
# Writing zeros over the free space was this repository's way of making `qemu-img convert`
# skip the blocks that deleted files had left behind. It works, and on a 128 GB disk it is
# absurd: it writes the whole free space, which both takes minutes and RE-ALLOCATES clusters
# that were never allocated, because qemu stores the zeros like any other data.
#
# Measured on run 41824878061, in one clean:
#
#     12:10:25  discarding the free blocks (fstrim, up to 120s)
#     12:10:26  /: 116.3 GiB (124887408640 bytes) trimmed        <- one second
#     12:11:15  writing 9129 MB of zeros over the free space     <- forty-nine seconds
#
# One second released 116 GiB. The forty-nine that followed allocated 9 GB of clusters back.
#
# `fstrim` is the right mechanism and the only one needed: virtio-blk discards, the qcow2
# driver drops the clusters, and nothing is written. What it cannot cover is a filesystem that
# does not support discard - and for that the answer is `zerofree`, which reads the block
# bitmap and zeroes only the blocks that are free AND non-zero. Not a blanket dd.

# THE TRIM IS BOUNDED, AND ITS EFFECT IS CHECKED RATHER THAN ASSUMED.
#
# 120 s is generous: the run above finished in one. A trim that returns non-zero, or that
# releases nothing, is reported - the caller decides whether that is fatal, and the number
# `fstrim` prints is the only evidence that it did anything.
TRIM = "sudo timeout 120 fstrim -v / 2>&1"


def clean(*, port: int, note=print, ssh_seconds: int = 900,
          disk: str | None = None) -> bool:
    """Run the steps over ssh, then release the free blocks. True when the trim worked.

    `disk` is the qcow2 on the host, when the caller has it. It is used only to report how much
    the image actually shrank - the trim's own output says what the guest released, and those
    are different numbers.
    """
    from session import ssh

    note("=== cleaning the guest, over ssh ===")

    start = _used(port)
    if start is None:
        note("=== THE GUEST DID NOT ANSWER `df` ===")
        note("  nothing was measured, so nothing will be reported")
        return False
    note(f"  used before: {start} MB")

    previous = start
    for what, command in STEPS:
        status, out = ssh(port, command, timeout=ssh_seconds)
        now = _used(port)
        detail = (out or "").strip().splitlines()
        last = detail[-1][:100] if detail else ""

        if now is None:
            note(f"  {what}: the guest stopped answering")
            return False

        freed = previous - now
        suffix = f"{freed:+d} MB" if freed else "no change"
        note(f"  {what}: {suffix} (now {now} MB)")

        if status != 0:
            note(f"    exit {status}: {last}")
            if what.startswith("apt") and "not found" in (out or ""):
                note("    no apt on this system; skipping")
            else:
                note("    continuing; the trim below is what decides")
        previous = now

    note("  releasing the free blocks (fstrim)")
    status, out = ssh(port, TRIM, timeout=180)
    trimmed = ""
    for line in (out or "").splitlines():
        text = line.strip()
        if not text or text.startswith("Warning: Permanently added"):
            continue
        note(f"    {text[:150]}")
        if "trimmed" in text:
            trimmed = text

    if status != 0 or not trimmed:
        note("=== fstrim RELEASED NOTHING ===")
        note("  the guest either does not support discard or the drive does not pass it on.")
        note("  The correct fallback is zerofree - it reads the block bitmap and zeroes the")
        note("  blocks that are free AND non-zero, which is megabytes, not the 100+ GB that")
        note("  writing the whole free space costs on a 128 GB disk.")
        if disk:
            note(f"  the image was NOT shrunk: {disk}")
        return False

    end = _used(port)
    note(f"  freed by deleting: {start - previous} MB")
    note(f"  released by trim:  {trimmed}")
    if end is not None:
        note(f"  used after:        {end} MB")
    return True
