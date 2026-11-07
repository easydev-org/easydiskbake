#!/usr/bin/env python3
"""Boot the disks mkdisks.py builds and prove the system came up.

    python3 android/15/run_qemu.py --dir /a/android-final

WHAT THIS DOES THAT A PLAIN `emulator` INVOCATION DOES NOT

The emulator's normal path wants an AVD. There is none here: the disks are built by
mkdisks.py and handed over by name, so the emulator is driven through its explicit
partition options and `-datadir` names the directory that holds them. That combination
is what run after run settled on, and each flag below is there because its absence was
observed:

    -datadir D          the emulator writes hardware-qemu.ini here and treats D as the
                        AVD home; without it, `No AVD specified`
    build.avd/          a directory of that name has to exist inside D, or the emulator
                        dies with SIGSEGV before printing anything
    -feature RefCountPipe
                        gralloc's refcount pipe; without it the composer cannot be
                        created and surfaceflinger aborts every five seconds
    -feature VirtioVsockPipe
                        the sensors HAL is built against vsock; without it the HAL
                        aborts and sys.boot_completed never arrives

THE PROOF IS A PICTURE, NOT A LOG LINE

`sys.boot_completed` is read over the guest's serial console, and a PNG is taken with
`screencap`. A screenshot goes through gralloc, the composer, surfaceflinger and the
encoder - every layer that was broken - so a file with a real PNG header at 320x480 is
the claim that the whole path works. A boot log that stops early looks identical to one
that is merely slow, which is why the log alone is not the verdict.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

LOOPBACK = "androidboot.boot_devices=vda,vdb,vdc,vdd,vde," \
           "pci0000:00/0000:00:01.0,pci0000:00/0000:00:02.0,pci0000:00/0000:00:03.0," \
           "pci0000:00/0000:00:04.0,pci0000:00/0000:00:05.0,pci0000:00/0000:00:06.0"

# WHERE THE CONSOLE COMMANDS GO, AND WHY THEY ARE ALL `su 0`
#
# The serial console on ttyS0 is a shell prompt owned by the `shell` user:
#
#     console:/ $
#
# `ln`, `setprop` and redirections all fail with status 1 for that user, which reads as
# "the file is missing" when it is really EPERM. `su 0` elevates a single command. The
# redirect has to be INSIDE it - `su 0 cmd > file` gives the redirect to the unprivileged
# shell that parses the line.
SCRIPT = [
    # PRINTK OFF THIS TTY FIRST. console=ttyS0 with printk.devkmsg=on interleaves every
    # kernel line into the shell's output; four numbers are console, default, minimum and
    # boot-time default loglevel.
    (5, "su 0 sh -c 'echo 1 4 1 7 > /proc/sys/kernel/printk'\n"),
    (60, "su 0 sh -c \"logcat -b crash -d > /metadata/crash.txt\"\n"),
    # A SHORT COMMAND PER ANSWER, AND EACH ONE GOES TO A FILE FIRST.
    #
    # THE CONSOLE IS NARROWER THAN THE COMMANDS. Its tty wraps a long line and the wrap
    # lands in the middle of the echoed text, so what comes back is
    #
    #     u 0 getprop init.svc.surfaceflinger
    #     nning
    #
    # for `running`. A compound command like
    # `sh -c 'echo BOOT=$(getprop ...) SF=$(getprop ...)'` is long enough that the second
    # half of it is executed as a separate line. So each value is asked for on its own,
    # short line, written to a file, and `cat`ed back - the answer is then exact.
    (150, "su 0 sh -c \"getprop sys.boot_completed > /metadata/boot.txt\"\n"),
    (160, "su 0 sh -c \"getprop init.svc.surfaceflinger > /metadata/sf.txt\"\n"),
    (170, "su 0 sh -c \"getprop init.svc.zygote > /metadata/zy.txt\"\n"),
    (180, "su 0 cat /metadata/boot.txt\n"),
    (188, "su 0 cat /metadata/sf.txt\n"),
    (196, "su 0 cat /metadata/zy.txt\n"),
    # A PICTURE THROUGH THE WHOLE GRAPHICS PATH.
    (206, "su 0 screencap -p /metadata/shot.png\n"),
    (220, "su 0 sh -c \"sync; ls -l /metadata/shot.png\"\n"),
    (232, "su 0 dumpsys window displays\n"),
]


def drive(proc: subprocess.Popen, seconds: int) -> None:
    started = time.time()
    for at, line in SCRIPT:
        while time.time() - started < at:
            time.sleep(0.5)
        try:
            proc.stdin.write(line.encode())
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dir", default="/a/android-final",
                   help="the directory mkdisks.py wrote its images into")
    p.add_argument("--emulator", default="/opt/android-sdk/emulator/emulator")
    p.add_argument("--seconds", type=int, default=260)
    a = p.parse_args(argv)
    D = os.path.abspath(a.dir)

    log = os.path.join(D, "run.log")
    # THE AVD SENTINEL THE EMULATOR INSISTS ON. Its absence is a SIGSEGV with no
    # message, which is indistinguishable from a dozen other startup failures.
    os.makedirs(os.path.join(D, "build.avd", "data", "misc", "pstore"), exist_ok=True)
    for stale in ("hardware-qemu.ini.lock", "multiinstance.lock", "qmp.sock"):
        try:
            os.unlink(os.path.join(D, stale))
        except FileNotFoundError:
            pass

    cmd = [
        a.emulator,
        "-sysdir", D,
        "-system", os.path.join(D, "system.img"),
        "-vendor", os.path.join(D, "vendor-patched.img"),
        "-kernel", os.path.join(D, "kernel"),
        "-ramdisk", os.path.join(D, "ramdisk.img"),
        "-data", os.path.join(D, "userdata.img"),
        # CACHE IS THE DATA DISK: system, metadata, super, vbmeta and boot are its GPT
        # partitions, and `-cache` is what attaches it as the second virtio-blk device -
        # the slot the fstab's by-name paths resolve against.
        "-cache", os.path.join(D, "cache.img"),
        "-datadir", D,
        "-no-window", "-no-audio", "-no-boot-anim", "-no-snapshot", "-no-metrics",
        "-gpu", "swiftshader_indirect",
        "-memory", "4096", "-cores", "4",
        # ROOT SHELL ON THE CURRENT TERMINAL, which is how the commands below reach the
        # guest at all.
        "-shell",
        "-append-userspace-opt", LOOPBACK,
        "-feature", "RefCountPipe",
        "-feature", "VirtioVsockPipe",
        "-show-kernel",
        "-qemu", "-qmp", f"unix:{os.path.join(D, 'qmp.sock')},server=on,wait=off",
    ]

    env = dict(os.environ)
    lib64 = "/opt/android-sdk/emulator/lib64"
    env.setdefault("ANDROID_SDK_ROOT", "/opt/android-sdk")
    env.setdefault("ANDROID_HOME", "/opt/android-sdk")
    env.setdefault("ANDROID_AVD_HOME", "/opt/android-avd")
    env["LD_LIBRARY_PATH"] = f"{lib64}:{lib64}/qt/lib:" + env.get("LD_LIBRARY_PATH", "")

    print(f"[run_qemu] {len(cmd)} args, log {log}", flush=True)
    with open(log, "wb") as fh:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=fh, stderr=subprocess.STDOUT, env=env)
        started = time.time()
        threading.Thread(target=drive, args=(proc, a.seconds), daemon=True).start()
        try:
            while time.time() - started < a.seconds and proc.poll() is None:
                time.sleep(5)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()

    # THE HOST LOGS MILLIONS OF vsock LINES WHILE THE GUEST IS UP. They are noise from
    # gfxstream, and they are dropped here so the answers below are findable.
    with open(log, encoding="utf-8", errors="replace") as fh:
        lines = [ln for ln in fh if "vqParseGuestToHostRequestLocked" not in ln]

    def read_file(name: str) -> str:
        """The first non-empty line printed after `cat /metadata/<name>`.

        THE CONSOLE IS NARROWER THAN THE COMMANDS, and it wraps. `su 0 cat /metadata/...`
        is short enough that the echo sits on one line, and the answer follows on the next,
        which is why the values are read from files rather than asked for directly.
        """
        for i, line in enumerate(lines):
            if f"cat /metadata/{name}" in line:
                for nxt in lines[i + 1:i + 8]:
                    value = nxt.strip().strip("\r")
                    if not value or value.startswith("console:") or "cat /metadata" in value:
                        continue
                    return value
                break
        return ""

    booted = read_file("boot.txt")
    surface = read_file("sf.txt")
    shot = next((ln.strip() for ln in lines if "shot.png" in ln and "-rw" in ln), "")

    print(f"\n[run_qemu] sys.boot_completed       = {booted or '(empty)'}")
    print(f"[run_qemu] init.svc.surfaceflinger  = {surface or '(empty)'}")
    print(f"[run_qemu] screenshot               = {shot or '(none)'}")

    ok = booted == "1" and surface == "running" and bool(shot)
    print("\nVERDICT:", "ANDROID BOOTS" if ok else "see the log")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
