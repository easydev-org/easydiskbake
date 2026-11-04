#!/bin/sh
# Clean a freshly installed Linux guest before its disk is exported.
#
# RUN INSIDE THE INSTALLED SYSTEM, as the last thing before poweroff: either through
# `curtin in-target` from Ubuntu's autoinstall late-commands, or at the end of NixOS's
# installation.
#
# WHY THE ZEROING IS THE PART THAT MATTERS
#
# lib/compress.py runs `qemu-img convert -O qcow2`, and convert only skips clusters that are
# ALREADY ZERO. Deleting a file marks its blocks free and leaves the bytes where they were,
# so convert stores them and the qcow2 comes out the same size as before. Writing zeros over
# the free space is what makes those blocks zero when convert looks.
#
# Everything before that step is what CREATES the free space. A disk that skips this ends up
# the size of its high-water mark, not the size of what it contains - which is the whole
# difference between a 6 GB image and a 14 GB one.
#
# WHAT IS KEPT
#
# Only caches, package lists, logs, temp files and history go. Nothing here touches an
# installed package, the kernel, the bootloader, the account, or sshd. The disk still has to
# boot and answer ssh after this has run, and the bake's own launch stage is what proves it.

set -u

say() { echo "  $*"; }

echo "=== linux-clean ==="
before=$(df -m / | awk 'NR==2 {print $3}')
say "used before: ${before} MB"

# ---------------------------------------------------------------- package caches
#
# THE DEB CACHE IS THE BIGGEST SINGLE ITEM after a desktop install: every .deb that was
# fetched stays in /var/cache/apt/archives, and a Kubuntu install leaves most of a gigabyte
# there. `clean` keeps the lists, `autoclean` removes only superseded packages - neither is
# what is wanted, so the archives themselves go.
if command -v apt-get >/dev/null 2>&1; then
    echo "--- apt ---"
    apt-get clean || say "apt-get clean failed"
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin
    say "archives and lists removed"
fi

# NixOS has its own store and this script is not for it - nix-collect-garbage is, and
# linux/nixos/bake.py runs it. Saying so is better than silently doing nothing.
if [ -e /nix/store ]; then
    say "nix store present; this script does not touch it"
fi

# ---------------------------------------------------------------- logs
#
# journald is capped by its config, not by its content, and a fresh install still writes
# tens of megabytes of it. The vacuum keeps the last day so a support question has something
# to read.
if command -v journalctl >/dev/null 2>&1; then
    echo "--- journal ---"
    journalctl --rotate 2>/dev/null || true
    journalctl --vacuum-time=1d 2>/dev/null || true
fi
rm -rf /var/log/*.gz /var/log/*.1 /var/log/installer /var/log/apt/term.log* 2>/dev/null || true
find /var/log -type f -size +1M -exec truncate -s 0 {} + 2>/dev/null || true
say "logs truncated"

# ---------------------------------------------------------------- temp and caches
echo "--- temp ---"
rm -rf /tmp/* /var/tmp/* 2>/dev/null || true
rm -rf /root/.cache/* /home/*/.cache/* 2>/dev/null || true
rm -rf /var/crash/* 2>/dev/null || true
say "temp cleared"

# ---------------------------------------------------------------- what the installer left
#
# The autoinstall's own record, and the installer's log directory. Neither is needed on the
# finished disk, and both describe how the machine was built to whoever boots it.
rm -rf /var/log/installer /var/lib/ubiquity 2>/dev/null || true
rm -f /etc/easydiskbake.installer-log 2>/dev/null || true

# ---------------------------------------------------------------- shell history
echo "--- history ---"
rm -f /root/.bash_history /root/.zsh_history /root/.python_history 2>/dev/null || true
for h in /home/*/.bash_history /home/*/.zsh_history; do
    [ -e "$h" ] && rm -f "$h"
done
say "history removed"

# ---------------------------------------------------------------- ssh trace of this run
rm -f /root/.ssh/known_hosts /home/*/.ssh/known_hosts 2>/dev/null || true

# ---------------------------------------------------------------- zero the free space
#
# THE STEP THAT DECIDES THE ARTIFACT'S SIZE. Write zeros into one file until the disk is
# nearly full, then delete it: every block that was free now holds zeros, so qemu-img
# convert can skip them.
#
# `sync` before reading df, because the writes have to be accounted for before the size is
# measured; and a 200 MB margin so the write does not fail on its last block and leave a
# zero-length file behind while looking successful.
echo "--- zeroing free space ---"
free_mb=$(df -m / | awk 'NR==2 {print $4}')
say "free before: ${free_mb} MB"
if [ "${free_mb:-0}" -gt 500 ]; then
    target=$((free_mb - 200))
    say "writing ${target} MB of zeros"
    # dd is given an exact count so a full disk cannot make it spin; `status=none` keeps the
    # log readable, and the exit code is reported rather than assumed.
    if dd if=/dev/zero of=/zero.fill bs=1M count="${target}" status=none 2>/dev/null; then
        sync
        rm -f /zero.fill
        say "zeroed and removed"
    else
        # A PARTIAL WRITE IS STILL USEFUL - the blocks it reached are zero now - so this is
        # reported and not treated as a failure of the whole clean.
        sync
        rm -f /zero.fill
        say "dd stopped early (disk filled); the blocks it reached are zeroed"
    fi
else
    say "not enough free space to bother"
fi

after=$(df -m / | awk 'NR==2 {print $3}')
say "used after: ${after} MB"

echo "CLEAN_DONE"
