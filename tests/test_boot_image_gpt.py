"""The boot image's partition table.

WHY THIS FILE EXISTS, IN ONE LINE

The first version of build_boot_image wrote a FAT filesystem at LBA 0 and called it a
disk. mtools read it, the build's own check passed, and OVMF then said

    TianoCore ... failed to load Boot0001

because firmware does not look for a filesystem - it looks for an EFI SYSTEM PARTITION
in a partition table. These tests assert the table, because the filesystem being
readable says nothing about it.

They run without mtools: the partition table is written to a file of the right size and
then read back by code that does not share anything with the writer except the format.
"""

import os
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import opencore  # noqa: E402

SECTOR = 512
# Deliberately small: the layout constants do not depend on the size, and a 2 MiB image
# makes these tests milliseconds instead of seconds.
SIZE_MB = 2


class GptLayout(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpt-test-")
        self.path = os.path.join(self.tmp, "boot.img")
        with open(self.path, "wb") as fh:
            fh.truncate(SIZE_MB * 1024 * 1024)
        total = (SIZE_MB * 1024 * 1024) // SECTOR
        self.esp_first = opencore.ESP_START_SECTOR
        self.esp_last = total - 34
        opencore.write_gpt(self.path, self.esp_first, self.esp_last)
        with open(self.path, "rb") as fh:
            self.raw = fh.read()
        self.total = len(self.raw) // SECTOR

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    # -- the MBR, which is what makes the table a "protective" one --------------------

    def test_protective_mbr_entry(self):
        entry = self.raw[0x1BE:0x1BE + 16]
        self.assertEqual(entry[4:5], b"\xEE",
                         "without a 0xEE entry the firmware reads this as an "
                         "unpartitioned disk")

    def test_mbr_signature(self):
        self.assertEqual(self.raw[0x1FE:0x200], b"\x55\xAA")

    # -- the header -------------------------------------------------------------------

    def test_primary_header_is_where_the_firmware_looks(self):
        self.assertEqual(self.raw[SECTOR:SECTOR + 8], b"EFI PART")

    def test_primary_header_crc(self):
        """The CRC covers the header's declared size, not the whole sector.

        Writing it over all 512 bytes produced a table that this module's own check
        agreed with and no other GPT implementation could read.
        """
        head = bytearray(self.raw[SECTOR:2 * SECTOR])
        stored = struct.unpack_from("<I", head, 16)[0]
        size = struct.unpack_from("<I", head, 12)[0]
        self.assertEqual(size, 92)
        struct.pack_into("<I", head, 16, 0)
        self.assertEqual(zlib.crc32(bytes(head[:size])) & 0xFFFFFFFF, stored)

    def test_backup_header_crc(self):
        head = bytearray(self.raw[(self.total - 1) * SECTOR:])
        stored = struct.unpack_from("<I", head, 16)[0]
        struct.pack_into("<I", head, 16, 0)
        self.assertEqual(zlib.crc32(bytes(head[:92])) & 0xFFFFFFFF, stored)

    def test_headers_point_at_each_other(self):
        primary = self.raw[SECTOR:2 * SECTOR]
        backup = self.raw[(self.total - 1) * SECTOR:]
        self.assertEqual(struct.unpack_from("<Q", primary, 24)[0], 1)
        self.assertEqual(struct.unpack_from("<Q", primary, 32)[0], self.total - 1)
        self.assertEqual(struct.unpack_from("<Q", backup, 24)[0], self.total - 1)
        self.assertEqual(struct.unpack_from("<Q", backup, 32)[0], 1)

    def test_the_partition_does_not_start_in_the_usable_gap(self):
        # LBA 34 is the first usable sector; the entry array ends at LBA 33.
        head = self.raw[SECTOR:2 * SECTOR]
        first_usable = struct.unpack_from("<Q", head, 40)[0]
        self.assertEqual(first_usable, 34)
        self.assertGreaterEqual(self.esp_first, first_usable)

    # -- the entry --------------------------------------------------------------------

    def test_the_first_partition_is_an_efi_system_partition(self):
        entry = self.raw[2 * SECTOR:2 * SECTOR + 128]
        self.assertEqual(entry[0:16], opencore.ESP_TYPE_GUID)
        self.assertEqual(str(uuid.UUID(bytes_le=entry[0:16])).upper(),
                         "C12A7328-F81F-11D2-BA4B-00A0C93EC93B")

    def test_the_entry_describes_the_whole_esp(self):
        entry = self.raw[2 * SECTOR:2 * SECTOR + 128]
        self.assertEqual(struct.unpack_from("<Q", entry, 32)[0], self.esp_first)
        self.assertEqual(struct.unpack_from("<Q", entry, 40)[0], self.esp_last)

    def test_the_partition_has_a_name(self):
        entry = self.raw[2 * SECTOR:2 * SECTOR + 128]
        self.assertEqual(entry[56:128].decode("utf-16-le").rstrip("\x00"), "primary")

    def test_the_partition_has_its_own_guid(self):
        # Not nil, and not the disk's GUID: two partitions sharing a unique GUID is a
        # table some firmware rejects.
        entry = self.raw[2 * SECTOR:2 * SECTOR + 128]
        unique = entry[16:32]
        self.assertNotEqual(unique, b"\x00" * 16)
        head = self.raw[SECTOR:2 * SECTOR]
        self.assertNotEqual(unique, head[56:72])

    # -- the backup -------------------------------------------------------------------

    def test_the_backup_entry_array_matches_the_primary(self):
        backup_lba = self.total - 1 - opencore.GPT_ENTRY_ARRAY_SECTORS
        self.assertEqual(
            self.raw[backup_lba * SECTOR:backup_lba * SECTOR + 128],
            self.raw[2 * SECTOR:2 * SECTOR + 128],
        )

    def test_the_esp_is_not_overwritten_by_the_backup(self):
        # The backup array and header sit in the last 33 sectors; the ESP ends 34
        # sectors before the end. One sector of slack is the whole margin, so this
        # catches an off-by-one that would corrupt the filesystem.
        backup_start = (self.total - 1 - opencore.GPT_ENTRY_ARRAY_SECTORS) * SECTOR
        self.assertLess(self.esp_last * SECTOR, backup_start)


class GptCheck(unittest.TestCase):
    """check_gpt is what a build runs before booting anything."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpt-check-")
        self.path = os.path.join(self.tmp, "boot.img")
        with open(self.path, "wb") as fh:
            fh.truncate(SIZE_MB * 1024 * 1024)
        self.total = (SIZE_MB * 1024 * 1024) // SECTOR
        opencore.write_gpt(self.path, opencore.ESP_START_SECTOR, self.total - 34)

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def test_a_correct_table_passes(self):
        opencore.check_gpt(self.path, opencore.ESP_START_SECTOR)

    def test_a_superfloppy_is_rejected(self):
        """The exact disk the first version produced.

        No partition table at all: a FAT filesystem at LBA 0 with 0x55AA at 0x1FE.
        mtools reads this and the firmware does not.
        """
        with open(self.path, "wb") as fh:
            fh.write(b"\xeb\x58\x90" + b"MSDOS5.0" + bytes(0x1FE - 11))
            fh.write(b"\x55\xAA")
        with self.assertRaises(opencore.OpenCoreError) as ctx:
            opencore.check_gpt(self.path, opencore.ESP_START_SECTOR)
        self.assertIn("0xEE", str(ctx.exception))

    def test_a_broken_header_crc_is_rejected(self):
        with open(self.path, "r+b") as fh:
            fh.seek(SECTOR + 40)          # inside the CRC's coverage
            fh.write(b"\xFF\xFF\xFF\xFF")
        with self.assertRaises(opencore.OpenCoreError) as ctx:
            opencore.check_gpt(self.path, opencore.ESP_START_SECTOR)
        self.assertIn("CRC", str(ctx.exception))

    def test_a_wrong_partition_type_is_rejected(self):
        with open(self.path, "r+b") as fh:
            fh.seek(2 * SECTOR)
            fh.write(bytes(16))           # a nil type GUID
        with self.assertRaises(opencore.OpenCoreError) as ctx:
            opencore.check_gpt(self.path, opencore.ESP_START_SECTOR)
        self.assertIn("EFI System Partition", str(ctx.exception))

    def test_an_esp_at_the_wrong_offset_is_rejected(self):
        with self.assertRaises(opencore.OpenCoreError) as ctx:
            opencore.check_gpt(self.path, 4096)
        self.assertIn("expected", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class EspFilesystem(unittest.TestCase):
    """The ESP must be a LEGAL FAT32 volume, not merely a readable one.

    THE BUG THIS EXISTS FOR. build_boot_image used a 200 MiB image and -c 8, which
    gives 50,836 clusters. FAT32 is defined as 65,525 or more; below that the volume is
    invalid. mtools writes it and reads it back, so mdir passed and every check on this
    side passed. OVMF's EDK II FAT driver validated it, refused to mount, and the
    firmware reported

        BdsDxe: failed to load Boot0001 "UEFI Misc Device" ... : Not Found

    while listing that same disk's partition in its own mapping table. Five runs went
    into the qemu arguments before the console log was read.

    dockur/macos had 256 MiB and -c 4 in buildOpenCoreImage() the whole time.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-fat-")
        self.path = os.path.join(self.tmp, "boot.img")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def build(self, size_mb, sectors_per_cluster):
        """A GPT disk with an ESP of the given geometry, without mtools."""
        old_mb = opencore.BOOT_IMG_MB
        old_cluster = opencore.ESP_SECTORS_PER_CLUSTER
        opencore.BOOT_IMG_MB = size_mb
        opencore.ESP_SECTORS_PER_CLUSTER = sectors_per_cluster
        try:
            total_sectors = size_mb * 1024 * 1024 // SECTOR
            esp_first = opencore.ESP_START_SECTOR
            esp_last = total_sectors - 34
            with open(self.path, "wb") as fh:
                fh.truncate(size_mb * 1024 * 1024)
            opencore.write_gpt(self.path, esp_first, esp_last)
            self.write_fat_boot_sector(esp_first, esp_last, sectors_per_cluster)
            return esp_first
        finally:
            opencore.BOOT_IMG_MB = old_mb
            opencore.ESP_SECTORS_PER_CLUSTER = old_cluster

    def write_fat_boot_sector(self, esp_first, esp_last, sectors_per_cluster):
        total = esp_last - esp_first + 1
        reserved, fats = 32, 2
        sectors_per_fat = 1016
        boot = bytearray(512)
        boot[0:3] = b"\xeb\x58\x90"
        boot[3:11] = b"MTOO4048"
        struct.pack_into("<H", boot, 11, SECTOR)
        boot[13] = sectors_per_cluster
        struct.pack_into("<H", boot, 14, reserved)
        boot[16] = fats
        struct.pack_into("<I", boot, 32, total)
        struct.pack_into("<I", boot, 36, sectors_per_fat)
        boot[82:90] = b"FAT32   "
        boot[510:512] = b"\x55\xAA"
        with open(self.path, "r+b") as fh:
            fh.seek(esp_first * SECTOR)
            fh.write(boot)

    def test_dockur_geometry_is_accepted(self):
        # 256 MiB with 4-sector clusters: what buildOpenCoreImage() uses.
        esp = self.build(256, 4)
        opencore.check_esp_filesystem(self.path, esp)

    def test_the_old_geometry_is_rejected_with_both_numbers(self):
        # 200 MiB with 8-sector clusters: what this repository used, and it is invalid.
        esp = self.build(200, 8)
        with self.assertRaises(opencore.OpenCoreError) as ctx:
            opencore.check_esp_filesystem(self.path, esp)
        message = str(ctx.exception)
        self.assertIn("65525", message)
        self.assertIn(str(opencore.FAT32_MIN_CLUSTERS), message)
        # The message has to point at the fix, not just the symptom.
        self.assertIn("BOOT_IMG_MB", message)
        self.assertIn("ESP_SECTORS_PER_CLUSTER", message)

    def test_the_constants_stay_on_the_legal_side(self):
        """The real geometry, not a fixture: 256 MiB and 4 sectors per cluster."""
        self.assertEqual(opencore.BOOT_IMG_MB, 256)
        self.assertEqual(opencore.ESP_SECTORS_PER_CLUSTER, 4)

        total_sectors = opencore.BOOT_IMG_MB * 1024 * 1024 // SECTOR
        esp_sectors = (total_sectors - 34) - opencore.ESP_START_SECTOR + 1
        # reserved 32 + 2 FATs is what mformat uses; the FAT itself is ~1016 sectors at
        # this size, so the bound below is generous on purpose - the point is the floor.
        clusters = (esp_sectors - 32 - 2 * 1016) // opencore.ESP_SECTORS_PER_CLUSTER
        self.assertGreaterEqual(clusters, opencore.FAT32_MIN_CLUSTERS, clusters)

    def test_a_volume_labelled_fat16_is_rejected(self):
        esp = self.build(256, 4)
        with open(self.path, "r+b") as fh:
            fh.seek(esp * SECTOR + 82)
            fh.write(b"FAT16   ")
        with self.assertRaises(opencore.OpenCoreError) as ctx:
            opencore.check_esp_filesystem(self.path, esp)
        self.assertIn("FAT32", str(ctx.exception))
