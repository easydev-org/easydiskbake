"""Clean a FreeBSD guest over ssh, before its disk is exported.

    from lib.bsdclean import clean
    ok = clean(port=2222, note=log)

WHY THIS IS A SEPARATE MODULE AND NOT A `-kind` FLAG ON linclean

The two share exactly one step - write zeros over the free space, because `qemu-img convert`
only skips clusters that are ALREADY ZERO - and nothing else. The package manager is not apt,
the logs are not journald, the paths are not /var/lib/apt or /home/*/.cache, and a shared
module with a `if kind ==` in every step would be a module where every step has two branches
and neither is tested by the other's runs.

WHAT IS THE SAME, AND WHY IT STILL LIVES HERE RATHER THAN SHARED

The measuring is the same shape: `df` before and after each step, and the two final numbers
kept apart - freed by deleting, and written as zeros. Those are different quantities and
reporting one "freed" over both is how a clean that removed 200 MB once read as one that
removed 16 GB.
"""

from __future__ import annotations

# THE COMMANDS, IN ORDER, ONE STATEMENT EACH. Sent through `ssh <host> "..."`, which is a
# shell on the guest, so each is a complete command line.
STEPS: list[tuple[str, str]] = [
    # THE PKG CACHE. `pkg clean -a` removes every downloaded package file; without -a it asks
    # for confirmation, which there is no terminal to give.
    ("pkg cache", "sudo pkg clean -a -y"),

    # The index and the metadata pkg keeps. Regenerated from the repository on the next
    # `pkg update`, which is one command for whoever needs it.
    ("pkg db cache", "sudo rm -rf /var/db/pkg/repo-*.sqlite /var/cache/pkg/*"),

    # FreeBSD keeps every rotation in /var/log by default and a bsdinstall leaves the whole
    # install transcript there.
    ("logs",
     "sudo rm -rf /var/log/bsdinstall_log /var/log/installer* /var/log/*.gz "
     "/var/log/*.[0-9] /var/log/messages.* /var/log/security.* /var/log/maillog.*"),
    ("large logs",
     "sudo find /var/log -type f -size +1M -exec truncate -s 0 {} + 2>/dev/null; true"),

    ("temp", "sudo rm -rf /tmp/* /var/tmp/*"),

    # The bsdinstall answer file and the work directory it leaves, which describe how the
    # machine was built to whoever boots it.
    ("installer traces",
     "sudo rm -rf /tmp/bsdinstall* /var/db/bsdinstall* /root/.bsdinstall*"),

    ("history",
     "sudo rm -f /root/.history /home/*/.history /root/.sh_history /home/*/.sh_history"),

    # The entries this pipeline wrote when it logged in.
    ("ssh traces", "sudo rm -f /root/.ssh/known_hosts /home/*/.ssh/known_hosts"),

    # FreeBSD's own caches.
    ("caches", "sudo rm -rf /root/.cache /home/*/.cache; true"),
]

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


