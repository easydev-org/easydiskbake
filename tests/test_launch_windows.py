"""A published Windows disk has to reach a desktop before anything types into it.

THE FAILURE THIS EXISTS FOR, MEASURED ON RUN 41483694400

The ssh stage started the gold disk, waited 900 seconds, and reported

    the guest did not reach a desktop

Seventy-five frames had been captured. Twenty-one of them had a picture in them, and the
brightest read

    BdsDxe: failed to load Boot000A "Windows Boot Manager" from HD(1,GPT,...)
      /EFI/Microsoft/Boot/bootmgfw.efi: Not Found
    >>Start PXE over IPv4.
    PXE-E16: No valid offer received.
    >>Start PXE over IPv6.
    PXE-E16: No valid offer received.
    UEFI Interactive Shell v2.2

The disk had no Windows boot loader on it. The firmware shell was on screen for
fourteen of the fifteen minutes and the report said none of it.

A launch check runs BEFORE the ssh stage: if the disk cannot reach a desktop, there is no
point typing a single command into it.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))

WORKFLOW = os.path.join(HERE, ".github", "workflows", "launch-windows.yml")
SCRIPT = os.path.join(HERE, "lib", "launch_windows.py")


class TheWorkflow(unittest.TestCase):

    def source(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            return fh.read()

    def test_it_is_manual_only(self):
        body = self.source()
        self.assertIn("workflow_dispatch:", body)
        head = body.split("concurrency:")[0]
        for trigger in ("push:", "pull_request:", "schedule:"):
            self.assertNotIn(trigger, head, trigger)

    def test_it_asks_for_kvm(self):
        body = self.source()
        self.assertIn("--device /dev/kvm", body)
        self.assertIn("python3 lib/ci.py kvm", body)

    def test_it_pulls_the_bootstate_from_a_run_id(self):
        """A disk and its firmware state come from the same run and travel together.

        ghcr holds the qcow2; the artifact store holds the few hundred KiB that make it
        start. Pulling one without the other produces a guest in the firmware shell.
        """
        body = self.source()
        self.assertIn("windows11-bootstate", body)
        self.assertIn("run-id: ${{ inputs.bootstate_run }}", body)

    def test_it_uploads_the_screenshot_whatever_happens(self):
        body = self.source()
        self.assertIn("windows-launch-screenshots", body)
        self.assertIn("out/screen.png", body)
        start = body.index("keep the screenshots")
        self.assertIn("if: always()", body[start - 200:start + 200])

    def test_it_runs_before_ssh_in_the_documented_order(self):
        """The docstring has to say why this precedes the ssh stage.

        A reader who finds launch-windows and bake-windows-ssh side by side needs the
        ordering, or the next person merges them and loses the separation.
        """
        body = self.source()
        self.assertIn("runs BEFORE the", body)
        self.assertIn("ssh stage: if the disk does not reach a desktop", body)


class TheScript(unittest.TestCase):

    def source(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            return fh.read()

    def test_it_checks_for_a_boot_loader_before_booting(self):
        """THE WHOLE POINT. Nine minutes of waiting is not a diagnosis.

        check_disk(disk, root_fs="windows") reads the EFI system partition and looks for
        bootmgfw.efi. If it is missing, the firmware will fall through to PXE and its own
        shell, and no amount of waiting changes that.
        """
        body = self.source()
        self.assertIn('smoke.check_disk(disk, root_fs="windows")', body)
        # Before the machine starts, not after it fails.
        self.assertLess(body.index('root_fs="windows"'),
                        body.index("vm.Guest("))

    def test_it_uses_the_ssh_stages_machine(self):
        """The eleven qemu parameters took eight runs to get right.

        A second copy of them here would be a second thing to keep in step, and the
        launch would be measuring a different machine than the stage it precedes.
        """
        body = self.source()
        self.assertIn("import ssh as sshstage", body)
        self.assertIn("sshstage.start_swtpm", body)
        self.assertIn("sshstage.take_shot", body)
        # and the machine itself is built here, because a launch has no keyboard stage
        self.assertIn("vm.Shape(", body)

    def test_it_samples_through_the_boot_rather_than_at_the_end(self):
        """One picture at the timeout is a black screen.

        A sequence of them shows the firmware printing, then its shell, then silence -
        which is the difference between "never started" and "started and stopped".
        """
        body = self.source()
        self.assertIn("sample_until", body)
        self.assertIn("SAMPLE_KEEP_SECONDS", body)

    def test_it_reports_what_the_last_frame_says(self):
        """The guest states its own failure, in words, if anything reads them.

        Not a decision input: nothing here branches on OCR. It is the report, after the
        fact, because "bootmgfw.efi: Not Found" is better than "no desktop".
        """
        body = self.source()
        self.assertIn("read_screen", body)
        self.assertIn("tesseract", body)

    def test_it_does_not_install_or_modify_anything(self):
        """A launch that changed the disk would make the next launch a test of something
        else."""
        body = self.source()
        for forbidden in ("open_powershell", "configure_sshd", "install("):
            self.assertNotIn(forbidden, body, forbidden)

    def test_it_writes_a_report_on_every_path(self):
        body = self.source()
        self.assertIn("launch.txt", body)
        self.assertGreaterEqual(body.count("return finish("), 4)

    def test_it_takes_a_local_disk_too(self):
        """--disk exists so this can be pointed at a bake's output without publishing."""
        body = self.source()
        self.assertIn("--disk", body)


if __name__ == "__main__":
    unittest.main()
