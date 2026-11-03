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

# --- the toolchain ----------------------------------------------------------
#
# pkg is FreeBSD's package manager and it needs a bootstrap on a fresh install:
# /usr/sbin/pkg is a stub that downloads the real one. Doing it here means the disk is
# ready to install things without a network round trip in a later stage that has less
# time.
ASSUME_ALWAYS_YES=yes pkg bootstrap -f

# WHAT A RUNNER NEEDS, as one list. git and curl are the basics; python3 and node are the
# two runtimes a runner reaches for; jq and unzip are what half the actions assume exists.
# build tools come with base (cc, make) so they are not here.
pkg install -y \
    git \
    curl \
    ca_root_nss \
    python3 \
    node18 \
    npm-node18 \
    jq \
    unzip \
    bash \
    sudo

# --- the disk says what made it ---------------------------------------------
echo 'built by easydiskbake bsd-freebsd' > /etc/easydiskbake

# THE INSTALLER REBOOTS AFTER THIS SCRIPT, by itself. Nothing here powers the machine
# off: a poweroff in the post-install chroot leaves the boot blocks unwritten on some
# versions, and a disk that does not boot is worse than one that boots to a prompt.
echo "=== easydiskbake: done ==="
