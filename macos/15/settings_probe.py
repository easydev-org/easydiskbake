"""Open System Settings' Sharing pane and photograph it, so the switch can be measured.

    python3 macos/15/settings_probe.py --disk out/macos15-desktop.qcow2

WHAT THIS IS FOR

`sudo systemsetup -setremotelogin on` refuses on macOS 13+:

    setremotelogin: Turning Remote Login on or off requires Full Disk Access privileges.

sudo and TCC are orthogonal - sudo says the process runs as root, TCC says whether THIS
APPLICATION may touch this KIND OF DATA. systemsetup is a command-line tool with no grant,
so it is refused however it is launched.

SYSTEM SETTINGS.APP HAS THE GRANT. It is Apple-signed and TCC-protected data is what it is
for, so the switch inside it does what systemsetup cannot. That also means it takes macOS's
own path to enabling Remote Login rather than the three launchctl calls this repository has
been issuing by hand - the difference being everything the daemon does around them, including
the network service records that the failing libinfo call (si_destination_compare) is about.

THIS SCRIPT PRODUCES NO DISK AND PUBLISHES NOTHING. It boots the desktop disk, logs in, opens
System Settings, navigates to Sharing, turns Remote Login on, and then dumps frames to
out/settings-probe/ so the pane can be measured. It is a probe: once the coordinates and the
sequence are known they go into macos/15/ssh.py and this file is deleted.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, HERE)


def log(message: str) -> None:
    print(f"[settings-probe {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True)
    p.add_argument("--out", default="out/settings-probe")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--login-delay", type=int, default=120)
    args = p.parse_args(argv)

    if not os.path.isfile(args.disk):
        raise SystemExit(f"no disk at {args.disk}")
    work = args.work or args.out
    os.makedirs(work, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)

    import machine
    import macos_recovery
    import opencore
    import qmp as qmpmod
    import vm
    from lib import keys

    log(f"disk {args.disk}")
    log("=== recovery image ===")
    recovery = macos_recovery.fetch_published("15", work)
    log(f"  {recovery}")

    log("=== OpenCore, firmware, APFS driver ===")
    assets = opencore.build("15", work)
    log(f"  identity {assets['identity']['serial']} / {assets['identity']['mac']}")

    shape = machine.shape(assets, cpus=args.cpus, mem_mb=args.mem)
    shape.ssh_port = args.ssh_port
    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            "-drive", machine.TARGET_DRIVE.format(disk=os.path.abspath(args.disk)),
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
            "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
        ],
        shape=shape,
        stdout=os.path.join(work, "console.log"),
    ).start()

    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    snap = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         guest.paths.record, frames, "1"],
        stdout=open(os.path.join(frames, "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )

    def shot(name: str) -> None:
        """Save a full-resolution screendump beside the recording."""
        ppm = os.path.join(args.out, f"{name}.ppm")
        try:
            q = qmpmod.QMP(guest.paths.drive)
            try:
                q.execute("screendump", filename=ppm)
            finally:
                q.close()
            import lastframe
            png = os.path.join(args.out, f"{name}.png")
            lastframe.convert(ppm, png)
            log(f"  frame -> {png}")
        except (OSError, RuntimeError) as exc:
            log(f"  screendump {name} failed: {exc}")

    ok = False
    try:
        log(f"waiting {args.login_delay}s for the login window")
        time.sleep(args.login_delay)
        shot("01-login-window")

        log("=== logging in ===")
        import ssh as sshmod
        sshmod.unlock(guest)
        time.sleep(10)
        shot("02-after-unlock")

        sshmod.dismiss_keyboard_assistant(guest)
        time.sleep(5)
        shot("03-after-keyboard")

        log("=== opening System Settings ===")
        q = qmpmod.QMP(guest.paths.drive)
        try:
            qmpmod.key(q, "esc")
            time.sleep(1)
            qmpmod.key_combo(q, "meta_l", "spc")
            time.sleep(3)
            keys.type_into(q, "system settings")
            time.sleep(8)
            shot("04-spotlight")
            qmpmod.key(q, "ret")
            time.sleep(12)
            shot("05-settings-open")
        finally:
            q.close()

        log("=== the Settings window should be up; leaving it for the frames ===")
        time.sleep(30)
        shot("06-settings-idle")
        ok = True
    finally:
        guest.stop(acpi_first=ok)
        snap.terminate()
        try:
            snap.wait(timeout=5)
        except subprocess.TimeoutExpired:
            snap.kill()
        log(f"frames in {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
