"""Stage three: boot a finished disk, put sshd on it, and prove the forwarded port answers.

    python3 lib/ssh_step.py --disk out/popos-gui.qcow2 --kind popos-gui
    python3 lib/ssh_step.py --tag linux-mint-gui --kind mint-gui --artifacts out/ssh

WHY THIS IS A SEPARATE STAGE, AND WHY IT IS NOT PART OF THE BAKE

A `-gui` disk is installed by clicking through an installer, and that installer does not
install an sshd. Measured on the images this repository bakes:

    NixOS        calamares-nixos-extensions' own configuration template ships
                 `# services.openssh.enable = true;` COMMENTED OUT, so a graphical NixOS
                 install has no sshd at all
    every other  the desktop ISO's own installer has no ssh package in its default set

Adding sshd during the bake would mean the disk that was verified is not the disk that ships -
the launch stage would be checking a different artifact. So it is a stage of its own, and the
run graph shows it as one: `bake` -> `launch` -> `ssh`.

HOW sshd GETS INSTALLED, PER SYSTEM

    a package manager that works offline or online
        Debian family   apt-get install -y openssh-server
        Fedora family   dnf install -y openssh-server
        Arch family     pacman -S --noconfirm openssh
        openSUSE        zypper --non-interactive install openssh
        NixOS           nixos-rebuild with services.openssh.enable = true

    the account that already exists
        every image here has docker/admin, created by the installer, so no user is added

    `PermitRootLogin` is left alone and password authentication is the default, because
    lib/session.py connects with the account's password through sshpass.

WHY IT IS DRIVEN OVER A SERIAL CONSOLE AND NOT SSH

There is no ssh yet - that is the whole point of the stage. So the commands go in through the
console, which every one of these systems puts on ttyS0 (the bakes already rely on that), and
the stage is finished when sshd ANSWERS ON THE FORWARDED PORT, measured from outside the guest.

WHAT PROVES IT

    a TCP connection to <forward-host>:2222 is accepted        the port is exposed
    sshpass ssh <user>@<forward-host> -p 2222 'echo ok'        the account works
    one command that reads the system's own state              ssh really ran something:
        uname -a, then `cat /etc/os-release` or `sw_vers`

The second and third are what make this "ssh from another container and run a command"
rather than "a port is open".
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import session  # noqa: E402
import uefi     # noqa: E402
import vm       # noqa: E402

# THE INSTALL COMMAND PER FAMILY, AND WHY EACH NEEDS A ROOT SHELL.
#
# The account every image here creates is in the sudo/wheel group, and the password is the
# same, so `sudo -S` works everywhere except the systems that ship no sudo - NixOS has it,
# Arch's live session has it, and Fedora's has it. FreeBSD uses `su` and is handled below.
INSTALL = {
    "debian": [
        "apt-get update || true",
        "DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server",
        "systemctl enable --now ssh || systemctl enable --now sshd || true",
    ],
    "fedora": [
        "dnf install -y openssh-server",
        "systemctl enable --now sshd",
    ],
    "arch": [
        "pacman -Sy --noconfirm --needed openssh",
        "systemctl enable --now sshd",
    ],
    "suse": [
        "zypper --non-interactive --no-gpg-checks install openssh",
        "systemctl enable --now sshd",
    ],
    "freebsd": [
        "sysrc sshd_enable=YES",
        "service sshd restart || service sshd start",
    ],
    # NIXOS IS THE ONE THAT IS NOT A PACKAGE INSTALL. Its configuration is a declaration, not
    # a package list: the graphical installer's own template has openssh commented out, so
    # the switch to make is `services.openssh.enable`, and the mechanism is nixos-rebuild.
    # THE ONE FAMILY THAT IS NOT A PACKAGE INSTALL, AND THE ONE PLACE A `sed` IS WRONG.
    #
    # The graphical installer writes a configuration.nix with
    #
    #     # services.openssh.enable = true;
    #
    # commented out. The obvious fix is a `sed` that uncomments it - and the obvious fix does
    # not work: turning one line into three needs an embedded newline in the replacement, and
    # GNU sed does not expand `\n` there without `-z`. What DOES work, and is what a person
    # would do, is append the three settings to the file. NixOS merges repeated attribute
    # definitions, so appending is correct even though the commented line is still there.
    #
    # `networking.firewall.enable` is set false as well: the graphical install leaves the
    # firewall ON, and a firewall that drops 22 is a disk that answers nothing.
    "nixos": [
        "printf '\\n  services.openssh.enable = true;\\n"
        "  services.openssh.settings.PasswordAuthentication = true;\\n"
        "  services.openssh.settings.PermitRootLogin = \"yes\";\\n"
        "  networking.firewall.enable = false;\\n' >> /etc/nixos/configuration.nix",
        "nixos-rebuild switch 2>&1 | tail -25",
        "systemctl is-active sshd || systemctl start sshd || true",
    ],
}

# WHICH FAMILY A KIND IS. The `-gui` suffix is stripped first: linux-popos-gui -> popos.
FAMILY = {
    "ubuntu-gui": "debian", "kubuntu-gui": "debian", "zorin-gui": "debian",
    "mint-gui": "debian", "popos-gui": "debian", "mx-gui": "debian",
    "fedora-gui": "fedora",
    "cachyos-gui": "arch", "manjaro-gui": "arch", "endeavour-gui": "arch",
    "opensuse-gui": "suse",
    "nixos-gui": "nixos",
    "ghostbsd-mate": "freebsd",
}

# THE CONSOLE IS WHERE THE COMMANDS GO, AND EACH SYSTEM'S PROMPT DIFFERS. A marker is what the
# stage waits for, and it is chosen to be a string that can only come from the command just
# sent - so a slow prompt cannot be mistaken for a finished command.
def log(message: str) -> None:
    print(f"[ssh-step {time.strftime('%H:%M:%S')}] {message}", flush=True)


def the_script(kind: str) -> str:
    """The shell script that installs sshd, as ONE text blob.

    WHY A SCRIPT AND NOT A LIST OF LINES TYPED AT A PROMPT. These commands go in through a
    serial console, which is a stream of characters with no quoting layer above it: a `sed`
    with a `|` delimiter inside single quotes (the NixOS case) or a `su -c "..."` (the
    FreeBSD case) has to be escaped for a shell, and then survive a terminal. The first
    version of this module built those strings by concatenation and produced

        sudo -n ... 2>&1 2>&1 || sudo -S ... 2>&1

    which is a different command from the one intended. A script delimited by a heredoc has
    exactly the same problem, because the heredoc is also typed.

    SO IT IS base64. One line to write the blob, one line to run it, and no character in the
    script can be misread on the way in. `base64 -d` is in coreutils, in FreeBSD's base and
    in every one of these images.
    """
    family = FAMILY[kind]
    lines = ["#!/bin/sh", "set -x"]
    lines.extend(INSTALL[family])
    return "\n".join(lines) + "\n"


def the_commands(kind: str, password: str) -> list[str]:
    """The TWO lines that install sshd, for the family this kind belongs to.

    THE SECOND LINE IS THE ONLY PLACE THE PASSWORD APPEARS, and it is piped rather than
    typed as an argument: `echo <pw> | sudo -S` is what every bake in this repository
    already does for the same reason (there is no tty to prompt at).
    """
    import base64

    blob = base64.b64encode(the_script(kind).encode()).decode()
    if FAMILY[kind] == "freebsd":
        # FreeBSD's base system has base64 and su; there is no sudo on a default install.
        return [
            f"echo {blob} | base64 -d > /tmp/easy-ssh.sh",
            f'echo {password} | su -m root -c "sh /tmp/easy-ssh.sh" 2>&1',
        ]
    # EVERY LINUX FAMILY: decode the blob, then run it as root.
    return [
        f"echo {blob} | base64 -d > /tmp/easy-ssh.sh",
        f"echo {password} | sudo -S sh /tmp/easy-ssh.sh 2>&1",
    ]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--disk", default=None, help="a local qcow2")
    p.add_argument("--tag", default=None, help="a published tag to pull instead")
    p.add_argument("--kind", required=True, choices=sorted(FAMILY))
    p.add_argument("--artifacts", default="out")
    p.add_argument("--work", default=None)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--user", default=os.environ.get("SSH_USER") or "docker")
    p.add_argument("--password", default=os.environ.get("SSH_PASSWORD") or "admin")
    p.add_argument("--resolution", default="1920x1080")
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
        note(f"VERDICT {'PASS' if code == 0 else 'FAIL'} kind={args.kind} "
             f"family={FAMILY[args.kind]} tag={args.tag or args.disk}")
        return code

    disk = args.disk
    if not disk:
        if not args.tag:
            note("=== NEITHER --disk NOR --tag ===")
            return 2
        disk = os.path.join(work, f"{args.kind}.qcow2")
        if not os.path.exists(disk):
            note(f"pulling {args.tag} from ghcr")
            import iso
            try:
                disk = iso.fetch_tag(args.tag, disk)
            except iso.IsoError as exc:
                note(f"=== {args.tag} IS NOT PUBLISHED: {exc}")
                return finish(2)
    if not os.path.exists(disk):
        note(f"=== NO DISK AT {disk} ===")
        return finish(2)
    note(f"disk {disk} ({os.path.getsize(disk) >> 20} MiB)")

    # THE CONSOLE IS THE ONLY WAY IN, BECAUSE THERE IS NO ssh YET.
    import serial
    console_path = os.path.join(artifacts, "console.log")
    ser = serial.Serial(work, "serial", sink=console_path)
    width, height = (int(v) for v in args.resolution.lower().split("x", 1))
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        vga="std", serial_pipe=ser.stdin.removesuffix(".in"),
        resolution=(width, height), ssh_port=args.ssh_port,
    )
    shape = uefi.shape_with_uefi(shape, work, args.kind)
    drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)

    try:
        guest.start()
        ser.open(timeout=60)
        note(f"qemu pid {guest.pid}, console {console_path}")

        # WAIT FOR THE SYSTEM TO REACH USERSPACE. A login prompt on the console is the only
        # signal that exists before sshd does, and every one of these systems prints one.
        deadline = time.time() + args.boot_timeout
        saw_prompt = False
        while time.time() < deadline:
            try:
                with open(console_path, "rb") as fh:
                    tail = fh.read().decode("utf-8", "replace")[-4000:]
            except OSError:
                tail = ""
            if any(marker in tail for marker in ("login:", "Password:", "# ", "$ ")):
                saw_prompt = True
                break
            time.sleep(5)
        if not saw_prompt:
            note("=== THE CONSOLE NEVER REACHED A LOGIN PROMPT ===")
            note(f"  last console: {tail[-400:]}")
            return finish(1)
        note("the console reached a login prompt")

        # LOG IN, THEN INSTALL. `root` first, then the account, because a `-gui` disk's account
        # is the same everywhere but root's password is not always set.
        for attempt in ("docker", "root"):
            ser.say(attempt, wait=4)
            time.sleep(4)
            ser.say(args.password, wait=5)
            time.sleep(5)
            try:
                with open(console_path, "rb") as fh:
                    tail = fh.read().decode("utf-8", "replace")[-2000:]
            except OSError:
                tail = ""
            if any(p in tail for p in ("$ ", "# ", "~]")):
                note(f"logged in as {attempt}")
                break

        for command in the_commands(args.kind, args.password):
            note(f"$ {command[:140]}")
            ser.say(command, wait=20)
            time.sleep(20)
        # GIVE THE INSTALL TIME, THEN MAKE SURE sshd IS RUNNING IN THIS BOOT TOO: the enable
        # makes it survive a reboot, and the restart makes it answer now.
        ser.say(f"echo {args.password} | sudo -S systemctl restart ssh 2>&1 || "
                f"echo {args.password} | sudo -S systemctl restart sshd 2>&1 || true",
                wait=15)
        note("sshd install commands have been sent")

        # THE PROOF, AND IT IS MEASURED FROM OUTSIDE THE GUEST.
        note(f"waiting up to {args.boot_timeout}s for sshd on the forwarded port "
             f"{args.ssh_port}")
        ok = session.wait_for_ssh(args.ssh_port, args.boot_timeout, note)
        if not ok:
            note("=== sshd NEVER ANSWERED ON THE FORWARDED PORT ===")
            return finish(1)

        # ONE COMMAND THAT PROVES ssh RAN SOMETHING IN THE GUEST.
        status, output = session.ssh(args.ssh_port, "uname -a; cat /etc/os-release 2>/dev/null "
                                                  "| head -3 || sw_vers")
        note(f"ssh: uname -> {status}")
        for line in (output or "").splitlines()[:6]:
            note(f"  {line}")
        if status != 0:
            note("=== ssh ANSWERED BUT THE COMMAND FAILED ===")
            return finish(1)

        status, output = session.ssh(args.ssh_port, "id -un; hostname")
        note(f"ssh: id/hostname -> {status}: {output.splitlines()[:2]}")
        if status != 0:
            return finish(1)
        return finish(0)
    finally:
        try:
            ser.close()
        except Exception:                              # noqa: BLE001
            pass
        guest.stop(acpi_first=False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
