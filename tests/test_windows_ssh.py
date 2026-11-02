"""Typing into a Windows guest that has no channel except the keyboard.

WHY THE KEY MAP IS THE INTERESTING PART

Before SSH exists, the only way into the guest is QMP key events, so every character in
every command has to have a qcode and the right modifier. The first version of
type_text() handled lowercase, digits and punctuation and not uppercase - which meant
"winget install --id Microsoft.OpenSSH.Preview" could not be typed, because five of its
characters were unmappable. The failure would have arrived as "no qcode for 'M'" with the
Run dialog already open.

These tests are about that boundary: every character of every command this stage types
must map, and the ones that need shift must be sent as a chord rather than a press.
"""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))

WINDOWS11 = os.path.join(HERE, "windows", "11")
_spec = importlib.util.spec_from_file_location(
    "windows_ssh", os.path.join(WINDOWS11, "ssh.py"))
ssh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssh)


class KeyMap(unittest.TestCase):
    """Every character the stage types has to reach the guest."""

    def can_type(self, text):
        """The characters type_text would refuse, by the same rules it uses."""
        bad = set()
        for ch in text:
            if ch in "\r\n\t":
                bad.add(ch)
                continue
            if ch.isupper() and ch.isalpha():
                continue          # shift + lowercase
            if ch in ssh.SHIFTED:
                continue          # shift + its unshifted key
            try:
                ssh.qcode_for(ch)
            except ValueError:
                bad.add(ch)
        return bad

    def test_the_winget_command_is_typeable(self):
        # The whole point: the package id is mixed case and the flags have dashes.
        bad = self.can_type(ssh.WINGET_INSTALL)
        self.assertEqual(bad, set(), f"cannot type {sorted(bad)}")

    def test_every_sshd_statement_is_typeable(self):
        for statement in ssh.configure_sshd():
            bad = self.can_type(statement)
            self.assertEqual(bad, set(),
                             f"cannot type {sorted(bad)} in: {statement[:80]}")

    def test_powershell_is_typeable(self):
        self.assertEqual(self.can_type("powershell"), set())

    def test_an_uppercase_letter_is_refused_by_qcode_for(self):
        """qcode_for() answers for one key. Shift is type_text()'s job.

        A test that only checked "does it map" would pass while the chord was never sent,
        and the guest would receive a lowercase letter where an uppercase one was meant -
        which for a package id is a different package.
        """
        with self.assertRaises(ValueError) as ctx:
            ssh.qcode_for("M")
        self.assertIn("shift", str(ctx.exception))

    def test_every_uppercase_letter_is_covered(self):
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            # type_text handles it via ch.lower(); qcode_for must have that key.
            self.assertEqual(ssh.qcode_for(ch.lower()), ch.lower())

    def test_a_newline_is_refused(self):
        # sendkey cannot produce one. A caller passing one has a command to split, and
        # that is a decision rather than a retry.
        kb = ssh.Keyboard.__new__(ssh.Keyboard)
        with self.assertRaises(ValueError):
            ssh.type_text(kb, "one\ntwo")


class Ports(unittest.TestCase):
    """sshd's port and the forward have to be the same number on the guest side."""

    def test_the_guest_port_matches_the_forward(self):
        """lib/vm.build_args() forwards host 2222 -> guest 22.

        Configuring sshd on anything else points the forward at nothing, and the symptom
        is a connection refused that looks like a firewall.
        """
        self.assertEqual(ssh.GUEST_SSH_PORT, 22)

    def test_the_host_port_is_the_one_vm_forwards(self):
        import vm
        shape = vm.Shape()
        args = vm.build_args("/tmp/sshports", [], shape)
        netdev = [a for a in args if a.startswith("user,id=n0")][0]
        self.assertIn(f"hostfwd=tcp::{shape.ssh_port}-:22", netdev)
        self.assertEqual(ssh.HOST_SSH_PORT, shape.ssh_port)

    def test_the_firewall_rule_names_the_guest_port(self):
        statements = " ".join(ssh.configure_sshd())
        self.assertIn(f"-LocalPort {ssh.GUEST_SSH_PORT}", statements)


