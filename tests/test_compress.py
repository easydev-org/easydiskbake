"""lib/compress.py: asking qemu-img what it can do instead of assuming.

THE BUG THIS EXISTS FOR

Every bake ended with

    qemu-img convert -O qcow2 -c -o compression_type=zstd disk.qcow2 out.qcow2

and on 2026-11-02 that failed every Windows bake at the last step, after a successful
installation and a passing disk check:

    qemu-img: Invalid parameter 'compression_type'

The bake image's qemu-img is 2.12.0 - a 2018 build, inside an image whose qemu BINARY
is 11.1. `compression_type` arrived in qemu 5.1, so the option has never existed in the
tool the bake runs, and nothing before the final step used qemu-img convert.

The tests below are about the two properties that keep that from recurring:

    the decision comes from the tool's own option list, not from a version string
    a failed conversion never leaves a file under the final name
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import compress  # noqa: E402


FAKE_QEMU_IMG = '''#!/usr/bin/env python3
"""A stand-in for qemu-img, with the behaviour a test asks for."""
import os
import shutil
import sys

ARGV = sys.argv[1:]
LOG = os.environ["FAKE_QEMU_IMG_LOG"]
MODE = os.environ["FAKE_QEMU_IMG_MODE"]

with open(LOG, "a") as fh:
    fh.write(" ".join(ARGV) + "\\n")

if "--version" in ARGV:
    print(os.environ.get("FAKE_QEMU_IMG_VERSION", "qemu-img version 2.12.0"))
    raise SystemExit(0)

if ARGV[:2] == ["create", "-f"]:
    open(ARGV[-2], "wb").write(b"\\0" * 1024)
    raise SystemExit(0)

if "convert" in ARGV:
    if "-o" in ARGV and "help" in ARGV:
        print("Supported options:")
        print("size             Virtual disk size")
        if MODE in ("zstd", "fail"):
            print("compression_type Compression method (zlib, zstd)")
        raise SystemExit(0)

    if MODE == "fail":
        sys.stderr.write("qemu-img: simulated conversion failure\\n")
        # A failing qemu-img may still have created a partial file; the caller must not
        # publish it. Write one to prove the temporary-name rule.
        open(ARGV[-1], "wb").write(b"partial")
        raise SystemExit(1)

    # A successful conversion writes something small.
    open(ARGV[-1], "wb").write(b"converted" * 100)
    raise SystemExit(0)

raise SystemExit(2)
'''


class Compress(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="compress-test-")
        self.log = os.path.join(self.tmp, "calls.log")
        self.fake = os.path.join(self.tmp, "qemu-img")
        with open(self.fake, "w") as fh:
            fh.write(FAKE_QEMU_IMG)
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IEXEC)

        self.source = os.path.join(self.tmp, "disk.qcow2")
        with open(self.source, "wb") as fh:
            fh.write(b"source" * 1000)
        self.dest = os.path.join(self.tmp, "out.qcow2")

        self.old_env = dict(os.environ)
        os.environ["FAKE_QEMU_IMG_LOG"] = self.log
        os.environ["QEMU_IMG"] = self.fake

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def calls(self):
        try:
            with open(self.log) as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def test_zstd_is_used_when_the_option_exists(self):
        os.environ["FAKE_QEMU_IMG_MODE"] = "zstd"
        compress.compress(self.source, self.dest)
        self.assertTrue(os.path.exists(self.dest))
        conversions = [c for c in self.calls() if "convert" in c and "help" not in c]
        self.assertEqual(len(conversions), 1)
        self.assertIn("compression_type=zstd", conversions[0])

    def test_a_tool_without_zstd_is_a_failure(self):
        """NO FALLBACK TO zlib.

        A quiet downgrade lived here for one revision. It produced disks 10-20% larger
        under a log line that read like a note, which is how a defect becomes permanent:
        nothing fails, the artifact is simply worse than the one it replaced.

        If qemu-img cannot write zstd, the tool is wrong - and the message says where to
        look, because the cause that actually happened was a qemu-img shadowed on PATH
        by the Android SDK's copy, reporting the same version string.
        """
        os.environ["FAKE_QEMU_IMG_MODE"] = "zlib"
        with self.assertRaises(RuntimeError) as ctx:
            compress.compress(self.source, self.dest)
        message = str(ctx.exception)
        self.assertIn("zstd", message)
        self.assertIn("shadow", message.lower())
        # Nothing was written: no artifact, and no .part for a retry to mistake for one.
        self.assertFalse(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_the_option_list_is_asked_not_the_version(self):
        """A version string must not decide this.

        A backport can carry the option without the version, and this build's version
        string is a vendor fork that a parser would misread.
        """
        os.environ["FAKE_QEMU_IMG_MODE"] = "zstd"
        os.environ["FAKE_QEMU_IMG_VERSION"] = "qemu-img version 2.12.0"
        compress.compress(self.source, self.dest)
        conversions = [c for c in self.calls() if "convert" in c and "help" not in c]
        self.assertIn("compression_type=zstd", conversions[0])

    def test_a_failed_conversion_leaves_nothing_under_the_final_name(self):
        # zstd mode, so the capability check passes and the failure being tested is the
        # conversion itself.
        os.environ["FAKE_QEMU_IMG_MODE"] = "fail"
        with self.assertRaises(RuntimeError) as ctx:
            compress.compress(self.source, self.dest)
        self.assertIn("qemu-img convert failed", str(ctx.exception))
        self.assertFalse(os.path.exists(self.dest),
                         "a failed conversion must not leave a file to publish")
        # And no stray .part either - it would be picked up by a retry as a cache hit.
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_a_missing_source_fails_before_running_anything(self):
        with self.assertRaises(FileNotFoundError):
            compress.compress(os.path.join(self.tmp, "nope.qcow2"), self.dest)
        self.assertEqual(self.calls(), [])

    def test_the_probe_cleans_up_after_itself(self):
        os.environ["FAKE_QEMU_IMG_MODE"] = "zstd"
        before = set(os.listdir(tempfile.gettempdir()))
        compress.supports_zstd()
        after = set(os.listdir(tempfile.gettempdir()))
        leaked = [name for name in after - before if name.startswith("qemu-img-probe-")]
        self.assertEqual(leaked, [], f"probe left {leaked} behind")


if __name__ == "__main__":
    unittest.main()
