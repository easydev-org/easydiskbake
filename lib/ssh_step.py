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

WHAT IT INSTALLS IS NOT DECIDED HERE

    <distro>/steps/ssh.yaml is the whole of that distribution's ssh install:

        linux/ubuntu-gui/steps/ssh.yaml     apt-get install openssh-server
        linux/cachyos-gui/steps/ssh.yaml    pacman -S openssh, `sshd` not `ssh`
        linux/fedora-gui/...                dnf, plus firewall-cmd --add-service=ssh
        linux/opensuse-gui/...              zypper, plus firewalld
        linux/nixos-gui/...                 append the attribute, nixos-rebuild switch
        bsd/ghostbsd/steps/ssh.yaml         sysrc sshd_enable=YES, service sshd start

    Thirteen distributions whose package manager, unit name, firewall and even whether sshd
    is a package at all differ. Each one is a file, not a row in a table here.

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

# THE KINDS THIS STAGE SUPPORTS - A LIST FOR THE ARGUMENT PARSER, NOT A TABLE OF BEHAVIOUR.
#
# Every fact about HOW ssh is installed lives in that distribution's steps/ssh.yaml. This is
# only so `--kind` refuses a typo with the real names, which is what argparse is for.
KINDS = (
    "ubuntu-gui", "kubuntu-gui", "zorin-gui", "mint-gui", "cachyos-gui", "manjaro-gui",
    "mx-gui", "opensuse-gui", "endeavour-gui", "fedora-gui", "popos-gui", "nixos-gui",
    "ghostbsd-mate",
)

# EACH DISTRIBUTION'S ssh.yaml IS THE WHOLE OF ITS ssh INSTALL.
#
# There is deliberately NO table here. The first version of this module carried one - a
# `FAMILY` map and an `INSTALL` map of package commands - and that was the mistake the whole
# change is undoing: thirteen distributions whose desktop, package manager, unit name,
# firewall and even whether sshd is a package at all all differ, written as one table of
# exceptions in a Python file. They live in <distro>/steps/ssh.yaml, where each one is a file
# a reader can open on its own.
#
# So this module only does what a runner must: boot the disk, wait for its desktop, then
# hand the walk the QMP socket and the verdict. The walk is the distribution's.

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
    p.add_argument("--kind", required=True, choices=sorted(KINDS))
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
             f"tag={args.tag or args.disk}")
        return code

    def shot(q, name: str) -> str:
        """A screenshot, saved and logged. THE ARTIFACT THAT EXPLAINS A FAILURE.

        The first real run of this stage ended `sshd NEVER ANSWERED` with nothing to look
        at: the desktop painted, the terminal was opened, two lines were typed, and the port
        stayed refused. Whether the terminal opened at all, whether the command ran, whether
        apt printed an error - all of it was on a screen nobody photographed. So every step
        that can be wrong takes a frame, and the frames are uploaded with the verdict.
        """
        ppm = os.path.join(artifacts, f"{name}.ppm")
        png = os.path.join(artifacts, f"{name}.png")
        try:
            q.execute("screendump", filename=ppm)
        except (OSError, RuntimeError, AttributeError):
            return ""
        try:
            import lastframe
            lastframe.convert(ppm, png)
        except (OSError, subprocess.SubprocessError):
            return ppm
        return png

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

            # THE WALK IS A YAML FILE, THE SAME AS EVERY OTHER GUI STAGE HERE.
            #
            # It opens the terminal with `run:` (ctrl+alt+t), waits for the daemon with
            # `require_ssh:`, and proves it with `ssh:` - the three actions
            # macos/15/steps/ssh.yaml uses for the same job. The distribution only decides
            # WHICH SCRIPT the run: step types, and that is what this module supplies.
            #
            # THE QMP SOCKET IS ALREADY OPEN, so this client is closed first: qemu serves one
            # client per socket and steps.run_file opens its own.
            q.close()
            import steps as stepmod
            # EACH DISTRIBUTION'S OWN FILE, BECAUSE THE FOUR THINGS IT DOES DIFFER.
            #
            # A single shared walk would have to be written as a table of exceptions - the
            # package manager, the unit name (`ssh` on Debian, `sshd` elsewhere), the firewall
            # that drops 22 on Fedora and openSUSE and does not exist on the others, and
            # whether sshd is a package at all (NixOS is declarative; FreeBSD has no sudo and
            # an rc.conf knob instead of a systemd unit). Those live in
            # <distro>/steps/ssh.yaml, beside the install.yaml that drives the same image.
            root = os.path.join("bsd", "ghostbsd") if args.kind == "ghostbsd-mate" \
                else os.path.join("linux", args.kind)
            walk = os.path.join(REPO, root, "steps", "ssh.yaml")
            if not os.path.isfile(walk):
                note(f"=== NO ssh.yaml FOR {args.kind}: {walk} ===")
                return finish(2)
            note(f"=== the walk: {os.path.join(root, 'steps', 'ssh.yaml')} ===")
            ok, ran = stepmod.run_file(
                walk, guest.paths.drive, work=work,
                user=args.user, password=args.password,
                ssh_port=args.ssh_port,
            )
            note(f"{len(ran)} step(s) ran" if ok
                 else f"the walkthrough failed after {len(ran)} step(s)")
            if not ok:
                # THE FRAME THAT EXPLAINS IT. The first real run of this stage ended with
                # `sshd NEVER ANSWERED` and nothing to look at: the terminal may or may not
                # have opened, and apt may have printed an error on a screen nobody took.
                # `steps.py` closes its QMP client on the way out, so this reopens one.
                try:
                    q2 = Q.QMP(guest.paths.drive)
                    try:
                        note(f"  last frame: {shot(q2, 'failed')}")
                    finally:
                        q2.close()
                except Exception:                  # noqa: BLE001
                    pass
                return finish(1)

            # THE LAST PROOF IS FROM OUTSIDE THE GUEST, AND IT IS THE ONE THE OBJECTIVE NAMES:
            # another container sshing in and running a command. The walk's `ssh:` steps go
            # over the same forwarded port, so this is a re-read of the same fact in the
            # stage's own words - and it is what the verdict line reports.
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
