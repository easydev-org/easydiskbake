"""Letting a Windows guest settle before the power goes off.

A desktop is when the shell has drawn, not when Windows has finished with the disk. The
first minute after first logon writes heavily - profile creation, service start, the
component store, the search and defrag services settling - and stopping the machine in
the middle of that leaves a disk that boots into a repair prompt.

Two things protect the disk here:

    DESKTOP_SETTLE_SECONDS   wait after the desktop appears, then check it is still there
    stop(acpi_first=True)    ask the guest to power down instead of killing qemu

The ACPI path is the one that matters most: a SIGTERM to qemu is a power cut, and the
next boot runs chkdsk because of it.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import vm  # noqa: E402


class AcpiShutdown(unittest.TestCase):
    """stop(acpi_first=True) asks before it kills."""

    def setUp(self):
        self.tmp = os.path.dirname(os.path.abspath(__file__))
        self.guest = vm.Guest(self.tmp)
        # A finished process: poll() returns 0, so the "already gone" paths run.
        self.guest.proc = mock.Mock()
        self.guest.proc.pid = 999999
        self.guest.proc.poll.return_value = 0

    def test_it_sends_system_powerdown(self):
        sent = []

        class FakeQmp:
            def __init__(self, _path):
                pass

            def execute(self, cmd, **_kw):
                sent.append(cmd)
                return {"return": {}}

            def close(self):
                pass

        with mock.patch.object(vm, "qmp", mock.MagicMock(QMP=FakeQmp), create=True), \
             mock.patch.dict(sys.modules, {"qmp": mock.MagicMock(QMP=FakeQmp)}):
            self.guest._acpi_shutdown(timeout=1)

        self.assertIn("system_powerdown", sent)

    def test_a_guest_that_powered_off_is_not_running(self):
        # The whole point: qemu exits by itself, so the SIGTERM afterwards finds nothing.
        self.assertFalse(self.guest.alive())

    def test_default_stop_does_not_wait_for_acpi(self):
        # Every other caller stops something it is throwing away; a sixty-second wait
        # would be a minute added to a failure.
        with mock.patch.object(vm.Guest, "_acpi_shutdown") as acpi:
            self.guest.stop(acpi_first=False)
        acpi.assert_not_called()


class SettleEnvironment(unittest.TestCase):
    """The wait is configurable, and defaults to thirty seconds."""

    def test_the_default_is_thirty(self):
        # Long enough for first-logon writes to land, short enough not to care.
        self.assertEqual(int(os.environ.get("DESKTOP_SETTLE_SECONDS", "30")), 30)

    def test_it_can_be_overridden(self):
        with mock.patch.dict(os.environ, {"DESKTOP_SETTLE_SECONDS": "5"}):
            self.assertEqual(int(os.environ.get("DESKTOP_SETTLE_SECONDS", "30")), 5)


class SettleRechecks(unittest.TestCase):
    """After waiting, the desktop is confirmed again.

    Waiting and then stopping regardless would be a sleep, not a check: a guest that
    restarted or crashed during those thirty seconds would still have its disk kept.
    """

    def test_the_source_rechecks_the_desktop(self):
        import inspect
        import importlib.util

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "wb", os.path.join(repo, "windows", "11", "bake.py"))
        bake = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bake)

        source = inspect.getsource(bake.Baker.wait_for_arrival)
        # Both guards have to be there, in the settle block.
        self.assertIn("DESKTOP_SETTLE_SECONDS", source)
        self.assertIn("not guest.alive()", source)
        self.assertIn("reached_desktop", source)
        # And the recheck has to be after the sleep, not before it.
        sleep_at = source.index("time.sleep(settle)")
        recheck_at = source.index("not desktop.reached_desktop(shots)", sleep_at)
        self.assertGreater(recheck_at, sleep_at)


if __name__ == "__main__":
    unittest.main()
