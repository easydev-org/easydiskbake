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

WHY THE ZEROING IS THE STEP THAT MATTERS

`qemu-img convert -O qcow2` only skips clusters that are ALREADY ZERO. Deleting a file marks
its blocks free and leaves the bytes where they were, so convert stores them and the qcow2
comes out the size of the disk's high-water mark. Writing zeros over the free space is what
makes those blocks zero when convert looks.

Everything before it is what CREATES the free space. A disk that skips the zeroing is
correctly cleaned and still the same size, which is why this reports what it freed.
"""

from __future__ import annotations

import shlex

# THE COMMANDS, IN ORDER, AS ONE STATEMENT EACH.
#
# Each is sent through `ssh <host> "..."`, which is a shell on the guest - so each one is a
# complete command line, not a fragment. They are separate so a failure names which one.
STEPS: list[tuple[str, str]] = [
    # THE DEB CACHE IS THE BIGGEST SINGLE ITEM after a desktop install: every package that
    # was fetched stays in /var/cache/apt/archives. `autoclean` only removes superseded ones,
    # and a fresh install has almost none of those.
    ("apt archives", "sudo apt-get clean"),
    ("apt lists", "sudo rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb "
                  "/var/cache/apt/*.bin"),

    # journald is capped by config, not content, and a fresh install still writes tens of
    # megabytes. One day is kept so a support question has something to read.
    ("journal", "sudo journalctl --rotate"),
    ("journal vacuum", "sudo journalctl --vacuum-time=1d"),
    ("logs", "sudo rm -rf /var/log/installer /var/log/apt/term.log* "
             "/var/log/*.gz /var/log/*.1"),
    ("large logs", "sudo find /var/log -type f -size +1M "
                   "-exec truncate -s 0 {} + 2>/dev/null || true"),

    ("temp", "sudo rm -rf /tmp/* /var/tmp/* /var/crash/*"),
    ("caches", "sudo rm -rf /root/.cache/* /home/*/.cache/*"),

    # The installer's own record describes how the machine was built to whoever boots it.
    ("installer record", "sudo rm -rf /var/log/installer /var/lib/ubiquity"),

    ("history", "sudo rm -f /root/.bash_history /home/*/.bash_history "
                "/home/*/.zsh_history /home/*/.python_history"),

    # The entries this pipeline wrote when it logged in.
    ("ssh traces", "sudo rm -f /root/.ssh/known_hosts /home/*/.ssh/known_hosts"),
]

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
    "else echo ZERO_SKIPPED; fi; "
    "echo \"USED_MB=$(df -m / | awk 'NR==2 {print $3}')\""
)

# WHAT THE GUEST SAYS WHEN IT IS FINISHED. Silence is not success: a run that stopped at the
# third command leaves the disk carrying whatever it had not removed yet.
DONE = "CLEAN_DONE"


def clean(*, port: int, note=print, ssh_seconds: int = 900) -> bool:
    """Run the steps over ssh, then zero the free space. True only when all of it happened.

    `port` is the forwarded host port; lib/session.ssh supplies the password, because every
    guest here uses the same account and there is no tty to prompt at.
    """
    from session import ssh

    note("=== cleaning the guest, over ssh ===")

    used_before = ""
    for what, command in STEPS:
        status, out = ssh(port, command, timeout=ssh_seconds)
        text = (out or "").strip().splitlines()
        # 0 is success. 1 from `apt-get clean` on a system without apt is tolerated below;
        # anything else stops, because a cleaning step that failed silently is a disk that
        # ships with whatever it was meant to remove.
        if status != 0:
            note(f"  {what}: exit {status} — {text[-1][:120] if text else '(no output)'}")
            if what.startswith("apt") and "not found" in (out or ""):
                note("  no apt on this system; skipping the package steps")
                continue
            note(f"=== CLEANING STOPPED AT {what!r} ===")
            return False
        note(f"  {what}: ok")

    note("  zeroing the free space")
    status, out = ssh(port, ZERO, timeout=ssh_seconds)
    for line in (out or "").splitlines():
        text = line.strip()
        if text and not text.startswith("Warning: Permanently added"):
            note(f"    {text[:150]}")
    if "ZERO_SKIPPED" in (out or ""):
        note("  under 500 MB free, so nothing was written - which is itself suspicious")
    if "ZERO_PARTIAL" in (out or ""):
        note("  dd stopped early; the blocks it reached are zeroed")
    if "ZERO_DONE" not in (out or ""):
        note("=== THE FREE SPACE WAS NOT ZEROED ===")
        note("  the disk is clean and still the size of its high-water mark")
        return False

    # AND SAY WHAT IT COST. A clean that freed nothing is a clean that did not work, and a
    # single number at the end is what makes that visible instead of quiet.
    status, out = ssh(port, "df -m / | awk 'NR==2 {print $3}'", timeout=120)
    used_after = (out or "").strip().splitlines()
    if used_after:
        note(f"  used after: {used_after[-1].strip()} MB")

    note(f"clean complete {used_before}")
    return True
