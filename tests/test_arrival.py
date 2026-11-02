"""The arrival decisions, without a VM.

wait_for_arrival is the only place that decides a bake succeeded, and getting it
wrong in either direction is expensive: too lenient ships a broken disk, too strict
throws away a good one after 90 minutes.

The loop itself needs qemu, so what is tested here is the classification it depends
on - which is where the real mistakes are. Each case below is a failure that would
otherwise only show up as "the bake timed out" with no explanation.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import vm  # noqa: E402


class RdpForward(unittest.TestCase):
    """3389 is the signal that proves the install finished, so it has to be wired."""

    def test_rdp_is_forwarded(self):
        args = vm.build_args("/tmp", shape=vm.Shape(rdp_port=3389))
        netdev = args[args.index("-netdev") + 1]
        self.assertIn("tcp::3389-:3389", netdev)

    def test_ssh_and_rdp_coexist(self):
        # hostfwd can be repeated; dropping one while adding the other would break
        # the MX bake silently.
        args = vm.build_args("/tmp", shape=vm.Shape(ssh_port=2222, rdp_port=3389))
        netdev = args[args.index("-netdev") + 1]
        self.assertIn("tcp::2222-:22", netdev)
        self.assertIn("tcp::3389-:3389", netdev)

    def test_rdp_port_is_configurable(self):
        # A parallel run on one host needs distinct host ports; a fixed 3389 would
        # make two bakes collide.
        args = vm.build_args("/tmp", shape=vm.Shape(rdp_port=13389))
        netdev = args[args.index("-netdev") + 1]
        self.assertIn("tcp::13389-:3389", netdev)


class BootLogClassification(unittest.TestCase):
    """What the console tells us about progress, read the way wait_for_arrival does.

    "Booting from Hard Disk" appears once per handover from the installer to the
    installed system. Its presence separates "setup never got that far" from "setup
    ran but something after it failed", which are different fixes.
    """

    @staticmethod
    def disk_boots(text):
        return text.count("Booting from Hard Disk")

    def test_no_disk_boot_means_setup_stalled(self):
        text = "SeaBIOS\nBooting from DVD/CD...\n"
        self.assertEqual(self.disk_boots(text), 0)

    def test_one_disk_boot_means_the_installer_handed_over(self):
        text = "Booting from DVD/CD...\nBooting from Hard Disk...\n"
        self.assertEqual(self.disk_boots(text), 1)

    def test_repeated_boots_are_counted(self):
        # Windows setup reboots itself at least twice. Counting distinct markers is
        # what tells the two failure modes apart in the timeout message.
        text = "Booting from Hard Disk...\n" * 3
        self.assertEqual(self.disk_boots(text), 3)

    def test_the_string_is_not_matched_loosely(self):
        # "Booting from Hard Disk" is what the firmware prints; a log line that only
        # mentions the phrase in passing must not count. This pins the exact string
        # so a future edit cannot loosen it.
        text = "note: we may boot from hard disk later\n"
        self.assertEqual(self.disk_boots(text), 0)


if __name__ == "__main__":
    unittest.main()
