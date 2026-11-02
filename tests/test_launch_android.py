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

    def test_it_does_not_build_or_patch_the_avd(self):
        """A verify that quietly fixes what it is checking is not checking anything.

        The published AVD must be used as it arrives: no avdmanager, no config edits, no
        app installs. Those are the bake's job, and doing them here would make "it boots"
        a claim about a disk this script built rather than about the artifact.
        """
        body = self.source()
        for forbidden in ("create_avd(", "config.put(", "adb('install'"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_the_system_image_is_installed_because_it_is_an_input(self):
        """The system image is not IN the archive, and the emulator refuses to start
        without it.

        MEASURED ON RUN 41493183039:

            WARNING | /opt/android-sdk/system-images/android-35/default/x86_64/ is not
                      a valid directory.
            FATAL   | Broken AVD system path. Check your ANDROID_SDK_ROOT value

        system.img is 1.7 GB, read-only, and shared by every AVD of that Android version,
        so pack() leaves it out - correctly. publish-android installs it from the pin in
        lib/images.py before creating the AVD; a launch that only unpacks does not.

        Installing it is not "fixing what is being checked": it is providing an input the
        artifact names in its own config.ini. The version comes from that file and is
        matched against lib/images.py, so the AVD and its system image are the pair that
        was published together - an unrecognised version raises rather than substituting.
        """
        body = self.source()
        self.assertIn("ensure_system_image(", body)
        self.assertIn("image.sysdir.1", body)
        self.assertIn("lib/images.py", body)
        # And it must refuse a version it has no pin for.
        self.assertIn("Refusing to substitute", body)

    def test_the_config_is_found_under_the_avd_suffix(self):
        """list_avds() returns the NAME; the directory is name + ".avd".

        MEASURED ON RUN 41494168764: this looked for

            /opt/android-avd/sandbox/config.ini

        which does not exist. The OSError branch reported it and returned, so the system
        image was never installed and the emulator failed with the same FATAL it had
        before the fix was written. The launch looked like the fix had not worked.

        Reading the config is not optional: without it there is no way to know which
        system image the artifact needs.
        """
        body = self.source()
        self.assertIn('f"{name}.avd"', body)
        self.assertNotIn('os.path.join(home, name, "config.ini")', body)


if __name__ == "__main__":
    unittest.main()
