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

# Record the screen for as long as qemu runs.
#
# ffmpeg reads qemu's own VNC server, so the recording is exactly what a person would
# have seen - including the parts no log line mentions. Stopped by killing the pid.
#
# -f vnc takes a host:port; -framerate 5 is enough to read a progress bar without
# producing a file nobody can open. yuv420p and faststart, because a recording that
# needs a particular player is a recording nobody watches.
bake_vm_record() {
  local out="${1:-run.mp4}"
  local port=$((5900 + VNC_DISPLAY))
  : "${RECORD_FPS:=5}"

  echo "[record] $VNC_BIND:$port -> $out at ${RECORD_FPS}fps"
  ffmpeg -hide_banner -loglevel warning -nostdin \
    -f vnc -framerate "$RECORD_FPS" -i "${VNC_BIND}:${port}" \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
    -movflags +faststart \
    "$out" &
  RECORD_PID=$!
  echo "[record] ffmpeg pid $RECORD_PID"
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
