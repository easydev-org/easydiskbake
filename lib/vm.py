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


# THE CPU EVERY GUEST GETS UNLESS IT ASKS FOR ANOTHER ONE, AND WHY IT IS NOT `host`.
#
# `-cpu host` PASSES THE RUNNER'S OWN FEATURE SET THROUGH, so THE SAME DISK IS A DIFFERENT MACHINE
# ON EVERY RUNNER. That stays invisible until something inside the guest depends on it, and then it
# is not a warning - it is this, in the guest's first second:
#
#     KERNEL PANIC!
#     Attempted to kill init! exitcode=0x00000004        <- 4 is SIGILL
#
# CachyOS is where it was found. It ships every package as a `v3` build (AVX2 floor) and a `v4`
# build (AVX-512 floor) and its installer picks one from the CPUs it can see. Installed on an
# Intel Xeon with AVX-512 it takes `v4`; booted on an AMD EPYC 7763 - Zen 3, AVX2 and NO AVX-512 -
# the disk panics. One disk, three cpu models, measured in a local pod:
#
#     -cpu Skylake-Client    no AVX-512    PANIC
#     -cpu Skylake-Server    AVX-512       boots to SDDM
#     -cpu host (Xeon 8336C) AVX-512       boots to SDDM
#
# `-cpu` CANNOT REPAIR THIS AFTERWARDS. It hides or reveals CPUID bits; the instructions still run
# on the host's silicon, so asking for AVX-512 on EPYC makes qemu print
#
#     qemu-system-x86_64: warning: host doesn't support requested feature: ...
#
# and the guest panics exactly as before. A DISK THAT DEPENDS ON THE HOST IS NOT PORTABLE, and the
# only way to publish one that is, is to make every machine the same machine.
#
# SO THERE IS ONE MODEL FOR ALL OF THEM. `Skylake-Client-v4`:
#
#     - x86-64-v3 LEVEL: AVX2, BMI1/2, FMA, XSAVE. That is the floor both runners here satisfy
#       (a Xeon 8336C and an EPYC 7763), and it is what the `v3`/`v4` split is drawn on.
#     - `-v4` IS QEMU'S OWN MODEL VERSION, NOT THE MICROARCHITECTURE LEVEL. v1..v4 are successive
#       revisions of the same Skylake-Client definition (IBRS, then no-TSX, then XSAVES). The
#       family has NO AVX-512 - that is `Skylake-Server` - so the name is not a contradiction.
#     - INVARIANT ON EVERY RUNNER. A disk installed under it and booted under it sees the same
#       CPU both times, and an installer that reads the CPU reads the same answer.
#
# MEASURED ON A PUBLISHED DISK: fedora under `Skylake-Client-v4` answers sshd in 20s. And a
# control, because one model change deserves one: manjaro's own `-ssh` disk fails to boot under
# `host` as well, so that disk's problem is its own and not this model.
#
# A GUEST THAT NEEDS SOMETHING ELSE SAYS SO WITH `cpu=`. macOS does, and must.
DEFAULT_CPU = "Skylake-Client-v4"


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
    # THE QEMU GUEST AGENT'S CHANNEL, OR "" FOR NONE.
    #
    # `qga_socket` MAKES A VIRTIO-SERIAL PORT NAMED org.qemu.guest_agent.0 AND A UNIX SOCKET THE
    # HOST CAN SPEAK QGA TO. What that buys: `guest-shutdown` is a REAL shutdown request that the
    # guest's own agent executes - not an ACPI power button, which macOS answers with a dialog.
    #
    # THE DEVICE IS VIRTIO-SERIAL ON BOTH FAMILIES, but what runs inside differs:
    #
    #     Windows   qemu-ga, from virtio-win. Ships as an MSI, installs as the QEMU-GA service.
    #     macOS     Apple ships AppleQEMUGuestAgent, and IT DOES NOT WORK UNDER QEMU: its IOKit
    #               match is AppleVirtIOAgentDevice, which the applevirtio.console driver creates
    #               only under Apple's own Virtualization.framework. So macOS needs a third-party
    #               agent (mav2287/mac-guest-agent), which by default speaks ISA SERIAL rather than
    #               this virtio port, so macOS takes qga_transport="isa" instead.
    qga_socket: str = ""
    # WHICH DEVICE CARRIES THE AGENT, AND THAT IS A PROPERTY OF THE GUEST-SIDE AGENT.
    #
    #     "virtio"   virtio-serial + virtserialport named org.qemu.guest_agent.0.
    #                What qemu-ga on Windows expects, and what Linux expects.
    #     "isa"      isa-serial. What mav2287/mac-guest-agent expects by default, because macOS
    #                under QEMU has no working virtio agent: Apple's own is gated on a driver that
    #                only Apple's Virtualization.framework provides, and the third-party one moved
    #                to ISA in v2.5.0 for exactly that reason.
    #
    # `qemu-guest-agent`'s protocol is the same either way - it is a JSON stream over a chardev -
    # so the host side only has to know which socket to open.
    qga_transport: str = "virtio"
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
    # The CPU model. "" MEANS `DEFAULT_CPU` - NOT `-cpu host`; see the constant above for the
    # panic that made that the answer.
    #
    # macOS overrides this entirely: its kernel reads the CPU's family and model and refuses to
    # boot on anything it does not recognise, so it is given a model Apple shipped in real
    # hardware with the feature flags the kernel expects (macos/15/machine.py).
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
    # THE MTU THE GUEST'S NIC IS TOLD, through virtio-net's MTU feature rather than DHCP
    # or ICMP. Zero means "do not set it", which is what every non-macOS guest did before
    # this field existed. Guest.start() fills it from the tap when left empty - see the
    # long note in build_args() for why a guest larger than its tap is the broken case.
    net_mtu: int = 0
    # A host directory to expose over SMB. Empty means no share, and the netdev is
    # exactly what it was before this field existed - see lib/hostshare.py for why SMB
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
    # NO TAP, WHEN THE CALLER HAS NO USE FOR AN ADDRESS.
    #
    # lib/net.py raises SystemExit when dnsmasq will not start, and every guest here defaults to
    # a tap because every other stage is reached over ssh. A stage that only reads the screen -
    # android/blissos16/steps/launch.yaml, whose frames come over QMP - has nothing to do with
    # dependency, and qemu's own `-netdev user` cannot fail the way a bridge can.
    net: str = "tap"
    # THE GUEST PORT adb LISTENS ON, forwarded 1:1 when `net` is "none".
    #
    # Android is the one family reached over adb before it has an sshd, and qemu's user-mode
    # network forwards nothing unless it is told to. Every other family leaves this 0 and gets no
    # forward at all. See `build_args` and lib/vm_session.py's android installed branch.
    adb_port: int = 0

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
        # `DEFAULT_CPU` AND NOT "host": see the constant above. A guest that needs a
        # different model sets `shape.cpu`, and macOS does.
        "-enable-kvm", "-cpu", shape.cpu or DEFAULT_CPU,
        "-smp", shape.smp_topology or str(shape.cpus), "-m", str(shape.mem_mb),
        "-display", "none",
        "-vnc", f"{shape.vnc_bind}:{shape.vnc_display}",
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-tablet,bus=xhci.0",
        # A USB KEYBOARD, BECAUSE NOT EVERY GUEST READS THE PS/2 ONE.
        #
        # QMP's `input-send-event` delivers a key press to whatever keyboard device the guest
        # has bound. Every guest here has a PS/2 keyboard through qemu's default i8042
        # emulation, and that is what the Calamares, Ubiquity, Flutter and Anaconda drivers
        # have always typed into successfully.
        #
        # NOT THIS ONE. Measured on MX-25.3's `minstall` under Xfce: a click on an entry field
        # followed by QMP keystrokes left the field unchanged, and `keys.type_text` returned
        # `unsupported: []`, so the characters had mapped to qcodes correctly - the events were
        # being produced and not arriving. A triple-click followed by typing did nothing
        # either. The same keystrokes sent by `xdotool` from a terminal INSIDE the guest put
        # `admin` into the password field at once. That is a guest whose GTK entries take input
        # from the HID device and not from the legacy i8042 path.
        #
        # So there is a USB HID keyboard next to the tablet, on the same xHCI controller, and
        # every guest now has both: the PS/2 one it may have been using and the USB one it may
        # need. A guest that binds only one binds the one it wants; a guest that binds both
        # gets every event twice, which is why this is a device and not a replacement - see
        # the driver note below.
        "-device", "usb-kbd,bus=xhci.0",
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
    # 10.0.2.4. See lib/hostshare.py.
    # THE ONE NETWORK MODE. lib/net.py creates the bridge, the tap, DHCP, NAT and the port
    # forwards; this only attaches qemu to the interface that already exists.
    #
    # `script=no,downscript=no` BECAUSE THERE IS NO HELPER. qemu's default is to run
    # /etc/qemu-ifup when the tap comes up, and a container has no such file - so the default
    # fails loudly, after the guest has been created.
    tap = shape.tap_ifname or "qemutap0"
    mac = shape.tap_mac or "52:54:00:56:4d:01"
    # host_mtu IS THE ONLY WAY THIS GUEST LEARNS THE LINK'S MTU.
    #
    # The bridge and the tap are created at the uplink's MTU (lib/net.py, `MTU`), and the
    # guest's default is 1500 because virtio-net advertises nothing. A guest at 1500 on a
    # 1450 link emits full-size 1500-byte frames that the host silently drops, and macOS
    # ignores both the DHCP `option:mtu` and the ICMP fragmentation-needed that would fix
    # it: measured on this cluster, `en0` stayed at 1500 through a lease from a dnsmasq
    # that was handing out mtu 1450, and gdmf.apple.com timed out at 000 while the same
    # request from the container returned 200.
    #
    # The failure it causes is "Failed to download required installer asset
    # (com_apple_MobileAsset_MacSoftwareUpdate.plist)". The TLS handshake is what breaks:
    # the server's certificate is 3443 bytes, so its first segment is a full 1448-byte
    # payload, and every retransmission is dropped the same way - the handshake never
    # completes, so the installer reports a failed download with no network error.
    #
    # host_mtu sets the virtio-net MTU feature, which the guest driver reads into the
    # interface at probe time. It bypasses DHCP and ICMP entirely, which is why it is the
    # fix rather than MSS clamping: the kernel here has no xt_TCPMSS, so lib/net.py's
    # clamp rules are rejected (`RULE_APPEND failed: No such file or directory`) and the
    # guest would keep offering an MSS of 1460 down a 1450-byte path.
    #
    # The value is the tap's MTU, and the ordering matters: a guest LARGER than its tap is
    # the broken case. Measured here, guest 1500 / tap 1450 drops every full segment, and
    # guest 1450 / tap 1450 works.
    net_mtu = shape.net_mtu or 0
    # `romfile=` MUST BE EMPTY. The empty value is what suppresses the iPXE option ROM;
    # a value there would be read as a path to a firmware image. `mtu_arg` begins with a
    # comma so it appends as another device property (`romfile=,host_mtu=1450`).
    mtu_arg = f",host_mtu={net_mtu}" if net_mtu else ""
    #
    # `shape.net = "none"` USES QEMU'S OWN NAT INSTEAD OF THE TAP, AND THIS IS THE HALF THAT
    # MAKES IT TRUE. Guest.start() already skips net.up() for that value, but build_args put a
    # tap on the command line anyway - so a guest that asked for no network still got
    # `-netdev tap,ifname=qemutap0`, and a container without /dev/net/tun had qemu exit before
    # the first QMP message:
    #
    #     ConnectionResetError: [Errno 104] Connection reset by peer
    #     File "lib/qmp.py", line 150, in _read_message
    #
    # - measured on android/blissos16/steps/launch.yaml, which reads the screen over QMP and has
    # for an address. `-netdev user` needs no privileges, no helper and no tap.
    if shape.net == "none":
        # USER-MODE NAT, WITH THE FORWARDS THAT MAKE IT USABLE.
        #
        # `-netdev user,id=n0` with no forwards is right for a stage that only reads the screen
        # over QMP, and wrong for one that reaches the guest: qemu's user-mode network forwards
        # nothing unless told to. android/blissos16/steps/ssh.yaml is that case - it gets in over
        # adb on 5555 and then proves sshd on 8022, and neither port exists on the host without
        # this.
        #
        # THE PORTS ARE FORWARDED TO THEMSELVES. `ssh_port` is what `require_ssh:` and every
        # `ssh:` step dial, and a Termux sshd listens on 8022 - so renumbering it to 22 here
        # would be a second place to keep in step with the file. Same for adb.
        forwards = [f"hostfwd=tcp::{shape.ssh_port}-:{shape.ssh_port}"]
        if shape.adb_port:
            forwards.append(f"hostfwd=tcp::{shape.adb_port}-:{shape.adb_port}")
        args += [
            "-netdev", "user,id=n0," + ",".join(forwards),
            "-device", f"virtio-net-pci,netdev=n0,mac={mac},romfile={mtu_arg}",
        ]
    else:
        args += [
            "-netdev", f"tap,id=n0,ifname={tap},script=no,downscript=no",
            "-device", f"virtio-net-pci,netdev=n0,mac={mac},romfile={mtu_arg}",
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

    # THE QEMU GUEST AGENT'S PORT. `server=on,wait=off` so qemu starts without a client - the
    # host connects when it wants to ask something, and a guest with no host attached is normal.
    #
    # ONE DEVICE, BOTH FAMILIES: virtio-serial is what the guest-side agents expect, and the port
    # NAME is what they look for (`/dev/virtio-ports/org.qemu.guest_agent.0` on POSIX, a device
    # interface on Windows). What differs is the agent installed in vm-configure, not the device.
    if shape.qga_socket:
        if shape.qga_transport == "isa":
            # ONE PORT, NO BUS. macOS's agent probes /dev/cu.serial1 first, which is the FIRST
            # isa-serial device qemu adds - so this must be the only one, and it is.
            args += [
                "-chardev", f"socket,id=qga0,path={shape.qga_socket},server=on,wait=off",
                "-device", "isa-serial,chardev=qga0",
            ]
        else:
            args += [
                "-chardev", f"socket,id=qga0,path={shape.qga_socket},server=on,wait=off",
                "-device", "virtio-serial",
                "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
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
        if self.shape.net == "none":
            # THE MACHINE, NOT THE NETWORK. The caller reads the screen over QMP and has no use
            # for an address, so it does not get one: lib/net.py raises SystemExit when dnsmasq
            # will not start, and that is a dependency with nothing to do with such a stage -
            # measured, the first run of android/blissos16/steps/launch.yaml died on
            # "dnsmasq: bad command line options" before qemu was even reached.
            args = build_args(self.work, self.extra, self.shape)
            log(f"vnc    {self.shape.vnc_bind}:{5900 + self.shape.vnc_display}")
            log("net    user-mode (this stage reads the screen, not the network)")
            self.proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=open(self.stdout, "ab"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            log(f"qemu pid {self.proc.pid}")
            return self

        net = _net()

        # THE DEFAULTS ARE FILLED BEFORE `up()`, BECAUSE `up()` NOW NEEDS THE MAC.
        #
        # It has to decide whether a running dnsmasq can still serve THIS guest, and the range is
        # one address - so the answer depends on which MAC the lease belongs to. Reading the
        # field first is the difference between a clean that boots and one that waits 900
        # seconds for a guest sitting at its login window (run 42899662670).
        self.shape.tap_ifname = self.shape.tap_ifname or net.TAP
        self.shape.tap_mac = self.shape.tap_mac or net.GUEST_MAC

        net.up(ssh_port=self.shape.ssh_port, rdp_port=self.shape.rdp_port,
               mac=self.shape.tap_mac)
        # THE GUEST'S MTU IS THE TAP'S, ALWAYS, UNLESS A CALLER ASKED OTHERWISE.
        #
        # Filling it here rather than in each of the twenty-odd callers is the same
        # argument the tap itself is filled here for: the one caller that forgets produces
        # a guest that boots, gets a lease, resolves names, and then cannot complete a TLS
        # handshake - a failure a long way from its cause. See build_args() for the
        # measurement.
        self.shape.net_mtu = self.shape.net_mtu or net.MTU

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

    def stop(self, *, acpi_first: bool = False, acpi_timeout: int = 300) -> None:
        """Stop qemu.

        `acpi_first` asks the guest to shut down through ACPI before the process is
        killed. That is the difference between a disk Windows closed properly and one
        that was powered off mid-write: on the next boot the second kind runs chkdsk or
        lands in the recovery environment.

        It is off by default because every other caller in this repository stops a
        machine that is going to be thrown away or a guest that has already hung.

        THE ORDER IS SSH, THEN ACPI, THEN THE KILL - see shutdown_command() for why the ssh step
        exists at all.
        """
        if self.proc is None:
            return

        if acpi_first:
            self._acpi_shutdown(acpi_timeout)

        # THE PROCESS HAS TO BE COLLECTED EVEN WHEN IT EXITED ON ITS OWN.
        #
        # `shutdown_command` and `_acpi_shutdown` both wait for `poll()`, so qemu may already be
        # gone by here - and `killpg` on a dead process group raises ProcessLookupError, which the
        # except below turns into an early `return`, SKIPPING the `wait()`. The result is a
        # zombie, an unreaped exit status, and - what actually mattered - a caller that proceeds
        # to read the disk while qemu's file descriptors are still being closed by the kernel.
        #
        # `qemu-img check` on that disk is what run 42763433967 died on: the share stage printed
        # "=== the disk ===" and was killed before it could print a verdict.
        if self.proc.poll() is not None:
            self.proc.wait()
            log("qemu stopped")
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

    def reset(self, *, wait: float = 10.0) -> None:
        """Reset the guest through QMP and wait for the monitor to answer again.

        `system_reset` IS THE VIRTUAL POWER BUTTON HELD DOWN - it does not ask the guest, it
        restarts it. That is what a YAML `reboot:` step wants: the guest's own shutdown path is
        the thing being avoided (macOS puts up a dialog on ACPI; Windows may install updates).

        THE SOCKET FILE IS THE SIGNAL THAT IT WORKED. qemu keeps the same path but unlinks and
        recreates it across a reset, so waiting for it to exist again is waiting for the monitor
        to be back. A reset that never completed leaves the file gone and this says so.
        """
        import time
        import qmp as qmpmod

        try:
            client = qmpmod.QMP(self.paths.drive)
        except (OSError, RuntimeError, ImportError) as exc:
            raise RuntimeError(f"could not open QMP to reset the guest: {exc}") from exc
        try:
            log("resetting the guest")
            client.execute("system_reset")
        finally:
            client.close()

        # THE FILE GOES AWAY AND COMES BACK. Polling for its absence first would race a reset
        # that had already finished, so this only waits for it to be THERE, with a settle.
        deadline = time.time() + wait
        while time.time() < deadline:
            if os.path.exists(self.paths.drive):
                break
            time.sleep(0.2)
        time.sleep(1.0)

    def shutdown_command(self, command: str, timeout: int, *, user: str = "vmuser",
                         password: str = "admin") -> bool:
        """Run an in-guest shutdown command and wait. Returns True if qemu exited.

        WHY THIS EXISTS AND ACPI IS NOT ENOUGH. macOS DOES NOT SHUT DOWN ON AN ACPI POWER BUTTON
        PRESS. It receives it and puts up the "Restart / Sleep / Cancel / Shut Down" dialog - so
        `system_powerdown` is not a shutdown request to macOS, it is a request to ASK. The guest
        then sits there until something answers the dialog, and what answered here was the
        timeout:

            [vm] asking the guest to shut down
            [vm] the guest did not power off within 300s; killing it      (run 42762244256)

        Five minutes, and it was never going to be enough, because nothing was in progress. The
        disk was then killed mid-flight and the next boot had a journal to replay.

        The command the caller passes is run over the ssh connection the caller already has; for
        macOS it is `sudo shutdown -h now`, which is a real shutdown. ACPI stays as the fallback
        for a guest whose ssh has already gone.

        THE CALLER OWNS THE COMMAND because this module must not learn how to log in to a guest.
        """
        import time

        try:
            import session
            status, output = session.ssh(self.shape.ssh_port, command, timeout=60,
                                         user=user, password=password)
            log(f"asked the guest to shut down over ssh -> {status}")
            for line in (output or "").splitlines()[:4]:
                log(f"    {line[:140]}")
        except Exception as exc:                                # noqa: BLE001
            log(f"the ssh shutdown could not be sent ({exc})")

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                # REAP IT AND LET THE KERNEL FINISH CLOSING THE DISK.
                #
                # `poll()` returning means the process left the run queue, not that its file
                # descriptors are closed - and the caller's next act is to open the qcow2 and
                # read it (`smoke.check_disk`). Reading a disk whose writer has not been reaped
                # is how run 42763433967 printed "=== the disk ===" and then died with no verdict
                # at all: the read was still waiting on a qemu that had not finished letting go.
                self.proc.wait()
                log("the guest powered off")
                return True
            time.sleep(2)
        log(f"the guest did not shut down within {timeout}s")
        return False

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
            # NO `client.close()` HERE: the `finally` below owns it. This block used to close and
            # then fall into a `finally` that closed again - harmless while a connection was one
            # object owned by one caller, and NOT harmless now that the client is shared and
            # reference counted: the second close would release another holder's claim and shut
            # a socket somebody else is still reading.
            log(f"system_powerdown failed ({exc}); killing instead")
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
