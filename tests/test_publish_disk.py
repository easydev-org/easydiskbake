"""The push step's argument assembly, without oras or a 64 GiB file.

The push is the last thing that happens to an expensive artifact, so a detail that is
wrong here fails after everything else succeeded. Everything except the subprocess call
is testable: the tag shape, the annotations, and the refusals.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import ci  # noqa: E402
import publish_disk  # noqa: E402


class Stamp(unittest.TestCase):

    def test_stamp_shape(self):
        # YYYYmmdd-HHMMSS: sorts lexicographically, which is what makes "the newest
        # windows-base-*" a string comparison rather than a registry query.
        stamp = publish_disk.utc_stamp()
        self.assertEqual(len(stamp), 15, stamp)
        self.assertEqual(stamp[8], "-")
        self.assertTrue(stamp[:8].isdigit())
        self.assertTrue(stamp[9:].isdigit())


class Refusals(unittest.TestCase):

    def test_missing_disk_fails_before_oras(self):
        with self.assertRaises(ci.Failed) as ctx:
            publish_disk.push("/nonexistent.qcow2", "t", "o/r", kind="windows-base")
        self.assertIn("no disk", str(ctx.exception))

    def test_unreadable_disk_fails(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "disk.qcow2")
            open(path, "wb").close()
            with mock.patch("os.access", return_value=False):
                with self.assertRaises(ci.Failed) as ctx:
                    publish_disk.push(path, "t", "o/r", kind="windows-base")
            self.assertIn("not readable", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Arguments(unittest.TestCase):
    """The argv oras receives, captured instead of run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.disk = os.path.join(self.tmp, "disk.qcow2")
        with open(self.disk, "wb") as fh:
            fh.write(b"\x00" * 4096)
        self.captured = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _capture(self, *args, **kwargs):
        self.captured = args[0]
        return mock.Mock(returncode=0, stdout="pushed\n", stderr="")

    def test_oras_gets_a_list_not_a_shell_string(self):
        # The whole reason this is Python: an annotation containing a URL with an "&"
        # or a path with a space is a list element here and a quoting problem in shell.
        with mock.patch("subprocess.run", side_effect=self._capture), \
             mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "EasyDev-Org/EasyDiskBake"}):
            publish_disk.push(self.disk, "windows-base-20260101-000000", "easydev-org/easydiskbake",
                              kind="windows-base")

        argv = self.captured
        self.assertEqual(argv[0], "oras")
        self.assertIn("push", argv)
        self.assertIn("ghcr.io/easydev-org/easydiskbake:windows-base-20260101-000000", argv)

        # The artifact type and the layer media type must agree, because the pull side
        # reads the layer from the manifest without inspecting it.
        artifact_type = argv[argv.index("--artifact-type") + 1]
        self.assertEqual(artifact_type, "application/vnd.easydev.disk.qcow2")
        self.assertTrue(any(a.endswith("+zstd") for a in argv))

    def test_push_uses_a_bare_filename_from_the_file_directory(self):
        # oras rejects an absolute path in a file:media:type reference. The flag it
        # suggests, --disable-path-validation, also turns off the "..", symlink and
        # outside-cwd checks - which exist so a typo cannot publish ~/.ssh/id_rsa.
        #
        # A bare filename with cwd set to the file's directory satisfies the check
        # instead of disabling it.
        with mock.patch("subprocess.run", side_effect=self._capture) as run, \
             mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r"}):
            publish_disk.push(self.disk, "t", "o/r", kind="windows-base")

        argv = self.captured
        self.assertNotIn("--disable-path-validation", argv)
        self.assertTrue(any(a.startswith("disk.qcow2:") for a in argv), argv)
        self.assertFalse(any(a.startswith("/") and ":application" in a for a in argv))

        # The directory is passed as cwd rather than changed globally, so it cannot
        # leak into a later call.
        self.assertEqual(run.call_args.kwargs.get("cwd"), self.tmp)

    def test_the_kind_is_recorded_as_an_annotation(self):
        with mock.patch("subprocess.run", side_effect=self._capture), \
             mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r"}):
            publish_disk.push(self.disk, "t", "o/r", kind="windows-ssh")

        joined = " ".join(self.captured)
        self.assertIn("dev.easydev.kind=windows-ssh", joined)
        self.assertIn("org.opencontainers.image.title=windows-ssh", joined)

    def test_a_failed_push_raises(self):
        with mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=1, stdout="", stderr="denied")):
            with self.assertRaises(ci.Failed) as ctx:
                publish_disk.push(self.disk, "t", "o/r", kind="windows-base")
        self.assertIn("denied", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
