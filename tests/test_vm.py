"""The tests that could not exist while this was shell.

Every assertion here is a claim that used to be checkable only by booting a VM -
or, on a machine without /dev/kvm, not checkable at all. `build_args` being a
pure function is what turns "I ran it once and it looked right" into something
that stays true.

    python3 -m unittest discover -s tests -v        (from the repo root)
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import vm  # noqa: E402


class DefaultShapeIsUnchanged(unittest.TestCase):
    """The MX bake worked before vm.py existed. It must work after.

    These are the exact tokens the shell version produced for a plain guest, and
    the whole point of Shape's defaults is that nothing which worked changes.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.args = vm.build_args(self.tmp, extra=["-kernel", "/k", "-initrd", "/i"],
                                  shape=vm.Shape())

    def test_no_uefi_by_default(self):
        joined = " ".join(self.args)
        self.assertNotIn("pflash", joined)
        self.assertNotIn("-machine", joined)
        self.assertNotIn("tpm", joined)

    def test_std_vga_by_default(self):
        self.assertIn("-vga", self.args)
        self.assertEqual(self.args[self.args.index("-vga") + 1], "std")

    def test_two_qmp_sockets(self):
        # One socket for the driver, one for the recorder. A single socket would
        # make them fight for it, because QMP serves one client per socket.
        qmps = [a for a in self.args if a.startswith("unix:") and a.endswith("qmp,server=on,wait=off")]
        self.assertEqual(len(qmps), 2, qmps)

    def test_guest_extras_land_last(self):
        # -kernel/-initrd must come after everything the shape contributes, or
        # qemu reads them as a later flag's argument.
        self.assertEqual(self.args[-4:], ["-kernel", "/k", "-initrd", "/i"])
        self.assertIn("-vga", self.args)
        self.assertLess(self.args.index("-vga"), self.args.index("-kernel"))

    def test_serial_is_mon_stdio(self):
        idx = self.args.index("-serial")
        self.assertEqual(self.args[idx + 1], "mon:stdio")


class UefiShape(unittest.TestCase):
    """Windows 11 and macOS need a machine, not a driver."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vars = os.path.join(self.tmp, "vars.fd")
        open(self.vars, "w").close()

    def test_code_is_readonly_but_vars_is_not(self):
        # The code file is shared between guests; the vars file is per-guest
        # NVRAM. Writing to the shared one would make every guest inherit the
        # previous one's boot order.
        args = vm.build_args(self.tmp, shape=vm.Shape(
            machine="q35",
            firmware_code="/OVMF_CODE.fd",
            firmware_vars=self.vars,
        ))
        joined = " ".join(args)
        self.assertIn("readonly=on,file=/OVMF_CODE.fd", joined)
        self.assertIn(f"format=raw,file={self.vars}", joined)
        self.assertNotIn(f"readonly=on,file={self.vars}", joined)

    def test_missing_vars_refuses_to_start(self):
        # A guest that silently boots with a fresh NVRAM loses Secure Boot state
        # and, for macOS, the OpenCore entry - and the failure surfaces as "the
        # VM hung", which is the wrong place to look.
        with self.assertRaises(ValueError):
            vm.build_args(self.tmp, shape=vm.Shape(
                mem_mb=2048,
                firmware_vars=os.path.join(self.tmp, "does-not-exist.fd"),
            ))

    def test_tpm_is_three_arguments(self):
        args = vm.build_args(self.tmp, shape=vm.Shape(tpm_socket="/tpm.sock"))
        joined = " ".join(args)
        self.assertIn("-chardev socket,id=chrtpm,path=/tpm.sock", joined)
        self.assertIn("-tpmdev emulator,id=tpm0,chardev=chrtpm", joined)
        self.assertIn("-device tpm-crb,tpmdev=tpm0", joined)

    def test_vga_none_omits_the_flag(self):
        # -vga and -device VGA are mutually exclusive; a caller wanting
        # vgamem_mb passes vga="none" and its own -device.
        args = vm.build_args(self.tmp, shape=vm.Shape(vga="none"))
        self.assertNotIn("-vga", args)


class FromEnv(unittest.TestCase):
    """The shell interface is still how the workflows call this."""

    def test_env_is_read(self):
        old = dict(os.environ)
        try:
            os.environ.update({
                "VM_CPUS": "8", "VM_MEM": "2048",
                "VM_MACHINE": "q35", "VM_VGA": "vmware",
                "SSH_PORT": "2200", "VNC_DISPLAY": "3",
            })
            s = vm.Shape.from_env()
            self.assertEqual(s.cpus, 8)
            self.assertEqual(s.mem_mb, 2048)
            self.assertEqual(s.machine, "q35")
            self.assertEqual(s.vga, "vmware")
            self.assertEqual(s.ssh_port, 2200)
            self.assertEqual(s.vnc_display, 3)
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
