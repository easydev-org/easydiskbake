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

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))

import bootstate  # noqa: E402

# save() stops swtpm before packing, and that polling waits for the process to go away.
# There is no swtpm in a unit test; the stop is what is being worked around, not tested.
_orig_stop = bootstate.stop_swtpm
bootstate.stop_swtpm = lambda work: None


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


class TheArchiveFollowsTheDisk(unittest.TestCase):
    """A stage that renames its working copy of the disk renames the archive too.

    THE FAILURE THIS EXISTS FOR, MEASURED ON RUN 41477859630

    bake-windows published the disk and its state together:

        out/windows11.qcow2.bootstate.tar.gz

    The ssh stage then copied the disk to a working name and looked for the archive
    there:

        copying out/windows11-gold.qcow2 -> .../ssh-work/disk.qcow2
        no bootstate archive was found

    The archive was in the same directory the whole time, under the published disk's
    name. One rename away from being found, and not found.

    THE FIX IS A RENAME, NOT A SEARCH. Looking for "any *.bootstate.tar.gz nearby"
    would have rescued that run and picked the wrong archive in the first run that has
    two disks in one directory. adopt() takes the archive and the disk explicitly.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bootstate-adopt-")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def test_adopt_moves_the_archive_to_the_disk_name(self):
        gold = os.path.join(self.tmp, "windows11.qcow2")
        open(gold, "wb").write(b"disk")
        with open(os.path.join(self.work, "windows.vars"), "wb") as fh:
            fh.write(b"vars" * 50)
        published = bootstate.save(self.work, gold)

        # The stage's working copy, under a different name.
        disk = os.path.join(self.work, "disk.qcow2")
        open(disk, "wb").write(b"disk")

        placed = bootstate.adopt(disk, published)
        self.assertEqual(placed, disk + ".bootstate.tar.gz")
        self.assertTrue(os.path.isfile(placed))
        self.assertFalse(os.path.exists(published))

    def test_restore_finds_it_after_the_adopt(self):
        gold = os.path.join(self.tmp, "windows11.qcow2")
        open(gold, "wb").write(b"disk")
        with open(os.path.join(self.work, "windows.vars"), "wb") as fh:
            fh.write(b"vars" * 50)
        published = bootstate.save(self.work, gold)

        disk = os.path.join(self.work, "disk.qcow2")
        open(disk, "wb").write(b"disk")

        # Before the rename there is nothing at the working disk's name, and restore
        # says so rather than searching.
        self.assertFalse(bootstate.restore(self.work, disk))
        bootstate.adopt(disk, published)
        self.assertTrue(bootstate.restore(self.work, disk))

    def test_no_searching_beside_the_disk(self):
        """An archive with a different name is NOT used.

        The lookup is by exact path. A neighbouring archive belongs to a neighbouring
        disk, and restoring firmware state from the wrong bake produces a machine that
        boots - into somebody else's Windows.
        """
        disk = os.path.join(self.work, "disk.qcow2")
        open(disk, "wb").write(b"disk")
        other = os.path.join(self.work, "some-other.qcow2.bootstate.tar.gz")
        with tarfile.open(other, "w:gz") as tar:
            stray = os.path.join(self.tmp, "windows.vars")
            open(stray, "wb").write(b"nope")
            tar.add(stray, arcname="windows.vars")

        self.assertFalse(bootstate.restore(self.work, disk),
                         "restore() must not pick up a neighbouring archive")

    def test_adopting_the_same_path_is_a_no_op(self):
        disk = os.path.join(self.work, "disk.qcow2")
        open(disk, "wb").write(b"disk")
        archive = bootstate.archive_path(disk)
        open(archive, "wb").close()
        self.assertEqual(bootstate.adopt(disk, archive), archive)
        self.assertTrue(os.path.isfile(archive))


class EveryStagePublishesWhatItWrote(unittest.TestCase):
    """A stage that can be another stage's input has to save the bootstate it ended with.

    THE FAILURE THIS EXISTS FOR

    bake-windows-dev starts from the ssh stage's disk, and the ssh workflow uploaded

        name: windows-ssh-bootstate
        path: out/windows11-ssh.qcow2.bootstate.tar.gz
        if-no-files-found: warn

    ssh.py never wrote that file. `if-no-files-found: warn` meant the upload uploaded
    nothing and reported success, so the dev stage would have failed exactly as the ssh
    stage had - "no bootstate archive" - with an empty artifact to blame.

    The save has to be in ssh.py, not only in bake.py, because this stage BOOTS the
    disk: the firmware writes to OVMF's variables during that boot, so what goes out is
    what this stage ended with, not what came in.
    """

    def source(self, name):
        with open(os.path.join(HERE, "windows", "11", name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_ssh_stage_saves_its_bootstate(self):
        source = self.source("ssh.py")
        self.assertIn("bootstate.save", source)
        # After the disk is written: save() names the archive after the disk.
        self.assertLess(source.index("BAKED {args.out}"),
                        source.index("bootstate.save"))

    def test_a_stage_that_names_a_bootstate_must_receive_it(self):
        """--bootstate pointing at nothing is an error, not a skipped step.

        Run 41480134822 passed `out/windows11-gold.qcow2.bootstate.tar.gz` while the
        artifact downloads as `windows11.qcow2.bootstate.tar.gz`. The old code skipped
        the missing file and then reported "no archive at <working disk>", which named
        a directory that was never the problem.
        """
        for name in ("ssh.py", "devtools.py"):
            source = self.source(name)
            self.assertIn("does not exist", source, name)

    def test_the_ssh_workflow_passes_the_name_the_artifact_has(self):
        path = os.path.join(HERE, ".github", "workflows", "bake-windows-ssh.yml")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("--bootstate out/windows11.qcow2.bootstate.tar.gz", source)
        # The tag's name is not the archive's name, and passing it was the bug.
        self.assertNotIn('--bootstate "out/windows11-gold.qcow2', source)
        self.assertNotIn("--bootstate out/windows11-gold.qcow2", source)


class TheTpmLockIsNotState(unittest.TestCase):
    """swtpm's lock file must not travel with the state it locks.

    MEASURED ON LAUNCH-WINDOWS RUN 41501928264

        qemu-system-x86_64: tpm-emulator: TPM result for CMD_INIT: 0x9 operation failed

    and qemu exited twelve seconds after it started, so the launch reported only
    "qemu exited during the boot" with a black screen and no frames.

    swtpm writes tpm/.lock to claim the directory for the running process, and save()
    packs the whole tpm/ directory - including that file. A restored lock tells the next
    swtpm that another process owns the state, and it refuses to initialise.

    A lock is about the process that held it, not about the state; it means nothing once
    that process is gone.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bootstate-lock-")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(os.path.join(self.work, "tpm"))
        with open(os.path.join(self.work, "OVMF_VARS.fd"), "wb") as fh:
            fh.write(b"vars" * 40)
        with open(os.path.join(self.work, "tpm", "tpm2-00.permall"), "wb") as fh:
            fh.write(b"state" * 40)
        self.disk = os.path.join(self.tmp, "windows11.qcow2")
        open(self.disk, "wb").write(b"disk")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def test_a_lock_is_not_packed(self):
        open(os.path.join(self.work, "tpm", ".lock"), "wb").close()
        archive = bootstate.save(self.work, self.disk)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        self.assertNotIn("tpm/.lock", names)
        # The state itself is still there.
        self.assertIn("tpm/tpm2-00.permall", names)

    def test_a_lock_already_in_an_archive_is_removed_on_restore(self):
        """Archives published before this change still carry one."""
        archive = bootstate.save(self.work, self.disk)
        # Write a lock into the archive by hand, the way an old one would have it.
        with tarfile.open(archive, "r:gz") as tar:
            members = [(m, tar.extractfile(m).read() if m.isfile() else None)
                       for m in tar.getmembers()]
        with tarfile.open(archive, "w:gz") as tar:
            for member, data in members:
                tar.addfile(member, io.BytesIO(data) if data is not None else None)
            info = tarfile.TarInfo("tpm/.lock")
            info.size = 0
            tar.addfile(info)

        second = os.path.join(self.tmp, "second")
        os.makedirs(second)
        disk = os.path.join(second, "disk.qcow2")
        open(disk, "wb").write(b"disk")
        subprocess.run(["cp", archive, bootstate.archive_path(disk)], check=True)

        self.assertTrue(bootstate.restore(second, disk))
        self.assertFalse(os.path.exists(os.path.join(second, "tpm", ".lock")),
                         "a restored lock makes swtpm refuse to start")
        self.assertTrue(os.path.isfile(os.path.join(second, "tpm", "tpm2-00.permall")))
