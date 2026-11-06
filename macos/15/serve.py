"""Boot a finished macOS disk and keep it running, with a VNC screen and ssh forwarded.

    python3 macos/15/serve.py --disk /disks/macos15-desktop.qcow2 --overlay

WHAT THIS IS

A long-running counterpart to the stages. Every other script in this directory does one job to
a disk and exits; this one boots the disk, publishes its screen on a VNC port and its ssh on
another, and stays up so a person (or the next experiment) can use the machine.

WHY IT IS NOT A STAGE. It publishes nothing and asserts nothing. It is the "start it and look
at it" path, which is what a 27 GiB disk on a local machine is for: an experiment that used to
cost a full CI run now costs a boot.

THE DISK IS READ-ONLY. --overlay makes a qcow2 whose backing file is the published disk, so
qemu reads through to it and writes only the overlay's own clusters. The base is opened
read-only by qemu while a backing file is in use, and the overlay is deleted on exit - every
run starts from the same bytes. Without --overlay the disk is used in place and IS modified.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, HERE)


def log(message: str) -> None:
    print(f"[serve {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True)
    p.add_argument("--overlay", action="store_true",
                   help="run on a throwaway overlay so the base disk is never written")
    p.add_argument("--work", default="/work/serve")
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--vnc-port", type=int, default=5900)
    p.add_argument("--release", default=os.environ.get("MACOS_RELEASE", "15"))
    p.add_argument("--tap", default=os.environ.get("MACOS_TAP", ""),
                   help="a bridged tap interface to attach instead of qemu's user-mode NAT. "
                        "The guest then has a real address and a real route out, which "
                        "Setup Assistant needs on macOS 26 to reach Apple's update API. "
                        "See Shape.tap_ifname.")
    p.add_argument("--tap-mac", default=os.environ.get("MACOS_TAP_MAC", ""),
                   help="the MAC dnsmasq is configured to hand 172.30.0.2 to")
    p.add_argument("--capture", default=None,
                   help="a directory to write one frame per second into")
    args = p.parse_args(argv)

    if not os.path.isfile(args.disk):
        raise SystemExit(f"no disk at {args.disk}")
    os.makedirs(args.work, exist_ok=True)

    import machine
    import macos_recovery
    import opencore
    import vm

    log(f"disk {args.disk} ({os.path.getsize(args.disk) >> 20} MiB)")

    disk = args.disk
    run = None
    if args.overlay:
        import overlay as overlaymod

        run = overlaymod.Overlay(args.disk, args.work, name="serve-overlay.qcow2")
        disk = run.open()

    log("=== recovery image ===")
    recovery = macos_recovery.fetch_published(args.release, args.work)
    log(f"  {recovery} ({os.path.getsize(recovery) >> 20} MiB)")

    log("=== OpenCore, firmware, APFS driver ===")
    assets = opencore.build(args.release, args.work)
    log(f"  identity {assets['identity']['serial']} / {assets['identity']['mac']}")

    shape = machine.shape(assets, cpus=args.cpus, mem_mb=args.mem)
    shape.ssh_port = args.ssh_port
    # THE SCREEN, ON EVERY INTERFACE: the VNC port is how a human looks at this machine, and
    # 127.0.0.1 inside a container is not reachable from outside it.
    shape.vnc_bind = "0.0.0.0"
    shape.vnc_display = 0
    # A TAP INSTEAD OF SLIRP, WHEN ONE IS NAMED. The interface is created, bridged and NATed
    # outside this process (see Shape.tap_ifname for the four commands), because those need
    # NET_ADMIN and belong to whoever prepared the machine.
    if args.tap:
        shape.tap_ifname = args.tap
        shape.tap_mac = args.tap_mac
        log(f"net    tap {args.tap} (bridged, NATed) mac={args.tap_mac or 'auto'}")
    # AND NO CONSOLE ON STDIO, BECAUSE THERE IS NO TTY.
    #
    # The default is `-serial mon:stdio`, which puts the guest's serial port on qemu's OWN
    # stdin/stdout. That is right for a bake, where the log is the point and the process has a
    # terminal. It is fatal for a long-running container: Kubernetes gives the process no tty,
    # so qemu's first write to that console hits a closed descriptor and the process dies -
    # measured, twice, as a pod that logged
    #
    #     [vm] qemu pid 18
    #     [serve] the machine is up
    #
    # at 02:54:41 and exited 1 at 02:54:43, two seconds later, with the whole boot still ahead
    # of it.
    #
    # A pipe chardev instead: the console goes to a file in `work`, which is also better -
    # `console.log` survives a restart and can be read while the machine runs, where stdio
    # only reaches the container log after the process ends.
    #
    # lib/serial.Serial OPENS BOTH SIDES. `-chardev pipe` is two fifos and qemu writes the
    # reader side; a fifo with no reader fills its 64 KiB buffer and then qemu BLOCKS, which
    # froze a NixOS install for forty-four minutes in run 41592290525. Serial().open() starts
    # the thread that drains it into console.log, so the machine can talk as much as it likes.
    import serial as serialmod

    console = serialmod.Serial(args.work, name="console", sink=os.path.join(args.work, "console.log"))
    shape.serial_pipe = console.stdin.removesuffix(".in")

    guest = vm.Guest(
        args.work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            # THE CLEANING DRIVE, WITH discard: this machine is meant to be used, and a disk
            # that can hand blocks back is the point of the whole exercise.
            "-drive", machine.TARGET_DRIVE.format(disk=os.path.abspath(disk)),
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
            "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
        ],
        shape=shape,
        stdout=os.path.join(args.work, "console.log"),
    ).start()

    # THE FIFOS ARE OPENED AFTER QEMU, WHICH IS THE ONLY ORDER THAT DOES NOT DEADLOCK.
    # `Serial.open()` blocks until qemu opens the read side of console.in, and qemu is the
    # process that does it - so opening first waits for a process that has not started, and
    # the 60-second timeout then reports "qemu never opened console.in". Starting qemu first
    # means the fifo already has its writer when open() runs, and the drain thread attaches
    # to console.out immediately afterwards.
    console.open(timeout=60)

    log(f"ssh  localhost:{args.ssh_port} -> guest:22")
    log(f"vnc   :{args.vnc_port}  (display :0)")
    log("the machine is up; Ctrl-C to stop")

    capture = None
    if args.capture:
        frames = os.path.abspath(args.capture)
        os.makedirs(frames, exist_ok=True)
        capture = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
             guest.paths.record, frames, "1"],
            stdout=open(os.path.join(frames, "snapshots.log"), "wb"),
            stderr=subprocess.STDOUT,
        )
        log(f"frames -> {frames}")

    stopping = {"now": False}

    def stop(signum, _frame):
        log(f"signal {signum}; shutting down")
        stopping["now"] = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)

    rc = 0
    try:
        while not stopping["now"]:
            if not guest.alive():
                log("qemu exited on its own")
                rc = 1
                break
            time.sleep(2)
    finally:
        guest.stop(acpi_first=True, acpi_timeout=180)
        if capture is not None:
            capture.terminate()
            try:
                capture.wait(timeout=5)
            except subprocess.TimeoutExpired:
                capture.kill()
        try:
            console.close()
        except Exception:                     # noqa: BLE001 - the console is not the verdict
            pass
        if run is not None:
            run.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
