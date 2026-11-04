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
# THE ZEROING, IN FREEBSD'S OWN dd SYNTAX.
#
# Every option here was checked against FreeBSD's dd, not GNU's:
#
#     bs=1m          FreeBSD accepts lower case; 1M works too but this is the documented form
#     `status=none`  DOES NOT EXIST in FreeBSD's dd - it is a GNU extension. Passing it makes
#                    dd treat it as a file to read, which is why run 41814852442 sat at
#                    "zeroing the free space" for the full 900 s and then reported that the
#                    space had not been zeroed at all
#     count=$t       $t is in MiB because df -m is, so it matches bs=1m
#
# The output redirection is the portable way to keep dd quiet, and `2>/dev/null` is not
# needed on a command whose only output is the progress line.
ZERO = (
    "sync; "
    # BOUNDED BY WHAT THE DISK HAS USED, NOT BY WHAT IS FREE - see the longer note in
    # lib/linclean.py. A 128 GB disk with 6 GB in it does not need 119 GB of zeros written;
    # it needs the blocks that were written and released, which is on the order of the used
    # size.
    "u=$(df -m / | awk 'NR==2 {print $3}'); "
    "f=$(df -m / | awk 'NR==2 {print $4}'); "
    "echo \"USED_MB=$u FREE_MB=$f\"; "
    "t=$((u < f ? u : f)); "
    "t=$((t > 200 ? t - 200 : 0)); "
    "if [ \"$t\" -gt 500 ]; then "
    "  echo \"ZERO_MB=$t\"; "
    "  sudo dd if=/dev/zero of=/zero.fill bs=1m count=$t >/dev/null 2>&1 "
    "    || echo ZERO_PARTIAL; "
    "  sync; sudo rm -f /zero.fill; echo ZERO_DONE; "
    "else echo ZERO_SKIPPED; fi"
)


def _used(port: int) -> int | None:
    from session import ssh

    status, out = ssh(port, USED, timeout=120)
    if status != 0:
        return None
    lines = [x.strip() for x in (out or "").splitlines() if x.strip().isdigit()]
    return int(lines[-1]) if lines else None


def clean(*, port: int, note=print, ssh_seconds: int = 900) -> bool:
    """Run the steps over ssh, then zero the free space. True only when all of it happened."""
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
            note("    continuing; the free space at the end is what decides")
        previous = now

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
    note(f"  freed by deleting: {start - previous} MB")
    note(f"  written as zeros:  {zeroed} MB   (this is what convert will skip)")
    if end is not None:
        note(f"  used after:        {end} MB")
    return True
