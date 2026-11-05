"""The macOS guest, built the same way by the bake and by the probe.

WHY THIS IS ITS OWN MODULE

macos/15/bake.py assembles a machine out of eleven parameters that took eight runs to
get right - the virtio-blk device with bootindex, Skylake-Client-v4 with the flags the
kernel reads, ICH9's sleep states disabled, the RTC on UTC, the SMBIOS baseboard
record, virtio-vga carrying xres/yres, the SMC key, and the disk order. A probe that
rebuilt that list itself would diverge on the first edit and then measure a different
machine than the one it was written to measure.

So the list lives here. bake.py calls make_guest(); macos/15/probe.py calls make_guest()
too, and the only thing either of them chooses is what to do with the guest afterwards.
"""

from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import opencore  # noqa: E402
import vm  # noqa: E402

# Apple's SMC key, obfuscated the way every hackintosh does it. Not a secret: it is the
# value macOS reads from the SMC device to decide whether the firmware is Apple's.
OSK_PLAIN = "ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc"

# THE TARGET DISK'S `-drive` ARGUMENT, IN ONE PLACE.
#
# Four stages start this machine - bake, desktop, ssh, dev - and each wrote its own copy of
#
#     "-drive", f"file={disk},if=none,format=qcow2,id=target",
#
# so the copies could differ and one of them always did. Adding discard=unmap to three of
# them and forgetting the fourth would have produced a disk that grew during setup and then
# stopped being trimmed at whichever stage kept the old line, with no sign of it in any log.
#
# discard=unmap IS THE macOS EQUIVALENT OF THE fstrim THE OTHER SYSTEMS RUN.
#
# Without it every block the guest has ever written stays allocated in the qcow2 forever:
# macOS deletes a file, APFS marks the extents free, and the information stops there because
# the virtio device was never told it may discard. APFS is copy-on-write, but that affects
# what happens INSIDE the guest - it does not carry a single byte of that knowledge across
# the virtio boundary.
#
# WHY lib/clean_disk.py SAID macOS WAS ABSENT, AND WHY THAT WAS ABOUT A DIFFERENT THING.
#
#     "its filesystem is copy-on-write, so writing zeros over the free space allocates new
#      blocks before the old ones are released"
#
# That is an argument against ZEROING, and it is right - it is why none of the four systems
# here write zeros. It says nothing about DISCARD, which allocates nothing, writes nothing,
# and is the only one of the two that a copy-on-write filesystem can do safely.
TARGET_DRIVE = "file={disk},if=none,format=qcow2,discard=unmap,id=target"


def shape(assets: dict, *, cpus: int = 4, mem_mb: int = 8192) -> vm.Shape:
    """The machine itself, without any storage.

    Each field is one difference from dockur/macos's src/*.sh, and each is silent when
    wrong - which is why they are listed rather than summarised.
    """
    return vm.Shape(
        cpus=cpus,
        mem_mb=mem_mb,
        machine="q35",
        firmware_code=assets["rom"],
        firmware_vars=assets["vars"],
        # vga=none because the adapter is a -device, below. On q35 -vga creates an ISA
        # VGA device on the legacy bus and macOS expects the PCIe one.
        vga="none",
        # Skylake-Client-v4 with the flags cpu.sh composes. macOS reads the CPU family
        # and model out of CPUID and refuses to boot on a model it does not know, and
        # the flags are not decoration:
        #     vendor=GenuineIntel    without it the kernel stops
        #     vmx=off               nested virtualisation confuses it
        #     vmware-cpuid-freq=on  the TSC frequency comes from this
        #     -pdpe1gb              1 GB pages are broken under this guest
        # `check` makes qemu refuse an unsupportable set at startup rather than boot a
        # guest that panics later.
        cpu=("Skylake-Client-v4,vendor=GenuineIntel,vmx=off,"
             "vmware-cpuid-freq=on,-pdpe1gb,"
             "+ssse3,+sse4.2,+popcnt,+avx,+avx2,+aes,+fma,+bmi1,+bmi2,+smep,"
             "+xsave,+xsaveopt,+xgetbv1,+movbe,+rdrand,check"),
        # dockur's configureSmp(): a topology macOS understands, not a bare count.
        smp_topology=f"{cpus},sockets=1,dies=1,cores={cpus},threads=1",
        extra_args=[
            # ICH9's sleep states hang this guest at a black screen; the PCIe hotplug
            # bridge is mishandled by macOS on ICH9.
            "-global", "ICH9-LPC.disable_s3=1",
            "-global", "ICH9-LPC.disable_s4=1",
            "-global", "ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off",
            # macOS reads the clock from the RTC and expects UTC.
            "-rtc", "base=utc",
            # The baseboard record macOS's platform check reads.
            "-smbios", "type=2",
            # THE SCREEN. virtio-vga takes xres/yres; vmware-svga does not, and its
            # 1280x800 default is what put every coordinate off the edge of the screen
            # for four runs. The numbers come from lib/opencore so the plist's
            # Resolution and this cannot disagree.
            "-device", f"virtio-vga,xres={opencore.GUEST_WIDTH},"
                       f"yres={opencore.GUEST_HEIGHT}",
        ],
    )


def drives(assets: dict, disk: str, recovery: str) -> list[str]:
    """The three storage devices, in boot order.

        boot.img    OpenCore, bootindex=0. The firmware must try this first; a disk
                    with no bootindex is not in the boot order at all, which is what
                    "failed to load Boot0001" meant for six runs.
        disk        the target, bootindex=1. After the installer writes a system,
                    OpenCore finds it here.
        recovery    as USB. macOS installs from removable media; the disk it installs
                    TO is not the disk it boots FROM. usb-storage has no bootindex
                    property and does not need one - the firmware reaches it only after
                    OpenCore hands over.
    """
    return [
        "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
        "-device", "virtio-blk-pci,drive=oc,bootindex=0",
        "-drive", TARGET_DRIVE.format(disk=disk),
        "-device", "virtio-blk-pci,drive=target,bootindex=1",
        "-drive", f"file={recovery},media=disk,if=none,id=rec,readonly=on",
        "-device", "usb-storage,bus=xhci.0,drive=rec",
        "-device", f"isa-applesmc,osk={OSK_PLAIN}",
    ]


def make_guest(work: str, assets: dict, disk: str, recovery: str, *,
               cpus: int = 4, mem_mb: int = 8192) -> vm.Guest:
    """A guest configured and not yet started."""
    return vm.Guest(
        work,
        extra=drives(assets, disk, recovery),
        shape=shape(assets, cpus=cpus, mem_mb=mem_mb),
        stdout=os.path.join(work, "console.log"),
    )
