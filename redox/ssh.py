"""Log in to a baked Redox disk over ssh, as the account this repository uses.

    python3 redox/ssh.py --tag redox-desktop

WHY THIS IS A STAGE OF ITS OWN

A launch stage proves a disk boots and an sshd answers. That is a banner, not a login: the
port can be open and the account unusable, and every one of this repository's systems has
that distinction somewhere. This stage is the login - password authentication, the shell,
and the commands a later stage would need to run.

WHAT IS DIFFERENT FROM THE OTHER SYSTEMS HERE

There is no `lib/session.py` call for the account check. That module asks logind and the
session bus, which Redox does not have: no systemd, no elogind, no `/proc`. What it can be
asked is `ion` itself - `echo $USER` and `id` - and those come back over the same channel
the next stage will use.

THE PASSWORD IS SENT, NOT AVOIDED

`sshpass` is what every workflow here already installs for this, because an ssh login with
a password needs a tty for the prompt. The bake set that password in /etc/users.toml; if it
is wrong, this fails with the authentication error rather than a timeout, and the two are
different bugs.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import qmp as qmpmod  # noqa: E402
import vm  # noqa: E402

CPU = "Broadwell,-pcid,-x2apic,-tsc-deadline,-hle,-rtm,-invpcid"

SSH_USER = "docker"
SSH_PASSWORD = "admin"


def log(message: str) -> None:
    print(f"[redox-ssh {time.strftime('%H:%M:%S')}] {message}", flush=True)


def ssh(port: int, command: str, password: str, *, timeout: int = 120):
    """One command over ssh, with the password supplied through sshpass."""
    return subprocess.run(
        ["sshpass", "-p", password, "ssh",
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-o", "ConnectTimeout=15",
         "-p", str(port), f"{SSH_USER}@127.0.0.1", command],
        capture_output=True, text=True, timeout=timeout,
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tag", default="redox-desktop")
    p.add_argument("--disk", default=None)
    p.add_argument("--artifacts", default="out")
    p.add_argument("--work", default=None)
    p.add_argument("--user", default=SSH_USER)
    p.add_argument("--password", default=SSH_PASSWORD)
    p.add_argument("--boot-timeout", type=int, default=600)
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--mem", type=int, default=2048)
    args = p.parse_args(argv)

    work = os.path.abspath(args.work or args.artifacts)
    artifacts = os.path.abspath(args.artifacts)
    os.makedirs(work, exist_ok=True)
    os.makedirs(artifacts, exist_ok=True)

    verdict_path = os.path.join(artifacts, "ssh.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)
        try:
            with open(verdict_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def finish(code: int) -> int:
        note(f"VERDICT {'PASS' if code == 0 else 'FAIL'} redox ssh tag={args.tag}")
        return code

    disk = args.disk
    if not disk:
        disk = os.path.join(work, "redox.qcow2")
        if not os.path.exists(disk):
            note(f"pulling {args.tag} from ghcr")
            import iso

            try:
                disk = iso.fetch_tag(args.tag, disk)
            except iso.IsoError as exc:
                note(f"=== {args.tag} IS NOT PUBLISHED: {exc}")
                note("  run its bake first")
                return finish(2)
    if not os.path.exists(disk):
        note(f"=== NO DISK AT {disk} ===")
        return finish(2)
    note(f"disk {disk} ({os.path.getsize(disk) >> 20} MiB)")

    shape = vm.Shape(cpus=2, mem_mb=args.mem, machine="q35", cpu=CPU,
                     vga="std", resolution=(1920, 1080), ssh_port=args.ssh_port)
    drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)

    try:
        guest.start()

        # THE BOOTLOADER WAITS FOR A KEY, exactly as in the probe and the launch stage.
        note("=== the bootloader ===")
        deadline = time.time() + min(args.boot_timeout, 300)
        menu = False
        while time.time() < deadline:
            time.sleep(5)
            try:
                c = qmpmod.QMP(guest.paths.drive)
            except (OSError, RuntimeError):
                continue
            try:
                ppm = os.path.join(artifacts, "boot.ppm")
                c.execute("screendump", filename=ppm)
            finally:
                c.close()
            if os.path.exists(ppm) and os.path.getsize(ppm) > 20000:
                menu = True
                break
        if not menu:
            note("=== THE BOOTLOADER NEVER DREW ===")
            return finish(1)
        c = qmpmod.QMP(guest.paths.drive)
        try:
            qmpmod.key(c, "ret")
        finally:
            c.close()
        note("  answered; waiting for sshd")

        # SSHD, THEN THE LOGIN. The wait is for sshd because it is what this stage needs;
        # the desktop is the launch stage's question and asking it twice would make a
        # failure ambiguous.
        deadline = time.time() + args.boot_timeout
        ready = False
        while time.time() < deadline:
            r = ssh(args.ssh_port, "true", args.password, timeout=60)
            if r.returncode == 0:
                ready = True
                break
            text = (r.stdout or "") + (r.stderr or "")
            if "Permission denied" in text:
                note("=== sshd ANSWERED AND REFUSED THE ACCOUNT ===")
                note(f"  {text.strip().splitlines()[-1][:200] if text.strip() else ''}")
                note("  the disk is up; the password in /etc/users.toml is what to check")
                return finish(1)
            time.sleep(10)
        if not ready:
            note("=== ssh NEVER ACCEPTED THE LOGIN IN THE BOOT WINDOW ===")
            return finish(1)
        note("  logged in")

        # WHAT THE SHELL SAYS IT IS. The same two questions a person would ask: who am I,
        # and what can I run.
        note("=== the account ===")
        checks = [
            ("the user", "echo $USER", ["docker"]),
            ("the id", "id", ["uid="]),
            ("the shell", "echo $SHELL", ["ion"]),
            ("the host", "hostname", ["redox", "localhost"]),
        ]
        failed = []
        for what, command, expected in checks:
            r = ssh(args.ssh_port, command, args.password, timeout=90)
            text = ((r.stdout or "") + (r.stderr or "")).strip()
            note(f"  {what}: exit {r.returncode} -> {text.splitlines()[-1][:120] if text else '(empty)'}")
            if r.returncode != 0:
                failed.append(what)
                continue
            low = text.lower()
            if not any(e.lower() in low for e in expected):
                note(f"    expected one of {expected}")
                failed.append(what)

        # THE PATH A LATER STAGE WOULD USE. Not a version check - that is the dev stage's
        # job - just proof that a command from /usr/bin runs at all.
        note("=== running something from the disk ===")
        r = ssh(args.ssh_port, "ls /usr/bin/sshd", args.password, timeout=90)
        text = ((r.stdout or "") + (r.stderr or "")).strip()
        note(f"  ls /usr/bin/sshd: exit {r.returncode} -> {text[-120:] if text else '(empty)'}")
        if r.returncode != 0 or "sshd" not in text:
            failed.append("/usr/bin/sshd")

        if failed:
            note(f"=== THE LOGIN WORKS AND {', '.join(failed)} DID NOT ===")
            return finish(1)

        note("=== THE ACCOUNT WORKS OVER SSH ===")
        return finish(0)
    finally:
        guest.stop()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
