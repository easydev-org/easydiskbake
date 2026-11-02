"""The qemu device strings, checked by qemu itself.

WHY THIS FILE IS SEPARATE FROM test_opencore.py

That file asserts on the argument vector: that a flag is present, that a value is the
one intended. It cannot tell whether qemu would accept the device, and that gap cost a
run:

    -device virtio-vga,xres=1920,yres=1080,vgamem_mb=64
    qemu-system-x86_64: Property 'virtio-vga.vgamem_mb' not found

Every string in the vector was the one intended. The property simply does not exist on
that device, and only qemu knows which properties a device has.

So this asks qemu. These tests are written to run in CI, where the bake image supplies
qemu-system-x86_64; locally they skip rather than fail, because a laptop without qemu is
not a broken repository.

WHAT THEY CATCH THAT A STRING ASSERTION CANNOT

    a property that does not exist on the device   (vgamem_mb on virtio-vga)
    a device name that has moved between releases
    a geometry qemu refuses to configure

Each of those is a one-line startup error in a bake that has already spent twenty
minutes, which is why it is worth three seconds here.
"""

import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import opencore  # noqa: E402

QEMU = shutil.which("qemu-system-x86_64")

# Devices this repository puts on a command line, by the argument it builds.
# `virtio-vga`'s xres/yres are inherited from its framebuffer base class and do NOT
# appear in `-device ...,help`, so they are covered behaviourally instead.
DEVICES = {
    "virtio-vga": [],
    "virtio-blk-pci": ["bootindex"],
    "isa-applesmc": ["osk"],
    "usb-storage": [],
    "vmware-svga": [],
}


def qemu(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([QEMU, *args], capture_output=True, text=True, timeout=timeout)


@unittest.skipUnless(QEMU, "qemu-system-x86_64 is not installed; CI provides it")
class DeviceProperties(unittest.TestCase):

    def test_every_device_exists(self):
        """A device name that does not exist is a startup failure with no fallback."""
        for device in DEVICES:
            result = qemu("-device", f"{device},help")
            text = f"{result.stdout}\n{result.stderr}"
            self.assertNotIn("not found", text.lower(),
                             f"{device} is not a device qemu knows:\n{text[:300]}")

    def test_the_properties_this_repository_uses_are_declared(self):
        for device, properties in DEVICES.items():
            if not properties:
                continue
            result = qemu("-device", f"{device},help")
            text = f"{result.stdout}\n{result.stderr}"
            for prop in properties:
                self.assertIn(prop, text, f"{device} declares no {prop}")

    def test_the_gpu_accepts_the_screen_geometry(self):
        """Hand it to qemu. A property absent from the listing may still be accepted -
        xres and yres are - and the only way to know is to try.

        This is the check that was missing. `virtio-vga,vgamem_mb=64` looked like a
        reasonable device string, named a property that looked like it should exist, and
        qemu rejected the whole machine in one line, ninety minutes into a bake.
        """
        device = (f"virtio-vga,xres={opencore.GUEST_WIDTH},"
                  f"yres={opencore.GUEST_HEIGHT}")
        # -S stops the CPU before it executes anything, so nothing is booted and the
        # process waits; the timeout ends it. What matters is whether qemu printed a
        # refusal first.
        try:
            result = qemu("-display", "none", "-machine", "q35", "-m", "256",
                          "-device", device, "-S", "-monitor", "none",
                          "-serial", "none", timeout=10)
        except subprocess.TimeoutExpired:
            return          # it started and kept running, which is the pass condition
        self.assertNotIn("not found", result.stderr.lower(), result.stderr[:300])
        self.assertNotIn("invalid", result.stderr.lower(), result.stderr[:300])

    def test_a_property_that_does_not_exist_is_refused(self):
        """The negative case, so the check above is known to be able to fail.

        A test that has never rejected anything is a test nobody has watched work.
        virtio-vga has no vgamem_mb; qemu says so.
        """
        result = qemu("-device", "virtio-vga,vgamem_mb=64,help")
        text = f"{result.stdout}\n{result.stderr}".lower()
        self.assertIn("not found", text)

    def test_the_whole_machine_parses(self):
        """Everything the macOS bake puts on the command line, together.

        Each device can be valid alone and the combination still refused - two devices
        on one bus, a property that conflicts with a machine type. One parse of the real
        vector costs a second.
        """
        argv = [
            "-display", "none", "-machine", "q35", "-m", "512", "-S",
            "-monitor", "none", "-serial", "none",
            "-device", "qemu-xhci,id=xhci",
            "-device", "virtual-tablet,bus=xhci.0",
            "-device", f"virtio-vga,xres={opencore.GUEST_WIDTH},"
                       f"yres={opencore.GUEST_HEIGHT}",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            "-device", "isa-applesmc,osk=ourhardworkbythesewordsguardedpleasedontsteal",
            "-rtc", "base=utc",
            "-smbios", "type=2",
            "-global", "ICH9-LPC.disable_s3=1",
            "-global", "ICH9-LPC.disable_s4=1",
        ]
        try:
            result = qemu(*argv, timeout=10)
        except subprocess.TimeoutExpired:
            return          # parsed, started, still running: that is the pass
        # `drive=oc` has no -drive behind it, which qemu reports separately; the
        # question here is whether the device strings parsed.
        self.assertNotIn("not found", result.stderr.lower(), result.stderr[:400])
        self.assertNotIn("invalid", result.stderr.lower(), result.stderr[:400])


if __name__ == "__main__":
    unittest.main()
