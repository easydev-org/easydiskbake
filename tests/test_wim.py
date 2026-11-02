"""Parse `wimlib-imagex info` output without needing a WIM.

The WIM itself is 4-5 GiB and lives inside a non-redistributable ISO, so it is not
in the repo and cannot be in the tests. What can be tested is the parsing, which is
where a silent mistake would be possible: a regex that misses the last image, or
that pairs an Index with the Description instead of the Name.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import wim  # noqa: E402


# Captured from `wimlib-imagex info` on a Windows 11 Consumer ISO, abridged.
SAMPLE = """\
WIM Information:
----------------
Path:           /tmp/x/install.wim
GUID:           0x1234567890abcdef
Version:        68864
Image Count:    10
Compression:    LZX
Part Number:    1/1

Available Images:
-----------------
Index:                  1
Name:                   Windows 11 Home
Description:            Windows 11 Home
Display Name:           Windows 11 Home
Display Description:    Windows 11 Home
Directory Count:        31234
File Count:             118795
Total Bytes:            18100000000
Hard Link Bytes:        3000000000
Creation Time:          Sat Sep 13 2025
Modification Time:      Sat Sep 13 2025
Architecture:           x86_64
Product Name:           Microsoft Windows 11 Home
Edition ID:             Core
Installation Type:      Client

Index:                  6
Name:                   Windows 11 Pro
Description:            Windows 11 Pro
Display Name:           Windows 11 Pro
Edition ID:             Professional

Index:                  10
Name:                   Windows 11 Pro N
Description:            Windows 11 Pro N
Display Name:           Windows 11 Pro N
Edition ID:             ProfessionalN
"""


def parse(text):
    """The same loop wim.editions uses, lifted so it can be fed a string."""
    found = []
    index = None
    for line in text.splitlines():
        if line.startswith("Index:"):
            index = int(line.split(":", 1)[1].strip())
        elif line.startswith("Name:") and index is not None:
            found.append((index, line.split(":", 1)[1].strip()))
            index = None
    return found


class Parsing(unittest.TestCase):

    def test_all_images_are_found(self):
        self.assertEqual(
            parse(SAMPLE),
            [
                (1, "Windows 11 Home"),
                (6, "Windows 11 Pro"),
                (10, "Windows 11 Pro N"),
            ],
        )

    def test_name_not_description(self):
        # Description and Display Name repeat the same string here, so a parser
        # keyed on the wrong field would still look right on this sample - which is
        # exactly why the field name is asserted explicitly.
        text = """\
Index:                  3
Description:            Should Not Be Picked Up
Name:                   Windows 11 Education
"""
        self.assertEqual(parse(text), [(3, "Windows 11 Education")])

    def test_duplicate_index_is_not_confused(self):
        # Edition ID also contains a colon and appears after Name; a parser that
        # kept looking after a Name would mispair the next image's fields.
        text = """\
Index:                  1
Name:                   Windows 11 Home
Edition ID:             Core

Index:                  2
Name:                   Windows 11 Enterprise
Edition ID:             Enterprise
"""
        self.assertEqual(
            parse(text),
            [(1, "Windows 11 Home"), (2, "Windows 11 Enterprise")],
        )


class IndexAssertion(unittest.TestCase):
    """assert_index against the parsed table, with editions() stubbed."""

    def setUp(self):
        self.real = wim.editions
        wim.editions = lambda _wim: parse(SAMPLE)

    def tearDown(self):
        wim.editions = self.real

    def test_correct_index_passes(self):
        wim.assert_index("ignored", 6, "Windows 11 Pro", forbid=" N")

    def test_wrong_index_fails(self):
        # Index 1 is Home, not Pro.
        with self.assertRaises(wim.WimError):
            wim.assert_index("ignored", 1, "Windows 11 Pro")

    def test_missing_index_fails(self):
        with self.assertRaises(wim.WimError):
            wim.assert_index("ignored", 99, "Windows 11 Pro")

    def test_forbid_catches_the_n_edition(self):
        # This is the real failure: "Pro" is a substring of "Pro N", so an index
        # pointing at 10 passes a naive check. forbid makes it fail.
        with self.assertRaises(wim.WimError):
            wim.assert_index("ignored", 10, "Windows 11 Pro", forbid=" N")


if __name__ == "__main__":
    unittest.main()
