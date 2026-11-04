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

# THE ZEROING. Same contract as linclean.ZERO: sync first so the writes above are accounted
# for, a 200 MB margin so the last block does not fail while looking like success, and a
# partial dd reported rather than treated as failure - the blocks it reached are zero.
# NO ZEROING. FREEBSD RELEASES ITS OWN BLOCKS.
#
# The reason is the same one lib/linclean.py records at length: writing zeros over the free
# space was how this made `qemu-img convert` skip deleted files' blocks, and on a 128 GB disk it
# costs the whole free space in writes and re-allocates clusters that were never allocated.
#
# FreeBSD has the trim: `kern.cam.da.*.delete_method` is set to `DISABLE` on some images, so the
# command is run with the sysctl set first - and if the trim releases nothing the caller is told
# so, with the fallback named.
#
#     fstrim -v /              fstrim(8), in the base system since FreeBSD 10.4
#
# `-v` prints the bytes released, which is the only evidence that it did anything.

TRIM = "sudo sysctl kern.cam.da.0.delete_method=DISABLE >/dev/null 2>&1; sudo timeout 120 fstrim -v / 2>&1"


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
        note("  FreeBSD disables the delete method on some virtual disks; if the trim above")
        note("  reported bytes, the image will be small, and if it did not, the correct")
        note("  fallback is zerofree - not a dd over the whole free space.")
        if disk:
            note(f"  the image was NOT shrunk: {disk}")
        return False

    end = _used(port)
    note(f"  freed by deleting: {start - previous} MB")
    note(f"  released by trim:  {trimmed}")
    if end is not None:
        note(f"  used after:        {end} MB")
    return True
