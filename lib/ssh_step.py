"""Stage three: boot a finished desktop disk, put sshd on it, and prove ssh works.

    python3 lib/ssh_step.py --disk out/popos-gui.qcow2 --kind popos-gui
    python3 lib/ssh_step.py --tag linux-mint-gui --kind mint-gui --artifacts out/ssh

WHY THIS STAGE EXISTS

A `-gui` disk is installed by clicking through a desktop installer, and that installer does
not install an sshd. Measured on the images this repository bakes:

    NixOS        calamares-nixos-extensions' own template ships
                 `# services.openssh.enable = true;` COMMENTED OUT
    every other  the desktop ISO's own installer has no ssh package in its default set

Adding sshd during the bake would mean the disk that was verified is not the disk that
ships, so this is a stage of its own: bake -> launch -> ssh.

WHY IT DRIVES THE SCREEN AND NOT THE SERIAL CONSOLE

The obvious transport is ttyS0, and it is the one that failed first. Run 42551299241 booted
the finished Pop!_OS disk, the console showed

    BdsDxe: starting Boot0001 "UEFI Misc Device"

and then NOTHING - no login prompt, ever. A graphical distribution's init does not spawn a
serial getty unless one is configured, and none of these installers configures one. The
answer-file bakes can use the serial console because their installers print to it; these
disks have a desktop and nothing else.

SO THE COMMANDS GO IN THROUGH THE DESKTOP: boot it, open the terminal the session already
has (ctrl+alt+t on every one of these), type the script, and then prove the result the only
way that means anything - connect from outside on the forwarded port and run a command.

WHAT IT INSTALLS, PER FAMILY

    debian      apt-get install openssh-server, systemctl enable --now ssh
    fedora      dnf install openssh-server, systemctl enable --now sshd
    arch        pacman -S --noconfirm openssh, systemctl enable --now sshd
    suse        zypper install openssh, systemctl enable --now sshd
    nixos       append services.openssh.enable and firewall settings to
                configuration.nix, then nixos-rebuild switch  (see INSTALL)
    freebsd     sysrc sshd_enable=YES, service sshd start

The script travels as ONE base64 blob typed as two lines, so no character in it - the `|` in
a sed pattern, the quotes in FreeBSD's `su -c` - has to survive a terminal's line discipline.

WHAT PROVES IT

    sshpass ssh docker@<forward-host> -p 2222 'uname -a'    the account works over the port
    sshpass ssh ... 'id -un; hostname'                     and it ran something
"""

from __future__ import annotations

import argparse
import base64
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

# THE INSTALL, PER FAMILY.
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
    # THE ONE FAMILY THAT IS NOT A PACKAGE INSTALL, AND THE ONE PLACE A `sed` IS WRONG.
    #
    # The graphical installer writes a configuration.nix with
    #
    #     # services.openssh.enable = true;
    #
    # commented out. Turning one line into three needs an embedded newline in a `sed`
    # replacement, and GNU sed does not expand `\n` there without `-z`. Appending is what a
    # person would do, and it is correct in Nix: repeated attribute definitions merge, so the
    # remaining comment is harmless. `networking.firewall.enable = false` is part of it - the
    # graphical install leaves the firewall ON, and a firewall that drops 22 is a disk that
    # answers nothing.
    "nixos": [
        "printf '\\n  services.openssh.enable = true;\\n"
        "  services.openssh.settings.PasswordAuthentication = true;\\n"
        "  services.openssh.settings.PermitRootLogin = \"yes\";\\n"
        "  networking.firewall.enable = false;\\n' >> /etc/nixos/configuration.nix",
        "nixos-rebuild switch 2>&1 | tail -25",
        "systemctl is-active sshd || systemctl start sshd || true",
    ],
    "freebsd": [
        "sysrc sshd_enable=YES",
        "service sshd restart || service sshd start",
    ],
}

