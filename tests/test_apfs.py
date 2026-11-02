"""The APFS partition check, and the two layouts that make it meaningful.

smoke.check_disk is the gate between a finished bake and a published artifact, so a
check that passes on the wrong shape is worse than no check. This file builds the two
disks that matter - an installed one and one that reached the end of the installer
without producing a container - and pins the verdict for each.

THE FIRST VERSION OF check_apfs SCANNED FOR THE MAGIC, and these tests are what say why
that was wrong: an installed APFS partition starts after macOS's EFI system partition,
which is 200-500 MiB in, so a fixed 64 MiB window never reached it. Finding the magic
anywhere would also have proved nothing about which partition it belonged to.
"""

import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import smoke  # noqa: E402

# The two type GUIDs, in the byte order GPT stores them.
ESP_TYPE = bytes.fromhex("28732ac11ff8d211ba4b00a0c93ec93b")
APFS_TYPE = bytes.fromhex("7c3457ef0000aa11aa1100306543ecac")

SECTOR = 512


def build_disk(path, *, apfs=True, nxsb=True, apfs_start=409600):
    """A GPT disk with an ESP and, optionally, an APFS partition.

    apfs_start is in sectors and deliberately far past the 64 MiB a naive scan would
    cover: 409600 sectors is 200 MiB, which is what macOS's own ESP is.
    """
    entries = [(ESP_TYPE, 40, 409599)]
    if apfs:
        entries.append((APFS_TYPE, apfs_start, apfs_start + 2048))

    with open(path, "wb") as fh:
        # Boot code in the MBR, which check_mbr requires: a disk with a signature and
        # no loader is a distinct failure from one that was never partitioned, and this
        # fixture is the installed case.
        fh.write(b"\xfa\xeb\xfe" + bytearray(SECTOR - 3))
        fh.seek(0x1BE)
        fh.write(bytes([0] * 4 + [0xEE] + [0] * 11))
        fh.seek(0x1FE)
        fh.write(b"\x55\xaa")

        header = bytearray(SECTOR)
        header[0:8] = b"EFI PART"
        struct.pack_into("<Q", header, 72, 2)      # entry array LBA
        struct.pack_into("<I", header, 80, 128)    # entry count
        struct.pack_into("<I", header, 84, 128)    # entry size
        fh.seek(SECTOR)
        fh.write(header)

        fh.seek(2 * SECTOR)
        for ptype, first, last in entries:
            entry = bytearray(128)
            entry[0:16] = ptype
            struct.pack_into("<Q", entry, 32, first)
            struct.pack_into("<Q", entry, 40, last)
            fh.write(entry)

        if apfs:
            fh.seek(apfs_start * SECTOR)
            fh.write(b"\x00" * 32 + (b"NXSB" if nxsb else b"XXXX") + b"\x00" * 100)


class ApfsPartition(unittest.TestCase):

    def setUp(self):
        self.old_read = smoke.qemu_io_read
        # qemu-io does not exist on a machine without qemu, and the check itself is
        # about the bytes rather than about how they were read.
        smoke.qemu_io_read = self._read
        self.tmp = tempfile.mkdtemp(prefix="smoke-apfs-")

    def tearDown(self):
        smoke.qemu_io_read = self.old_read
        import subprocess
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    @staticmethod
    def _read(disk, offset, length):
        with open(disk, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    def disk(self, **kwargs):
        path = os.path.join(self.tmp, "disk.qcow2")
        build_disk(path, **kwargs)
        return path

    def test_an_installed_disk_passes(self):
        self.assertTrue(smoke.check_apfs(self.disk()))

    def test_the_container_is_found_past_a_naive_scan_window(self):
        # 200 MiB in, which the first version's 64 MiB search never reached. This is
        # the test for that bug specifically.
        self.assertTrue(smoke.check_apfs(self.disk(apfs_start=409600)))

    def test_a_partition_without_the_container_fails(self):
        # The installer created the partition and then did not write a container.
        self.assertFalse(smoke.check_apfs(self.disk(nxsb=False)))

    def test_no_apfs_partition_fails(self):
        # The installer never ran, or ran against another disk.
        self.assertFalse(smoke.check_apfs(self.disk(apfs=False)))

    def test_a_non_gpt_disk_fails(self):
        path = os.path.join(self.tmp, "mbr.qcow2")
        with open(path, "wb") as fh:
            fh.write(bytearray(4096))
            fh.seek(0x1FE)
            fh.write(b"\x55\xaa")
        self.assertFalse(smoke.check_apfs(path))


class CheckDiskAcceptsApfs(unittest.TestCase):
    """check_disk's root_fs dispatch: "apfs" must mean GPT *and* a container."""

    def setUp(self):
        self.old_read = smoke.qemu_io_read
        smoke.qemu_io_read = ApfsPartition._read
        self.tmp = tempfile.mkdtemp(prefix="smoke-rootfs-")

    def tearDown(self):
        smoke.qemu_io_read = self.old_read
        import subprocess
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def test_apfs_is_a_valid_root_fs_name(self):
        path = os.path.join(self.tmp, "disk.qcow2")
        build_disk(path)
        # Does not raise.
        smoke.check_disk(path, root_fs="apfs")

    def test_an_unknown_name_raises_and_lists_the_known_ones(self):
        path = os.path.join(self.tmp, "disk.qcow2")
        build_disk(path)
        with self.assertRaises(ValueError) as ctx:
            smoke.check_disk(path, root_fs="hfs")
        message = str(ctx.exception)
        for name in ("ext4", "gpt", "apfs", "none"):
            self.assertIn(name, message)

    def test_apfs_needs_both_the_table_and_the_container(self):
        good = os.path.join(self.tmp, "good.qcow2")
        build_disk(good)
        self.assertTrue(smoke.check_disk(good, root_fs="apfs"))

        no_container = os.path.join(self.tmp, "no-container.qcow2")
        build_disk(no_container, nxsb=False)
        self.assertFalse(smoke.check_disk(no_container, root_fs="apfs"))

        no_apfs = os.path.join(self.tmp, "no-apfs.qcow2")
        build_disk(no_apfs, apfs=False)
        self.assertFalse(smoke.check_disk(no_apfs, root_fs="apfs"))

    def test_gpt_alone_does_not_require_a_container(self):
        # The Windows bake asks for "gpt", and its disk has no APFS container. If
        # "apfs" leaked into that path, every Windows bake would fail its disk check.
        no_apfs = os.path.join(self.tmp, "windows.qcow2")
        build_disk(no_apfs, apfs=False)
        self.assertTrue(smoke.check_disk(no_apfs, root_fs="gpt"))


if __name__ == "__main__":
    unittest.main()
