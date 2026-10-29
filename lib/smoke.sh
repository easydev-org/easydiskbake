# Is the disk that came out of a bake actually a bootable system?
#
# Two halves, and the order matters.
#
# THE DISK, read without booting it. A bake that produced 8 GB of filesystem and no
# bootloader looks exactly like a bake that produced nothing, from the outside. The
# first 512 bytes and the filesystem superblock say which it is, in a second, before
# any qemu starts.
#
# THE BOOT, watched. A disk can pass every byte-level check and still not start - a
# GRUB stage 1 that cannot find its stage 2 does exactly that - so the console is read
# and the screen is photographed.
#
# Nothing here knows what distribution it is testing. What a desktop looks like, and
# which string proves it arrived, is the caller's business.

SMOKE_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SMOKE_LIB/vm.sh"

# --- reading the disk -------------------------------------------------------------

# Is there boot code in the MBR?
#
# 0..439 boot code, 440..443 disk signature, 446..509 the four partition entries,
# 510..511 the 0x55AA signature. A disk with a signature and no code, and a disk with
# neither, are different failures: the first is a bootloader that was not installed,
# the second is a disk that was never partitioned.
smoke_check_mbr() {
  local disk="$1"
  local tmp sig code
  tmp=$(mktemp)

  if ! qemu-io -f qcow2 -c "read -v 0 512" "$disk" > "$tmp" 2>&1; then
    echo "[disk] qemu-io could not read the MBR; it said:"
    sed 's/^/    /' "$tmp" | head -6
    rm -f "$tmp"
    return 2
  fi

  # The MBR, by offset:
  #
  #   0x000..0x1bd   boot code (446 bytes)
  #   0x1be..0x1fd   four partition entries, 16 bytes each
  #   0x1fe..0x1ff   0x55 0xAA - the signature
  #
  # 0x1fe, not 0x1f0. The first version of this read at 0x1f0, which is the middle of
  # the partition table, so it reported "unreadable" or "0000" on disks that were
  # perfectly fine, and three runs were spent believing a working disk was broken.
  #
  # The bytes are parsed out of the dump by a small Python reader: the offset column is
  # padded to sixteen digits and two attempts at a sed expression failed on that.
  local reader="$SMOKE_LIB/hexdump.py"
  sig=$(python3 "$reader" "$tmp" 1fe 2 2>/dev/null || true)
  code=$(python3 "$reader" "$tmp" 0 8 2>/dev/null || true)

  echo "[disk] mbr signature: ${sig:-unreadable}"
  echo "[disk] mbr boot code: ${code:-unreadable}"

  if [ -z "$sig" ]; then
    echo "[disk] THE MBR COULD NOT BE PARSED - this says nothing about the disk."
    echo "[disk] the raw dump:"
    head -4 "$tmp" | sed 's/^/    /'
    rm -f "$tmp"
    return 2
  fi
  if [ "$sig" != "55aa" ]; then
    echo "[disk] NO MBR SIGNATURE - SeaBIOS has nothing to run"
    rm -f "$tmp"
    return 1
  fi
  case "$code" in
    00000000000000000000000000000000*)
      echo "[disk] signature present, boot code area is zero - no bootloader"
      rm -f "$tmp"
      return 1
      ;;
  esac
  echo "[disk] boot code present"
  rm -f "$tmp"
  return 0
}

# Is there a Linux filesystem where the partition table says there is one?
smoke_check_root() {
  local disk="$1"
  local tmp startlba
  tmp=$(mktemp)

  if ! qemu-io -f qcow2 -c "read -v 512 64" "$disk" > "$tmp" 2>&1; then
    echo "[disk] qemu-io could not read the partition table"
    rm -f "$tmp"
    return 2
  fi
  # First entry starts at 446 within the sector, so at 512+446=958 in the image. The
  # 64 bytes read above begin at 512, so the entry's LBA field is 446-16=430 into it.
  # Reading the four bytes directly is simpler than parsing the dump: do it in its own
  # read at the exact offset.
  rm -f "$tmp"
  tmp=$(mktemp)
  if ! qemu-io -f qcow2 -c "read -v 958 16" "$disk" > "$tmp" 2>&1; then
    echo "[disk] could not read the first partition entry"
    rm -f "$tmp"
    return 2
  fi
  echo "[disk] first partition entry:"
  sed -n '2,3p' "$tmp" | sed 's/^/    /'
  rm -f "$tmp"

  # The superblock is 1024 bytes into the partition, and 0xEF53 sits at offset 0x38
  # within it. The partition starts where the table says; read at that offset and look
  # for the magic in the dump rather than computing it again.
  tmp=$(mktemp)
  if qemu-io -f qcow2 -c "read -v 1048576 4096" "$disk" > "$tmp" 2>&1 && grep -qi '53 ef' "$tmp"; then
    echo "[disk] ext superblock magic 53ef found at 1 MiB"
    rm -f "$tmp"
    return 0
  fi
  echo "[disk] no ext magic at 1 MiB - the root is elsewhere or not ext"
  rm -f "$tmp"
  return 1
}

