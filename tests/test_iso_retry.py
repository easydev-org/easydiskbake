"""A ten gigabyte download over the public internet does not succeed once.

THE FAILURE THIS EXISTS FOR, MEASURED ON RUN 41481670715

    [iso]  3392 MiB / 10763 MiB (32%)
    iso.IsoError: size mismatch: got 3472883712, the manifest says 11286607360

The connection stopped at 3.2 GiB of 10.5 GiB and nothing was retried, so the run - which
had already prepared the runner and pulled the base image - was lost to a dropped
connection. The partial file was on disk, complete up to that byte, and unused.

WHAT THE RETRY HAS TO DO THAT A PLAIN LOOP DOES NOT

Resume. `_download` writes to `<dest>.part` and the retry sends `Range: bytes=<have>-`
so a second attempt costs the missing gigabytes and not the whole blob. That needs care
in both directions: a server that honours the Range answers 206 and the part is
appended to; a server that ignores it answers 200 with the blob from byte zero, and the
part must be truncated first or the file becomes the blob followed by its first half.
"""

import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock
import urllib.error

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))

import iso  # noqa: E402


class Answer:
    """A urlopen() replacement that serves a blob, optionally in pieces."""

    def __init__(self, body: bytes, *, status: int = 200, fail_after: int | None = None):
        self.body = body
        self.status = status
        self.fail_after = fail_after
        self.headers = {"Content-Length": str(len(body))}

    def read(self, amount: int = -1) -> bytes:
        if self.fail_after is not None and self.pos >= self.fail_after:
            raise OSError("connection reset")
        if amount < 0:
            amount = len(self.body)
        chunk = self.body[self.pos:self.pos + amount]
        self.pos += len(chunk)
        return chunk

    def __enter__(self):
        self.pos = 0
        return self

    def __exit__(self, *exc):
        return False


class Resuming(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iso-retry-")
        self.dest = os.path.join(self.tmp, "disk.qcow2")
        self.body = b"x" * (4 << 20)   # 4 MiB, four read() calls

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def run_download(self, side_effect, expected_size=None):
        with mock.patch("urllib.request.urlopen", side_effect=side_effect), \
             mock.patch("time.sleep"):
            iso._download("https://example.invalid/blob", self.dest, scope="",
                          expected_size=expected_size if expected_size is not None
                          else len(self.body))

    def test_a_short_transfer_is_retried_and_resumes(self):
        """First attempt dies at 2 MiB; the second sends Range and finishes."""
        calls = []

        def fake(req, timeout=None):
            calls.append(req)
            if len(calls) == 1:
                return Answer(self.body, fail_after=2 << 20)
            # Second attempt: the part holds 2 MiB, the server sends the rest.
            return Answer(self.body[2 << 20:], status=206)

        self.run_download(fake)

        self.assertTrue(os.path.isfile(self.dest))
        self.assertEqual(os.path.getsize(self.dest), len(self.body))
        self.assertEqual(len(calls), 2)
        # The second request asked for the remainder rather than the whole blob.
        self.assertEqual(calls[1].headers.get("Range"), f"bytes={2 << 20}-")

    def test_a_server_that_ignores_the_range_does_not_duplicate_bytes(self):
        """A 200 response means the body starts at zero; the part must be truncated.

        Appending a whole blob to a half-written part is the failure this guards: the
        file would be 6 MiB for a 4 MiB blob, which the size check would reject - but
        only after the transfer, and with a message about size rather than about ranges.
        """
        calls = []

        def fake(req, timeout=None):
            calls.append(req)
            if len(calls) == 1:
                return Answer(self.body, fail_after=2 << 20)
            return Answer(self.body, status=200)   # no Range support

        self.run_download(fake)

        self.assertEqual(os.path.getsize(self.dest), len(self.body))
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), self.body)

    def test_it_gives_up_after_the_attempt_budget_with_a_reason(self):
        def fake(req, timeout=None):
            raise urllib.error.URLError("no route to host")

        with self.assertRaises(iso.IsoError) as ctx:
            self.run_download(fake)
        self.assertIn("attempts", str(ctx.exception))
        self.assertIn("no route to host", str(ctx.exception))

    def test_an_already_complete_part_is_moved_without_a_transfer(self):
        with open(self.dest + ".part", "wb") as fh:
            fh.write(self.body)
        called = []
        self.run_download(lambda *a, **k: called.append(1) or Answer(self.body))
        self.assertFalse(called, "a complete part must not be downloaded again")
        self.assertTrue(os.path.isfile(self.dest))


if __name__ == "__main__":
    unittest.main()
