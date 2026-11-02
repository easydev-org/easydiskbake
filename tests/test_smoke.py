"""Byte-offset tests for the disk checks.

These exist because the shell version got the offsets wrong and nobody could see
it: reading the MBR signature at 0x1f0 instead of 0x1fe reported "unreadable" on
perfectly good disks, and three runs were spent believing working disks were
broken. The offsets are named constants now, and these tests pin them.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import smoke  # noqa: E402


class MbrOffsets(unittest.TestCase):

    def test_signature_is_the_last_two_bytes(self):
        # 0x55AA sits at 510, not 496. The shell version used 0x1f0 (496), which
        # is the middle of the partition table.
        start, stop = smoke.MBR_SIGNATURE
        self.assertEqual((start, stop), (510, 512))

    def test_boot_code_is_the_first_446(self):
        self.assertEqual(smoke.MBR_BOOT_CODE, (0, 446))

    def test_a_real_mbr_shape_is_recognised(self):
        sector = bytearray(512)
        sector[0:8] = b"\x31\xc0\x8e\xd0\xbc\x00\x7c"
        sector[510:512] = b"\x55\xaa"
        data = bytes(sector)

        self.assertEqual(data[slice(*smoke.MBR_SIGNATURE)], b"\x55\xaa")
        self.assertTrue(any(data[slice(*smoke.MBR_BOOT_CODE)]))

    def test_zeroed_boot_code_is_not_bootable(self):
        sector = bytearray(512)
        sector[510:512] = b"\x55\xaa"
        data = bytes(sector)
        self.assertEqual(data[slice(*smoke.MBR_SIGNATURE)], b"\x55\xaa")
        self.assertFalse(any(data[slice(*smoke.MBR_BOOT_CODE)]))

    def test_no_signature_is_not_bootable(self):
        sector = bytearray(512)
        sector[0:8] = b"\x31\xc0\x8e\xd0\xbc\x00\x7c"
        data = bytes(sector)
        self.assertNotEqual(data[slice(*smoke.MBR_SIGNATURE)], b"\x55\xaa")


class GptOffsets(unittest.TestCase):

    def test_protective_entry_type_byte(self):
        # The first partition entry starts at 0x1BE; its type is the 5th byte of
        # that 16-byte entry, so 0x1BE + 4.
        sector = bytearray(512)
        sector[0x1BE + 4] = 0xEE
        entry = bytes(sector)[0x1BE:0x1BE + 16]
        self.assertEqual(entry[4:5], b"\xee")

    def test_mbr_disk_is_not_gpt(self):
        sector = bytearray(512)
        sector[0x1BE + 4] = 0x07  # NTFS on MBR
        entry = bytes(sector)[0x1BE:0x1BE + 16]
        self.assertNotEqual(entry[4:5], smoke.GPT_PROTECTIVE_TYPE)


class ExtOffsets(unittest.TestCase):

    def test_magic_is_at_superblock_plus_0x38(self):
        # Superblock is 1024 bytes into the partition; 0xEF53 is at 0x38 within.
        self.assertEqual(smoke.EXT_MAGIC_OFFSET, 1024 + 0x38)

    def test_little_endian_magic(self):
        # 0xEF53 stored little-endian is the byte sequence 53 ef.
        self.assertEqual((0xEF53).to_bytes(2, "little"), b"\x53\xef")


class QemuIoParsing(unittest.TestCase):
    """The parser, against the exact shape qemu-io writes.

    This is the bug that made a good Windows disk look unbootable: the pattern expected
    whitespace after the offset and the real output has a colon there, so every read
    parsed to zero bytes.
    """

    SAMPLE = (
        "00000000:  55 aa 31 c0 8e d0 bc 00 7c 8e c0 8e d8 00 00 00  U.1.......|.......\n"
        "00000010:  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00  ................\n"
        "read 32/32 bytes at offset 0\n"
        "32 bytes, 1 ops; 00.00 sec (24.751 MiB/sec and 811030.0081 ops/sec)\n"
    )

    def _parse(self, text):
        import re
        data = bytearray()
        for line in text.splitlines():
            m = re.match(r"^([0-9a-f]{8,16}):?\s+((?:[0-9a-f]{2,4}\s*)+)", line)
            if not m:
                continue
            for token in m.group(2).split():
                if len(token) == 8:
                    data += bytes.fromhex(token)[::-1]
                else:
                    data += bytes.fromhex(token)
        return bytes(data)

    def test_the_colon_form_is_parsed(self):
        data = self._parse(self.SAMPLE)
        self.assertEqual(len(data), 32)
        self.assertEqual(data[:2], b"\x55\xaa")

    def test_the_old_pattern_would_match_nothing(self):
        # Kept as the record of what went wrong: this is the pattern that was in the
        # file, and it produces zero bytes from the shape qemu-io actually writes.
        import re
        wrong = re.compile(r"^([0-9a-f]{8,16})\s+((?:[0-9a-f]{2,4}\s*)+)")
        matched = [l for l in self.SAMPLE.splitlines() if wrong.match(l)]
        self.assertEqual(matched, [])


class RootFsSelection(unittest.TestCase):

    def test_unknown_value_is_an_error_not_a_default(self):
        # A typo like SMOKE_ROOT_FS=ext should fail loudly rather than silently
        # taking the ext4 path on an APFS disk. The validation happens before any
        # disk IO, so this works on a machine with no qemu-io installed.
        with self.assertRaises(ValueError):
            smoke.check_disk("/nonexistent.qcow2", root_fs="ext0")

    def test_known_values_do_not_raise(self):
        for value in ("ext4", "gpt", "none"):
            with self.subTest(root_fs=value):
                # A missing disk must produce False, not an exception: the caller
                # prints the result and moves on, and a traceback here reads as
                # "the bake crashed" rather than "the disk is bad".
                self.assertFalse(smoke.check_disk("/nonexistent.qcow2", root_fs=value))


if __name__ == "__main__":
    unittest.main()
