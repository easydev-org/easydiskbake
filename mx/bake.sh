#!/usr/bin/env bash
# Bake MX Linux, driving its graphical installer with OCR, and record the whole thing.
#
# WHY OCR AT ALL
#
# MX's minstall has an unattended mode and it does not work in any of the three forms
# this project could reach:
#
#   minstall --tui --auto --config     stops at 'Scanning disk drives' for ever
#   minstall --auto --config (GUI)     reports 'Paused for required ops' on the very
#                                      pane the configuration was meant to fill
#   minstall-launcher                  refuses every argument, so the config is never
#                                      read at all
#
# So the GUI is driven: OCR reads the screen, QMP clicks at absolute coordinates, and
# keys are typed into the account fields. Every coordinate below was measured from a
# frame this pipeline captured, not guessed.
#
# WHY A SECOND BOOT
#
# minstall installs no bootloader. The disk it produces holds a complete system and a
# zeroed MBR, and the fix is grub-install in a chroot - which cannot run in the first
# boot because minstall has taken the console. So the live ISO is booted a second time
# with one job and nobody else using the serial line.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/../lib"

OUT="${OUT:?set OUT to the qcow2 to produce}"
MX_VERSION="${MX_VERSION:-25.3}"
MX_EDITION="${MX_EDITION:-Xfce}"
DISK_SIZE="${DISK_SIZE:-24G}"
DRIVE_SECONDS="${DRIVE_SECONDS:-1800}"
ACCOUNT_USER="${ACCOUNT_USER:-docker}"
ACCOUNT_PASS="${ACCOUNT_PASS:-admin}"

WORK="$(dirname "$OUT")"
mkdir -p "$WORK/frames" && cd "$WORK"
FRAMES="$WORK/frames"

log() { echo "[bake $(date -u +%H:%M:%S)] $*" | tee -a "$FRAMES/bake.log"; }

. "$LIB/iso.sh"
. "$LIB/vm.sh"

log "=== ISO ==="
ISO="$WORK/mx.iso"
bake_fetch_iso mx "$ISO"
ls -la "$ISO"

log "=== kernel and initrd from /antiX ==="
rm -rf x && mkdir -p x
7z x -y -o"x" "$ISO" "antiX/vmlinuz" "antiX/initrd.gz" 2>&1 | tail -3
K=$(find "$PWD/x" -name 'vmlinuz*' -type f | head -1)
I=$(find "$PWD/x" -name 'initrd*' -type f | head -1)
log "kernel: $K"
log "initrd: $I ($(stat -c%s "$I") bytes)"
[ -s "$K" ] && [ -s "$I" ] || { log "PAIR_FAIL"; exit 2; }

log "=== disk ==="
rm -f disk.qcow2
qemu-img create -f qcow2 disk.qcow2 "$DISK_SIZE"
qemu-img info disk.qcow2 | sed -n 's/^virtual size:.*/  &/p'

log "=== the live session, with vnc and a recording ==="
rm -f serial.in && mkfifo serial.in

(
  # Log in. Three cycles, not fourteen: every cycle after the first success types
  # 'demo' at a shell prompt, which runs it as a command and buries whatever follows.
  for _ in 1 2 3; do
    printf 'demo\n'; sleep 3; printf 'demo\n'; sleep 12
  done
  printf '\n'; sleep 3
  printf 'echo BAKE_LOGIN_OK $(id -u)\n'; sleep 4

  # The disk has to look like a disk before the installer offers it: minstall runs
  # lsblk and parted, and a disk with no partition table has nothing for parted to
  # describe, so the drive list stays empty.
  printf 'echo demo | sudo -S sgdisk --zap-all /dev/vda; echo ZAP_RC=$?\n'; sleep 8

  # minstall with no arguments at all: the driver fills every pane.
  printf 'echo demo | sudo -S sh -c "minstall > /tmp/minstall.out 2>&1; echo MINSTALL_RC=\\$? > /tmp/mx-rc" &\n'
  sleep 20
  printf 'echo MINSTALL_STARTED; pgrep -a -f minstall | head -3\n'
  sleep 40
) > serial.in &
FEED=$!

VM_STDIN="$WORK/serial.in" VM_STDOUT="$WORK/console.log" \
  bake_vm_start "$WORK" \
    -kernel "$K" -initrd "$I" \
    -append "console=ttyS0,115200n8 init=/lib/systemd/systemd" \
    -drive "file=$ISO,media=cdrom,readonly=on" \
    -drive "file=$WORK/disk.qcow2,if=virtio,format=qcow2"

log "=== waiting for vnc to answer ==="
bake_wait_port 127.0.0.1 5900 90 "vnc" || log "vnc did not answer"

# Record every frame the guest draws, for the whole run.
#
# Not the VNC stream: this ffmpeg has no vnc demuxer ("Unknown input format: 'vnc'").
# The frames are captured over QMP instead and encoded afterwards, which needs nothing
# the image does not already have.
mkdir -p "$FRAMES/shots"
python3 "$LIB/snapshots.py" "$QMP_RECORD" "$FRAMES/shots" "${SNAPSHOT_INTERVAL:-1}" 1500 \
  2>&1 | tee -a "$FRAMES/shots/snapshots.log" | sed -u 's/^/[snap] /' &
SNAP_PID=$!
log "snapshot every ${SNAPSHOT_INTERVAL:-5}s, pid $SNAP_PID"

log "=== driving the installer ==="
set +e
timeout "$DRIVE_SECONDS" python3 "$HERE/drive-mx.py" "$QMP_DRIVE" "$FRAMES"
DRIVE_RC=$?
set -e
log "driver exit $DRIVE_RC"

log "=== waiting for the installer to finish ==="
wait "$FEED" 2>/dev/null || true

bake_vm_stop
kill "$SNAP_PID" 2>/dev/null || true

log "=== the disk so far ==="
DISK_BYTES=$(stat -c%s disk.qcow2)
log "disk.qcow2 is ${DISK_BYTES} bytes"
if [ "$DISK_BYTES" -lt 1000000000 ]; then
  log "=== THE DISK IS EMPTY ==="
  exit 1
fi

log "=== second boot: install the bootloader ==="
bash "$HERE/fix-grub.sh" "$WORK" || log "grub fix reported failure"

# One recording, both boots.
#
# The install happens in the first boot and the bootloader in the second, and the first
# attempt encoded only the first: 55 seconds of a nine-minute job. The two frame
# directories are concatenated in the order they were captured, so the video runs from
# the live ISO starting to grub.cfg being written.
log "=== encode the recording: install, then bootloader ==="
bake_vm_record_many "$WORK/install.mp4" "$FRAMES/shots" "$WORK/shots-grubfix" || true

bake_compress_disk() {
  qemu-img convert -O qcow2 -c -o compression_type=zstd "$1" "$2"
}
bake_compress_disk disk.qcow2 "$OUT"
log "BAKED mx -> $OUT"