class GuestIsTheBakes(unittest.TestCase):
    """This stage boots the disk the bake produced, so it needs the bake's machine."""

    def test_it_uses_the_shared_vm_shape(self):
        with open(os.path.join(WINDOWS11, "ssh.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("vm.Shape(", source)
        self.assertIn("vm.Guest(", source)

    def test_it_does_not_reimplement_the_configuration(self):
        """The guest is reached through lib/qmp.py, like every other driver here.

        A second QMP client would be a second place to fix the atomic key pair, and that
        bug is exactly what made the Windows bake open a Start menu.
        """
        with open(os.path.join(WINDOWS11, "ssh.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("import qmp as qmpmod", source)
        self.assertIn("qmpmod.key_combo", source)

    def test_a_failed_stage_does_not_overwrite_the_gold_disk(self):
        """The gold disk is an input. The work happens on a copy.

        A run that fails half way leaves a disk with a half-installed OpenSSH; re-running
        from the untouched gold disk is the only cheap recovery.

        The copy is a real one rather than a qcow2 backing file, because a published disk
        is zstd-compressed and qcow2 will not open that as -b. See
        PublishedDiskCannotBeABackingFile for the failure that taught this.
        """
        with open(os.path.join(WINDOWS11, "ssh.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("--reflink=auto", source)
        self.assertIn("work", source)


if __name__ == "__main__":
    unittest.main()


class Devtools(unittest.TestCase):
    """The third stage: MSVC, the Windows SDK, the .NET SDK.

    easysandbox-windows has run this four times and its workflow publishes the result
    under the tag `dev`. The choices below are that repository's measurements, and each
    one replaced something that failed.
    """

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "windows_devtools", os.path.join(WINDOWS11, "devtools.py"))
        cls.devtools = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.devtools)

    def package(self, pid):
        for entry in self.devtools.PACKAGES:
            if entry[0] == pid:
                return entry
        return None

    def test_buildtools_is_pinned_and_carries_the_workload(self):
        """NOT the evergreen bootstrapper, and NOT whatever winget resolves by default.

        17.9.7, what a stale index returns, caps supported OS at 10.0.22000; this guest is
        10.0.26200 and every component is marked "not applicable". vs_BuildTools.exe got
        further - it fetched 17.14 - and then produced an empty extraction directory.
        winget carries 17.14.44, whose manifest has no such cap.
        """
        entry = self.package("Microsoft.VisualStudio.2022.BuildTools")
        self.assertIsNotNone(entry)
        _pid, version, override, why = entry
        self.assertEqual(version, "17.14.44")
        self.assertIn("Microsoft.VisualStudio.Workload.VCTools", override)
        self.assertIn("--includeRecommended", override)
        self.assertIn("manifest", why.lower())

    def test_powershell_is_installed_last(self):
        """Its MSI can drop the ssh session it is installed over.

        Anything scheduled after it would run over a connection that no longer exists, and
        the symptom is a later package reported as missing rather than an error here.
        """
        ids = [p[0] for p in self.devtools.PACKAGES]
        self.assertEqual(ids[-1], "Microsoft.PowerShell")

    def test_the_dotnet_sdk_is_installed(self):
        # VCTools does not bring one, and a job that runs `dotnet build` needs it.
        ids = [p[0] for p in self.devtools.PACKAGES]
        self.assertIn("Microsoft.DotNet.SDK.10", ids)

    def test_the_windows_sdk_is_installed(self):
        ids = [p[0] for p in self.devtools.PACKAGES]
        self.assertIn("Microsoft.WindowsSDK.10.0.26100", ids)

    def test_verification_compiles_something(self):
        """An installer's exit code says a program ran, not that a compiler exists.

        The check is a real compile through vcvars64 plus a dotnet version, and both
        scripts are shipped beside this module rather than embedded.
        """
        for name in ("msvc-check.ps1", "clean.ps1"):
            path = os.path.join(WINDOWS11, name)
            self.assertTrue(os.path.isfile(path), name)

        with open(os.path.join(WINDOWS11, "msvc-check.ps1"), encoding="utf-8") as fh:
            source = fh.read()
        # vswhere is asked for the component, not the product: the product can be present
        # with no C++ tools.
        self.assertIn("Microsoft.VisualStudio.Component.VC.Tools.x86.x64", source)
        self.assertIn("COMPILE_OK", source)

    def test_an_override_goes_through_a_batch_file(self):
        """The override string cannot survive ssh -> cmd -> winget; the quotes are eaten.

        Same failure the ssh stage records for its own commands, and the same cure: write
        the line to a file, copy it up, run it by path.
        """
        with open(os.path.join(WINDOWS11, "devtools.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("--override", source)
        self.assertIn(".bat", source)
        self.assertIn("scp_to", source)

    def test_cleaning_is_opt_in_and_verifiable(self):
        """The disk ships whatever this run left on it.

        Silence is not success: clean.ps1 ends with CLEAN_DONE, and without the marker the
        step failed and the disk still carries its build history.
        """
        with open(os.path.join(WINDOWS11, "clean.ps1"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("CLEAN_DONE", source)
        # Zeroing free space is what makes qemu-img convert smaller at all: deleting a file
        # marks its clusters free and leaves the bytes, and convert only skips zeroes.
        self.assertIn("zero.fill", source)
        self.assertIn("Write", source)

    def test_cleaning_keeps_the_vs_payload_cache(self):
        """A repair or update needs it. Removing it saves space and breaks the product."""
        with open(os.path.join(WINDOWS11, "clean.ps1"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("Visual Studio\\Packages", source)


class DevWorkflow(unittest.TestCase):
    """The workflow is the contract: which disk in, which tag out."""

    def source(self):
        path = os.path.join(HERE, ".github", "workflows", "bake-windows-dev.yml")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_it_starts_from_the_ssh_disk(self):
        source = self.source()
        self.assertIn("windows11-ssh.qcow2", source)
        self.assertIn("pull.py", source)

    def test_it_publishes_a_dev_kind(self):
        # The chain's tags are base, ssh, dev - and the third is what easysandbox calls it.
        source = self.source()
        self.assertIn("windows-dev", source)

    def test_it_installs_sshpass(self):
        # A password prompt cannot be answered in a job, and this stage logs in rather
        # than looking at a screen.
        self.assertIn("sshpass", self.source())


class PublishedDiskCannotBeABackingFile(unittest.TestCase):
    """A zstd-compressed qcow2 is not a valid backing file, and every disk here is one.

    THE FAILURE

        qemu-img: /.../disk.qcow2: Unsupported qcow2 feature(s): compression type
        Could not open backing image to determine size.

    qemu refuses a compressed image as -b, and lib/compress.py publishes every disk with
    -o compression_type=zstd. So a stage that starts from a published disk copies it.

    These tests are about the source, because the failure is a one-second startup error in
    a job that has just spent ten minutes downloading 12 GiB.
    """

    def stages(self):
        for name in ("ssh.py", "devtools.py"):
            with open(os.path.join(WINDOWS11, name), encoding="utf-8") as fh:
                yield name, fh.read()

    def test_no_stage_uses_a_backing_file(self):
        for name, source in self.stages():
            self.assertNotIn('"-b",', source,
                             f"{name} uses a backing file; the published disks are "
                             f"zstd-compressed and qcow2 will not open one as -b")
            self.assertNotIn('"-F", "qcow2"', source,
                             f"{name} names a backing format")

    def test_each_stage_copies_the_input(self):
        for name, source in self.stages():
            self.assertIn("--reflink=auto", source,
                          f"{name} should copy the disk, asking the filesystem for "
                          f"copy-on-write and falling back to a byte copy")

    def test_the_copy_is_still_a_copy(self):
        """The gold disk must not be modified.

        A stage that failed half way would otherwise leave a disk with a partial install
        and no untouched input to re-run from.
        """
        for name, source in self.stages():
            self.assertIn("could not copy the disk", source, name)


class EveryStageParses(unittest.TestCase):
    """A name used before it is assigned is caught by a run, and a run costs ten minutes.

    THE FAILURE

        File "windows/11/ssh.py", line 347, in main
            log(f"copying {args.gold} -> {disk}")
        NameError: name 'disk' is not defined

    A refactor replaced the block that built the disk path with the block that copies to
    it, and the assignment went with the old text. Python only notices when the line
    executes, which is after a 12 GiB download.

    These tests read the source with ast, so they run in milliseconds.
    """

    def files(self):
        """The two stage modules, not bake.py.

        bake.py's main() calls Baker and parse, which are module-level functions - a
        different question from the one this class asks, and one that a compile already
        answers.
        """
        # pull.py unpacks a tuple (tag, dest = argv), which this coarse scan cannot
        # see through - and it is four lines that a compile already covers.
        for name in ("ssh.py", "devtools.py"):
            path = os.path.join(WINDOWS11, name)
            if os.path.isfile(path):
                yield name, path

    def test_every_module_compiles(self):
        import py_compile
        import tempfile
        for name, path in self.files():
            with tempfile.NamedTemporaryFile(suffix=".pyc", delete=True) as out:
                try:
                    py_compile.compile(path, cfile=out.name, doraise=True)
                except py_compile.PyCompileError as exc:
                    self.fail(f"{name} does not compile: {exc}")

    def test_main_defines_what_it_uses(self):
        """Locals that main() reads must be assigned somewhere in main().

        Coarse on purpose: it catches the case above - a name whose assignment was removed
        while its use stayed - without needing to model control flow.
        """
        import ast
        import builtins

        known = set(dir(builtins)) | {
            "args", "log", "vm", "qmpmod", "time", "shutil", "subprocess", "os", "sys",
            "re", "ssh_run", "ssh_base", "scp_to", "wait_for_ssh", "start_swtpm",
            "stop_swtpm", "verify", "clean", "install_package", "installed_packages",
            "wait_for_desktop", "take_shot", "open_powershell", "install", "ocr",
            "configure_sshd", "type_text", "qcode_for", "Keyboard", "PACKAGES",
            "WINGET_INSTALL", "SSH_PORT", "SSH_USER", "SSH_PASSWORD", "INSTALL_TIMEOUT",
            "GUEST_SSH_PORT", "HOST_SSH_PORT", "DISK_NAME", "WINGET_TIMEOUT",
            "GUEST_USER", "GUEST_PASSWORD", "SHIFTED", "NAMED", "HERE", "REPO",
            "main", "argv", "AttributeError", "ValueError", "SystemExit", "Exception",
            "argparse", "numpy", "json", "socket", "tempfile", "importlib", "plistlib",
        }

        for name, path in self.files():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef) or fn.name != "main":
                    continue
                assigned = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                            for t in n.targets if isinstance(t, ast.Name)}
                assigned |= {a.arg for a in fn.args.args}
                for node in ast.walk(fn):
                    if isinstance(node, (ast.For,)):
                        assigned |= {t.id for t in ast.walk(node.target)
                                     if isinstance(t, ast.Name)}
                    if isinstance(node, ast.withitem) and node.optional_vars:
                        assigned |= {t.id for t in ast.walk(node.optional_vars)
                                     if isinstance(t, ast.Name)}
                used = {n.id for n in ast.walk(fn)
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
                missing = used - assigned - known
                self.assertEqual(missing, set(),
                                 f"{name}: main() reads {sorted(missing)} without "
                                 f"assigning them")


class DevArgNames(unittest.TestCase):
    """devtools.py takes --ssh; ssh.py takes --gold. They are different stages."""

    def test_devtools_reads_its_own_argument(self):
        with open(os.path.join(WINDOWS11, "devtools.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('add_argument("--ssh"', source)
        # A leftover --gold in devtools would be an input that is always empty.
        self.assertNotIn("args.gold", source)

    def test_ssh_reads_its_own_argument(self):
        with open(os.path.join(WINDOWS11, "ssh.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('add_argument("--gold"', source)
        self.assertNotIn("args.ssh", source)
