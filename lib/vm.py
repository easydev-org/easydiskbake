"""Boot a guest that can be watched, typed into, and reached over SSH.

    from vm import Guest, build_args

    args = build_args(work, extra=["-kernel", k, "-initrd", i])   # pure, testable
    guest = Guest(work, extra=[...]).start()
    ...
    guest.stop()

Four channels, and each has a reason:

  -vnc host:port   a human can look at the screen and take over with a mouse.
                   This is the difference between a test that reports "the
                   deadline passed" and one where somebody can see what was on
                   the screen.
  -qmp unix:...    the bake drives it: absolute pointer, key events,
                   screendump. Two sockets, because QMP serves one client per
                   socket and the driver and the recorder must not fight for it.
  -serial stdio    the guest's own words, which is what most automated checks
                   read.
  hostfwd 22->2222 a way in that does not depend on the console at all.

WHY hostfwd AND NOT A BRIDGE

User-mode networking gives the guest 10.0.2.15 with the host at 10.0.2.2, and
hostfwd puts a host port in front of a guest port. No bridge, no tap device, no
privileges beyond /dev/kvm - which is what makes this runnable inside a container
job. The cost is that the guest cannot be dialled from elsewhere on the network;
for a bake that is a feature, not a limitation.

The VNC port is bound to 127.0.0.1 unless VNC_BIND says otherwise. A bake is not
a service, and an unauthenticated VNC server on a shared runner is not something
to publish by accident.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field

QEMU = "qemu-system-x86_64"


def log(msg: str) -> None:
    print(f"[vm] {msg}", flush=True)


@dataclass
class Shape:
    """The machine the guest is given.

    A Linux installer is happy with SeaBIOS and a std VGA: it boots from the ISO,
    writes a disk and prints to a serial console. Everything above that -
    Windows 11 and macOS - is not, and the difference is not a driver but a
    machine: UEFI instead of SeaBIOS, q35 instead of the default 440fx, pflash
    vars that have to persist between boots, and for Windows 11 a TPM.

    They live here, as parameters, rather than duplicated per platform, because a
    guest that needs UEFI needs the same three things done the same way.

    The defaults are exactly what MX used before this existed, so nothing that
    worked changes.
    """

    cpus: int = 4
    mem_mb: int = 6144
    machine: str = ""            # "" -> qemu default i440fx; "q35" for UEFI guests
    firmware_code: str = ""      # OVMF_CODE.fd, read-only pflash
    firmware_vars: str = ""      # a WRITABLE copy per guest; boot state accumulates
    tpm_socket: str = ""         # swtpm chardev socket, or "" for no TPM
    vga: str = "std"             # "std" (default), "vmware", "none"
    vnc_bind: str = "127.0.0.1"
    vnc_display: int = 0
    ssh_port: int = 2222
    rdp_port: int = 3389

    @classmethod
    def from_env(cls) -> "Shape":
        env = os.environ
        return cls(
            cpus=int(env.get("VM_CPUS", 4)),
            mem_mb=int(env.get("VM_MEM", 6144)),
            machine=env.get("VM_MACHINE", ""),
            firmware_code=env.get("VM_FIRMWARE_CODE", ""),
            firmware_vars=env.get("VM_FIRMWARE_VARS", ""),
            tpm_socket=env.get("VM_TPM", ""),
            vga=env.get("VM_VGA", "std"),
            vnc_bind=env.get("VNC_BIND", "127.0.0.1"),
            vnc_display=int(env.get("VNC_DISPLAY", 0)),
            ssh_port=int(env.get("SSH_PORT", 2222)),
            rdp_port=int(env.get("RDP_PORT", 3389)),
        )


@dataclass
class Paths:
    """Where the two QMP sockets and the monitor socket live."""

    work: str
    drive: str = field(init=False)
    record: str = field(init=False)
    monitor: str = field(init=False)

    def __post_init__(self) -> None:
        self.drive = os.path.join(self.work, "drive.sock.qmp")
        self.record = os.path.join(self.work, "record.sock.qmp")
        self.monitor = os.path.join(self.work, "monitor.sock")

    def clear(self) -> None:
        for p in (self.drive, self.record, self.monitor):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


def build_args(work: str, extra: list[str] | None = None,
               shape: Shape | None = None) -> list[str]:
    """The complete qemu argument vector. Pure: no process, no filesystem writes.

    This is the function that makes the machine shape reviewable and testable.
    The shell version appended to an array inside the same function that forked
    qemu, so the only way to find out what command line it produced was to run a
    VM - and on a machine without /dev/kvm that was impossible.
    """
    shape = shape or Shape.from_env()
    paths = Paths(work)

    args: list[str] = [
        QEMU,
        "-enable-kvm", "-cpu", "host",
        "-smp", str(shape.cpus), "-m", str(shape.mem_mb),
        "-display", "none",
        "-vnc", f"{shape.vnc_bind}:{shape.vnc_display}",
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-tablet,bus=xhci.0",
        "-monitor", f"unix:{paths.monitor},server=on,wait=off",
        "-qmp", f"unix:{paths.drive},server=on,wait=off",
        "-qmp", f"unix:{paths.record},server=on,wait=off",
        "-netdev", f"user,id=n0,hostfwd=tcp::{shape.ssh_port}-:22,hostfwd=tcp::{shape.rdp_port}-:3389",
        "-device", "virtio-net-pci,netdev=n0",
        "-serial", "mon:stdio",
    ]

    if shape.machine:
        args += ["-machine", shape.machine]

    # The vars file is the guest's NVRAM: a firmware that booted once keeps its
    # boot order, its Secure Boot state and - for macOS - the OpenCore entries.
    # It has to be a writable copy, never the shared template.
    if shape.firmware_code:
        args += ["-drive", f"if=pflash,format=raw,readonly=on,file={shape.firmware_code}"]
    if shape.firmware_vars:
        if not os.path.exists(shape.firmware_vars):
            raise ValueError(
                f"VM_FIRMWARE_VARS={shape.firmware_vars} does not exist. "
                "It must be a writable copy of the vars template, made by the caller."
            )
        args += ["-drive", f"if=pflash,format=raw,file={shape.firmware_vars}"]

    # A TPM is a separate process (swtpm) that this does not start: it owns state
    # that has to outlive one boot, and starting it here would make stop() the
    # only thing that ever cleans it up. The caller starts swtpm and passes its
    # chardev socket.
    if shape.tpm_socket:
        args += [
            "-chardev", f"socket,id=chrtpm,path={shape.tpm_socket}",
            "-tpmdev", "emulator,id=tpm0,chardev=chrtpm",
            "-device", "tpm-crb,tpmdev=tpm0",
        ]

    # -vga and -device VGA are mutually exclusive; a caller wanting vgamem_mb
    # passes vga="none" and its own -device.
    if shape.vga != "none":
        args += ["-vga", shape.vga]

    if extra:
        args += list(extra)
    return args


class Guest:
    """A running qemu, with the paths and the pid that belong to it."""

    def __init__(self, work: str, extra: list[str] | None = None,
                 shape: Shape | None = None,
                 stdin: str | None = None, stdout: str | None = None):
        self.work = work
        self.paths = Paths(work)
        self.shape = shape or Shape.from_env()
        self.extra = extra or []
        self.stdin = stdin or "/dev/null"
        self.stdout = stdout or os.path.join(work, "console.log")
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Guest":
        self.paths.clear()
        args = build_args(self.work, self.extra, self.shape)

        log(f"vnc    {self.shape.vnc_bind}:{5900 + self.shape.vnc_display}")
        log(f"ssh    localhost:{self.shape.ssh_port} -> guest:22")
        log(f"qmp    drive={self.paths.drive} record={self.paths.record}")
        log("serial stdio")
        if self.shape.machine:
            log(f"machine {self.shape.machine}")
        if self.shape.firmware_code:
            log(f"uefi   code={self.shape.firmware_code}")
        if self.shape.firmware_vars:
            log(f"uefi   vars={self.shape.firmware_vars}")
        if self.shape.tpm_socket:
            log(f"tpm    {self.shape.tpm_socket}")

        os.makedirs(self.work, exist_ok=True)
        self.proc = subprocess.Popen(
            args,
            stdin=open(self.stdin, "rb") if os.path.exists(self.stdin) else subprocess.DEVNULL,
            stdout=open(self.stdout, "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log(f"qemu pid {self.proc.pid}")
        return self

    @property
    def pid(self) -> int:
        if self.proc is None:
            raise RuntimeError("guest not started")
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=5)
        log("qemu stopped")


def wait_port(host: str, port: int, seconds: int = 60, label: str = "port") -> bool:
    """Wait for a TCP port to answer, or give up saying which one did not."""
    for i in range(1, seconds + 1):
        try:
            with socket.create_connection((host, port), timeout=1):
                log(f"{label} is up: {host}:{port} after {i}s")
                return True
        except OSError:
            time.sleep(1)
    log(f"{label} did NOT come up: {host}:{port} after {seconds}s")
    return False


def record_many(out: str, *frame_dirs: str, fps: int = 1) -> bool:
    """Encode several frame directories as one video, in the order given.

    A bake is not one boot. MX installs in the first one and installs its
    bootloader in a second, and a recording that covers only the first is 55
    seconds of a nine-minute job - which is exactly what the first attempt
    produced.

    The frames are renumbered into a scratch directory rather than renamed in
    place, so the originals keep the timestamps that say when each was taken.

    Not the VNC stream: this ffmpeg has no vnc demuxer ("Unknown input format:
    'vnc'") because Debian builds it without --enable-libvncclient. The frames
    are captured over QMP instead, which needs nothing the image does not
    already have.
    """
    import tempfile

    if not shutil.which("ffmpeg"):
        log("record: no ffmpeg")
        return False

    stage = tempfile.mkdtemp()
    try:
        n = 0
        for d in frame_dirs:
            if not os.path.isdir(d):
                continue
            frames = sorted(f for f in os.listdir(d) if f.startswith("frame-") and f.endswith(".ppm"))
            if not frames:
                log(f"record: no frames in {d}, skipping")
                continue
            log(f"record: {len(frames)} frames from {d}")
            for i, name in enumerate(frames):
                shutil.copyfile(os.path.join(d, name), os.path.join(stage, f"frame-{n:04d}.ppm"))
                n += 1

        if n == 0:
            log("record: nothing to encode")
            return False

        log(f"record: {n} frames in total -> {out} at {fps}fps")
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
                "-framerate", str(fps), "-start_number", "0",
                "-i", os.path.join(stage, "frame-%04d.ppm"),
                "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                out,
            ],
            check=False,
        )
        if result.returncode != 0:
            log("record: encoding failed")
            return False
        log(f"record: {os.path.getsize(out)} bytes")
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)
