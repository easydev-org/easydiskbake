#!/bin/bash
# Expand an Xcode .xip into /Applications and select it. Runs IN THE GUEST.
#
#     bash install-xcode.sh /Users/docker/Xcode_26.3_Universal.xip
#
# WHY THIS IS A FILE AND NOT A `ssh:` LINE
#
# Nothing here fits on one line: the expansion takes many minutes and prints nothing, the
# license has to be accepted before xcodebuild will answer at all, and `-runFirstLaunch`
# installs the platform packages (about ten minutes on this machine). It is also the part
# most worth reading before it runs, which is what a file gives and a quoted one-liner does
# not.
#
# WHY THE EXPANSION IS NOT `xip --expand`
#
# `xip --expand` is interactive on some releases: it asks for the password through a
# dialog when it has to move the result, and over ssh there is no dialog to answer. `xip -x`
# expands in place without prompting, which is what the unattended installs use. The
# default target is the working directory, so this runs from a directory with room for the
# expanded app (about 15 GiB).
#
# WHY --runFirstLaunch IS NOT OPTIONAL
#
# Without it Xcode is a 15 GiB directory that refuses to build: the platform packages are
# the SDKs, and `xcodebuild -version` answers while `xcrun --sdk iphonesimulator` fails.
# It prints almost nothing while it works, so the caller gives it its own timeout rather
# than sharing the expansion's.
set -uo pipefail

XIP=${1:?usage: install-xcode.sh <path to .xip> <password> [dest] [work]}
# SUDO OVER SSH HAS NO TTY, AND THAT IS NOT A DETAIL - IT IS THE FIRST THING THAT FAILS.
#
# session.ssh is `sshpass ... ssh host command` with no terminal, so sudo cannot prompt:
#
#     sudo: no tty present and no askpass program specified
#     sudo: a password is required
#
# lib/macclean.py already carries this lesson from run 42390179102, where it cost an
# otherwise-green stage its publish. The same `-S` + stdin form is used here, in one variable
# so a command cannot be written that forgets it.
ADMIN_PASSWORD=${2:?usage: install-xcode.sh <path to .xip> <password> [dest] [work]}
DEST=${3:-/Applications}
WORK=${4:-/Users/$(id -un)/xcode-expand}

sudo_() {
  printf '%s\n' "$ADMIN_PASSWORD" | sudo -S -p '' "$@"
}

if [ ! -f "$XIP" ]; then
  echo "no xip at $XIP"
  exit 1
fi

echo "xip: $(basename "$XIP") ($(du -h "$XIP" | cut -f1))"
echo "expanding into $WORK (this takes several minutes and prints nothing)"

mkdir -p "$WORK"
cd "$WORK" || exit 1

# ALREADY EXPANDED. A resumed run must not spend twenty minutes on a step that is done -
# and a half-expanded Xcode.app is worse than none, which is why the check is the binary
# xcodebuild itself and not the directory's existence.
if [ -x "$WORK/Xcode.app/Contents/Developer/usr/bin/xcodebuild" ]; then
  echo "already expanded in $WORK"
else
  rm -rf "$WORK/Xcode.app"
  if ! xip -x "$XIP" > /tmp/xip.log 2>&1; then
    echo "=== xip -x FAILED ==="
    tail -20 /tmp/xip.log
    exit 1
  fi
  tail -3 /tmp/xip.log
  # THE PRODUCT IS NAMED AFTER THE ARCHIVE, NOT ALWAYS "Xcode.app". An .xip expands to the name
  # inside it, and a versioned download (Xcode_26.3.xip) has produced "Xcode.app" on every
  # release so far - but "so far" is not a guarantee, and the failure would be a silent
  # `mv: no such file` after a twenty-minute expansion.
  if [ ! -d "$WORK/Xcode.app" ]; then
    found=$(find "$WORK" -maxdepth 1 -name 'Xcode*.app' -type d | head -1)
    if [ -n "$found" ]; then
      echo "the archive expanded to $(basename "$found"), not Xcode.app"
      mv "$found" "$WORK/Xcode.app"
    else
      echo "=== THE ARCHIVE EXPANDED BUT THERE IS NO Xcode*.app ==="
      ls -la "$WORK" | head -10
      exit 1
    fi
  fi
fi

echo "moving to $DEST"
sudo_ rm -rf "$DEST/Xcode.app"
if ! sudo_ mv "$WORK/Xcode.app" "$DEST/Xcode.app"; then
  echo "=== COULD NOT MOVE Xcode.app INTO $DEST ==="
  exit 1
fi

# THE QUARANTINE FLAG IS WHAT MAKES GATEKEEPER PROMPT. It is set because the file came from
# the network, and a dialog has nobody to answer it over ssh.
sudo_ xattr -dr com.apple.quarantine "$DEST/Xcode.app" 2>/dev/null || true

# `xcode-select -s` IS THE SWITCH, and it fails silently-ish when the path is wrong, so the
# result is read back rather than assumed.
echo "selecting $DEST/Xcode.app"
sudo_ xcode-select -s "$DEST/Xcode.app/Contents/Developer" || exit 1
selected=$(xcode-select -p)
echo "xcode-select -p: $selected"
case "$selected" in
  "$DEST/Xcode.app/Contents/Developer") ;;
  *) echo "=== xcode-select DID NOT TAKE (got $selected) ==="; exit 1 ;;
esac

# THE LICENSE, BEFORE ANYTHING ASKS XCODE A QUESTION. `xcodebuild -version` on an unaccepted
# license exits with a message about the agreement, and every later check would read that as
# a broken toolchain.
echo "accepting the license"
sudo_ xcodebuild -license accept 2>&1 | tail -2

echo "installing the bundled platform packages (about ten minutes)"
sudo_ xcodebuild -runFirstLaunch 2>&1 | tail -5

sudo_ DevToolsSecurity -enable 2>&1 | tail -1

# AND THE CLAIM THAT MATTERS: it compiles. A selected Xcode with no SDK answers --version.
echo "verifying"
xcodebuild -version || exit 1
swift --version || exit 1

echo "XCODE_OK"
