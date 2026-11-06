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
  a tap             a real interface on a real link, so the guest's packets leave
                    through the HOST's stack rather than being re-issued by qemu.

WHY A TAP AND NOT qemu's USER-MODE NAT

The userspace NAT looked cheaper: hostfwd, smb= and a DHCP server come free, and it needs no
privileges beyond /dev/kvm. It also terminates the guest's packets inside qemu and re-issues
them from the QEMU PROCESS, which is a different process from the one that has this cluster's
proxy configuration:

    container -> proxy      apple.com 200 in 0.17s
    container -> direct     gdmf.apple.com (macOS's update API) FAILS
    guest -> slirp -> container -> direct   hangs forever

macOS 26's Setup Assistant sat on "Update Mac Automatically" for six minutes because of that,
still spinning, and Escape did not interrupt it. A tap makes the guest's packets leave through
the container's own stack, where whatever routing that stack has applies.

THE THREE THINGS SLIRP GAVE FOR FREE ARE IN lib/net.py, once for every system: the bridge and
tap, dnsmasq for DHCP and DNS, MASQUERADE for outbound, and iptables DNAT for the port forwards
that used to be hostfwd. Guest.start() calls net.up(), so no caller has to remember.

The VNC port is bound to 127.0.0.1 unless VNC_BIND says otherwise. A bake is not
a service, and an unauthenticated VNC server on a shared runner is not something
to publish by accident.

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


def _net():
    """lib/net.py, however this file was reached.

    `import net` ONLY WORKS WHEN lib/ IS ON sys.path, and not every entry point puts it
    there: bsd/freebsd/bake.py adds the repository root and imports `from lib import ...`,
    which is enough for every module except this one. Run 42209680016 died on exactly that,
    forty seconds in, at the first Guest.start():

        File "lib/vm.py", line 459, in start
            import net
        ModuleNotFoundError: No module named 'net'

    A FUNCTION-RUN IMPORT IS WHY IT WAS INVISIBLE FOR SO LONG - the freebsd bake was the
    first caller that starts a guest without lib/ on the path, and every other system
    happened to add it.

    Loading it from this file's own directory removes the question: lib/net.py sits beside
    lib/vm.py by construction, so there is always exactly one answer.
    """
    global _NET
    try:
        return _NET
    except NameError:
        pass
    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("net", os.path.join(here, "net.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # type: ignore[union-attr]
    _NET = module
    return module


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
    # THE GUEST NETWORK, WHEN THE DEFAULT ONE IS NOT USABLE.
    #
    # qemu's user-mode network hands out 10.0.2.0/24 by default: the guest is 10.0.2.15 and
    # its peer, when the HOST connects through a forwarded port, is 10.0.2.2.
    #
    # THAT RANGE IS WHAT BREAKS sshd ON macOS. Every other system here authenticates from it
    # without trouble, but on this guest, run 42119835234:
    #
    #     dscl . -authonly docker admin   AUTHOK                    password right
    #     ssh docker@127.0.0.1            Permission denied         loopback fine
    #     ssh docker@10.0.2.15            Permission denied         own LAN fine
    #     host -> 10.0.2.2 peer           Connection closed         this range, not fine
    #
    # and the unified log names an ADDRESS operation, not an authentication one:
    #
    #     sshd: [com.apple.network:libinfo:si_destination_compare] send failed: Invalid
    #           argument
    #
    # si_destination_compare compares socket addresses in libsystem_info. 10.0.0.0/8 is
    # shared address space, and macOS's libinfo treats parts of it specially - the same
    # library that answers "is this address local" differently from how Linux does.
    #
    # 192.168.240.0/24 is not special to anything: RFC1918, unused by this repository's other
    # stages, and the guest and gateway addresses are the conventional .2 and .15 within it.
    # Empty for every system that does not need it, so their command lines do not change.
    # THE NIC'S OWN ARGUMENTS, WHEN THE DEFAULT DEVICE IS NOT ENOUGH.
    #
    # easydev-org/dockur-macos's drive-in-container.sh, which is a macOS guest that answers
    # ssh, declares its card as
    #
    #     -device virtio-net-pci,id=net0,netdev=hostnet0,romfile=,mac=00:16:CB:53:E9:8C
    #
    # and this repository declared a bare `virtio-net-pci,netdev=n0`. Two differences:
    #
    #     romfile=   no iPXE option ROM. The firmware does not probe this card for network
    #                boot, so macOS never sees the "is this a boot device" path for it.
    #     mac=       the MAC matches the one in OpenCore's identity, which is what macOS
    #                builds its network service records from. A guest whose NIC address and
    #                whose configured identity disagree is described by two records, and the
    #                libinfo calls that fail here (si_destination_compare) are exactly the
    #                "which address is this host" lookups.
    #
    # Empty by default so no other system's command line changes.
    # A BRIDGED TAP INSTEAD OF SLIRP, WHEN THE GUEST NEEDS A REAL ROUTE OUT.
    #
    # qemu's user-mode network is a userspace NAT: the guest's packets are terminated inside
    # qemu and re-issued by the qemu PROCESS. That is fine when the only peer is the host, and
    # useless when the guest must reach the internet through a proxy the container has and
    # qemu does not:
    #
    #     container -> proxy      apple.com 200 in 0.17s
    #     container -> direct     gdmf.apple.com (macOS's update API) FAILS
    #     guest -> slirp -> container -> direct   hangs
    #
    # macOS 26's Setup Assistant sits on "Update Mac Automatically" waiting for that API -
    # measured here, still spinning after six minutes, and Escape does not interrupt it.
    #
    # A tap on a bridge with MASQUERADE is what easydev-org/easysandbox-macos does
    # (sandbox.sh, setup_network), and it gives the guest a real address on a real link:
    #
    #     ip link add qemubr0 type bridge; ip addr add 172.30.0.1/24
    #     ip tuntap add qemutap0 mode tap; ip link set qemutap0 master qemubr0
    #     dnsmasq --dhcp-range=172.30.0.2,172.30.0.2 --dhcp-host=<mac>,,172.30.0.2
    #     iptables -t nat -A POSTROUTING -s 172.30.0.0/24 -j MASQUERADE
    #
    # The guest then ARPs, DHCPs and routes like any machine, and its packets leave through
    # the same stack the container uses - including whatever policy routing that stack has.
    # THERE IS ONE NETWORK MODE. See lib/net.py for why: a userspace NAT re-issues the
    # guest's packets from qemu's own process, which does not carry the proxy this cluster
    # needs, and macOS 26 hung for six minutes waiting for an Apple API it could not reach.
    #
    # Every guest now runs on a tap created by lib/net.py - bridge, dnsmasq, MASQUERADE and
    # the port forwards - so this field only says WHICH tap. Empty means the default.
    tap_ifname: str = ""
    tap_mac: str = ""
    # A host directory to expose over SMB. Empty means no share, and the netdev is
    # exactly what it was before this field existed - see lib/share.py for why SMB
    # rather than virtiofs or 9p.
    # HOW THE GUEST's SERIAL PORT IS WIRED.
    #
    # The default is "-serial mon:stdio", which multiplexes the monitor and the console
    # onto the process's own stdio. That is right for a bake whose guest says everything
    # once and is then photographed: the console lands in console.log and nothing ever
    # writes back.
    #
    # ONLY NIXOS USES THE OTHER MODE, AND THAT IS DELIBERATE.
    #
    # NixOS has no unattended answer file. Its installer is nixos-install, run by hand at
    # a root shell, so its bake types whole commands at the serial console - the way
    # easysandbox-vm's bake-nixos-minimal.sh works and why that script needs a fifo.
    #
    # EVERY OTHER DISTRIBUTION HERE IS DRIVEN THROUGH THE SCREEN. MX 25.3's minstall has
    # no working unattended mode (three were measured and rejected, recorded in
    # linux/mx/minstall.conf), and Mint's ubiquity is the same kind of wizard. Those go
    # through the GUI: QMP clicks at measured coordinates, with OCR to confirm a pane
    # arrived. That is linux/mx/drive-mx.py's design and it does not change.
    #
    # So this stays "" unless a caller is installing something that is answered at a
    # shell. Setting it to a path makes qemu's serial a chardev on that pipe instead, and
    # the caller writes the script into it - see lib/serial.py.
    #
    # "" means the default above. Anything else is a path to a fifo, and the caller owns
    # creating and opening it: qemu opens the writer side, so the file must exist or the
    # guest has no console at all.
    serial_pipe: str = ""
    # THE GUEST's KERNEL COMMAND LINE, for a boot that supplies -kernel/-initrd itself.
    #
    # Only meaningful together with those two flags in `extra`; qemu ignores -append
    # otherwise, so an empty value is the normal case and changes nothing.
    kernel_append: str = ""

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

    # KVM, ALWAYS, AND A MACHINE WITHOUT IT IS A MACHINE THAT DOES NOT RUN THIS.
    #
    # There was a VM_ACCEL=tcg switch here for one revision. It is gone. Software emulation
    # is twenty to fifty times slower, which turns a twenty-minute bake into an hour and
    # makes every timing in every driver wrong - a settle that is right under KVM is far too
    # short under TCG, so a run that passes says nothing about the run that matters. A
    # fallback like that does not let you test the driver; it lets you test a different
    # machine and then be surprised by CI.
    #
    # So there is no fallback and no detection. qemu is told to use KVM, and on a host
    # without /dev/kvm it exits with
    #
    #     Could not access KVM kernel module: No such file or directory
    #
    # which is the correct answer: this repository's bakes run on GitHub's runners, and they
    # have KVM. lib/ci.py checks for it before any of this is reached.
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
    ]

    # THE CONSOLE, TWO WAYS. See Shape.serial_pipe: "" is the multiplexed stdio every
    # photographed bake uses, and a path is a chardev for a guest that is installed by
    # typing at it - NixOS only.
    #
    # THE MONITOR SOCKET IS UNTOUCHED. It is on its own unix socket above, so a guest
    # whose console is a pipe still has QMP and the human monitor exactly as before. The
    # only thing that changes is where the SERIAL port goes.
    if shape.serial_pipe:
        args += [
            "-chardev", f"pipe,id=serial0,path={shape.serial_pipe}",
            "-serial", "chardev:serial0",
        ]
    else:
        args += ["-serial", "mon:stdio"]

    # THE KERNEL COMMAND LINE, when the caller supplies -kernel/-initrd in `extra`.
    # qemu ignores -append without them, so both orderings are safe.
    if shape.kernel_append:
        args += ["-append", shape.kernel_append]

    # Networking, with or without a share. A share adds `smb=<dir>` to the same
    # user-mode netdev, which makes qemu fork its own smbd; the guest reaches it at
    # 10.0.2.4. See lib/share.py.
    # THE ONE NETWORK MODE. lib/net.py creates the bridge, the tap, DHCP, NAT and the port
    # forwards; this only attaches qemu to the interface that already exists.
    #
    # `script=no,downscript=no` BECAUSE THERE IS NO HELPER. qemu's default is to run
    # /etc/qemu-ifup when the tap comes up, and a container has no such file - so the default
    # fails loudly, after the guest has been created.
    tap = shape.tap_ifname or "qemutap0"
    mac = shape.tap_mac or "52:54:00:56:4d:01"
    args += [
        "-netdev", f"tap,id=n0,ifname={tap},script=no,downscript=no",
        "-device", f"virtio-net-pci,netdev=n0,mac={mac},romfile=",
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

        # THE LINK IS BROUGHT UP HERE, NOT BY EACH CALLER.
        #
        # Twenty-odd places create a Guest and call start(), and every one of them used to get
        # its networking from qemu's user-mode NAT with no setup at all. Moving to a tap means
        # somebody has to create the bridge, the tape device, the DHCP server, the MASQUERADE
        # rules and the port forwards - and "somebody" cannot be twenty callers, because the
        # one that forgets produces a guest with no network and a failure a long way from the
        # cause.
        #
        # net.up() is idempotent: the second and later calls find what is already there.
        net = _net()

        net.up(ssh_port=self.shape.ssh_port, rdp_port=self.shape.rdp_port)
        self.shape.tap_ifname = self.shape.tap_ifname or net.TAP
        self.shape.tap_mac = self.shape.tap_mac or net.GUEST_MAC

        args = build_args(self.work, self.extra, self.shape)

        log(f"vnc    {self.shape.vnc_bind}:{5900 + self.shape.vnc_display}")
        log(f"ssh    {net.FORWARD_HOST}:{self.shape.ssh_port} -> guest:22")
        log(f"qmp    drive={self.paths.drive} record={self.paths.record}")
        # SAY WHICH CONSOLE, NOT A CONSTANT. This line used to read "serial stdio"
        # whatever the shape said, and during an earlier debugging pass it read that way for
        # a run that was in fact using a chardev - so the log argued against the code.
        log(f"serial {'chardev ' + self.shape.serial_pipe if self.shape.serial_pipe else 'stdio'}")
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


def rdp_serving(rdp_port: int, host: str = "") -> bool:
    """Is the guest actually answering RDP, rather than just the forward existing?

    THIS IS NOT THE SAME AS port_open, AND THE DIFFERENCE COST A RUN.

    A PORT FORWARD BINDS THE HOST PORT THE MOMENT IT EXISTS, not when the guest answers. That
    was true of qemu's `hostfwd` and it is still true of the iptables DNAT that replaced it
    (lib/net.py): the rule is in place before qemu has even started, and a TCP handshake with
    that port
    therefore succeeds immediately and proves only that QEMU is alive - and a bake
    that checked port_open() declared "the guest is a running Windows" six seconds
    after qemu-img create, having installed nothing at all.

    What distinguishes a real RDP server is the protocol: the server speaks first,
    sending a TPKT/COTP Connection Confirm (or a Negotiation Response) before the
    client says anything. So the check reads bytes and requires them to look like
    X.224: version 3, and a packet length that matches what arrived.

    THE DEFAULT HOST IS THE BRIDGE ADDRESS, not loopback: a forwarded port exists only
    there. lib/net.py explains the measurement.
    """
    if not host:
        net = _net()
        host = net.FORWARD_HOST
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
            frames = sorted(f for f in os.listdir(d)
                            if f.startswith("frame-") and f.endswith((".ppm", ".jpg")))
            if not frames:
                log(f"record: no frames in {d}, skipping")
                continue
            log(f"record: {len(frames)} frames from {d}")
            for i, name in enumerate(frames):
                # EVERY STAGED FRAME IS A JPEG, WHATEVER IT ARRIVED AS.
                #
                # The encoder needs one extension to build a sequence from, and
                # `-pattern_type glob -i 'frame-*'` with no extension does not work: ffmpeg's
                # image2 demuxer rejects it with
                #
                #     Error opening input: No such file or directory
                #
                # on ffmpeg 7.1.5, while `frame-*.jpg` and `frame-*.ppm` both work. A mixed
                # set - which is what exists while snapshots.py is converting - has neither.
                # So anything that is not already a JPEG is converted here, and the sequence
                # the encoder is given is uniform.
                dst = os.path.join(stage, f"frame-{n:04d}.jpg")
                src = os.path.join(d, name)
                if name.endswith(".jpg"):
                    shutil.copyfile(src, dst)
                else:
                    r = subprocess.run(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                         "-i", src, "-q:v", "4", dst],
                        capture_output=True, text=True,
                    )
                    if r.returncode != 0 or not os.path.exists(dst):
                        log(f"record: could not convert {name}: "
                            f"{(r.stderr or '').strip()[:120]}")
                        continue
                n += 1

        if n == 0:
            log("record: nothing to encode")
            return False

        log(f"record: {n} frames in total -> {out} at {fps}fps")
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
                "-framerate", str(fps), "-start_number", "0",
                "-i", os.path.join(stage, "frame-%04d.jpg"),
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
