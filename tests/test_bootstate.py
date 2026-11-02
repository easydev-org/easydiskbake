"""The firmware and TPM state a baked disk needs in order to start.

THE FAILURE THIS EXISTS FOR, MEASURED ON RUN 41468968378

    BdsDxe: failed to load Boot0001 "UEFI Misc Device"  ... : Not Found
    BdsDxe: failed to load Boot0002 "UEFI Misc Device 2" ... : Not Found
    BdsDxe: failed to load Boot0003 "UEFI HTTPv4 (MAC:...)" : Not Found
    BdsDxe: failed to load Boot0004 "UEFI HTTPv6 (MAC:...)" : Not Found
    BdsDxe: loading Boot0005 "EFI Internal Shell"
    Shell>

The disk held a complete Windows 11. The firmware had no boot entry for it, because the
entry that matters was written into OVMF's variables during setup and bake-windows
published only the qcow2.

www.easysandbox-windows says the same in the comment above its own check: "windows.vars
carries the UEFI boot entry (Boot0005 \"Windows Boot Manager\"), and without it the
firmware falls back to the generic hard-disk entry Boot0002, fails to boot".
"""

import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))

import bootstate  # noqa: E402


class Save(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bootstate-")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.disk = os.path.join(self.tmp, "windows11.qcow2")
        open(self.disk, "wb").write(b"disk")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def test_the_vars_file_is_collected(self):
        # bake.py names it OVMF_VARS.fd; the stages name it windows.vars. All are looked
        # for rather than assumed.
        with open(os.path.join(self.work, "OVMF_VARS.fd"), "wb") as fh:
            fh.write(b"vars" * 100)
        archive = bootstate.save(self.work, self.disk)
        self.assertIsNotNone(archive)
        with tarfile.open(archive) as tar:
            self.assertIn("OVMF_VARS.fd", tar.getnames())

    def test_the_tpm_directory_is_collected(self):
        tpm = os.path.join(self.work, "tpm")
        os.makedirs(tpm)
        open(os.path.join(tpm, "tpm2-00.permall"), "wb").write(b"state")
        with open(os.path.join(self.work, "windows.vars"), "wb") as fh:
            fh.write(b"vars")
        archive = bootstate.save(self.work, self.disk)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        self.assertTrue(any(n == "tpm" or n.startswith("tpm/") for n in names), names)

    def test_nothing_to_save_is_not_an_error(self):
        """A bake that failed before starting a VM has no variables to keep.

        Turning that into a second failure would hide the first.
        """
        self.assertIsNone(bootstate.save(self.work, self.disk))

    def test_the_archive_is_named_after_the_disk(self):
        with open(os.path.join(self.work, "windows.vars"), "wb") as fh:
            fh.write(b"vars")
        archive = bootstate.save(self.work, self.disk)
        self.assertEqual(os.path.basename(archive), "windows11.qcow2.bootstate.tar.gz")

    def test_an_empty_vars_file_is_skipped(self):
        # A zero-length vars file is what a failed copy leaves; it restores into a
        # firmware with no variables, which is the state this whole module exists to
        # avoid.
        open(os.path.join(self.work, "OVMF_VARS.fd"), "wb").close()
        self.assertIsNone(bootstate.save(self.work, self.disk))


class Restore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bootstate-restore-")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.disk = os.path.join(self.tmp, "windows11.qcow2")
        open(self.disk, "wb").write(b"disk")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def test_a_saved_archive_restores(self):
        with open(os.path.join(self.work, "windows.vars"), "wb") as fh:
            fh.write(b"vars" * 100)
        archive = bootstate.save(self.work, self.disk)

        # A fresh work directory, as a later stage has.
        second = os.path.join(self.tmp, "second")
        os.makedirs(second)
        second_disk = os.path.join(second, "windows11.qcow2")
        open(second_disk, "wb").write(b"disk")
        subprocess.run(["cp", archive,
                        bootstate.archive_path(second_disk)], check=True)

        self.assertTrue(bootstate.restore(second, second_disk))
        self.assertTrue(os.path.isfile(os.path.join(second, "windows.vars")))

    def test_no_archive_reports_false_rather_than_raising(self):
        # The caller decides what a missing bootstate means; for the ssh stage it is
        # fatal, and the message there says why.
        self.assertFalse(bootstate.restore(self.work, self.disk))

    def test_describe_says_so_when_it_is_missing(self):
        line = bootstate.describe(self.disk)
        self.assertIn("NO BOOTSTATE", line)
        self.assertIn("will not boot", line)


class TheStagesKnow(unittest.TestCase):
    """A stage that starts from a published disk has to restore the bootstate first."""

    def stages(self):
        for name, attr in (("ssh.py", "gold"), ("devtools.py", "ssh")):
            path = os.path.join(HERE, "windows", "11", name)
            with open(path, encoding="utf-8") as fh:
                yield name, attr, fh.read()

    def test_each_stage_restores_before_starting_the_vm(self):
        for name, _attr, source in self.stages():
            self.assertIn("bootstate.restore", source, name)
            # Before the swtpm and the guest: variables restored after the firmware read
            # them are variables the firmware never saw.
            self.assertLess(source.index("bootstate.restore"),
                            source.index("start_swtpm(work)"), name)

    def test_a_missing_bootstate_is_fatal_with_a_reason(self):
        for name, _attr, source in self.stages():
            self.assertIn("will not boot", source, name)
            self.assertIn("Windows Boot Manager", source, name)

    def test_each_stage_takes_a_bootstate_flag(self):
        for name, _attr, source in self.stages():
            self.assertIn('add_argument("--bootstate"', source, name)


class TheBakeSavesIt(unittest.TestCase):

    def test_bake_saves_bootstate_after_compressing(self):
        with open(os.path.join(HERE, "windows", "11", "bake.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("bootstate.save", source)
        # After the artifact exists: save() is named after the disk, so the disk must.
        self.assertLess(source.index("compress.compress"),
                        source.index("bootstate.save"))
