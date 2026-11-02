"""The three checks publish_iso.py runs, each against a synthetic file.

The download and the push need a network and gigabytes; the checks do not, and the
checks are where the judgement is. Each one catches a different failure:

    exact byte count      a truncated transfer that exited 0
    sha256                a different build entirely
    ISO9660 at sector 16  an HTML error page served with 200 OK
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import publish_iso  # noqa: E402


def make_iso(path: str, *, sectors: int = 20, payload: bytes = b"") -> str:
    """A file that looks like an ISO9660 image to the extent the check cares."""
    with open(path, "wb") as fh:
        fh.write(b"\x00" * (16 * 2048))
        # Primary volume descriptor: type 1, then "CD001", version 1.
        fh.write(b"\x01CD001\x01")
        fh.write(b"\x00" * (sectors * 2048 - 16 * 2048 - 7))
        fh.write(payload)
    return path


class SizeCheck(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.iso = make_iso(os.path.join(self.tmp, "x.iso"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matching_size_passes(self):
        pin = {"size": os.path.getsize(self.iso), "sha256": None}
        publish_iso.check(self.iso, pin)

    def test_wrong_size_fails_with_the_pin_in_the_message(self):
        pin = {"size": os.path.getsize(self.iso) + 1, "sha256": None}
        with self.assertRaises(publish_iso.PublishError) as ctx:
            publish_iso.check(self.iso, pin)
        # The message has to say what to change, not just that something is wrong.
        self.assertIn("lib/images.py", str(ctx.exception))

    def test_unpinned_size_is_accepted(self):
        publish_iso.check(self.iso, {"size": None, "sha256": None})


class Iso9660Check(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_real_iso_header_passes(self):
        path = make_iso(os.path.join(self.tmp, "ok.iso"))
        publish_iso.check(path, {"size": None, "sha256": None})

    def test_html_error_page_fails(self):
        # A 200 OK carrying an error page is the failure this check exists for: the
        # transfer succeeds, the size may even be plausible, and the file is not an
        # image.
        path = os.path.join(self.tmp, "err.iso")
        with open(path, "wb") as fh:
            fh.write(b"<!DOCTYPE html><html><body>404 Not Found</body></html>")
            fh.write(b"\x00" * 40000)
        with self.assertRaises(publish_iso.PublishError) as ctx:
            publish_iso.check(path, {"size": None, "sha256": None})
        self.assertIn("ISO9660", str(ctx.exception))

    def test_zero_filled_file_fails(self):
        path = os.path.join(self.tmp, "zero.iso")
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 40000)
        with self.assertRaises(publish_iso.PublishError):
            publish_iso.check(path, {"size": None, "sha256": None})

    def test_truncated_before_sector_16_fails(self):
        path = os.path.join(self.tmp, "short.iso")
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 1000)
        with self.assertRaises(publish_iso.PublishError):
            publish_iso.check(path, {"size": None, "sha256": None})


class Sha256Check(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.iso = make_iso(os.path.join(self.tmp, "x.iso"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _digest(self):
        return publish_iso.sha256_of(self.iso)

    def test_matching_hash_passes(self):
        publish_iso.check(self.iso, {"size": None, "sha256": self._digest()})

    def test_mismatched_hash_fails(self):
        wrong = hashlib.sha256(b"different build").hexdigest()
        with self.assertRaises(publish_iso.PublishError) as ctx:
            publish_iso.check(self.iso, {"size": None, "sha256": wrong})
        message = str(ctx.exception)
        self.assertIn(wrong, message)
        self.assertIn("lib/images.py", message)

    def test_unpinned_hash_is_accepted(self):
        publish_iso.check(self.iso, {"size": None, "sha256": None})


class Sha256Of(unittest.TestCase):

    def test_it_hashes_the_file_not_the_path(self):
        tmp = tempfile.mkdtemp()
        try:
            a = os.path.join(tmp, "a.bin")
            b = os.path.join(tmp, "b.bin")
            with open(a, "wb") as fh:
                fh.write(b"same content")
            with open(b, "wb") as fh:
                fh.write(b"same content")
            self.assertEqual(publish_iso.sha256_of(a), publish_iso.sha256_of(b))
            self.assertEqual(
                publish_iso.sha256_of(a),
                hashlib.sha256(b"same content").hexdigest(),
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
