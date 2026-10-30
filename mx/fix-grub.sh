#!/usr/bin/env bash
# Put a bootloader on a disk MX installed but cannot start.
#
# WHY THIS IS A SEPARATE BOOT
#
# MX's installer does not install GRUB in any mode this project can reach:
#
#   minstall-launcher          refuses every argument, so minstall.conf is never read
#   minstall --auto (GUI)      does not skip panes; 'Paused for required ops'
#   minstall --tui --auto -c   stops at 'Scanning disk drives', log=119 forever
#
# So grub-install runs afterwards, in a chroot on the finished disk. The first attempts
# did that in the same boot, from a timed serial fifo - and the log says exactly why
# that cannot work:
#
#   grub> export XDG_RUNTexport WAYLAND_export LIBGL_Alif command -v z...
#
# Those are the bake's own shell commands, arriving at a GRUB prompt. minstall reboots
# when it finishes, the disk boots, GRUB fails to find its menu, and the console by then
# belongs to GRUB - not to a shell that could mount anything.
#
# The fix is not to race the installer for the console. Boot the live ISO a second time,
# with nobody else using the serial line, and do one job there.
set -Eeuo pipefail

WORK="${1:?usage: fix-grub.sh <workdir with mx.iso, x/vmlinuz, x/initrd, disk.qcow2>}"
cd "$WORK"

log() { echo "[grub $(date -u +%H:%M:%S)] $*"; }

# Wait for a line to appear in what the guest has printed.
#
# The alternative was a fixed sleep before each step, with the sum of all of them as an
# implicit deadline. That made the whole stage depend on how fast the machine happened
# to be: the same script gave RC_MOUNT=0 at 330 seconds and RC_MOUNT=32 at 280, and the
# 32 case went on to destroy the MBR while reporting success.
#
# Waiting for the guest to say it has finished is not a longer guess, it is not a guess.
# The ceiling only exists so that a genuinely hung guest still fails the run.
wait_for_marker() {
  local marker="$1" seconds="${2:-180}"
  local CONSOLE="${CONSOLE_LOG:-serial.out}"
  local i
  for i in $(seq 1 "$seconds"); do
    if grep -qa "$marker" "$CONSOLE" 2>/dev/null; then
      log "saw $marker after ${i}s"
      return 0
    fi
    sleep 1
  done
  log "NEVER SAW $marker after ${seconds}s - the guest stopped responding"
  return 1
}

K=$(find "$PWD/x" -name 'vmlinuz*' -type f | head -1)
I=$(find "$PWD/x" -name 'initrd*' -type f | head -1)
[ -s "$K" ] || { log "no kernel in $WORK/x"; exit 2; }
[ -s "$I" ] || { log "no initrd in $WORK/x"; exit 2; }
[ -s disk.qcow2 ] || { log "no disk.qcow2 in $WORK"; exit 2; }

log "kernel $K"
log "initrd $I"
log "disk   $(stat -c%s disk.qcow2) bytes"

rm -f serial.in serial.out && mkfifo serial.in

qemu-system-x86_64 -enable-kvm -cpu host -smp 2 -m 4096 \
  -kernel "$K" -initrd "$I" \
  -append "console=ttyS0,115200n8 init=/lib/systemd/systemd" \
  -drive "file=$PWD/mx.iso,media=cdrom,readonly=on" \
  -drive "file=$PWD/disk.qcow2,if=virtio,format=qcow2" \
  -display none -vga std \
  -device qemu-xhci,id=xhci -device usb-tablet,bus=xhci.0 \
  -qmp "unix:$PWD/grubfix.qmp,server=on,wait=off" \
  -serial stdio \
  < serial.in > serial.out 2>&1 &
QPID=$!
log "qemu pid $QPID"

# The second boot is photographed too: if grub-install or update-grub fails, its error
# is on the guest's screen, and a serial log of a curses-free console still loses the
# ordering of what was drawn.
mkdir -p shots-grubfix
python3 "$(dirname "$0")/../lib/snapshots.py" "$PWD/grubfix.qmp" "$PWD/shots-grubfix" "${SNAPSHOT_INTERVAL:-1}" 400 \
  > shots-grubfix/snapshots.log 2>&1 &
