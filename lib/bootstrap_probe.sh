#!/bin/sh
# A bootstrap that proves the whole chain, in the shell macOS already has.
#
#     put this in ~/shared/bootstrap.sh before a launch
#
# IT IS THE TEST, NOT A FIXTURE. The chain it has to prove is longer than "something ran":
#
#     the login launcher fired
#     the share was mounted by then
#     the disk's own shell was found
#     the script ran with the user's HOME
#
# So it writes ~/hello.txt - INSIDE THE GUEST, NOT INTO THE SHARE - and that is deliberate. A
# file written to the share would appear on the host whether or not the guest ever ran anything
# (a mount is not proof of execution). ~/hello.txt can only exist if the guest ran this.
#
# THE OUTPUT IS READ BY lib/launch_selftest.py, one assertion per line:
#
#     hello.sh              the script ran at all
#     user=<account>        it ran as the account, not as root through a helper
#     shell=sh              which interpreter the disk had
#     share=readable        the mount was there when it fired
#     stamp=<unix>          it ran during this launch and not a previous one

set -u

HOME_DIR=${HOME:-/Users/$(id -un)}
OUT="$HOME_DIR/hello.txt"

# THE SHARE IS READ, NOT WRITTEN. If it is missing the launch still proves the launcher and the
# shell; it only fails to prove the mount, and it says which.
SHARE="$HOME_DIR/shared"
if [ -d "$SHARE" ] && [ -f "$SHARE/bootstrap.sh" ]; then
    SHARE_STATE=readable
else
    SHARE_STATE=MISSING
fi

{
    echo "hello.sh"
    echo "user=$(id -un)"
    echo "shell=sh"
    echo "share=$SHARE_STATE"
    echo "stamp=$(date +%s)"
} > "$OUT"

# AND ON STDOUT, so the launcher's log has it even if the file write was refused.
cat "$OUT"
