"""A published AVD that boots is not the same as a published AVD that CAN boot.

publish-android boots the AVD once before packing it - it has to, because avdmanager
writes only config.ini and userdata-qemu.img appears on the first start. That boot
proves the build is sane. It says nothing about the artifact: the pack, the tar, the
zstd stream, the ghcr push and the pull each get a chance to break something in
between.

launch-android starts from the published bytes and photographs the result, which is why
it exists as a separate workflow rather than a step in the publisher.
"""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))

WORKFLOW = os.path.join(HERE, ".github", "workflows", "launch-android.yml")
SCRIPT = os.path.join(HERE, "lib", "launch_android.py")


class TheWorkflow(unittest.TestCase):

    def source(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            return fh.read()

    def test_it_is_manual_only(self):
        """Every workflow in this repository is workflow_dispatch and nothing else.

        A workflow that fires on push turns an experiment into a scheduled cost, and
        these jobs are KVM machines for up to three hours.
        """
        body = self.source()
        self.assertIn("workflow_dispatch:", body)
        for trigger in ("push:", "pull_request:", "schedule:"):
            self.assertNotIn(trigger, body.split("concurrency:")[0], trigger)

    def test_it_asks_for_kvm(self):
        """Without /dev/kvm an Android boot is tens of minutes and this job times out."""
        body = self.source()
        self.assertIn("--device /dev/kvm", body)
        self.assertIn("python3 lib/ci.py kvm", body)

    def test_it_uploads_the_screenshots(self):
        """A launch that reports success and shows nothing has demonstrated nothing.

        That is the lesson of run 41480111851, where seventeen macOS clicks were
        reported as successful against a blank display.
        """
        body = self.source()
        self.assertIn("android-launch-screenshots", body)
        self.assertIn("out/screen.png", body)

    def test_the_screenshots_are_kept_even_when_the_launch_fails(self):
        # `if: always()` - a failed launch is exactly when the pictures are wanted.
        body = self.source()
        start = body.index("keep the screenshots")
        self.assertIn("if: always()", body[start - 200:start + 200])

    def test_it_runs_from_the_builder_image(self):
        """The launch needs the emulator package and platform-tools, not cmdline-tools."""
        body = self.source()
        self.assertIn("android-builder", body)

    def test_wipe_data_defaults_to_false(self):
        """Booting with -wipe-data would erase the very state being verified.

        A wiped AVD is indistinguishable from a fresh one, so "it boots" would become a
        claim about Android instead of about this artifact.
        """
        body = self.source()
        self.assertIn("wipe_data:", body)
        start = body.index("wipe_data:")
        section = body[start:start + 260]
        self.assertIn("default: false", section)


class TheScript(unittest.TestCase):

    def source(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            return fh.read()

    def test_it_pulls_the_tag_it_is_given(self):
        body = self.source()
        self.assertIn("iso.fetch_tag(args.tag", body)
        self.assertIn("avd.unpack(", body)

    def test_it_writes_the_missing_avd_ini(self):
        """A tar of the .avd directory alone is not a launchable AVD.

        MEASURED BY HAND WHILE LAUNCHING THE PUBLISHED TAG ON 2026-11-02:

            Unknown AVD name [sandbox], use -list-avds to see valid list
            ANDROID_AVD_HOME is defined but there is no file sandbox.ini in $ANDROID_AVD_HOME

        avdmanager writes two things: <home>/<name>.ini and <home>/<name>.avd/. pack()
        is given the directory, so the .ini is not in the archive. The launcher writes it
        rather than changing pack(), because the caller owns where the AVD lives.
        """
        body = self.source()
        self.assertIn(".ini", body)
        self.assertIn("path.rel", body)

    def test_it_reports_what_the_framework_says(self):
        # getprop, through the shell - `adb getprop` is not a host command, which cost a
        # whole run before it was found.
        body = self.source()
        self.assertIn("'shell', 'getprop'", body)
        self.assertIn("ro.product.model", body)

    def test_it_takes_a_screenshot_inside_the_guest(self):
        body = self.source()
        self.assertIn("screencap", body)
        self.assertIn("screen.png", body)

    def test_the_foreground_activity_is_read(self):
        """The launcher in the foreground is the closest thing Android has to a desktop."""
        body = self.source()
        self.assertIn("topResumedActivity", body)

    def test_a_failed_start_keeps_the_emulator_log(self):
        body = self.source()
        self.assertIn("note_tail", body)
        self.assertIn("emulator.log", body)

    def test_it_writes_a_report_whatever_happens(self):
        body = self.source()
        self.assertIn("launch.txt", body)
        # finish() is called on every exit path, including the failures.
        self.assertGreaterEqual(body.count("return finish("), 4, body.count("return finish("))

    def test_it_does_not_build_or_patch_anything(self):
        """A verify that quietly fixes what it is checking is not checking anything."""
        body = self.source()
        for forbidden in ("create_avd(", "install_system_image("):
            self.assertNotIn(forbidden, body, forbidden)


if __name__ == "__main__":
    unittest.main()
