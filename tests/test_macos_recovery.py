"""The parts of the Apple recovery exchange that do not need Apple.

The live exchange cannot be in a test suite: it needs a session cookie, fresh nonces
and a board identifier that Apple currently serves. What can be tested is everything
that decides what to do with the reply - which is where the mistakes were:

    - the token is a Cookie header, not a query parameter
    - the reply's AT field contains '~'-separated attributes, not a bare .dmg
    - the board identifier selects the release
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import macos_recovery as mr  # noqa: E402


# A real reply, as Apple sent it (URL and hashes are from a live request).
REPLY = """AP: 093-10615
AU: http://oscdn.apple.com/content/downloads/52/42/093-10615/f1wx7fb81oy1x1l3d5kjfce2iurbhw9qbk/RecoveryImage/BaseSystem.dmg
AH: F0B5184B6D5B28F94ECBD180017B5F20A80EAFBF94AA0C85C622193B9864F4A4
AT: expires=1793605576~access=/content/downloads/52/42/093-10615/f1wx7fb81oy1x1l3d5kjfce2iurbhw9qbk/RecoveryImage/BaseSystem.dmg~md5=19c1692f70129214140d3db60bf457c5
CU: http://oscdn.apple.com
"""


class Releases(unittest.TestCase):

    def test_numbers_and_names_resolve_to_the_same_board(self):
        for number, name in (("14", "sonoma"), ("15", "sequoia"), ("26", "tahoe")):
            with self.subTest(release=number):
                self.assertEqual(mr.resolve(number), mr.resolve(name))

    def test_each_release_has_its_own_board(self):
        # A shared board identifier would silently fetch one release three times.
        boards = {mr.resolve(r)[2] for r in ("14", "15", "26")}
        self.assertEqual(len(boards), 3)

    def test_point_releases_collapse_to_the_major(self):
        # The catalog is per major release; "15.1" and "15" get the same image.
        self.assertEqual(mr.resolve("15.1")[1], "15")

    def test_unknown_release_names_the_alternatives(self):
        with self.assertRaises(mr.RecoveryError) as ctx:
            mr.resolve("99")
        self.assertIn("15", str(ctx.exception))

    def test_nonsense_is_an_error_not_a_default(self):
        # Defaulting to the newest would download 850 MiB of the wrong thing.
        with self.assertRaises(mr.RecoveryError):
            mr.resolve("latest")


class Parsing(unittest.TestCase):
    """The reply's shape, which is not what it looks like."""

    def test_url_and_token_are_extracted(self):
        url, token, _ = mr.parse_urls(REPLY)
        self.assertTrue(url.startswith("http://oscdn.apple.com/"))
        self.assertTrue(url.endswith("BaseSystem.dmg"))
        self.assertTrue(token.startswith("expires="))
        # The token carries the access path and an md5, separated by '~'. An earlier
        # version looked for a token ending in ".dmg" and found none, because the
        # `.dmg` lives inside access= rather than at the end of the record.
        self.assertIn("access=", token)
        self.assertIn("md5=", token)

    def test_token_is_not_part_of_the_url(self):
        # THE BUG: the token looks like a signed query string, so gluing it on looks
        # right. It is sent as `Cookie: AssetToken=...` instead, and a URL with it
        # appended gets a 200 with an error body or a truncated transfer.
        url, token, _ = mr.parse_urls(REPLY)
        self.assertNotIn("expires=", url)
        self.assertNotIn("~", url)

    def test_missing_url_is_an_error(self):
        with self.assertRaises(mr.RecoveryError):
            mr.parse_urls("AP: 093-10615\nCU: http://oscdn.apple.com\n")

    def test_missing_token_is_an_error(self):
        # Without AT the download would 403, and saying so now is cheaper than saying
        # it from curl.
        body = "AU: http://oscdn.apple.com/x/BaseSystem.dmg\n"
        with self.assertRaises(mr.RecoveryError) as ctx:
            mr.parse_urls(body)
        self.assertIn("403", str(ctx.exception))

    def test_the_md5_is_read_out_of_the_token(self):
        _, token, _ = mr.parse_urls(REPLY)
        import re
        self.assertEqual(re.search(r"md5=([0-9a-f]{32})", token).group(1),
                         "19c1692f70129214140d3db60bf457c5")


class Payload(unittest.TestCase):

    def test_shape_matches_what_apple_expects(self):
        body = mr._payload("Mac-937A206F2EE63C01").decode()
        fields = dict(line.split("=", 1) for line in body.splitlines())
        self.assertEqual(set(fields), {"cid", "sn", "bid", "k", "fg", "os"})
        self.assertEqual(fields["bid"], "Mac-937A206F2EE63C01")
        self.assertEqual(fields["os"], "latest")
        self.assertEqual(len(fields["cid"]), 16)
        self.assertEqual(len(fields["k"]), 64)
        self.assertEqual(len(fields["fg"]), 64)

    def test_nonces_are_fresh_each_time(self):
        # Apple rejects a replayed nonce set; two calls must not produce the same body.
        self.assertNotEqual(mr._payload("B"), mr._payload("B"))


class DmgCheck(unittest.TestCase):

    def test_koly_trailer_is_accepted(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "x.dmg")
            # A UDIF image ends with a 512-byte trailer; "koly" is the last four bytes
            # OF THAT TRAILER, not of the file.
            with open(path, "wb") as fh:
                fh.write(b"\x00" * 1024)
                fh.write(b"\x00" * 508)
                fh.write(b"koly")
            mr.verify_dmg(path)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_html_is_rejected(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "x.dmg")
            with open(path, "wb") as fh:
                fh.write(b"<!DOCTYPE html><html>403 Forbidden</html>")
            with self.assertRaises(mr.RecoveryError):
                mr.verify_dmg(path)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
