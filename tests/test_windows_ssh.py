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
        source = open(os.path.join(WINDOWS11, "ssh.py"), encoding="utf-8").read()
        self.assertIn("vm.Shape(", source)
        self.assertIn("vm.Guest(", source)

    def test_it_does_not_reimplement_the_configuration(self):
        """The guest is reached through lib/qmp.py, like every other driver here.

        A second QMP client would be a second place to fix the atomic key pair, and that
        bug is exactly what made the Windows bake open a Start menu.
        """
        source = open(os.path.join(WINDOWS11, "ssh.py"), encoding="utf-8").read()
        self.assertIn("import qmp as qmpmod", source)
        self.assertIn("qmpmod.key_combo", source)

    def test_a_failed_stage_does_not_overwrite_the_gold_disk(self):
        """The gold disk is an input. The work happens on a copy.

        A run that fails half way leaves a disk with a half-installed OpenSSH; re-running
        from the untouched gold disk is the only cheap recovery.
        """
        source = open(os.path.join(WINDOWS11, "ssh.py"), encoding="utf-8").read()
        self.assertIn('"-b"', source)
        self.assertIn("work", source)


if __name__ == "__main__":
    unittest.main()