# NO ZEROING. FREEBSD RELEASES ITS OWN BLOCKS.
#
# The reason is the same one lib/linclean.py records at length: writing zeros over the free
# space was how this made `qemu-img convert` skip deleted files' blocks, and on a 128 GB disk it
# costs the whole free space in writes and re-allocates clusters that were never allocated.
#
# `fstrim` DOES NOT EXIST ON FREEBSD, AND THIS MODULE CALLED IT FOR ONE RUN.
#
# fstrim(8) is util-linux. FreeBSD's base system has no such command, and run 41835299937 said
# so in one line, after a completely successful install:
#
#     [clean-step 13:14:23]     timeout: exec(fstrim): No such file or directory
#     [clean-step 13:14:23] === fstrim RELEASED NOTHING ===
#
# The `timeout` wrapper printed that, which is why it read as a timeout rather than as a missing
# binary. The fallback message then named zerofree - which is also Linux-only. Both halves of
# that advice were wrong for this system.
#
# WHAT FREEBSD HAS IS `fsck_ffs -E`, and it is the same operation by a different name. From the
# fsck_ffs(8) manual for 15.1:
#
#     -E   Clear unallocated blocks, notifying the underlying device that they are not used and
#          that their contents may be discarded. This is useful for filesystems which have been
#          mounted on systems without TRIM support, or with TRIM support disabled, as well as
#          filesystems which have been copied from one device to another.
#
# and from pass5.c, during the cylinder-group scan, every run of free fragments is passed to
#
#     clear_blocks() -> blerase() -> ioctl(fd, DIOCGDELETE, {offset, size})
#
# so the discard reaches the block device, and `discard=unmap` on the qemu -drive drops the
# clusters. Nothing is written: the blocks are already free and blerase only tells the device so.
#
# IT WORKS ON A MOUNTED FILE SYSTEM, which is what makes it usable here. The manual says so
# ("useful for filesystems which have been mounted ... with TRIM support disabled"), and the
# kernel keeps a mounted UFS consistent during the check. The one thing it does not do is
# report bytes, unlike fstrim's `-v`: fsck_ffs prints a summary line of files, used and free
# blocks. So the evidence is that line plus the image size afterwards, and the caller measures
# the image itself.
#
#     fsck_ffs -y -E "$(df / | awk 'NR==2 {print $1}')"
#
# `-y` IS WHAT MAKES IT NOT HANG. Without it fsck_ffs is interactive: it prompts before each
# correction, and on a mounted read-write root it always finds something to correct. The ssh
# here allocates no tty, so a prompt reads EOF and the command's behaviour depends on how
# reply() treats a closed stdin - which is not a thing to rely on in a pipeline. `-y` answers
# every question in advance. It is the man page's "free license to continue after essentially
# unlimited trouble", and that is the correct trade for a scratch image rebuilt from an ISO.
#
# NOT `-p`. Preen mode skips a file system marked clean, which is exactly the file system this
# needs to erase blocks on: a bsdinstall leaves it clean, and the run would print
# "FILE SYSTEM CLEAN; SKIPPING CHECKS" and release nothing while looking like success.
#
# THE DEVICE IS READ FROM `df`, NOT ASSUMED. A bsdinstall with PARTITIONS=DEFAULT labels the
# root `gpt/rootfs` on this image, and that is what the console shows - but the label is a
# property of the installer, not of this command, and a name that does not exist produces
# "could not find special device", which reads as a permissions error rather than as a wrong
# path. `df /` reports what is actually mounted there.
#
# THE WHOLE LINE IS ONE REMOTE SHELL COMMAND. lib/session.ssh builds an argv list, so the local
# shell never sees this string and the quoting below is the remote sh's, not Python's.
TRIM = (
    "dev=$(df / | awk 'NR==2 {print $1}'); "
    "echo \"root filesystem: $dev\"; "
    "sudo fsck_ffs -y -E \"$dev\" 2>&1"
)


def clean(*, port: int, note=print, ssh_seconds: int = 900,
          disk: str | None = None) -> bool:
    """Run the steps over ssh, then trim the free blocks. True when the trim worked."""
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
            note("    continuing; the trim below is what decides")
        previous = now

    # `fsck_ffs -E` REPORTS NO BYTES, SO THE EVIDENCE IS DIFFERENT.
    #
    # fstrim -v prints "trimmed 116.3 GiB", which is why the Linux cleaner's contract is "the
    # trim reported bytes or the clean failed". fsck_ffs prints a summary of files, used and
    # free blocks instead - the same line it prints after any check:
    #
    #     123456 files, 1234567 used, 20000000 free (1234 frags, 5678 blocks, 0.0% fragmentation)
    #
    # and its exit status is 0 on success or 4 when a mounted file system was modified. Both
    # mean the free blocks were erased; only 4 says the check also wrote metadata, which is
    # normal on a live root and is why the guest is shut down properly afterwards.
    note("  releasing the free blocks (fsck_ffs -E)")
    status, out = ssh(port, TRIM, timeout=600)
    released = ""
    summary = ""
    for line in (out or "").splitlines():
        text = line.strip()
        if not text or text.startswith("Warning: Permanently added"):
            continue
        note(f"    {text[:170]}")
        if text.startswith("root filesystem:"):
            released = text
        elif "free" in text and "used" in text:
            summary = text

    # 4 IS SUCCESS WITH A WRITE. See the note above: it is what fsck_ffs returns when the
    # file system was mounted read-write and the check changed something.
    if status not in (0, 4) or not released:
        note(f"=== fsck_ffs -E DID NOT RUN (exit {status}) ===")
        note("  if it could not find the device or said 'NOT A DISK DEVICE', the root")
        note("  filesystem is not where this expected it and the command above names what")
        note("  it was given; if it printed an I/O error, the disk itself is the problem.")
        if disk:
            note(f"  the image was NOT shrunk: {disk}")
        return False

    end = _used(port)
    note(f"  freed by deleting: {start - previous} MB")
    note(f"  erased by trim:    {released}")
    if summary:
        note(f"  the filesystem:    {summary}")
    if end is not None:
        note(f"  used after:        {end} MB")
    return True
