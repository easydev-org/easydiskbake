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
    # A RESOLUTION, IN PIXELS, FOR THE GUEST'S PRIMARY SCREEN.
    #
    # Empty means -vga <vga>, which is what every guest had before this existed: a
    # Bochs VGA adapter at whatever mode the firmware picks, 1280x800 on the OVMF
    # image used here. Non-empty replaces that with -device VGA,xres=,yres=, because
    # -vga and -device are exclusive and -vga has no size option.
    #
    # WHY IT MATTERS FOR A BAKE. Every coordinate in the drivers is a pixel position
    # measured on a particular screen. macos/15/install.py already learned this: with
    # vmware-svga's 1280x800 default its coordinates were off the edge of the screen
    # for four runs. The Windows driver types rather than clicks, so a wrong size was
    # not fatal there - but a launch-windows recording at 1280x800 is a picture of a
    # desktop nobody will ever see.
    #
    # 1920x1080 IS THE DEFAULT, NOT None, AND THAT IS THE FIX FOR RUN 41584280121.
    #
    # None meant "let the firmware choose", and OVMF chooses 1280x800. So bake.py,
    # ssh.py and devtools.py each installed and inspected Windows at 1280x800 while
    # launch_windows.py photographed it at 1920x1080 - four stages of one pipeline on
    # two different screens. The frames from the ssh stage prove it: 1280x800, and the
    # desktop failed lib/desktop.py's saturation test at 0.0966 against a 0.10
    # threshold, because the taskbar occupies the sampled bottom band at that size.
    #
    # Passing None is still allowed for a caller that genuinely wants the firmware
    # default - Windows setup's own graphics are drawn at whatever the adapter reports.
    resolution: tuple[int, int] | None = (1920, 1080)
    # The CPU model, and "" means "-cpu host".
    #
    # macOS cannot use "host": its kernel reads the CPU's family and model and refuses
    # to boot on anything it does not recognise, so the guest has to be given a model
    # Apple shipped in real hardware (Penryn, Haswell, ...) with the feature flags the
    # kernel expects. Every other guest here is happy with "host" and gets it.
    cpu: str = ""
    # The -smp value, when a guest needs a topology rather than a count. macOS is the
    # one that does: dockur/macos describes cores as sockets=1,dies=1,cores=N,threads=1
    # because the kernel reads the topology and a bare "4" leaves qemu to invent one it
    # does not expect. Empty means `-smp <cpus>`, which is what every other guest gets.
    smp_topology: str = ""
    # Extra flags appended to the argument vector, for machines that need a global
    # (macOS needs ICH9-LPC's sleep states disabled, which is not a per-device option).
    extra_args: list[str] = field(default_factory=list)
    vnc_bind: str = "127.0.0.1"
    vnc_display: int = 0
    ssh_port: int = 2222
    rdp_port: int = 3389
    # A host directory to expose over SMB. Empty means no share, and the netdev is
    # exactly what it was before this field existed - see lib/share.py for why SMB
    # rather than virtiofs or 9p.
    share_path: str = ""

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
            cpu=env.get("VM_CPU", ""),
            smp_topology=env.get("VM_SMP", ""),
            vnc_bind=env.get("VNC_BIND", "127.0.0.1"),
            vnc_display=int(env.get("VNC_DISPLAY", 0)),
            ssh_port=int(env.get("SSH_PORT", 2222)),
            rdp_port=int(env.get("RDP_PORT", 3389)),
            share_path=env.get("VM_SHARE_PATH", ""),
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
        "-enable-kvm", "-cpu", shape.cpu or "host",
        "-smp", shape.smp_topology or str(shape.cpus), "-m", str(shape.mem_mb),
        "-display", "none",
        "-vnc", f"{shape.vnc_bind}:{shape.vnc_display}",
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-tablet,bus=xhci.0",
        "-monitor", f"unix:{paths.monitor},server=on,wait=off",
        "-qmp", f"unix:{paths.drive},server=on,wait=off",
        "-qmp", f"unix:{paths.record},server=on,wait=off",
        "-serial", "mon:stdio",
    ]

    # Networking, with or without a share. A share adds `smb=<dir>` to the same
    # user-mode netdev, which makes qemu fork its own smbd; the guest reaches it at
    # 10.0.2.4. See lib/share.py.
    if shape.share_path:
        from share import Share

        share = Share(shape.share_path)
        args += share.netdev_args(
            ssh_port=shape.ssh_port, rdp_port=shape.rdp_port,
        )
    else:
        args += [
            "-netdev", f"user,id=n0,hostfwd=tcp::{shape.ssh_port}-:22,"
                       f"hostfwd=tcp::{shape.rdp_port}-:3389",
            "-device", "virtio-net-pci,netdev=n0",
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

    # -vga AND -device ARE EXCLUSIVE, AND vga="none" MEANS THE CALLER BRINGS ITS OWN.
    #
    # The order here is deliberate. vga="none" wins: macOS passes it and puts a
    # virtio-vga in extra_args with its own xres/yres, and adding a second VGA from this
    # branch would give the guest two adapters. That was harmless while resolution
    # defaulted to None and macOS was the only caller using "none" - it is not harmless
    # now that 1920x1080 is the default for everybody.
    if shape.vga == "none":
        pass
    elif shape.resolution:
        # -device VGA, not -vga std: only the device form takes xres/yres.
        #
        # VGAMEM_MB IS NOT OPTIONAL AT THIS SIZE. -vga std and -device VGA default to
        # 8 MiB of video memory, and 1920x1080 at 32 bits per pixel is 7.91 MiB before
        # double buffering - so the default is just too small and the adapter comes up
        # unable to hold a frame at the requested mode. The symptom is not an error:
        # the screen is black from the first captured frame, which is what
        # launch-windows run 41565960604 reported (1920x1080, mean grey 0, for 521
        # seconds) after the same disk had drawn a firmware menu at 1280x800.
        #
        # 32 MiB is four frames of headroom at this size, and it is the value the
        # virtio-vga path in macos/15/machine.py has been using.
        width, height = shape.resolution
        args += ["-device", f"VGA,xres={width},yres={height},vgamem_mb=32"]
    else:
        args += ["-vga", shape.vga]

    # Machine-level flags that are not a device and not a -global the caller can put in
    # `extra` without it looking like a device. macOS needs two of these.
    if shape.extra_args:
        args += list(shape.extra_args)

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

    def stop(self, *, acpi_first: bool = False, acpi_timeout: int = 60) -> None:
        """Stop qemu.

        `acpi_first` asks the guest to shut down through ACPI before the process is
        killed. That is the difference between a disk Windows closed properly and one
        that was powered off mid-write: on the next boot the second kind runs chkdsk or
        lands in the recovery environment.

        It is off by default because every other caller in this repository stops a
        machine that is going to be thrown away or a guest that has already hung.
        """
        if self.proc is None:
            return

        if acpi_first:
            self._acpi_shutdown(acpi_timeout)

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

    def _acpi_shutdown(self, timeout: int) -> None:
        """Send the guest a power button press and wait for it to go away.

        system_powerdown is what the ACPI power button does: Windows sees it, stops its
        services, flushes its filesystems and powers off. The qemu process then exits on
        its own, and the SIGTERM that follows finds nothing to do.

        If the guest does not respond the caller's SIGTERM still lands, so this can only
        make the shutdown cleaner - it cannot hang the bake.
        """
        import time

        try:
            import qmp as qmpmod

            client = qmpmod.QMP(self.paths.drive)
        except (OSError, RuntimeError, ImportError) as exc:
            log(f"could not open QMP for an ACPI shutdown ({exc}); killing instead")
            return

        try:
            log("asking the guest to shut down")
            client.execute("system_powerdown")
        except (OSError, RuntimeError) as exc:
            log(f"system_powerdown failed ({exc}); killing instead")
            client.close()
            return
        finally:
            client.close()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                log(f"the guest powered off after {timeout - int(deadline - time.time())}s")
                return
            time.sleep(2)

        log(f"the guest did not power off within {timeout}s; killing it")


def wait_port(host: str, port: int, seconds: int = 60, label: str = "port") -> bool:
    """Wait for a TCP port to answer, or give up saying which one did not."""
    for i in range(1, seconds + 1):
        if port_open(host, port):
            log(f"{label} is up: {host}:{port} after {i}s")
            return True
        time.sleep(1)
    log(f"{label} did NOT come up: {host}:{port} after {seconds}s")
    return False


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """One TCP connect attempt. No retries, no sleeping."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def rdp_serving(rdp_port: int, host: str = "127.0.0.1") -> bool:
    """Is the guest actually answering RDP, rather than just the forward existing?

    THIS IS NOT THE SAME AS port_open, AND THE DIFFERENCE COST A RUN.

    `-netdev user,hostfwd=tcp::3389-:3389` makes QEMU bind the HOST port the moment
    it starts, before the guest has booted anything. A TCP handshake with that port
    therefore succeeds immediately and proves only that QEMU is alive - and a bake
    that checked port_open() declared "the guest is a running Windows" six seconds
    after qemu-img create, having installed nothing at all.

    What distinguishes a real RDP server is the protocol: the server speaks first,
    sending a TPKT/COTP Connection Confirm (or a Negotiation Response) before the
    client says anything. So the check reads bytes and requires them to look like
    X.224: version 3, and a packet length that matches what arrived.
    """
    try:
        with socket.create_connection((host, rdp_port), timeout=3) as sock:
            sock.settimeout(3)
            try:
                data = sock.recv(11)
            except socket.timeout:
                # A forward with nothing behind it accepts and then says nothing.
                return False
    except OSError:
        return False

    # TPKT header: version 3, reserved 0, then a 2-byte big-endian length.
    #   byte 0  0x03
    #   byte 1  0x00
    #   bytes 2-3  length, always >= 11 for X.224
    if len(data) < 4:
        return False
    if data[0] != 0x03 or data[1] != 0x00:
        return False
    length = (data[2] << 8) | data[3]
    return length >= 11


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
