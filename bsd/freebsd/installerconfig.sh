#!/bin/sh
# Runs INSIDE the newly installed FreeBSD, chrooted, before bsdinstall exits.
#
# THIS IS THE SECOND HALF OF /etc/installerconfig, AND IT IS THE HALF THAT MATTERS HERE.
# The preamble above it (in linux/../bsd/freebsd/installerconfig) partitions the disk and
# unpacks the distribution; this script is what makes the result a disk this repository
# can use: sshd on, the account every image here uses, and the toolchain a GitHub Actions
# runner expects.
#
# NOTHING HERE TALKS TO THE HOST. bsdinstall runs the whole installation from the media
# and then runs this; there is no marker to notice and no second keystroke to wait for.
# That is the same rule the NixOS bake follows, and for the same reason: a bake that
# depends on the host noticing something has a failure mode that looks like a hang.
set -eu

echo "=== easydiskbake: configuring the installed FreeBSD ==="

# --- accounts ---------------------------------------------------------------
#
# THE SAME ACCOUNT AS EVERY OTHER IMAGE HERE. docker with admin as the password, and
# wheel so sudo works - a runner that has to escalate with a password it does not have
# is a runner that fails at the first apt/pkg call.
#
# `pw` is the FreeBSD-native user tool. `-m` creates the home directory and `-s` sets the
# shell, both of which matter: without a shell the account exists and cannot log in over
# ssh, which reads afterwards as an ssh failure.
pw useradd docker -m -s /bin/sh -G wheel -c "easydiskbake"
echo 'admin' | pw usermod docker -h 0

# ROOT CAN LOG IN TOO, and that is deliberate. This disk has no keys and no network
# exposure beyond a forwarded port somebody started on purpose; a root that cannot log in
# out of the box buys nothing and costs an emergency-console recovery when a stage has to
# fix something. Every other image here makes the same trade.
echo 'admin' | pw usermod root -h 0

# --- ssh --------------------------------------------------------------------
#
# PASSWORD AUTHENTICATION, because that is how the next stage logs in - it has the
# password and does not have a key. PermitRootLogin stays at its default (prohibit-
# password would defeat the account above).
sysrc sshd_enable=YES
sysrc ifconfig_DEFAULT=DHCP
echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config

# --- network ----------------------------------------------------------------
#
# SCRIPT MODE NEVER RUNS netconfig, SO THE INSTALLER HAS NO NAME RESOLUTION.
#
# bsdinstall's `auto` target asks the user for a network configuration and writes it into
# the new system; `script` mode skips that menu entirely, and nothing else does it. The
# result is a guest with a virtio NIC, an empty /etc/resolv.conf and no route - so any
# command that reaches the internet fails with:
#
#     pkg: Error fetching https://pkg.FreeBSD.org/...: Non-recoverable resolver failure
#
# Run 41638444422 hit exactly that. Its pkgbase install had COMPLETED - 214 of 214
# packages unpacked, bootconfig and config done - and then this script's `pkg bootstrap`
# died on DNS, and `set -e` took the whole post-install with it.
#
# dhclient on the interface is what the installer's own netconfig would have run. The name
# is looked up rather than assumed: vtnet0 on a VM, em0 on some images, and a hardcoded
# name is a bug that only shows on the machine that is not this one.
iface=$(ifconfig -l | tr ' ' '\n' | grep -vE '^(lo|enc|bridge|pflog|pfsync)' | head -1)
if [ -n "$iface" ]; then
    echo "=== easydiskbake: dhcp on $iface ==="
    dhclient "$iface" || echo "  dhclient failed; the disk still boots, it just has no network"
    # resolv.conf is what actually missing: dhclient writes it, but a failed lease leaves
    # the file absent and every pkg call then reports a resolver failure rather than a
    # network one. Naming the host's resolver is the fallback QEMU's user-mode network
    # always answers on.
    if [ ! -s /etc/resolv.conf ]; then
        echo 'nameserver 10.0.2.3' > /etc/resolv.conf
    fi
    for i in 1 2 3 4 5; do
        if ping -c 1 -W 2 10.0.2.2 >/dev/null 2>&1; then break; fi
        sleep 2
    done
fi

# --- the toolchain ----------------------------------------------------------
#
# NO `pkg bootstrap` HERE, AND ITS ABSENCE IS THE FIX FOR RUN 41638444422.
#
# A distset install needs it: /usr/sbin/pkg is a stub and the real pkg is downloaded. A
# PKGBASE install does not, because pkg is one of the packages - it was [214/214] in that
# run's log:
#
#     [214/214] Extracting pkg-2.6.2_1: 100%
#
# So `pkg bootstrap -f` was re-downloading a package the installer had just written, and
# against a guest with no resolver it failed - which, under `set -e`, ended the script
# before sshd, the account or any of the toolchain below had been configured.
#
# WHAT A RUNNER NEEDS, as one list. git and curl are the basics; python3 and node are the
# two runtimes a runner reaches for; jq and unzip are what half the actions assume exists.
# build tools come with base (cc, make) so they are not here.
#
# A FAILED pkg INSTALL IS NOT FATAL ANY MORE. The disk is already a working system at this
# point - sshd and the account are below and do not need the network - and a bake that
# throws away a complete FreeBSD install because a mirror was unreachable is worse than one
# that ships a disk whose toolchain a later stage can add. The failure is printed either
# way.
# node22, NOT node18, AND THE COMMENT IS ABOVE THE COMMAND FOR A REASON.
#
# `node18` and `npm-node18` are gone from the quarterly repository. Run 41647564426's log
# says so in two lines, after the DHCP lease and three successful repository updates had
# already proved the network was fine:
#
#     pkg: No packages available to install matching 'node18' have been found
#     pkg: No packages available to install matching 'npm-node18' have been found
#
# node22 is what the repository carries now; npm comes with it, so the separate npm-nodeXX
# entry is gone as well.
#
# THE COMMENT USED TO SIT INSIDE THE CONTINUATION, between `python3 \` and `node22 \`,
# and that broke the command in a way that reads as a missing package rather than a broken
# line. A backslash continues to the NEXT PHYSICAL LINE - a `#` comment there ends the
# command, so `pkg install -y git curl ca_root_nss python3` ran on its own and `node22`
# became a separate command that does not exist:
#
#     /tmp/installscript: node22: not found
#     === easydiskbake: pkg install failed; the base system is still complete ===
#
# That is run 41660262298, which also showed why the `|| echo` from the previous commit
# matters: the disk was a complete FreeBSD with no toolchain, instead of no disk at all.
pkg install -y \
    git \
    curl \
    ca_root_nss \
    python3 \
    node22 \
    jq \
    unzip \
    bash \
    sudo \
    || echo "=== easydiskbake: pkg install failed; the base system is still complete ==="

# --- the disk says what made it ---------------------------------------------
echo 'built by easydiskbake bsd-freebsd' > /etc/easydiskbake

# THE INSTALLER REBOOTS AFTER THIS SCRIPT, by itself. Nothing here powers the machine
# off: a poweroff in the post-install chroot leaves the boot blocks unwritten on some
# versions, and a disk that does not boot is worse than one that boots to a prompt.
echo "=== easydiskbake: done ==="