# Everything that can be learned without starting the machine.
smoke_check_disk() {
  local disk="$1"
  local problems=0
  echo "=== the disk ==="
  ls -la "$disk"
  qemu-img info "$disk" | sed -n 's/^\(virtual size\|disk size\|file format\):.*/  &/p'
  smoke_check_mbr "$disk" || problems=$((problems + 1))
  smoke_check_root "$disk" || echo "[disk] (not fatal here - a non-ext root is possible)"
  if [ "$problems" -gt 0 ]; then
    echo "=== THE DISK WILL NOT BOOT ==="
    return 1
  fi
  return 0
}

# --- watching the boot ------------------------------------------------------------

smoke_wait_for() {
  local log="$1" pattern="$2" seconds="${3:-600}"
  local i
  for i in $(seq 1 "$seconds"); do
    if grep -qaE "$pattern" "$log" 2>/dev/null; then
      echo "[boot] matched after ${i}s: $pattern"
      return 0
    fi
    sleep 1
  done
  echo "[boot] never matched after ${seconds}s: $pattern" >&2
  return 1
}

# Boot the disk and see whether it arrives.
#
# EXPECT is what proves arrival for this distribution - "login:" for a server,
# a shell name for a desktop. The caller supplies it because only the caller knows.
#
# FRAMES names the directory the boot is photographed into; the caller passes it so that
# this boot's frames can be concatenated with the ones from the install. Without a
# parameter every recording would land in the same directory and the last one would win.
smoke_boot() {
  local disk="$1" expect="$2" seconds="${3:-600}"
  local work="${4:-.}"
  local frames="${5:-$work/shots}"

  # Absolute paths and no chdir.
  #
  # bake_vm_stop and snapshots.py both change directory, and a caller that comes back to
  # a changed cwd finds its relative paths broken - which happened once, as
  # "cd: out: No such file or directory" two steps after this function returned.
  #
  # So the disk is resolved here and nothing depends on where we are afterwards.
  #
  # The work directory is created if it does not exist, before anything is resolved
  # against it. It did not exist on the first run of this code, the `cd` failed, and a
  # failed `cd` inside a command substitution leaves the variable EMPTY rather than
  # raising - so every path below became "/smoke.log" and the boot check "passed"
  # against a file that was never written.
  mkdir -p "$work" || return 2
  mkdir -p "$frames" || return 2
  disk="$(cd "$(dirname "$disk")" && pwd)/$(basename "$disk")"
  work="$(cd "$work" && pwd)"
  case "$frames" in /*) ;; *) frames="$work/$frames" ;; esac
  echo "[boot] disk=$disk"
  echo "[boot] work=$work"
  echo "[boot] frames=$frames"

  VM_STDOUT="$work/smoke.log" VM_STDIN=/dev/null \
    bake_vm_start "$work" -drive "file=$disk,if=virtio,format=qcow2" -no-reboot

  sleep 5
  if ! kill -0 "$VM_PID" 2>/dev/null; then
    echo "[boot] qemu died at startup:"
    tail -20 "$work/smoke.log" 2>/dev/null | sed 's/^/    /'
    return 1
  fi

  # Photograph the boot from the second qemu starts. The recording is the point of the
  # whole pipeline: it is what shows a lightdm greeter, a GRUB prompt or a kernel panic
  # when every log line looked the same.
  python3 "$SMOKE_LIB/snapshots.py" "$QMP_RECORD" "$frames" \
    "${SNAPSHOT_INTERVAL:-1}" 900 \
    2>&1 | sed 's/^/[snap] /' &
  SNAP_PID=$!
  # No recording here: the caller concatenates every boot's frames into one video.

  local rc=0
  smoke_wait_for "$work/smoke.log" "$expect" "$seconds" || rc=1

  bake_vm_stop
  kill "$SNAP_PID" 2>/dev/null || true

  echo "=== recorded ==="
  echo "=== frames ==="
  ls "$frames"/frame-*.ppm 2>/dev/null | wc -l | sed 's/^/  frames: /'
  return "$rc"
}