SNAP_PID=$!

# One writer, one reader, and the whole console to ourselves.
#
# Each step is a separate line with its own exit status printed, because a single long
# command with semicolons hides which part failed - and 'the chroot did not work' is not
# an actionable statement.
(
  # The getty takes a while; four attempts is a minute and a half, which is more than it
  # has ever needed.
  for _ in 1 2 3 4; do
    printf 'demo\n'; sleep 3; printf 'demo\n'; sleep 12
  done
  printf '\n'; sleep 3
  printf 'echo GRUBFIX_LOGIN $(id -u)\n'; sleep 5

  printf 'echo demo | sudo -S mkdir -p /mnt/mxroot\n'; sleep 4

  # WAIT FOR THE PARTITION, DO NOT GUESS HOW LONG IT TAKES.
  #
  # The previous version put a fixed sleep before every step and used their sum as a
  # deadline. Mount then succeeded or failed depending on how long the live session took
  # to settle, which varied run to run - 330 seconds worked, 280 did not - and the
  # failure was silent:
  #
  #   RC_MOUNT=32          mount /dev/vda1: no such device
  #   RC_INSTALL=0         grub-install chrooted into an empty directory, wrote zeroes
  #                        over the MBR, and returned success
  #   mbr signature: 0000
  #
  # So the waiting happens in the guest now. This loop polls for the device node and
  # prints one line either way, and the mount below only runs if it appeared. The
  # numbers: 120 tries, half a second each, a minute - four times the longest wait ever
  # observed for the node to appear once the installer had finished with the disk.
  printf 'echo demo | sudo -S sh -c "for i in $(seq 1 120); do if [ -b /dev/vda1 ]; then echo VDA1_READY_AT_TRY_$i; exit 0; fi; sleep 0.5; done; echo VDA1_NEVER_APPEARED; ls /dev/vda* 2>&1"\n'
  sleep 20
  printf 'echo VDA1_CHECK_DONE\n'; sleep 4
  # What does the disk actually look like before touching it?
  #
  # A run where mount failed with RC=32 went on to chroot into an EMPTY directory and
  # run grub-install there. grub-install could not find its modules, wrote zeroes over
  # the MBR's boot code, and returned 0 - so the log said GRUB was installed while the
  # disk had just been made unbootable:
  #
  #   mbr signature: 0000
  #   mbr boot code: 0000000000000000
  #   ext superblock magic 53ef found      <- the filesystem was fine all along
  #
  # So the partition list is printed first, and the mount is not attempted unless a
  # partition is there.
  # Written to a file and then printed, because the serial line drops lines whose
  # output arrives in a burst - lsblk and parted were both sent and neither produced
  # anything in the log, so the one piece of evidence needed was the one that vanished.
  printf 'echo demo | sudo -S sh -c "lsblk > /tmp/lsblk.txt 2>&1; parted -s /dev/vda print >> /tmp/lsblk.txt 2>&1; blkid >> /tmp/lsblk.txt 2>&1"\n'; sleep 12
  printf 'echo DISK_REPORT_BEGIN; echo demo | sudo -S cat /tmp/lsblk.txt; echo DISK_REPORT_END\n'; sleep 10
  printf 'echo demo | sudo -S parted -s /dev/vda print; echo PARTED_RC=$?\n'; sleep 6

  # The mount itself retries in the guest, for the same reason.
  #
  # Waiting for the device node and then mounting once is still a race: the node can
  # exist while the kernel is not yet ready to read a filesystem on it, and a device a
  # process has only just stopped writing can answer EBUSY for a moment. Ten attempts, a
  # second apart, and whichever result the last one gives is what gets reported.
  printf 'echo demo | sudo -S sh -c "for i in $(seq 1 10); do if mount /dev/vda1 /mnt/mxroot 2>/tmp/mnt.err; then echo RC_MOUNT=0; exit 0; fi; sleep 1; done; echo RC_MOUNT_FAILED; cat /tmp/mnt.err"\n'
  sleep 25
  printf 'echo MOUNT_CHECK_DONE\n'; sleep 4
  printf 'echo demo | sudo -S ls /mnt/mxroot/etc/debian_version; echo RC_ETC=$?\n'; sleep 6
  printf 'echo demo | sudo -S ls /mnt/mxroot/boot | head -5\n'; sleep 6
  printf 'echo demo | sudo -S ls /mnt/mxroot/usr/sbin/grub-install; echo RC_GRUBBIN=$?\n'; sleep 6

  # GATE: is there actually a system at /mnt/mxroot?
  #
  # Everything below this point writes to the disk. If the mount failed, chroot enters
  # an EMPTY directory and grub-install writes zeroes over the MBR while returning 0 -
  # which turns a working 8 GB installation into an unbootable disk and reports success
  # doing it. That happened once:
  #
  #   RC_MOUNT=32
  #   RC_INSTALL=0
  #   mbr signature: 0000
  #   mbr boot code: 0000000000000000
  #   ext superblock magic 53ef found     <- the system was intact on a partition
  #                                          that had simply not been mounted
  #
  # So the marker file is checked and the rest is skipped outright when it is missing.
  # A bake that stops here has cost one run; a bake that continues has cost the disk.
  printf 'if [ -f /mnt/mxroot/etc/debian_version ]; then echo FIXUP_GATE=open; else echo FIXUP_GATE=closed; fi\n'; sleep 6

  # An empty directory has no /proc, so even the bind mounts are pointless when the gate
  # is shut. They are written to run only in the open case.
  printf 'if [ -f /mnt/mxroot/etc/debian_version ]; then \
'
  printf '  echo demo | sudo -S mount --bind /dev /mnt/mxroot/dev; echo RC_DEV=$?; \
'
  printf '  echo demo | sudo -S mount --bind /dev/pts /mnt/mxroot/dev/pts; echo RC_PTS=$?; \
'
  printf '  echo demo | sudo -S mount --bind /proc /mnt/mxroot/proc; echo RC_PROC=$?; \
'
  printf '  echo demo | sudo -S mount --bind /sys /mnt/mxroot/sys; echo RC_SYS=$?; \
'
  printf '  echo BINDS_DONE; \\
'
  printf 'else echo RC_DEV=skipped RC_PTS=skipped RC_PROC=skipped RC_SYS=skipped; echo BINDS_DONE; fi\n'
  sleep 8
  wait_for_marker "BINDS_DONE" 60

  # grub-install first: it writes stage 1 and creates /boot/grub/i386-pc. update-grub
  # then writes the menu. The order matters - update-grub alone produces a menu for a
  # bootloader that was never installed.
  #
  # Also gated, and for a sharper reason: this is the command that destroys the MBR when
  # its chroot is empty.
  printf 'if [ -f /mnt/mxroot/etc/debian_version ]; then \
'
  printf '  echo demo | sudo -S chroot /mnt/mxroot grub-install --target=i386-pc --recheck /dev/vda 2>&1 | tail -25; echo RC_INSTALL=$?; \
'
  printf '  echo GRUB_INSTALL_DONE; \\\n'
  printf 'else echo RC_INSTALL=skipped_no_root; echo GRUB_INSTALL_DONE; fi\n'
  sleep 10
  # Wait for grub-install to say it is finished instead of sleeping fifty seconds and
  # hoping. grub-install on a 64 GB disk takes anywhere from ten to forty seconds, and
  # the previous fixed sleep was simultaneously too long in the common case and too
  # short when the disk was slow.
  wait_for_marker "GRUB_INSTALL_DONE" 180

  # The menu has to boot by itself, and it has to talk to the serial port.
  #
  # The previous run got as far as this, and the smoke test saw:
  #
  #   GRUB loading.
  #   Welcome to GRUB!
  #   <nothing>
  #
  # That is GRUB displaying a menu on the VGA console and waiting at it: 'Welcome to
  # GRUB!' with no prompt after it means grub.cfg was found. Under -nographic the menu
  # is invisible and the timeout is what the smoke test sits through.
  #
  # GRUB_TIMEOUT=0 boots the default entry immediately, and GRUB_TERMINAL="console
  # serial" plus the console= on the kernel line put the menu and the boot on ttyS0 -
  # which is also what makes the smoke test able to read it.
  # Everything from here writes into /mnt/mxroot. Ungated, a failed mount means these
  # lines edit the LIVE system's own /mnt, which is both useless and confusing to read
  # afterwards.
  printf 'if [ -f /mnt/mxroot/etc/debian_version ]; then echo demo | sudo -S touch /mnt/mxroot/etc/default/grub; echo RC_TOUCH=$?; else echo RC_TOUCH=skipped_no_root; fi\n'; sleep 5
  printf 'if [ -f /mnt/mxroot/etc/debian_version ]; then echo demo | sudo -S sed -i "/^GRUB_TIMEOUT=/d;/^GRUB_TERMINAL=/d;/^GRUB_CMDLINE_LINUX_DEFAULT=/d" /mnt/mxroot/etc/default/grub; echo RC_SED=$?; else echo RC_SED=skipped_no_root; fi\n'; sleep 5
  # Written one line at a time with echo and no quoting at all.
  #
  # The first attempt bundled three appends into one sh -c with escaped quotes inside a
  # printf inside a serial string. Serial input mangles quoting more often than it
  # mangles anything else, and a malformed line here writes a broken grub default file
  # - which fails later, on a screen nobody is watching.
  printf 'echo demo | sudo -S tee -a /mnt/mxroot/etc/default/grub <<< GRUB_TIMEOUT=0\n'; sleep 4
  printf 'echo demo | sudo -S tee -a /mnt/mxroot/etc/default/grub <<< GRUB_TERMINAL=serial\n'; sleep 4
  printf 'echo demo | sudo -S tee -a /mnt/mxroot/etc/default/grub <<< GRUB_CMDLINE_LINUX_DEFAULT=console=ttyS0,115200n8\n'; sleep 4
  printf 'echo demo | sudo -S cat /mnt/mxroot/etc/default/grub; echo RC_GRUBDEF=$?\n'; sleep 6

  printf 'if [ -f /mnt/mxroot/etc/debian_version ]; then echo demo | sudo -S chroot /mnt/mxroot update-grub 2>&1 | tail -25; echo RC_UPDATE=$?; echo UPDATE_GRUB_DONE; else echo RC_UPDATE=skipped_no_root; echo UPDATE_GRUB_DONE; fi\n'
  sleep 10
  # update-grub runs os-prober and probes every filesystem; sixty seconds was the old
  # fixed wait and it is both too long when there is one disk and too short when
  # os-prober decides to look harder.
  wait_for_marker "UPDATE_GRUB_DONE" 240

  # Prove it, rather than assume it - and if there was no root to write into, say that
  # instead of reporting a missing file as a failure of update-grub.
  printf 'if [ -f /mnt/mxroot/etc/debian_version ]; then echo demo | sudo -S ls -la /mnt/mxroot/boot/grub/grub.cfg; echo RC_CFG=$?; else echo "RC_CFG=skipped_no_root (grub was NOT installed)"; fi\n'; sleep 8
  printf 'echo demo | sudo -S wc -l /mnt/mxroot/boot/grub/grub.cfg\n'; sleep 6
  printf 'echo demo | sudo -S head -20 /mnt/mxroot/boot/grub/grub.cfg\n'; sleep 8
  printf 'echo GRUBFIX_DONE\n'; sleep 5
) > serial.in &
FEED=$!

# The writer's own lifetime, bounded. If it hangs, this must not hang with it.
for i in $(seq 1 60); do
  if grep -q GRUBFIX_DONE serial.out 2>/dev/null; then
    log "fixup finished after ~$((i * 10))s"
    break
  fi
  sleep 10
done

if ! grep -q GRUBFIX_DONE serial.out 2>/dev/null; then
  log "fixup did not reach its end - what the guest said:"
  tail -40 serial.out 2>/dev/null | sed 's/^/    /'
fi

kill "$QPID" 2>/dev/null || true
wait "$QPID" 2>/dev/null || true
wait "$FEED" 2>/dev/null || true
kill "${SNAP_PID:-}" 2>/dev/null || true

log "=== every RC_ line from the guest ==="
grep -aE 'RC_[A-Z]+=|GRUBFIX_' serial.out | tail -30 || log "(none)"

log "=== grub.cfg, if it exists ==="
grep -a -A3 'boot/grub/grub.cfg' serial.out | tail -12 || true

log "done"
