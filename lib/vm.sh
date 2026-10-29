# Boot a guest that can be watched, typed into, and reached over SSH.
#
# Four channels, and each has a reason:
#
#   -vnc host:port   a human can look at the screen and take over with a mouse. This
#                    is the difference between a test that reports "the deadline passed"
#                    and one where somebody can see what was on the screen.
#   -qmp unix:...     the bake drives it: absolute pointer, key events, screendump. Two
#                    sockets, because QMP serves one client per socket and the driver
#                    and the recorder must not fight for it.
#   -serial stdio     the guest's own words, which is what most automated checks read.
#   hostfwd 22->2222  a way in that does not depend on the console at all.
#
# WHY hostfwd AND NOT A BRIDGE
#
# User-mode networking gives the guest 10.0.2.15 with the host at 10.0.2.2, and
# hostfwd puts a host port in front of a guest port. No bridge, no tap device, no
# privileges beyond /dev/kvm - which is what makes this runnable inside a container
# job. The cost is that the guest cannot be dialled from elsewhere on the network; for
# a bake that is a feature, not a limitation.
#
# The VNC port is bound to 127.0.0.1 unless VNC_BIND says otherwise. A bake is not a
# service, and an unauthenticated VNC server on a shared runner is not something to
# publish by accident.

bake_vm_start() {
  local work="$1"; shift

  : "${VNC_DISPLAY:=0}"
  : "${VNC_BIND:=127.0.0.1}"
  : "${SSH_PORT:=2222}"
  : "${VM_CPUS:=4}"
  : "${VM_MEM:=6144}"
  : "${VM_DISK:=disk.qcow2}"

  QMP_DRIVE="$work/drive.sock.qmp"
  QMP_RECORD="$work/record.sock.qmp"
  MON="$work/monitor.sock"
  rm -f "$QMP_DRIVE" "$QMP_RECORD" "$MON"

  echo "[vm] vnc    ${VNC_BIND}:$((5900 + VNC_DISPLAY))"
  echo "[vm] ssh    localhost:${SSH_PORT} -> guest:22"
  echo "[vm] qmp    drive=$QMP_DRIVE record=$QMP_RECORD"
  echo "[vm] serial stdio"

  qemu-system-x86_64 \
    -enable-kvm -cpu host -smp "$VM_CPUS" -m "$VM_MEM" \
    -display none \
    -vga std \
    -vnc "${VNC_BIND}:${VNC_DISPLAY}" \
    -device qemu-xhci,id=xhci \
    -device usb-tablet,bus=xhci.0 \
    -monitor "unix:$MON,server=on,wait=off" \
    -qmp "unix:$QMP_DRIVE,server=on,wait=off" \
    -qmp "unix:$QMP_RECORD,server=on,wait=off" \
    -netdev "user,id=n0,hostfwd=tcp::${SSH_PORT}-:22" \
    -device virtio-net-pci,netdev=n0 \
    -serial mon:stdio \
    "$@" \
    < "${VM_STDIN:-/dev/null}" \
    > "${VM_STDOUT:-console.log}" 2>&1 &
  VM_PID=$!
  echo "[vm] qemu pid $VM_PID"
}

