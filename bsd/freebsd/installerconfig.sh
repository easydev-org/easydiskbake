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

# SUDO MUST NOT ASK FOR A PASSWORD, BECAUSE NOTHING CAN TYPE ONE.
#
# The comment above says why wheel was chosen, but no sudoers file was ever written - so
# `sudo pkg clean` on this disk waits at "Password:" and the ssh session has no terminal to
# answer with. It reads as a hung clean, not as a missing configuration.
#
# Every other image here already does this: Ubuntu's autoinstall writes
# /etc/sudoers.d/90-docker-nopasswd, and NixOS gives the account wheel plus
# security.sudo.wheelNeedsPassword = false. This is the same line, in the file format FreeBSD
# uses. The mode is 0440 because sudo refuses a sudoers file anyone else can read.
cat > /usr/local/etc/sudoers.d/90-docker-nopasswd << 'EOF'
docker ALL=(ALL) NOPASSWD:ALL
EOF
chmod 0440 /usr/local/etc/sudoers.d/90-docker-nopasswd

# ROOT CAN LOG IN TOO, and that is deliberate. This disk has no keys and no network
# exposure beyond a forwarded port somebody started on purpose; a root that cannot log in
# out of the box buys nothing and costs an emergency-console recovery when a stage has to
# fix something. Every other image here makes the same trade.
echo 'admin' | pw usermod root -h 0

# --- console ----------------------------------------------------------------
#
# THE INSTALLED SYSTEM HAS TO TALK ON comconsole, OR NOTHING CAN READ IT.
#
# The installer runs its console on the serial port, and that is why a bake can follow it
# line by line. The system it writes does NOT inherit that: /boot/loader.conf starts empty,
# so the first boot after the install sends everything to the VGA adapter and the serial
# port is silent. Run 41757463732's launch is exactly that - its console.log is 0 bytes and
# the stage reported "the disk did not reach userspace" about a disk that had booted fine.
#
# comconsole puts the loader and the kernel on ttyu0, which is what lib/serial.py reads.
# The boot_multicons line keeps the VGA output too, so the same disk is readable either
# way - useful for the console artifact and for anyone booting it locally.
cat >> /boot/loader.conf <<'LOADERCONF'
console="comconsole,vidconsole"
boot_multicons="YES"
boot_serial="YES"
comconsole_speed="115200"
LOADERCONF

# /etc/ttys is what gives a LOGIN prompt on the serial port; without it the console is
# readable but not usable, and the next stage could not log in. ttyu0 is the first serial
# port on amd64.
sed -i '' -e 's|^ttyu0.*|ttyu0\t"/usr/libexec/getty std.115200"\tvt100\ton  secure|' /etc/ttys \
    || echo "  could not enable ttyu0 in /etc/ttys"

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
    xorg \
    xfce \
    lightdm \
    lightdm-gtk-greeter \
    xfce4-terminal \
    || echo "=== easydiskbake: pkg install failed; the base system is still complete ==="

# --- the desktop --------------------------------------------------------------
#
# XFCE ON X, STARTED BY LIGHTDM WITH AUTOMATIC LOGIN.
#
# THIS DISK HAD NO DESKTOP AT ALL until now: the package list above was sshd, a toolchain
# and nothing else, so the artifact was a console system. A "gold disk" for this family is
# expected to come up to something a person can look at, and every other image here does.
#
# WHY XFCE AND NOT GNOME OR KDE. On FreeBSD both are large and both drag in a
# Linux-compatibility layer for their desktop services; XFCE is the one FreeBSD recommends
# for a VM and the one that starts reliably on the emulated VGA adapter, which has no
# accelerated anything.
#
# WHY lightdm AND NOT xdm. Automatic login is the requirement - a disk that stops at a
# login prompt has no desktop session, which is what the launch stage reads - and lightdm
# has it as a one-line configuration where xdm has none.
#
# THE USER IS docker, THE ONE THE ACCOUNT SECTION ABOVE CREATED.
if [ -f /usr/local/etc/lightdm/lightdm.conf ]; then
    cat >> /usr/local/etc/lightdm/lightdm.conf <<'LIGHTDM'
[Seat:*]
autologin-user=docker
autologin-user-timeout=0
user-session=xfce
greeter-session=lightdm-gtk-greeter
LIGHTDM
else
    echo "  lightdm.conf is not where it was expected; autologin NOT configured"
fi

# dbus and lightdm at boot. Without dbus the session starts and every panel applet fails
# to talk to anything, which looks like a broken desktop rather than a missing daemon.
sysrc dbus_enable=YES
sysrc lightdm_enable=YES
# hald is gone from modern FreeBSD; what a desktop needs now is the devfs rules for the
# input devices, which is this.
sysrc devfs_system_ruleset=desktop 2>/dev/null || true
pw groupmod video -m docker 2>/dev/null || true

# --- the disk says what made it ---------------------------------------------
echo 'built by easydiskbake bsd-freebsd' > /etc/easydiskbake

# THE INSTALLER REBOOTS AFTER THIS SCRIPT, by itself. Nothing here powers the machine
# off: a poweroff in the post-install chroot leaves the boot blocks unwritten on some
# versions, and a disk that does not boot is worse than one that boots to a prompt.
echo "=== easydiskbake: done ==="
