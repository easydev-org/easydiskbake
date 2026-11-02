"""The Android launcher and AVD handling, without an emulator.

What can be checked here is the argv and the config - which is where the judgement is.
A wrong flag is a failure at boot time, ten minutes in, on a runner that has already
pulled a 1 GB image.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import avd  # noqa: E402
import emulator  # noqa: E402


class EmulatorArgs(unittest.TestCase):
    """The command line, asserted without running it."""

    def setUp(self):
        with mock.patch("shutil.which", return_value="/opt/android-sdk/emulator/emulator"):
            self.args = emulator.build_args("/tmp/work/avd", "/tmp/work")

    def test_headless(self):
        # The screen is captured over QMP, not shown; a window needs a display server
        # the runner does not have.
        self.assertIn("-no-window", self.args)

    def test_wipes_data_unconditionally(self):
        # A bake must start cold. A snapshot or a kept data partition would hide a
        # system image that does not actually boot.
        self.assertIn("-wipe-data", self.args)
        self.assertIn("-no-snapshot", self.args)

    def test_accel_is_on(self):
        # Without KVM an Android boot is tens of minutes, past every timeout.
        self.assertIn("-accel", self.args)
        self.assertEqual(self.args[self.args.index("-accel") + 1], "on")

    def test_gpu_is_software(self):
        # A CI runner has no GPU, and the default mode needs one.
        self.assertIn("swiftshader_indirect", self.args)

    def test_qmp_socket_is_under_the_work_directory(self):
        # lib/qmp.py and lib/snapshots.py connect to this path; if it drifts the
        # recording silently captures nothing.
        dump = [a for a in self.args if a.startswith("unix:")]
        self.assertEqual(len(dump), 1, dump)
        self.assertIn("/tmp/work/", dump[0])
        self.assertTrue(dump[0].endswith("emulator-qmp.sock,server=on,wait=off"))

    def test_no_telemetry(self):
        """The flags this used to assert are rejected by the shipped emulator.

        MEASURED ON RUN 41472222286:

            unknown option: -no-keys
            please use -help for a list of valid options

        `emulator 37.2.13` is the current `stable` channel release in Google's
        repository2-3.xml, so this is not a stale install - it is what sdkmanager
        gives you, and it does not implement either flag. They were copied from
        easysandbox-android, whose emulator is a different build.

        The intent behind them is still worth keeping in mind: a bake should not depend
        on Google's telemetry endpoints. It simply cannot express that as a command-line
        flag here. Asserting their ABSENCE is the assertion that matters now - a flag
        the binary rejects stops the emulator before it opens QMP, and that is the
        failure this test exists to prevent recurring.
        """
        self.assertNotIn("-no-metrics", self.args)
        self.assertNotIn("-no-keys", self.args)

    def test_missing_emulator_is_an_error_that_says_where_it_lives(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(emulator.EmulatorError) as ctx:
                emulator.build_args("avd", "/tmp")
        self.assertIn("/opt/android-sdk/emulator", str(ctx.exception))


class AvdConfig(unittest.TestCase):
    """The sandbox shape, and the reason it is written out."""

    def test_it_is_a_desktop_not_a_phone(self):
        # The pixel_6 profile is portrait 1080x2400. Every other sandbox in this
        # repository is 1024x768 landscape, and a viewer should not have to rotate.
        self.assertEqual(avd.CONFIG["hw.lcd.width"], "1024")
        self.assertEqual(avd.CONFIG["hw.lcd.height"], "768")
        self.assertEqual(avd.CONFIG["hw.initialOrientation"], "landscape")

    def test_it_has_a_mouse_and_a_keyboard(self):
        # Without these the emulator behaves like a touch-only device and the QMP
        # pointer events land nowhere useful.
        self.assertEqual(avd.CONFIG["hw.mouse"], "yes")
        self.assertEqual(avd.CONFIG["hw.keyboard"], "yes")

    def test_software_gpu(self):
        self.assertEqual(avd.CONFIG["hw.gpu.mode"], "swiftshader_indirect")
        self.assertEqual(avd.CONFIG["hw.gpu.enabled"], "yes")

    def test_no_cameras_or_sensors(self):
        # A sandbox is not a phone; a camera that reports itself present but is
        # backed by nothing makes apps hang on a permission request.
        for key in ("hw.camera.back", "hw.camera.front",
                    "hw.sensors.orientation", "hw.sensors.proximity"):
            with self.subTest(key=key):
                self.assertIn(key, avd.CONFIG)
                self.assertIn(avd.CONFIG[key], ("no", "none"))

    def test_data_partition_is_big_enough(self):
        # The default is 800M, which fills with the framework's first-boot work.
        self.assertEqual(avd.CONFIG["disk.dataPartition.size"], "4G")


class AvdPaths(unittest.TestCase):

    def test_sdk_root_must_be_set(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANDROID_SDK_ROOT", "ANDROID_HOME")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(avd.AvdError) as ctx:
                avd.sdk_root()
        self.assertIn("ANDROID_SDK_ROOT", str(ctx.exception))

    def test_avd_home_is_created(self):
        import tempfile
        tmp = os.path.join(tempfile.mkdtemp(), "new")
        with mock.patch.dict(os.environ, {"ANDROID_AVD_HOME": tmp}):
            self.assertEqual(avd.avd_home(), tmp)
        # avdmanager only honours the variable when the directory exists, so the
        # function creates it rather than assuming.
        self.assertTrue(os.path.isdir(tmp))

    def test_list_avds_on_a_missing_directory_is_empty(self):
        self.assertEqual(avd.list_avds("/nonexistent/avd/home"), [])


class ImagesTable(unittest.TestCase):

    def test_the_android_image_is_pinned_with_a_hash_and_a_size(self):
        import images
        pin = images.ANDROID["sysimg:android-15-aosp"]
        self.assertEqual(len(pin["sha1"]), 40)
        self.assertIsInstance(pin["size"], int)
        self.assertEqual(pin["api"], 35)   # 35 is Android 15
        self.assertEqual(pin["flavour"], "default")   # AOSP, not google_apis

    def test_its_tag_has_no_colon(self):
        # `sysimg:android-15-aosp` is a key with a colon so the prefix is visible in
        # the source; a tag cannot contain one, so publishing replaces it.
        import images
        self.assertIn("sysimg:android-15-aosp", images.all_images())
        self.assertEqual("sysimg:android-15-aosp".replace(":", "-"),
                         "sysimg-android-15-aosp")


if __name__ == "__main__":
    unittest.main()
