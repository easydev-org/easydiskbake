"""Checks on the post-install script that do not need a guest.

WHY THESE EXIST. Every failure this file guards against was found by waiting thirty-six minutes
for a bake to time out, and the first two are visible without booting anything:

  - writing into a directory that does not exist yet, which POSIX sh does NOT treat as a command
    failure and therefore does not stop on;
  - blocking on a network call with no bound, whose only symptom is a disk size that stops
    growing.

A bake is the most expensive test in this repository. These are the cheapest.
"""

from __future__ import annotations

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "installerconfig.sh")


def read() -> str:
    with open(SCRIPT, encoding="utf-8") as fh:
        return fh.read()


class InstallerConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read()

    def test_sudoers_directory_is_created_before_the_file(self):
        """`cat > missing/dir` fails at the REDIRECTION, which `set -e` does not catch.

        It printed one line of console noise, left the disk with no sudoers file, and every later
        `sudo` hung at a password prompt with no terminal to answer it.
        """
        mkdir_at = self.text.find("mkdir -p /usr/local/etc/sudoers.d")
        write_at = self.text.find("/usr/local/etc/sudoers.d/90-docker-nopasswd")
        self.assertNotEqual(mkdir_at, -1, "the sudoers.d directory is never created")
        self.assertLess(mkdir_at, write_at, "the directory must be created before the file is written")

    def test_sudoers_write_is_checked_not_assumed(self):
        """A failed redirection does not stop the script, so its result has to be inspected."""
        self.assertRegex(
            self.text,
            r"if mkdir -p /usr/local/etc/sudoers\.d",
            "the sudoers write is not guarded; a failure would be silent",
        )

    def test_dhclient_is_bounded(self):
        """No lease must not mean no progress.

        bsdinstall prints nothing while the post-install script runs, so an unbounded wait here is
        indistinguishable from a slow package download — the console's last line stays
        "Running installation step: config" for the whole timeout.
        """
        self.assertIn("dhclient", self.text, "sanity: the network step is expected to be present")
        self.assertRegex(
            self.text,
            r"dhclient \"\$iface\" &",
            "dhclient must be backgrounded so a watchdog can bound it",
        )
        self.assertRegex(
            self.text,
            r"\(\s*sleep \d+;\s*kill",
            "no watchdog kills the backgrounded dhclient",
        )

    def test_no_timeout_command_from_coreutils(self):
        """`timeout(1)` is not in the FreeBSD base system.

        Using it here would fail with "not found" under `set -e` — turning a hang it was meant to
        prevent into an early, unexplained exit.
        """
        self.assertNotRegex(
            self.text,
            r"^\s*timeout \d",
            "timeout(1) ships with coreutils, which is not installed at this point",
        )

    def test_exit_is_announced(self):
        """The console must say whether the script finished, or a hang and a crash look alike."""
        self.assertIn("trap _finish EXIT", self.text)
        self.assertIn("post-install finished", self.text)
        self.assertIn("post-install FAILED", self.text)


if __name__ == "__main__":
    unittest.main()
