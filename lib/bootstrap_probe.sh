#!/bin/sh
# A bootstrap that proves the whole chain, in the shell macOS already has.
#
#     put this in ~/shared/bootstrap.sh before a launch
#
# IT WRITES INTO THE SHARE, AND THAT IS THE POINT. The output is hello.txt beside this script, so
# it lands on the HOST - which is where a person can look at it without a guest running, and which
# means nothing it writes is ever part of the disk being published.
#
# "NOTHING IT WRITES IS EVER PART OF THE DISK" IS A PROPERTY THIS FILE HAS TO ENFORCE, NOT ASSUME.
# If the share failed to mount, ~/shared is a LOCAL EMPTY DIRECTORY on the disk - the mount point
# the mount command made before trying - and writing hello.txt into it would leave a file in the
# image. So the first thing this does is ask whether the path is really a mount, and it writes
# nothing at all if it is not.
#
# WHAT THAT LEAVES UNPROVEN WHEN IT REFUSES: the launcher and the shell. It says which, in
# ~/bootstrap-refused.txt inside the guest, and that file IS on the disk - deliberately, because a
# refusal nobody can see is worse than a small file that explains it. The clean step removes it.
#
# THE CHAIN IT PROVES WHEN IT DOES WRITE:
#
#     the login launcher fired
#     the share was mounted by then
#     the disk's own shell was found
#     the script ran with the user's HOME
#
# THE OUTPUT, one key per line, read by lib/launch_selftest.py:
#
#     hello.sh          the script ran at all
#     user=<account>    it ran as the account, not as root through a helper
#     shell=sh          which interpreter the disk had
#     share=mounted     the mount was real when it fired
#     stamp=<unix>      when - the host deletes this file first, so it can only be this launch

set -u

HOME_DIR=${HOME:-/Users/$(id -un)}
SHARE="$HOME_DIR/shared"
OUT="$SHARE/hello.txt"

# A REAL MOUNT, NOT A DIRECTORY THAT HAPPENS TO BE THERE.
#
# `mount` lists it as "//guest@172.30.0.1/shared on /Users/<user>/shared (smbfs, ...)". The
# simplest test that does not depend on the server's address is to ask `mount` for the path at
# all - a local directory never appears there.
if ! mount | grep -q " on $SHARE "; then
    # SAID INSIDE THE GUEST, because the share is exactly what is missing.
    {
        echo "hello refused"
        echo "reason=share is not mounted"
        echo "path=$SHARE"
        echo "user=$(id -un)"
        echo "stamp=$(date +%s)"
    } > "$HOME_DIR/bootstrap-refused.txt" 2>/dev/null
    cat "$HOME_DIR/bootstrap-refused.txt" 2>/dev/null
    exit 0
fi

{
    echo "hello.sh"
    echo "user=$(id -un)"
    echo "shell=sh"
    echo "share=mounted"
    echo "stamp=$(date +%s)"
} > "$OUT"

# AND ON STDOUT, so the launcher's log has it even if the share refused the write.
cat "$OUT"