FAMILY = {
    "ubuntu-gui": "debian", "kubuntu-gui": "debian", "zorin-gui": "debian",
    "mint-gui": "debian", "popos-gui": "debian", "mx-gui": "debian",
    "fedora-gui": "fedora",
    "cachyos-gui": "arch", "manjaro-gui": "arch", "endeavour-gui": "arch",
    "opensuse-gui": "suse",
    "nixos-gui": "nixos",
    "ghostbsd-mate": "freebsd",
}

# THE TERMINAL SHORTCUT, AND THE SETTLE AFTER IT. ctrl+alt+t opens a terminal on GNOME,
# KDE, Xfce, Cinnamon, MATE and COSMIC alike - it is the one shortcut all six agree on, and
# it is what every driver in this repository already types to reach a shell.
TERMINAL_KEYS = ("ctrl", "alt", "t")
TERMINAL_WAIT = 12.0

# HOW LONG THE PACKAGE INSTALL MAY TAKE. apt on a live network is the slowest of these;
# `nixos-rebuild` has to evaluate and fetch a store path. Both are bounded, and the stage
# fails with the console in the log rather than hanging.
INSTALL_WAIT = {"debian": 240, "fedora": 240, "arch": 240, "suse": 300,
                "nixos": 420, "freebsd": 120}


def log(message: str) -> None:
    print(f"[ssh-step {time.strftime('%H:%M:%S')}] {message}", flush=True)


def the_script(kind: str) -> str:
    """The shell script that installs sshd, as one text blob - see the module docstring."""
    lines = ["#!/bin/sh", "set -x"]
    lines.extend(INSTALL[FAMILY[kind]])
    return "\n".join(lines) + "\n"


def the_lines(kind: str, password: str) -> list[str]:
    """The two lines to type into the guest's terminal.

    ONE base64 WRITE, ONE base64 RUN. The password appears once, piped rather than passed as
    an argument, because there is no tty to prompt at - the same shape every bake here uses.
    """
    blob = base64.b64encode(the_script(kind).encode()).decode()
    if FAMILY[kind] == "freebsd":
        return [
            f"echo {blob} | base64 -d > /tmp/easy-ssh.sh",
            f'echo {password} | su -m root -c "sh /tmp/easy-ssh.sh" 2>&1',
        ]
    return [
        f"echo {blob} | base64 -d > /tmp/easy-ssh.sh",
        f"echo {password} | sudo -S sh /tmp/easy-ssh.sh 2>&1",
    ]


