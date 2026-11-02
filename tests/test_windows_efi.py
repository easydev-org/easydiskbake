"""A GPT with no boot loader is a partition table, not a system.

THE FAILURE THIS EXISTS FOR, MEASURED ON RUN 41483694400

The ssh stage started the published Windows gold disk and the firmware said:

    BdsDxe: failed to load Boot000A "Windows Boot Manager" from
      HD(1,GPT,...) /EFI/Microsoft/Boot/bootmgfw.efi: Not Found
    >>Start PXE over IPv4.
    PXE-E16: No valid offer received.
    >>Start PXE over IPv6.
    PXE-E16: No valid offer received.
    ...
    UEFI Interactive Shell v2.2

The bootstate did its job: Boot000A exists, and it was restored from the archive, so the
firmware looked exactly where the entry pointed. There was no bootmgfw.efi on the disk.

The stage then spent fifteen minutes waiting for a desktop that was never going to
appear, and reported only "waiting 845s for a desktop".

WHY THE BAKE DID NOT CATCH IT

check_disk(disk, root_fs="gpt") verified a protective MBR and a GPT header. An empty disk
that has been partitioned has both. The bake printed

    BAKED windows11 -> out/windows11.qcow2 (10763 MiB)

and published a disk that could never start. A size is not an installation.

WHAT IS CHECKED NOW

The EFI system partition (type C12A7328-F81F-11D2-BA4B-00A0C93EC93B) is located in the
GPT, copied out with qemu-io, and listed with mtools. bootmgfw.efi has to be in
/EFI/Microsoft/Boot. That file cannot be present unless Windows installed its boot
loader, which is the claim the bake was making without evidence.

These tests run against images built with sgdisk, mkfs.fat and mcopy, and skip when those
tools are absent - the point is the reader, not the image builder.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))

import smoke  # noqa: E402

TOOLS = ("qemu-img", "qemu-io", "sgdisk", "mkfs.fat", "mcopy")


def have_tools():
    return all(shutil.which(t) for t in TOOLS)


@unittest.skipUnless(have_tools(), f"needs {', '.join(TOOLS)}")
class TheBootLoader(unittest.TestCase):
    """A disk with and without bootmgfw.efi, read the way the bake reads one."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="efi-check-")
        cls.raw = os.path.join(cls.tmp, "disk.raw")
        with open(cls.raw, "wb") as fh:
            fh.truncate(64 << 20)

        subprocess.run(["sgdisk", "-n", "1:2048:+32M", "-t", "1:EF00",
                        "-mbrtogpt", cls.raw],
                       check=True, capture_output=True)
        esp = os.path.join(cls.tmp, "esp.img")
        with open(esp, "wb") as fh:
            fh.truncate(32 << 20)
        subprocess.run(["mkfs.fat", "-F", "32", esp], check=True, capture_output=True)
        with open(cls.raw, "r+b") as fh:
            fh.seek(2048 * 512)
            with open(esp, "rb") as src:
                fh.write(src.read())

        # The empty one, as a qcow2.
        cls.empty = os.path.join(cls.tmp, "empty.qcow2")
        subprocess.run(["qemu-img", "convert", "-O", "qcow2", cls.raw, cls.empty],
                       check=True, capture_output=True)

        # Now put a loader in the ESP and make a second disk.
        tree = os.path.join(cls.tmp, "tree", "EFI", "Microsoft", "Boot")
        os.makedirs(tree)
        with open(os.path.join(tree, "bootmgfw.efi"), "wb") as fh:
            fh.write(b"MZ" + b"\x00" * 1000)
        subprocess.run(["mcopy", "-i", esp, "-s", os.path.join(cls.tmp, "tree", "EFI"),
                        "::/"], check=True, capture_output=True)
        with open(cls.raw, "r+b") as fh:
            fh.seek(2048 * 512)
            with open(esp, "rb") as src:
                fh.write(src.read())
        cls.installed = os.path.join(cls.tmp, "installed.qcow2")
        subprocess.run(["qemu-img", "convert", "-O", "qcow2", cls.raw, cls.installed],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_disk_without_the_loader_is_refused(self):
        """The empty partition is the failure that was published as a gold disk."""
        self.assertFalse(smoke.check_efi_bootloader(self.empty))

    def test_a_disk_with_the_loader_is_accepted(self):
        self.assertTrue(smoke.check_efi_bootloader(self.installed))

    def test_check_disk_reports_a_windows_disk_without_a_loader(self):
        # The whole point: this is what bake.py calls, and it has to say False.
        self.assertFalse(smoke.check_disk(self.empty, root_fs="windows"))

    def test_windows_is_a_known_root_fs(self):
        # An unknown value raises, which would be a crash in the bake rather than a
        # verdict about the disk.
        smoke.check_disk(self.installed, root_fs="windows")


class TheCallers(unittest.TestCase):
    """The bake and the verifier have to ask for the Windows check."""

    def test_the_bake_checks_for_the_boot_loader(self):
        with open(os.path.join(HERE, "windows", "11", "bake.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('root_fs="windows"', source)
        self.assertNotIn('root_fs="gpt"', source)

    def test_the_verifier_checks_for_the_boot_loader(self):
        with open(os.path.join(HERE, "windows", "11", "verify.py"),
                  encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('root_fs="windows"', source)


class TheMessage(unittest.TestCase):
    """When it fails, it has to say which of the two things is missing.

    "The disk is bad" is not actionable. "There is no EFI system partition" and "the ESP
    has no /EFI/Microsoft/Boot" point at different steps in the install.
    """

    def test_the_two_failures_read_differently(self):
        with open(os.path.join(HERE, "lib", "smoke.py"), encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("def check_efi_bootloader")
        end = source.index("def _gpt_partitions")
        body = source[start:end]
        self.assertIn("no EFI system partition", body)
        self.assertIn("has no /EFI/Microsoft/Boot", body)
        self.assertIn("holds no", body)


if __name__ == "__main__":
    unittest.main()
