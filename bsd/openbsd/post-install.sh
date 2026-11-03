#!/bin/sh
# Runs on the freshly installed OpenBSD, once, at the end of the installer.
#
# OPENBSD'S INSTALLER HAS NO POST-INSTALL HOOK, so this is fetched and run by the bake
# over the serial console after the machine first boots. It is here rather than inlined in
# bsd/openbsd/bake.py so it can be read on its own - and so a person booting the media by
# hand can paste the same thing.
#
# WHAT IT DOES, AND WHY EACH PART
#
#   sshd            already enabled by the installer (sshd=yes in the answers), so there
#                   is nothing to turn on. This checks it rather than assuming, because
#                   "the answer landed" and "the service is up" are different facts.
#   the account     root gets the admin password from the installer; this adds a docker
#                   account with the same password and a doas rule, which is OpenBSD's
#                   sudo. A runner that has to escalate with a password it does not have
#                   fails at the first pkg_add.
#   the toolchain   git, python3, node and jq, from OpenBSD's own package sets. The
#                   compilers are in base; nothing here is built from source.
set -eu

echo "=== easydiskbake: configuring OpenBSD ==="

# --- sshd -------------------------------------------------------------------
# THE ANSWER SAID sshd=yes; THIS CHECKS IT. Two facts are wanted: the daemon is
# configured to start at boot, and it is running now.
grep -q '^sshd_flags=' /etc/rc.conf.local 2>/dev/null || echo 'sshd_flags=""' >> /etc/rc.conf.local
rcctl enable sshd || true
rcctl start sshd || true
rcctl check sshd && echo "sshd is running"

# --- the account ------------------------------------------------------------
# THE SAME ACCOUNT AS EVERY OTHER IMAGE HERE. -m creates the home, -s gives it a shell;
# without a shell the account exists and cannot log in, which reads later as an ssh
# failure rather than as a user-creation one.
if ! id docker >/dev/null 2>&1; then
    useradd -m -s /bin/ksh -c "easydiskbake" docker
fi
echo 'admin' | passwd docker >/dev/null 2>&1 || true

# DOAS, NOT SUDO. OpenBSD removed sudo from base; doas is the supported tool and the
# config file is one line per rule.
mkdir -p /etc
if ! grep -q '^permit persist :wheel' /etc/doas.conf 2>/dev/null; then
    echo 'permit persist :wheel' >> /etc/doas.conf
fi
usermod -G wheel docker 2>/dev/null || pw groupmod wheel -m docker 2>/dev/null || true

# --- the toolchain ----------------------------------------------------------
#
# PKG_ADD PROMPTS ON FIRST USE. -I answers yes to installing the signature key, which is
# the only question it asks on a fresh system, and the bake has nobody to answer it.
export PKG_PATH="$(cat /etc/installurl 2>/dev/null || echo https://cdn.openbsd.org/pub/OpenBSD)/$(uname -r)/packages/$(uname -m)/"

for package in git node python3 jq curl; do
    pkg_add -I "$package" || echo "  $package did not install"
done

# --- the disk says what made it ---------------------------------------------
echo 'built by easydiskbake bsd-openbsd' > /etc/easydiskbake

echo "=== easydiskbake: done ==="