def type_line(q, text: str, per_char: float = 0.002) -> None:
    """Type one line into the guest and press Return.

    `per_char` IS SMALL AND THAT IS DELIBERATE. These lines are a base64 blob - a few hundred
    characters - and the terminal is a real terminal: at 100 ms a character a 900-character
    line is a minute and a half of nothing. 2 ms is fast enough that xterm keeps up (measured
    on the Pop!_OS run that installed sshd) and slow enough that no keystroke is lost.
    """
    import keys
    keys.type_text(q, text, per_char=per_char)
    time.sleep(0.4)
    q.execute("input-send-event", events=[
        {"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": "ret"}}},
        {"type": "key", "data": {"down": False, "key": {"type": "qcode", "data": "ret"}}},
    ])


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--disk", default=None, help="a local qcow2")
    p.add_argument("--tag", default=None, help="a published tag to pull instead")
    p.add_argument("--kind", required=True, choices=sorted(FAMILY))
    p.add_argument("--artifacts", default="out")
    p.add_argument("--work", default=None)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--desktop-timeout", type=int, default=300,
                   help="how long the desktop gets to paint before the terminal is opened")
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--user", default=os.environ.get("SSH_USER") or "docker")
    p.add_argument("--password", default=os.environ.get("SSH_PASSWORD") or "admin")
    p.add_argument("--resolution", default="1920x1080")
    p.add_argument("--no-clean", action="store_true",
                   help="accepted and ignored: this stage does not clean, it installs")
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

    # THE CONSOLE IS KEPT FOR THE RECORD, NOT FOR INPUT - see the module docstring: these
    # disks print nothing to ttyS0. It is still a chardev so a run that hangs has something
    # in its artifact that says what the firmware did.
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

    import qmp as Q
    import keys

    try:
        guest.start()
        ser.open(timeout=60)
        note(f"qemu pid {guest.pid}, console {console_path}")

        # DRIVE THE DESKTOP. The same qmp socket every GUI stage uses; this module opens its
        # own client because nothing else is holding it here.
        q = Q.QMP(guest.paths.drive)
        try:
            note(f"waiting up to {args.desktop_timeout}s for the desktop")
            deadline = time.time() + args.desktop_timeout
            painted = False
            while time.time() < deadline:
                shot = os.path.join(artifacts, "desktop.ppm")
                try:
                    q.execute("screendump", filename=shot)
                except (OSError, RuntimeError):
                    time.sleep(5)
                    continue
                import lastframe
                png = os.path.join(artifacts, "desktop.png")
                try:
                    lastframe.convert(shot, png)
                except (OSError, subprocess.SubprocessError):
                    pass
                import launch_linux
                ok, why = launch_linux.frame_is_painted(shot)
                note(f"  frame: {why}")
                if ok:
                    painted = True
                    break
                time.sleep(10)
            if not painted:
                note("=== NO DESKTOP PAINTED ===")
                return finish(1)

            # A DESKTOP THAT JUST PAINTED IS NOT A DESKTOP THAT ACCEPTS INPUT. The session
            # starts its shell, its compositor and its portal after it draws, and a ctrl+alt+t
            # sent into that gap goes nowhere - measured on the GUI launches, which the
            # `desktop-settle` exists for.
            settle = min(60, max(20, args.desktop_timeout // 5))
            note(f"  letting the session settle for {settle}s before the terminal")
            time.sleep(settle)

            # THE TERMINAL, WHICH IS THE ONLY WAY IN. See the module docstring.
            note(f"opening a terminal with {'+'.join(TERMINAL_KEYS)}")
            keys_q = q
            keys_q.execute("input-send-event", events=[
                {"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": "ctrl"}}},
                {"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": "alt"}}},
                {"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": "t"}}},
                {"type": "key", "data": {"down": False, "key": {"type": "qcode", "data": "t"}}},
                {"type": "key", "data": {"down": False, "key": {"type": "qcode", "data": "alt"}}},
                {"type": "key", "data": {"down": False, "key": {"type": "qcode", "data": "ctrl"}}},
            ])
            time.sleep(TERMINAL_WAIT)

            # TYPE THE TWO LINES. The first writes the script, the second runs it.
            typed = the_lines(args.kind, args.password)
            for index, line in enumerate(typed, start=1):
                note(f"typing line {index}/{len(typed)} ({len(line)} chars)")
                type_line(q, line)
                # THE INSTALL IS THE SLOW PART AND ONLY THE LAST LINE IS IT.
                wait = INSTALL_WAIT[FAMILY[args.kind]] if index == len(typed) else 6
                note(f"  waiting {wait}s for it")
                time.sleep(wait)

            # THE PROOF, FROM OUTSIDE THE GUEST. A port that accepts a connection and refuses
            # every command is not what the objective asks for.
            note(f"waiting up to {args.boot_timeout}s for sshd on the forwarded port "
                 f"{args.ssh_port}")
            if not session.wait_for_ssh(args.ssh_port, args.boot_timeout, note):
                note("=== sshd NEVER ANSWERED ON THE FORWARDED PORT ===")
                return finish(1)

            status, output = session.ssh(
                args.ssh_port, "uname -a; (cat /etc/os-release || true) | head -3")
            note(f"ssh: uname -> {status}")
            for line in (output or "").splitlines()[:6]:
                note(f"  {line}")
            if status != 0:
                note("=== ssh ANSWERED BUT THE COMMAND FAILED ===")
                return finish(1)

            status, output = session.ssh(args.ssh_port, "id -un; hostname")
            note(f"ssh: id/hostname -> {status}: {(output or '').splitlines()[:2]}")
            if status != 0:
                return finish(1)
            return finish(0)
        finally:
            try:
                q.close()
            except Exception:                      # noqa: BLE001
                pass
    finally:
        try:
            ser.close()
        except Exception:                          # noqa: BLE001
            pass
        guest.stop(acpi_first=False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
