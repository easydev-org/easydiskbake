"""Android's gold disk: what the bake writes and what the validate step proves.

THE SEMANTICS THIS FILE PINS

Android's bake has the same shape as the other four platforms: produce a disk, then
boot that disk and show it works. For Android the disk is an AVD directory, and the
thing that distinguishes a baked disk from its input is the /data partition - which is
userdata-qemu.img, created on the first boot and carried in the archive.

So the bake writes a token into /data and the validate step boots WITHOUT -wipe-data and
reads it back. Both halves are asserted here, because either one alone is worthless:

    a bake that writes nothing   validates a copy of the system image
    a validate that wipes        creates a fresh /data and finds nothing, or worse,
                                 finds nothing and is taught to ignore it

The second failure is the one worth writing tests about. -wipe-data is the DEFAULT of
build_args, because every other caller wants a clean device. A validate that forgot to
turn it off would look like it was doing the right thing.
"""

import importlib.util
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))

import avd  # noqa: E402
import emulator  # noqa: E402

ANDROID15 = os.path.join(HERE, "android", "15")
_spec = importlib.util.spec_from_file_location("android_bake", os.path.join(ANDROID15, "bake.py"))
android_bake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(android_bake)


class WipeData(unittest.TestCase):
    """-wipe-data is what separates a bake boot from a validate boot."""

    def setUp(self):
        patcher = mock.patch("shutil.which", return_value="/opt/android-sdk/emulator/emulator")
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, **kwargs):
        return emulator.build_args("sandbox", "/tmp/work", **kwargs)

    def test_a_bake_boot_wipes(self):
        # The default, and the reason: a bake must start from a known-clean device.
        self.assertIn("-wipe-data", self.args())

    def test_a_validate_boot_does_not_wipe(self):
        # The line android/15/validate.py depends on. Without it the marker check can
        # only ever fail, or be made to pass by not checking.
        self.assertNotIn("-wipe-data", self.args(wipe_data=False))

    def test_everything_else_is_identical(self):
        # The flag is the ONLY difference. If turning it off changed anything else -
        # the port, the QMP socket, the GPU - then a validate would be testing a
        # different machine than the bake produced the disk on.
        bake = self.args(wipe_data=True)
        check = self.args(wipe_data=False)
        self.assertEqual([a for a in bake if a != "-wipe-data"], check)


class GoldMarker(unittest.TestCase):

    def test_the_marker_is_unique_per_run(self):
        # Two bakes must not produce disks that a validate cannot tell apart, and a run
        # id alone repeats across re-runs of the same workflow.
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "41438714442"}):
            first = android_bake.gold_token()
            second = android_bake.gold_token()
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("gold-41438714442-"), first)

    def test_it_works_without_a_ci_environment(self):
        # A local bake has no GITHUB_RUN_ID and must still produce a token rather than
        # an empty string, which mark() would then treat as a successful write.
        with mock.patch.dict(os.environ, {}, clear=True):
            token = android_bake.gold_token()
        self.assertTrue(token.startswith("gold-local-"), token)
        self.assertGreater(len(token), len("gold-local-"))

    def test_the_marker_path_is_on_data(self):
        # /data is userdata-qemu.img. A marker in /sdcard or /tmp would be on a different
        # partition - or not on a partition at all - and would not prove anything about
        # the file the bake publishes.
        self.assertTrue(android_bake.GOLD_MARK.startswith("/data/"),
                        android_bake.GOLD_MARK)


