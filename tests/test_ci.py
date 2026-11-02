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

    def test_it_grants_access_when_the_device_is_denied(self):
        # What a stock GitHub runner looks like: /dev/kvm is 0o660 root:kvm and the
        # job's uid is neither. Refusing would fail every bake in prepare, so the
        # function chmods it and tries again.
        fake_stat = type("S", (), {"st_mode": 0o660, "st_rdev": 0})()
        handle = mock.MagicMock()
        handle.__enter__ = mock.Mock(return_value=handle)
        handle.__exit__ = mock.Mock(return_value=False)
        calls = {"n": 0}

        def fake_open(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "denied")
            return handle

        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.stat", return_value=fake_stat), \
             mock.patch("os.major", lambda _: 10), mock.patch("os.minor", lambda _: 232), \
             mock.patch("builtins.open", side_effect=fake_open), \
             mock.patch("os.access", return_value=False), \
             mock.patch("shutil.which", return_value="/usr/bin/sudo"), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stderr="")
            ci.check_kvm()

        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ["sudo", "chmod", "0666", "/dev/kvm"])

    def test_it_fails_when_chmod_does_not_help(self):
        fake_stat = type("S", (), {"st_mode": 0o660, "st_rdev": 0})()

        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.stat", return_value=fake_stat), \
             mock.patch("os.major", lambda _: 10), mock.patch("os.minor", lambda _: 232), \
             mock.patch("builtins.open", side_effect=PermissionError(13, "denied")), \
             mock.patch("os.access", return_value=False), \
             mock.patch("shutil.which", return_value="/usr/bin/sudo"), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stderr="chmod: failed")
            with self.assertRaises(ci.Failed) as ctx:
                ci.check_kvm()
        self.assertIn("--device /dev/kvm", str(ctx.exception))

    def test_permission_is_decided_by_opening_not_by_os_access(self):
        # os.access() checks the real uid, not the group membership qemu will have, and
        # reported "no write permission" on a runner where KVM worked. This asserts the
        # behaviour rather than the source text: os.access is made to lie, and the
        # function must still succeed because the open succeeds.
        fake_stat = type("S", (), {"st_mode": 0o660, "st_rdev": 0})()

        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.stat", return_value=fake_stat), \
             mock.patch("os.major", lambda _: 10), mock.patch("os.minor", lambda _: 232), \
             mock.patch("builtins.open", mock.mock_open()):
            # No assertion after the call: reaching this line is the assertion. If
            # check_kvm trusted os.access, it would have raised Failed above - which is
            # exactly what happened on a runner where KVM worked.
            ci.check_kvm()


class FreeDisk(unittest.TestCase):
    """It checks, and refuses early. It does not delete.

    The deleting version pruned fourteen toolchain paths copied from dockur/windows,
    on a runner that had 121 GB free against a 30 GB requirement. Two of those paths
    are used by Actions itself.
    """

    def test_insufficient_space_fails_and_says_how_much_is_needed(self):
        usage = type("U", (), {"free": 5 * 1024 ** 3})()
        with mock.patch("shutil.disk_usage", return_value=usage):
            with self.assertRaises(ci.Failed) as ctx:
                ci.free_disk(45)
        self.assertIn("45", str(ctx.exception))
        self.assertIn("5", str(ctx.exception))

    def test_enough_space_passes(self):
        usage = type("U", (), {"free": 100 * 1024 ** 3})()
        with mock.patch("shutil.disk_usage", return_value=usage):
            ci.free_disk(45)

    def test_it_does_not_delete_anything(self):
        # The whole point of the change: no subprocess call, so nothing can be removed.
        usage = type("U", (), {"free": 100 * 1024 ** 3})()
        with mock.patch("shutil.disk_usage", return_value=usage), \
             mock.patch("subprocess.run") as run:
            ci.free_disk(45)
        run.assert_not_called()

    def test_no_prune_list_remains(self):
        # A re-added list would make the previous test pass while still deleting,
        # because the deletion would be inside free_disk rather than the list.
        self.assertFalse(hasattr(ci, "PRUNE"))


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


class Login(unittest.TestCase):
    """oras first, docker second - the image has no docker CLI."""

    def test_uses_oras_when_docker_is_absent(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_ACTOR": "a"}), \
             mock.patch("shutil.which", side_effect=lambda n: "/usr/bin/oras" if n == "oras" else None), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stderr=b"")
            ci.login("ghcr.io")
        self.assertEqual(run.call_args[0][0][0], "oras")

    def test_prefers_oras_when_both_exist(self):
        # oras covers every push this repository makes; docker is the fallback for a
        # host runner where an image build needs the session.
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_ACTOR": "a"}), \
             mock.patch("shutil.which", return_value="/usr/bin/x"), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stderr=b"")
            ci.login("ghcr.io")
        self.assertEqual(run.call_args[0][0][0], "oras")

    def test_falls_back_to_docker(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_ACTOR": "a"}), \
             mock.patch("shutil.which", side_effect=lambda n: "/usr/bin/docker" if n == "docker" else None), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stderr=b"")
            ci.login("ghcr.io")
        self.assertEqual(run.call_args[0][0][0], "docker")

    def test_neither_tool_is_a_clear_failure(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_ACTOR": "a"}), \
             mock.patch("shutil.which", return_value=None):
            with self.assertRaises(ci.Failed) as ctx:
                ci.login("ghcr.io")
        self.assertIn("oras", str(ctx.exception))

    def test_the_token_goes_through_stdin(self):
        # A shell pipe puts it where any process in the job can read it.
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "secret-token", "GITHUB_ACTOR": "a"}), \
             mock.patch("shutil.which", return_value="/usr/bin/oras"), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stderr=b"")
            ci.login("ghcr.io")
        self.assertEqual(run.call_args.kwargs["input"], b"secret-token")


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
