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
python3 "$(dirname "$0")/../lib/snapshots.py" "$PWD/grubfix.qmp" "$PWD/shots-grubfix" 30 120 \
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
  printf 'echo demo | sudo -S mount /dev/vda1 /mnt/mxroot; echo RC_MOUNT=$?\n'; sleep 8
  printf 'echo demo | sudo -S ls /mnt/mxroot/etc/debian_version; echo RC_ETC=$?\n'; sleep 6
  printf 'echo demo | sudo -S ls /mnt/mxroot/boot | head -5\n'; sleep 6
  printf 'echo demo | sudo -S ls /mnt/mxroot/usr/sbin/grub-install; echo RC_GRUBBIN=$?\n'; sleep 6

  # Bind mounts, one line each. update-grub needs /proc and /sys to enumerate devices,
  # and grub-install needs /dev to write. Missing any of them fails quietly.
  printf 'echo demo | sudo -S mount --bind /dev /mnt/mxroot/dev; echo RC_DEV=$?\n'; sleep 5
  printf 'echo demo | sudo -S mount --bind /dev/pts /mnt/mxroot/dev/pts; echo RC_PTS=$?\n'; sleep 5
  printf 'echo demo | sudo -S mount --bind /proc /mnt/mxroot/proc; echo RC_PROC=$?\n'; sleep 5
  printf 'echo demo | sudo -S mount --bind /sys /mnt/mxroot/sys; echo RC_SYS=$?\n'; sleep 5

  # grub-install first: it writes stage 1 and creates /boot/grub/i386-pc. update-grub
  # then writes the menu. The order matters - update-grub alone produces a menu for a
  # bootloader that was never installed.
  printf 'echo demo | sudo -S chroot /mnt/mxroot grub-install --target=i386-pc --recheck /dev/vda 2>&1 | tail -25; echo RC_INSTALL=$?\n'; sleep 45

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
  printf 'echo demo | sudo -S touch /mnt/mxroot/etc/default/grub\n'; sleep 3
  printf 'echo demo | sudo -S sed -i "/^GRUB_TIMEOUT=/d;/^GRUB_TERMINAL=/d;/^GRUB_CMDLINE_LINUX_DEFAULT=/d" /mnt/mxroot/etc/default/grub\n'; sleep 4
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

  printf 'echo demo | sudo -S chroot /mnt/mxroot update-grub 2>&1 | tail -25; echo RC_UPDATE=$?\n'; sleep 60

  # Prove it, rather than assume it.
  printf 'echo demo | sudo -S ls -la /mnt/mxroot/boot/grub/grub.cfg; echo RC_CFG=$?\n'; sleep 8
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
