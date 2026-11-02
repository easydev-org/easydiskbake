"""The answer file, checked as a document rather than as a template.

Two bugs here each cost a full run, and neither was visible from the outside: setup
shows a page and waits for a person, and the only symptom is a screen that never
changes.

    a missing <ProductKey>    setup stops at "Enter a product key"
    an unsubstituted index    setup cannot resolve the edition, abandons the answer
                              file, and lands on the same page
"""

import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSWER = os.path.join(REPO, "windows", "11", "answer", "Autounattend.xml")


def read():
    with open(ANSWER, encoding="utf-8") as fh:
        return fh.read()


class ProductKey(unittest.TestCase):
    """Without one, an unattended install stops and asks."""

    def test_a_generic_key_is_present(self):
        text = read()
        self.assertIn("<ProductKey>", text)
        self.assertRegex(text, r"<Key>[A-Z0-9]{5}(-[A-Z0-9]{5}){4}</Key>")

    def test_it_is_the_windows_11_pro_generic_key(self):
        # VK7JG-NPHTM-C97JM-9MPGT-3V66T selects Pro and activates nothing. The edition
        # has to agree with the image index the bake verifies with lib/wim.py, or the
        # key and the index disagree about what is being installed.
        self.assertIn("VK7JG-NPHTM-C97JM-9MPGT-3V66T", read())

    def test_setup_is_told_not_to_show_it(self):
        self.assertIn("<WillShowUI>Never</WillShowUI>", read())

    def test_it_sits_in_userdata_of_windowspe(self):
        # windowsPE is where the key is consumed; in another pass it is ignored and the
        # page appears anyway.
        text = read()
        pe = text.split('pass="windowsPE"')[1].split("</settings>")[0]
        self.assertIn("<ProductKey>", pe)


class Placeholders(unittest.TestCase):
    """Every placeholder must be substituted, and the bake now checks that."""

    def test_the_known_placeholders_are_the_ones_substituted(self):
        # bake.py builds this set; anything else in the file would survive into the
        # answer file that setup reads.
        known = {"__ADMIN_PASSWORD__", "__COMPUTER_NAME__", "__WINDOWS_IMAGE_INDEX__"}
        found = set(re.findall(r"__[A-Z_]+__", read()))
        self.assertEqual(found, known, f"unexpected placeholders: {found - known}")

    def test_the_image_index_placeholder_is_used_for_the_edition(self):
        text = read()
        self.assertIn("/IMAGE/INDEX", text)
        self.assertIn("__WINDOWS_IMAGE_INDEX__", text)


class Structure(unittest.TestCase):

    def test_all_four_passes_are_present(self):
        text = read()
        for p in ("windowsPE", "offlineServicing", "specialize", "oobeSystem"):
            with self.subTest(pass_=p):
                self.assertIn(f'pass="{p}"', text)

    def test_the_disk_index_matches_a_cd_install(self):
        # The answer file travels on a CD, not as disk 0 as dockur does, so the virtio
        # disk keeps index 0. A file that says 1 here would partition the wrong disk.
        self.assertIn("<DiskID>0</DiskID>", read())

    def test_it_is_valid_xml(self):
        import xml.etree.ElementTree as ET
        ET.parse(ANSWER)


if __name__ == "__main__":
    unittest.main()
