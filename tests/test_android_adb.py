"""The adb calls the bake makes, and the one that was not a command.

THE FAILURE THIS EXISTS FOR, MEASURED ON RUN 41477751923

    [emulator] waiting 60s: boot_completed=FAILED:adb: unknown command getprop

`adb getprop` is not a host command. The adb client forwards `shell`, `install`, `emu`
and a short list of others to the device; everything else is an error. The property has
to be read on the device, through the shell:

    adb -s emulator-5554 shell getprop sys.boot_completed

WHY IT SURVIVED UNTIL NOW

Every earlier Android run died before the boot wait - first at "unknown option: -qmp",
then at "unknown option: -no-keys" - so this line had never been executed in CI. It was
correct-looking, plausible, and never run. Those are the bugs a test is for.
"""

import os
import subprocess
import sys
import unittest
import unittest.mock as mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))

import emulator  # noqa: E402


class WaitForBoot(unittest.TestCase):
    """wait_for_boot asks the device, through the shell, for the property."""

    def guest(self):
        with mock.patch("shutil.which", return_value="/x/emulator"):
            e = emulator.Emulator(work="/tmp/work", avd="sandbox")
        # A live process object, without starting one.
        e.proc = mock.Mock()
        e.proc.poll.return_value = None
        return e

    def calls(self, outputs):
        """Run wait_for_boot against a scripted adb, and return the argvs it used."""
        seen = []
        scripted = list(outputs)

        def fake_run(argv, **kwargs):
            seen.append(argv)
            out = scripted[min(len(seen) - 1, len(scripted) - 1)]
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        # time.sleep(5) between polls is real waiting in a unit test; the polling is
        # what is under test, not the pacing.
        with mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("time.sleep"):
            self.assertTrue(self.guest().wait_for_boot(seconds=30))
        return seen

    def test_getprop_goes_through_the_shell(self):
        seen = self.calls(["", "1"])
        getprops = [a for a in seen if "getprop" in a]
        self.assertTrue(getprops, seen)
        for argv in getprops:
            self.assertIn("shell", argv,
                          f"adb getprop is not a host command: {argv}")
            self.assertLess(argv.index("shell"), argv.index("getprop"), argv)

    def test_the_serial_selects_the_emulator_the_bake_started(self):
        # Without -s, adb picks whichever device is first, which on a runner with a
        # stale emulator is not this one.
        for argv in self.calls(["", "1"]):
            self.assertIn("emulator-5554", argv, argv)

    def test_a_failed_getprop_is_not_mistaken_for_booted(self):
        # "FAILED:adb: unknown command getprop" must not read as "1".
        seen = self.calls(["FAILED:adb: unknown command getprop"] * 2 + ["1"])
        self.assertTrue(any("FAILED" not in a[-1] for a in seen if "getprop" in a))


class TheMarker(unittest.TestCase):
    """The gold marker is what proves the published /data is the baked one."""

    def test_mark_writes_and_reads_through_the_shell(self):
        argv_seen = []

        def fake_run(argv, **kwargs):
            argv_seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="tok-123\n", stderr="")

        with mock.patch("shutil.which", return_value="/x/emulator"):
            e = emulator.Emulator(work="/tmp/work", avd="sandbox")
        with mock.patch("subprocess.run", side_effect=fake_run):
            self.assertTrue(e.mark("tok-123"))

        argv = argv_seen[0]
        self.assertIn("shell", argv)
        self.assertIn("tok-123", " ".join(argv))

    def test_read_mark_returns_empty_when_adb_fails(self):
        with mock.patch("shutil.which", return_value="/x/emulator"):
            e = emulator.Emulator(work="/tmp/work", avd="sandbox")
        with mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1, "", "boom")):
            self.assertEqual(e.read_mark(), "")


if __name__ == "__main__":
    unittest.main()
