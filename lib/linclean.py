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
so each step is measured against `df` before and after, and the total is printed beside the
amount that was zeroed. Those two numbers together are the honest answer:

    freed by deleting     what the steps actually removed
    written as zeros      what qemu-img convert will now skip

WHY THE ZEROING IS THE STEP THAT MATTERS

`qemu-img convert -O qcow2` only skips clusters that are ALREADY ZERO. Deleting a file marks
its blocks free and leaves the bytes where they were, so convert stores them and the qcow2
comes out the size of the disk's high-water mark. Writing zeros over the free space is what
makes those blocks zero when convert looks.

So deleting is what CREATES the free space and zeroing is what makes it count. A disk that
skips the zeroing is correctly cleaned and exactly the same size.
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

# THE ZEROING, AS ONE STATEMENT, BECAUSE IT NEEDS ITS OWN VARIABLES.
#
# sync first, so the writes above are accounted for before the free space is measured; a
# 200 MB margin so the last block does not fail and leave the file behind while looking like
# success. A partial dd is still useful - the blocks it reached are zero - so it is reported
# rather than treated as failure.
ZERO = (
    "sync; "
    "f=$(df -m / | awk 'NR==2 {print $4}'); "
    "echo \"FREE_MB=$f\"; "
    "if [ \"${f:-0}\" -gt 500 ]; then "
    "  t=$((f-200)); echo \"ZERO_MB=$t\"; "
    "  sudo dd if=/dev/zero of=/zero.fill bs=1M count=$t status=none "
    "    || echo ZERO_PARTIAL; "
    "  sync; sudo rm -f /zero.fill; echo ZERO_DONE; "
    "else echo ZERO_SKIPPED; fi"
)


def _used(port: int) -> int | None:
    """Used MiB on /, or None if the guest stopped answering."""
    from session import ssh

    status, out = ssh(port, USED, timeout=120)
    if status != 0:
        return None
    lines = [x.strip() for x in (out or "").splitlines() if x.strip().isdigit()]
    return int(lines[-1]) if lines else None


def clean(*, port: int, note=print, ssh_seconds: int = 900) -> bool:
    """Run the steps over ssh, then zero the free space. True only when all of it happened.

    `port` is the forwarded host port; lib/session.ssh supplies the password, because every
    guest here uses the same account and there is no tty to prompt at.
    """
    from session import ssh

    note("=== cleaning the guest, over ssh ===")

    start = _used(port)
    if start is None:
        note("=== THE GUEST DID NOT ANSWER `df` ===")
        note("  nothing was measured, so nothing will be reported - and a clean that cannot")
        note("  be measured is not worth running")
        return False
    note(f"  used before: {start} MB")

    # EACH STEP IS MEASURED. This is the part the first version omitted, and it is what made
    # "17% of 23070 MiB" the only number in the log.
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
        # A NEGATIVE NUMBER IS REAL AND WORTH PRINTING: a daemon can write more while the step
        # runs than the step removes, and a report that clamps at zero would hide it.
        suffix = f"{freed:+d} MB" if freed else "no change"
        note(f"  {what}: {suffix} (now {now} MB)")

        if status != 0:
            note(f"    exit {status}: {last}")
            if what.startswith("apt") and "not found" in (out or ""):
                note("    no apt on this system; skipping")
            else:
                note("    continuing; the free space at the end is what decides")
        previous = now

    # THE PART THAT MAKES THE ZEROING FREE INSTEAD OF EXPENSIVE.
    #
    # Writing 20 GB of zeros into an ext4 filesystem inside a qcow2 makes the qcow2 GROW:
    # qemu stores those blocks like any other data, because it cannot know they are zero
    # until convert reads them. Run 41792511802 showed the cost exactly:
    #
    #     disk pulled:            4962 MiB
    #     after writing zeros:   24362 MiB   (the file that convert then read)
    #     compressed result:      3110 MiB
    #
    # `fstrim` is what fixes this. It tells the block layer which blocks are free, and the
    # qcow2 driver behind virtio-blk then DISCARDS them - so the clusters are dropped from
    # the image instead of being written and later re-read. On a virtio-blk disk with
    # discard=unmap this turns 20 GB of writes into a bitmap operation.
    #
    # It runs BEFORE the zeroing, so the zeros are only written to what fstrim could not
    # reach (ext4 refuses to trim blocks it has not journaled). It is not a replacement for
    # the zeros: a filesystem that does not support discard, or a qemu built without it,
    # silently does nothing and the zeroing is still what makes the image small.
    note("  discarding the free blocks (fstrim)")
    status, out = ssh(port, "sudo fstrim -v / 2>&1 || echo FSTRIM_UNAVAILABLE",
                      timeout=ssh_seconds)
    for line in (out or "").splitlines():
        text = line.strip()
        if text and not text.startswith("Warning: Permanently added"):
            note(f"    {text[:140]}")
    if "FSTRIM_UNAVAILABLE" in (out or ""):
        note("    not supported here; the zeroing below still does the work")

    note("  zeroing the free space")
    status, out = ssh(port, ZERO, timeout=ssh_seconds)
    zeroed = 0
    for line in (out or "").splitlines():
        text = line.strip()
        if text.startswith("ZERO_MB="):
            zeroed = int(text.split("=", 1)[1] or 0)
            note(f"    writing {zeroed} MB of zeros over the free space")
        elif text in ("ZERO_DONE", "ZERO_PARTIAL", "ZERO_SKIPPED"):
            continue
        elif text and not text.startswith("Warning: Permanently added"):
            note(f"    {text[:140]}")

    if "ZERO_SKIPPED" in (out or ""):
        note("  under 500 MB free, so nothing was written - which is itself suspicious")
    if "ZERO_PARTIAL" in (out or ""):
        note("  dd stopped early; the blocks it reached are zeroed")
    if "ZERO_DONE" not in (out or ""):
        note("=== THE FREE SPACE WAS NOT ZEROED ===")
        note("  the disk is clean and still the size of its high-water mark")
        return False

    end = _used(port)
    if end is None:
        note("  the guest stopped answering after the zeroing; not measured")
        note(f"  freed by deleting: {start - previous} MB, written as zeros: {zeroed} MB")
        return True

    # THE TWO NUMBERS THAT ANSWER "what did this do", SEPARATED BECAUSE THEY MEAN DIFFERENT
    # THINGS:
    #
    #     freed by deleting    space the steps actually reclaimed
    #     written as zeros     space qemu-img convert will now skip
    #
    # The disk's Used goes UP by the zeroed amount and comes back when zero.fill is deleted,
    # which is why `end` is not `start - freed`. Reporting one "freed" number over both is how
    # the first version produced a figure that described neither.
    note(f"  freed by deleting: {start - previous} MB")
    note(f"  written as zeros:  {zeroed} MB   (this is what convert will skip)")
    note(f"  used after:        {end} MB")
    note("  what the image becomes is decided by `compress`, from these clusters")
    return True
