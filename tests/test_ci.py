"""The small operations every workflow calls.

These replaced five `run:` blocks that were the only shell left in the pipeline. What
can be tested without a CI runner is the part that has judgement in it: which paths
get pruned, what counts as enough disk, and the fact that a missing summary file does
not fail a job.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import ci  # noqa: E402


class Kvm(unittest.TestCase):

    def test_missing_kvm_is_a_failure_with_a_reason(self):
        # The message has to say why it is fatal rather than "slower": a bake without
        # KVM does not fit in any of these timeouts at all.
        with mock.patch("os.path.exists", return_value=False):
            with self.assertRaises(ci.Failed) as ctx:
                ci.check_kvm()
        self.assertIn("/dev/kvm", str(ctx.exception))
        self.assertIn("impossible", str(ctx.exception))


class FreeDisk(unittest.TestCase):

    def test_the_prune_list_is_absolute_paths_only(self):
        # These are handed to `sudo rm -rf`. A relative path would delete from the
        # checkout, which is the working copy of the repository being built.
        for path in ci.PRUNE:
            self.assertTrue(path.startswith("/"), path)
            self.assertNotIn("..", path, path)

    def test_insufficient_space_fails_and_says_how_much_is_needed(self):
        fake = shutil.disk_usage("/huge") if os.path.exists("/huge") else None
        # Simulate a full disk: free bytes below the threshold in GB.
        usage = type("U", (), {"free": 5 * 1024 ** 3})()
        with mock.patch("shutil.disk_usage", return_value=usage), \
             mock.patch("subprocess.run"), \
             mock.patch("os.path.exists", return_value=False):
            with self.assertRaises(ci.Failed) as ctx:
                ci.free_disk(45)
        self.assertIn("45", str(ctx.exception))
        self.assertIn("5", str(ctx.exception))

    def test_enough_space_passes(self):
        usage = type("U", (), {"free": 100 * 1024 ** 3})()
        with mock.patch("shutil.disk_usage", return_value=usage), \
             mock.patch("subprocess.run"), \
             mock.patch("os.path.exists", return_value=False):
            ci.free_disk(45)


class Repository(unittest.TestCase):

    def test_ghcr_paths_are_lowercased(self):
        # ghcr requires it and github.repository preserves the owner's case; getting
        # this wrong is a push to a package that does not exist, reported as "denied".
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "EasyDev-Org/EasyDiskBake"}):
            self.assertEqual(ci.lowercase_repository(), "easydev-org/easydiskbake")

    def test_missing_repository_is_an_error(self):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_REPOSITORY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ci.Failed):
                ci.lowercase_repository()


class Summary(unittest.TestCase):

    def test_appends_to_the_file_when_it_exists(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "summary.md")
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": path}):
                ci.append_summary("first")
                ci.append_summary("second")
            with open(path) as fh:
                self.assertEqual(fh.read(), "first\nsecond\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_summary_does_not_fail(self):
        # A summary is decoration; a job whose real work succeeded must not fail
        # because the runner did not provide a file.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
        with mock.patch.dict(os.environ, env, clear=True):
            ci.append_summary("text with nowhere to go")

    def test_ensures_a_trailing_newline(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "s.md")
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": path}):
                ci.append_summary("no newline")
            with open(path) as fh:
                self.assertTrue(fh.read().endswith("\n"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