class SystemImageContents(unittest.TestCase):
    """What Google's zip actually contains, and what it does not."""

    def test_userdata_img_is_not_required(self):
        """The failure that stopped the first publish-android run.

        Google ships system.img and vendor.img and a data/ template; the emulator
        creates userdata-qemu.img from the template on first boot. The first version
        required userdata.img from the unzipped directory, so a zip that had downloaded
        and sha1-verified correctly was reported as "did not contain a system image".
        """
        import tempfile
        tmp = tempfile.mkdtemp(prefix="avd-required-")
        try:
            target = os.path.join(tmp, "system-images", "android-35", "default", "x86_64")
            os.makedirs(target)
            for name in ("system.img", "vendor.img", "kernel-ranchu", "ramdisk.img"):
                open(os.path.join(target, name), "wb").write(b"x")
            os.makedirs(os.path.join(target, "data"))
            open(os.path.join(target, "data", "empty_data_disk"), "wb").write(b"x")

            with mock.patch.object(avd, "sdk_root", return_value=tmp):
                got = avd.install_system_image("unused.zip", api=35)
            self.assertEqual(os.path.realpath(got), os.path.realpath(target))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_directory_without_system_img_is_not_treated_as_installed(self):
        """The early return used to test "is not empty", which is a different question.

        A directory left behind by an interrupted unpack was therefore accepted as a
        finished system image, and the failure surfaced much later - in avdmanager, or
        in the emulator - with nothing pointing here. The presence of system.img is what
        decides it, so a directory holding only vendor.img must fall through to the
        unpack rather than returning.
        """
        import tempfile
        tmp = tempfile.mkdtemp(prefix="avd-partial-")
        try:
            target = os.path.join(tmp, "system-images", "android-35", "default", "x86_64")
            os.makedirs(target)
            open(os.path.join(target, "vendor.img"), "wb").write(b"x")

            with mock.patch.object(avd, "sdk_root", return_value=tmp):
                # It must not return the directory as-is; it tries to unpack and fails
                # on the missing zip, which is a different error from "already there".
                with self.assertRaises(avd.AvdError) as ctx:
                    avd.install_system_image("/nonexistent/sysimg.zip", api=35)
            self.assertIn("unzip failed", str(ctx.exception),
                          "a partial directory must not short-circuit the install")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class ArtifactChecks(unittest.TestCase):
    """The files the baked AVD is checked for before it is published."""

    def test_the_check_names_userdata_qemu_not_userdata(self):
        """userdata.img is an INPUT that Android does not ship.

        The emulator's output is userdata-qemu.img, and that is the file a failed first
        boot leaves zero-length. Checking the wrong name would either fail every good
        bake or pass every bad one.
        """
        source = open(os.path.join(ANDROID15, "bake.py"), encoding="utf-8").read()
        checks = source[source.index("required = {"):source.index("}", source.index("required = {"))]
        self.assertIn("userdata-qemu.img", checks)
        # No bare "userdata.img" entry: the substring test above would match it, so this
        # looks for the quoted exact name.
        self.assertNotIn('"userdata.img"', checks)


class BuilderImage(unittest.TestCase):
    """avdmanager refuses to create an AVD without the emulator package."""

    def test_the_builder_installs_the_emulator_package(self):
        """The second failure of publish-android, recorded.

            [avd] creating AVD sandbox from system-images;android-35;default;x86_64
            [android] FAILED: avdmanager failed: Error: "emulator" package must be
                                  installed!

        avdmanager writes an AVD that the emulator opens, so it validates for the
        package first. The builder image had left it out on the reasoning that building
        an AVD boots nothing - which is true and not what avdmanager checks.
        """
        path = os.path.join(HERE, "android", "15", "Containerfile.builder")
        source = open(path, encoding="utf-8").read()
        self.assertIn('sdkmanager" "emulator"', source)

    def test_the_builder_still_has_no_kvm_dependency(self):
        # The reason the emulator package was left out in the first place is still
        # true: nothing in this image boots a guest.
        source = open(os.path.join(HERE, "android", "15",
                                   "Containerfile.builder"), encoding="utf-8").read()
        self.assertIn("No qemu, no KVM", source)


class ValidateWithoutWipe(unittest.TestCase):
    """validate.py must construct its Emulator with wipe_data=False."""

    def test_validate_passes_wipe_data_false(self):
        source = open(os.path.join(ANDROID15, "validate.py"), encoding="utf-8").read()
        self.assertIn("wipe_data=False", source)

    def test_validate_explains_what_a_failure_means(self):
        # A check that does not say why it failed gets disabled the first time it is
        # inconvenient.
        source = open(os.path.join(ANDROID15, "validate.py"), encoding="utf-8").read()
        self.assertIn("THE GOLD MARKER IS NOT THERE", source)
        self.assertIn("copy of the input", source)


if __name__ == "__main__":
    unittest.main()
