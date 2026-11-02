"""The image table is the single source both the publisher and the bake read.

Two things must not drift: what publish-iso.yml pushes, and what windows/11/bake.py
pulls. When they drift, a bake verifies one file and installs another, and the only
symptom is a build number that does not match - which nothing downstream reads.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import images  # noqa: E402


class Tags(unittest.TestCase):

    def test_tag_prefix(self):
        self.assertEqual(images.tag("windows11"), "iso-windows11")

    def test_every_image_gets_a_unique_tag(self):
        table = images.all_images()
        self.assertEqual(len(table), len(set(table)))

    def test_windows_images_are_tagged_for_the_bake_to_find(self):
        # bake.py calls images.tag(self.args.image) and pulls that. If an entry were
        # keyed differently the pull would 404 and fall back to the vendor, which
        # would hide the mismatch rather than report it.
        for name in images.WINDOWS:
            self.assertIn(images.tag(name), images.all_images())

    def test_virtio_is_publishable_and_pullable(self):
        # The driver CD is not optional in practice: without it setup cannot see the
        # disk. It has to be in the table, not fetched separately by the bake.
        self.assertIn("iso-virtio-win", images.all_images())


class Pins(unittest.TestCase):

    def test_every_entry_has_a_url(self):
        for tag, pin in images.all_images().items():
            self.assertTrue(pin.get("url"), tag)
            self.assertTrue(pin["url"].startswith("https://"), tag)

    def test_windows_entries_carry_size_and_hash(self):
        # Windows media is not redistributable and is replaced without notice, so a
        # pin has to be exact: an ISO one build newer installs a different edition at
        # the same index.
        for name, pin in images.WINDOWS.items():
            self.assertIsInstance(pin.get("size"), int, name)
            self.assertIsInstance(pin.get("sha256"), str, name)
            self.assertEqual(len(pin["sha256"]), 64, name)

    def test_windows_entries_name_an_edition_and_an_index(self):
        for name, pin in images.WINDOWS.items():
            self.assertIn("edition", pin, name)
            self.assertIsInstance(pin.get("index"), int, name)
            self.assertGreater(pin["index"], 0, name)

    def test_pro_n_is_forbidden_where_pro_is_expected(self):
        # "Pro" is a substring of "Pro N", so the check needs the negative as well.
        pin = images.WINDOWS["windows11"]
        self.assertEqual(pin["edition"], "Windows 11 Pro")
        self.assertEqual(pin["forbid"], " N")


class PinForBake(unittest.TestCase):

    def test_known_image_returns_its_pin(self):
        pin = images.pin_for_bake("windows11")
        self.assertEqual(pin["index"], 6)

    def test_unknown_image_names_the_alternatives(self):
        with self.assertRaises(KeyError) as ctx:
            images.pin_for_bake("windows98")
        # The message has to say what is available: this is what a user sees after a
        # typo in a workflow input.
        self.assertIn("windows11", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
