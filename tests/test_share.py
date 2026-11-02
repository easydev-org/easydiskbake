"""One share mechanism for every guest that can mount one: SMB.

virtiofs covers Linux and, with WinFsp plus a service, Windows - and not macOS or
Android. 9p covers Linux and BSD. SMB is the only thing Windows, Linux, macOS and BSD
all mount natively, and it needs no kernel module in any of them.

Android is the exception, and the module says so rather than pretending otherwise.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import share  # noqa: E402


class Address(unittest.TestCase):
    """10.0.2.4 is qemu's own constant, not a choice."""

    def test_the_smb_server_is_the_fourth_address(self):
        # qemu documentation: "By default the 4th IP in the guest network is used,
        # i.e. x.x.x.4". The guest is .15 and the host is .2.
        self.assertEqual(share.SMB_SERVER, "10.0.2.4")

    def test_the_share_is_called_qemu(self):
        # qemu names it; a caller cannot choose it with `smb=`.
        self.assertEqual(share.SMB_SHARE, "qemu")


class Validation(unittest.TestCase):

    def test_a_missing_directory_is_an_error(self):
        # qemu's smb= needs an existing directory and does not create one; failing
        # here says which path rather than leaving qemu to refuse the netdev.
        with self.assertRaises(share.ShareError) as ctx:
            share.Share("/nonexistent/share")
        self.assertIn("/nonexistent/share", str(ctx.exception))

    def test_a_file_is_not_a_directory(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "file")
            open(path, "w").close()
            with self.assertRaises(share.ShareError):
                share.Share(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_directory_is_accepted_and_absolutised(self):
        tmp = tempfile.mkdtemp()
        try:
            s = share.Share(tmp)
            self.assertEqual(s.path, os.path.abspath(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_readonly_false_on_a_readonly_directory_is_an_error(self):
        tmp = tempfile.mkdtemp()
        try:
            with mock.patch("os.access", side_effect=lambda p, m: m == os.R_OK):
                with self.assertRaises(share.ShareError) as ctx:
                    share.Share(tmp, readonly=False)
            self.assertIn("writable", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NetdevArgs(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patch = mock.patch.object(share, "_smbd_available", return_value=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_smb_is_added_to_the_existing_netdev(self):
        args = share.Share(self.tmp).netdev_args()
        netdev = args[args.index("-netdev") + 1]
        self.assertIn(f"smb={os.path.abspath(self.tmp)}", netdev)

    def test_it_is_the_same_netdev_not_a_second_one(self):
        # Two -netdev user entries would give the guest two NICs and one of them with
        # no routes. One netdev carries the forwards and the share together.
        args = share.Share(self.tmp).netdev_args()
        self.assertEqual(args.count("-netdev"), 1)
        self.assertEqual(args.count("-device"), 1)

    def test_the_port_forwards_survive(self):
        # A sandbox that had RDP before must still have it. Adding a share must not
        # be the reason a port stops working.
        args = share.Share(self.tmp).netdev_args(ssh_port=2222, rdp_port=3389)
        netdev = args[args.index("-netdev") + 1]
        self.assertIn("hostfwd=tcp::2222-:22", netdev)
        self.assertIn("hostfwd=tcp::3389-:3389", netdev)

    def test_forwards_can_be_omitted(self):
        args = share.Share(self.tmp).netdev_args(host_ports=False)
        netdev = args[args.index("-netdev") + 1]
        self.assertNotIn("hostfwd", netdev)
        self.assertIn("smb=", netdev)

    def test_no_host_port_is_opened_for_smb(self):
        # The server lives inside the user-mode network; nothing on the host listens.
        args = share.Share(self.tmp).netdev_args()
        netdev = args[args.index("-netdev") + 1]
        self.assertNotIn("445", netdev)
        self.assertNotIn("139", netdev)

    def test_missing_smbd_is_a_clear_error(self):
        # qemu checks CONFIG_SMBD_COMMAND at startup and refuses the netdev. Saying so
        # here names the missing package instead of leaving a qemu error to decode.
        with mock.patch.object(share, "_smbd_available", return_value=False):
            with self.assertRaises(share.ShareError) as ctx:
                share.Share(self.tmp).netdev_args()
        self.assertIn("smbd", str(ctx.exception))
        self.assertIn("samba", str(ctx.exception))


class GuestPaths(unittest.TestCase):
    """What each guest runs, so the bake log can say it and tests can assert it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.share = share.Share(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_windows_uses_the_unc_path(self):
        hint = self.share.guest_path_hint("windows")
        self.assertIn(r"\\10.0.2.4\qemu", hint)
        self.assertIn("net use", hint)

    def test_linux_uses_cifs(self):
        hint = self.share.guest_path_hint("linux")
        self.assertIn("mount -t cifs //10.0.2.4/qemu", hint)

    def test_macos_and_bsd_use_smbfs(self):
        for platform in ("macos", "bsd"):
            with self.subTest(platform=platform):
                self.assertIn("mount_smbfs", self.share.guest_path_hint(platform))

    def test_android_is_documented_as_unsupported(self):
        # The emulator has no virtiofs, no 9p, and Android has no SMB client. Files
        # reach it with adb push, which is a copy rather than a mount.
        hint = self.share.guest_path_hint("android")
        self.assertIn("not supported", hint)
        self.assertIn("adb", hint)


class SmbdDetection(unittest.TestCase):

    def test_it_finds_the_usual_path(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch("os.path.exists",
                        side_effect=lambda p: p == "/usr/sbin/smbd"):
            self.assertTrue(share._smbd_available())

    def test_it_finds_it_on_path(self):
        with mock.patch("shutil.which",
                        side_effect=lambda n: "/usr/bin/smbd" if n == "smbd" else None):
            self.assertTrue(share._smbd_available())

    def test_absent_is_absent(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch("os.path.exists", return_value=False):
            self.assertFalse(share._smbd_available())


if __name__ == "__main__":
    unittest.main()
