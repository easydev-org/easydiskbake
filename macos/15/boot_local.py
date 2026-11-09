"""Boot an EXISTING macOS disk on this pod, and stay out of the way.

    python3 macos/15/boot_local.py --disk /work/probe.qcow2 --work /work/run-desktop

WHY THIS IS A SCRIPT AND NOT A SHELL ONE-LINER

A pod's container command is one `sh -c` string. Three attempts to launch qemu from a heredoc
inside it died the same way: the launching shell exits, the process it backgrounded goes with
it, and the only evidence is a stale unix socket from an earlier run. A file in the repository
can be re-run at any time against the same volume, which is what iterating on a walkthrough
needs.

WHAT IT DOES NOT DO

It does not fetch, build or verify anything. The disk, the OpenCore boot image and the firmware
variables are inputs; `macos/15/appliance.py` and `lib/opencore.py` produce them. This starts
the machine those produced and prints the socket the driver will connect to.

THE QMP SOCKET IS CHECKED BEFORE qemu IS LAUNCHED. A leftover socket from a previous run
answers nothing and refuses a connection, which reads as "qemu is broken" rather than "that is
the last run's socket" - see Paths.clear().
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

REPO = os.environ.get("REPO") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for path in (REPO, os.path.join(REPO, "lib"), os.path.join(REPO, "macos", "15")):
    if path not in sys.path:
        sys.path.insert(0, path)


def log(message: str) -> None:
    print(f"[boot {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the qcow2 to boot")
    p.add_argument("--work", required=True, help="where the sockets, firmware vars and log go")
    p.add_argument("--release", default="15")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--mem", type=int, default=8192)
    a = p.parse_args(argv)

    import machine
    import vm

    if not os.path.isfile(a.disk):
        raise SystemExit(f"no disk at {a.disk}")
    os.makedirs(a.work, exist_ok=True)

    # THE THREE FILES opencore.build() LEAVES IN A WORK DIRECTORY. The boot image and the
    # firmware vars are the two the machine cannot start without; macos.rom is read-only pflash.
    assets = {
        "boot": os.path.join(a.work, "boot.img"),
        "rom": os.path.join(a.work, "macos.rom"),
        "vars": os.path.join(a.work, "macos.vars"),
        "identity": {"mac": "52:54:00:56:4d:01"},
    }
    for key in ("boot", "rom", "vars"):
        if not os.path.isfile(assets[key]):
            raise SystemExit(f"no {key} at {assets[key]}; run opencore.build() into --work first")

    shape = machine.shape(assets, cpus=a.cpus, mem_mb=a.mem)
    shape.vnc_bind = "0.0.0.0"

    extra = [
        "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
        "-device", "virtio-blk-pci,drive=oc,bootindex=0",
        # discard=unmap: this boots a FINISHED disk, which is what the flag is for. The
        # installer needs the plain drive - see machine.TARGET_DRIVE_PLAIN.
        "-drive", machine.TARGET_DRIVE.format(disk=a.disk),
        "-device", "virtio-blk-pci,drive=target,bootindex=1",
        "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
    ]

    paths = vm.Paths(a.work)
    paths.clear()          # a stale socket answers nothing; see the module docstring
    args = vm.build_args(a.work, extra, shape)

    console = os.path.join(a.work, "console.log")
    log(f"disk {a.disk} ({os.path.getsize(a.disk) >> 20} MiB)")
    log(f"console {console}")
    with open(console, "ab") as out:
        proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    log(f"qemu pid {proc.pid}")

    # WAIT FOR THE SOCKET, THEN SAY IT IS READY. A caller that starts the driver immediately
    # races qemu's bind; lib/steps.py waits too, and this makes the contract visible.
    deadline = time.time() + 60
    while time.time() < deadline:
        if os.path.exists(paths.drive):
            log(f"qmp {paths.drive}")
            log("RUNNING")
            return 0
        if proc.poll() is not None:
            log(f"=== qemu EXITED with {proc.returncode} before the socket appeared ===")
            with open(console, "rb") as fh:
                tail = fh.read()[-1500:].decode(errors="replace")
            log(tail)
            return 1
        time.sleep(1)
    log("=== qemu is alive but never bound its socket ===")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