# Wait for a TCP port to answer, or give up saying which one did not.
bake_wait_port() {
  local host="$1" port="$2" seconds="${3:-60}" label="${4:-port}"
  local i
  for i in $(seq 1 "$seconds"); do
    if (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
      exec 3<&- 3>&-
      echo "[vm] $label is up: $host:$port after ${i}s"
      return 0
    fi
    sleep 1
  done
  echo "[vm] $label did NOT come up: $host:$port after ${seconds}s" >&2
  return 1
}

# Record the run as video, from frames -- not from the VNC stream.
#
# The obvious approach is `ffmpeg -f vnc -i localhost:5900`. It does not work here:
#
#   [in#0] Unknown input format: 'vnc'
#
# Debian builds ffmpeg without --enable-libvncclient, so there is no VNC demuxer in the
# package at all. VNC itself is fine - the port answered in two seconds - the decoder is
# simply not in this build.
#
# The frames are already being captured over QMP, which needs no extra dependency, is
# driven by us rather than by a protocol, and produces something a person can look at
# individually as well as in sequence. So the recording is made from those:
#
#   QMP screendump every N seconds  ->  ffmpeg -framerate N -i frame-%04d.ppm  ->  mp4
#
# The cost is a cap on smoothness: an installer animating at 30fps is captured at one
# frame every few seconds, so the video is a time-lapse, not a film. For the question
# this answers - what was on the screen, and when did it change - a time-lapse is
# enough, and it is the part that a serial log cannot show at all.
bake_vm_record_from_frames() {
  local dir="$1"
  local out="${2:-run.mp4}"
  : "${RECORD_FPS:=1}"

  if ! ls "$dir"/frame-*.ppm >/dev/null 2>&1; then
    echo "[record] no frames in $dir - nothing to encode"
    return 1
  fi

  local n
  n=$(ls "$dir"/frame-*.ppm | wc -l)
  echo "[record] $n frames from $dir -> $out at ${RECORD_FPS}fps"

  # -start_number 0 matches the frame-0000 naming; yuv420p and faststart because a file
  # that needs a particular player is a file nobody watches.
  if ffmpeg -hide_banner -loglevel warning -nostdin -y \
       -framerate "$RECORD_FPS" -start_number 0 -i "$dir/frame-%04d.ppm" \
       -c:v libx264 -preset ultrafast -pix_fmt yuv420p -movflags +faststart \
       "$out"; then
    ls -la "$out"
    ffprobe -v error -show_entries format=duration,size \
      -show_entries stream=width,height,codec_name,nb_frames \
      -of default=nw=1 "$out" 2>/dev/null | sed 's/^/[record]   /'
    return 0
  fi
  echo "[record] encoding failed"
  return 1
}

# Record several frame directories as one video, in the order given.
#
# A bake is not one boot. MX installs in the first one and installs its bootloader in a
# second, and a recording that covers only the first is 55 seconds of a nine-minute job -
# which is exactly what the first attempt produced.
#
# The frames are renumbered into a scratch directory rather than renamed in place, so the
# originals keep the timestamps that say when each was taken.
bake_vm_record_many() {
  local out="$1"; shift
  : "${RECORD_FPS:=1}"
  local stage
  stage=$(mktemp -d)
  local n=0 dir

  for dir in "$@"; do
    [ -d "$dir" ] || continue
    if ! ls "$dir"/frame-*.ppm >/dev/null 2>&1; then
      echo "[record] no frames in $dir, skipping"
      continue
    fi
    local count
    count=$(ls "$dir"/frame-*.ppm | wc -l)
    echo "[record] $count frames from $dir"
    # Renumbered in shell-glob order, which is the order they were captured.
    for f in "$dir"/frame-*.ppm; do
      cp "$f" "$stage/$(printf 'frame-%04d.ppm' "$n")"
      n=$((n + 1))
    done
  done

  if [ "$n" -eq 0 ]; then
    echo "[record] nothing to encode"
    rm -rf "$stage"
    return 1
  fi

  echo "[record] $n frames in total -> $out at ${RECORD_FPS}fps ($(echo "scale=1; $n / $RECORD_FPS" | bc 2>/dev/null || echo "?")s)"
  local rc=0
  ffmpeg -hide_banner -loglevel warning -nostdin -y \
    -framerate "$RECORD_FPS" -start_number 0 -i "$stage/frame-%04d.ppm" \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -movflags +faststart \
    "$out" || rc=1
  rm -rf "$stage"

  if [ "$rc" -eq 0 ]; then
    ls -la "$out"
    ffprobe -v error -show_entries format=duration,size \
      -show_entries stream=width,height,codec_name,nb_frames \
      -of default=nw=1 "$out" 2>/dev/null | sed 's/^/[record]   /'
  else
    echo "[record] encoding failed"
  fi
  return "$rc"
}

bake_vm_stop() {
  local rc=0
  if [ -n "${RECORD_PID:-}" ]; then
    kill "$RECORD_PID" 2>/dev/null || true
    wait "$RECORD_PID" 2>/dev/null || true
    echo "[record] stopped"
  fi
  if [ -n "${VM_PID:-}" ]; then
    kill "$VM_PID" 2>/dev/null || true
    sleep 3
    kill -9 "$VM_PID" 2>/dev/null || true
    wait "$VM_PID" 2>/dev/null || rc=$?
    echo "[vm] qemu stopped"
  fi
  return 0
}
