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
import shutil
import subprocess
import sys
import threading
import time

# THE REPOSITORY'S OWN NETWORK, NOT SLIRP.
#
# Every other guest here runs on a tap bridged to lib/net.py: a real interface with dnsmasq
# for DHCP and DNS, MASQUERADE for outbound, and iptables DNAT for the port forwards. The
# reason is in that module's docstring and it applies here too - a slirp guest's packets are
# re-issued by the qemu process, which does not carry the container's proxy or routing, and
# a guest that needs to reach anything is a guest that hangs.
#
# Android was started on slirp by the emulator's default and got NO NETWORK AT ALL:
#
#     lo 127.0.0.1/8
#     dummy0, ifb0, ifb1, tunl0, gre0 ...     all DOWN
#     ping: connect: Network is unreachable
#
# virtio_net was loaded and never bound to a PCI device, because no -netdev was passed.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "lib"))
try:
    import net as netlib  # noqa: E402
except ImportError:
    from lib import net as netlib  # noqa: E402

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
#
# THE TIMES ARE FRACTIONS OF THE WINDOW, NOT SECONDS.
#
# They were seconds, calibrated for a 240 s run, and `--seconds` only changed how long the
# emulator was left alive - so `--seconds 420` still asked for the screenshot at 206 s and
# the extra time bought nothing. A restored /data needs longer to finish booting, and
# run 42475590459 screenshotted a guest still in apexd-bootstrap because of exactly this.
#
# A fraction says "at 60% of whatever window this run has", which is the thing that was
# meant. The probes are the ones that have to stay late; the early ones only need to be
# clear of init's own output.
SCRIPT = [
    # PRINTK OFF THIS TTY FIRST. console=ttyS0 with printk.devkmsg=on interleaves every
    # kernel line into the shell's output; four numbers are console, default, minimum and
    # boot-time default loglevel.
    (0.02, "su 0 sh -c 'echo 1 4 1 7 > /proc/sys/kernel/printk'\n"),
    (0.25, "su 0 sh -c \"logcat -b crash -d > /metadata/crash.txt\"\n"),
    # THE ANSWERS GO INTO FILES, AND THE FILES ARE READ OFF THE DISK.
    #
    # THE CONSOLE IS NARROWER THAN THE COMMANDS AND TRUNCATES THEM. `su 0 sh -c "getprop
    # init.svc.surfaceflinger > /metadata/sf.txt"` is 66 characters and what arrived was
    #
    #     u 0 sh -c "getprop init.svc.surfaceflinger > /metadata/sf.txt"
    #
    # - the leading `s` eaten by the tty's redraw - so the shell ran nothing at all and
    # `cat` then reported a file that was never written. Every command below is kept under
    # 60 characters, and short names are used for the same reason.
    #
    # WHY NOT JUST `cat` THEM BACK: the reply is wrapped too (`running` came back as
    # `nning`), so parsing it is guesswork. /metadata is a partition of cache.img, and the
    # host already reads the screenshot out of it with debugfs - the same route, applied to
    # two short text files, is exact regardless of how the console decides to draw.
    (0.60, "su 0 sh -c \"getprop sys.boot_completed>/metadata/b\"\n"),
    (0.64, "su 0 sh -c \"getprop init.svc.surfaceflinger>/metadata/s\"\n"),
    (0.68, "su 0 sh -c \"sync\"\n"),
    # A PICTURE THROUGH THE WHOLE GRAPHICS PATH.
    (0.82, "su 0 screencap -p /metadata/shot.png\n"),
    (0.88, "su 0 sh -c \"sync; ls -l /metadata/shot.png\"\n"),
    (0.94, "su 0 dumpsys window displays\n"),
]


def qemu_net_args(tap: str) -> list[str]:
    """The qemu-side tap arguments, or nothing when no tap was asked for.

    THESE ARE lib/vm.py's, COPIED RATHER THAN SHARED. That module builds its argv for a
    Guest object it owns; this one drives the emulator, whose argument handling differs in
    one important way - anything after `-qemu` goes to qemu untouched, and that is the only
    route to `-netdev tap`. Sharing the code would mean sharing the shape, and the shapes
    are not the same.

    The MAC and the MTU match lib/net.py's: 52:54:00:56:4d:01 is what the rest of this
    repository hands its guests, and the MTU is the bridge's, so the guest never sends a
    frame larger than the path it leaves on.
    """
    if not tap:
        return []
    mtu = netlib.MTU
    return [
        "-netdev", f"tap,id=n0,ifname={tap},script=no,downscript=no",
        "-device", f"virtio-net-pci,netdev=n0,mac=52:54:00:56:4d:01,"
                   f"romfile=,host_mtu={mtu}",
    ]


def drive(proc: subprocess.Popen, seconds: int) -> None:
    """Type each command when `fraction * seconds` has elapsed.

    A FRACTION, NOT A SECOND. These were absolute, calibrated for a 240 s run, and
    `--seconds` only changed how long the emulator stayed alive - so a 420 s run still asked
    for the screenshot at 206 s and the extra time bought nothing. Run 42475590459
    screenshotted a guest still in apexd-bootstrap for exactly this reason: it had a
    carried-over /data to decrypt and the deadline did not move with the window.
    """
    started = time.time()
    budget = max(seconds, 60)
    for fraction, line in SCRIPT:
        when = fraction * budget
        while time.time() - started < when:
            time.sleep(0.5)
        try:
            proc.stdin.write(line.encode())
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return


def read_metadata(work: str, evidence: str) -> dict[str, str]:
    """Read files out of the guest's /metadata and return {name: content}.

    THE PNG IS WRITTEN TO `evidence`; THE TEXT FILES COME BACK AS STRINGS. The two are
    different on purpose: the picture is the artifact a human looks at, and the prop values
    are what the verdict is computed from.

    THREE STEPS, AND EACH ONE IS A PLACE A PREVIOUS ATTEMPT WENT WRONG:

      merge   qemu-img convert merges cache.img + cache.img.qcow2 into one raw file.
              Reading cache.img directly shows the PRE-BOOT bytes, where /metadata holds
              nothing but a fresh ext4, so a file the guest did write looks absent rather
              than new.

      carve   the metadata partition is found by NAME in the merged disk's GPT, not at an
              offset written down here. The layout has already changed once - system moved
              from the fifth slot to the first - and a hardcoded sector number would have
              kept reading the wrong partition without complaint.

      dump    debugfs, because /metadata is ext4 and debugfs reads it without mounting.
              The container has no privileges to mount anything.

    debugfs ANSWERS 0 EVEN WHEN THE FILE IS NOT THERE, so every result is verified by
    looking at what arrived: a PNG by its magic, a text file by its length.
    """
    out: dict[str, str] = {}
    merged = os.path.join(work, "cache-merged.img")
    part = os.path.join(work, "metadata-part.img")

    # THE EMULATOR'S OWN qemu-img. The SDK puts a 2.12 build in /opt/android-sdk/emulator
    # ahead of /usr/bin, and it cannot read an image the newer build wrote - which is the
    # same trap lib/compress.py documents.
    qemu_img = "/opt/android-sdk/emulator/qemu-img"
    overlay = os.path.join(work, "cache.img.qcow2")
    if not os.path.isfile(qemu_img):
        print(f"[run_qemu] {qemu_img} is missing; cannot merge the overlay")
        return out
    if not os.path.isfile(overlay):
        print(f"[run_qemu] no overlay at {overlay}; the guest wrote nothing")
        return out

    result = subprocess.run([qemu_img, "convert", "-O", "raw", overlay, merged],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[run_qemu] qemu-img convert failed: {result.stderr[:300]}")
        return out

    first, last = _partition(merged, "metadata")
    if first is None:
        print("[run_qemu] the merged disk has no `metadata` partition")
        return out
    with open(merged, "rb") as src, open(part, "wb") as dst:
        src.seek(first * 512)
        remaining = (last - first + 1) * 512
        while remaining > 0:
            chunk = src.read(min(8 * 2 ** 20, remaining))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)

    for name in ("b", "s"):
        result = subprocess.run(["debugfs", "-R", f"cat /{name}", part],
                                capture_output=True, text=True)
        text = (result.stdout or "").strip()
        if text:
            out[name] = text

    scratch = os.path.join(work, "shot.png")
    subprocess.run(["debugfs", "-R", f"dump /shot.png {scratch}", part],
                   capture_output=True, text=True)
    if os.path.isfile(scratch) and os.path.getsize(scratch) > 1024:
        with open(scratch, "rb") as fh:
            header = fh.read(8)
        if header == b"\x89PNG\r\n\x1a\n":
            shutil.move(scratch, evidence)
            out["shot.png"] = str(os.path.getsize(evidence))
        else:
            os.unlink(scratch)
            print(f"[run_qemu] the exported file is not a PNG: {header.hex()}")
    else:
        print(f"[run_qemu] debugfs found no shot.png: "
              f"{(result.stderr or result.stdout)[:200]}")

    return out


def _partition(disk: str, name: str) -> tuple[int | None, int | None]:
    """(first LBA, last LBA) of a GPT partition, by its name.

    BY NAME BECAUSE THE LAYOUT HAS MOVED. system was the fifth partition when this was
    first written and the first one after the fix that made by-name resolve; a hardcoded
    . a sector number would have kept reading after a change that is deliberate. The GPT
    array starts at LBA 2 and holds 128 entries of 128 bytes.
    """
    with open(disk, "rb") as fh:
        fh.seek(512)
        header = fh.read(92)
        if header[0:8] != b"EFI PART":
            return None, None
        entries_lba = int.from_bytes(header[72:80], "little")
        count = int.from_bytes(header[80:84], "little")
        fh.seek(entries_lba * 512)
        array = fh.read(count * 128)

    for i in range(count):
        entry = array[i * 128:(i + 1) * 128]
        if not any(entry):
            continue
        if entry[56:128].decode("utf-16-le", "ignore").rstrip("\x00") != name:
            continue
        return (int.from_bytes(entry[32:40], "little"),
                int.from_bytes(entry[40:48], "little"))
    return None, None


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dir", default="/a/android",
                   help="the directory mkdisks.py wrote its images into")
    p.add_argument("--emulator", default="/opt/android-sdk/emulator/emulator")
    p.add_argument("--seconds", type=int, default=260)
    p.add_argument("--evidence", default="",
                   help="write the guest's screenshot to this path, for upload")
    p.add_argument("--tap", default="",
                   help="a tap interface from lib/net.py; empty for the emulator's default "
                        "(a userspace NAT that gives the guest no reachable network)")
    p.add_argument("--ssh-port", type=int, default=2222,
                   help="host port forwarded to the guest's ssh port")
    a = p.parse_args(argv)
    D = os.path.abspath(a.dir)

    # THE LINK FIRST, BECAUSE THE EMULATOR OPENS IT BY NAME.
    #
    # lib/net.py creates the bridge, the tap, dnsmasq and the DNAT rules, and is idempotent -
    # a second call finds what is already there. The forward is 2222 -> the guest's ssh port,
    # which is what every other system in this repository uses and what a caller expects.
    if a.tap:
        ok, why = netlib.available()
        if not ok:
            raise SystemExit(
                f"=== NO BRIDGED NETWORK AVAILABLE ===\n  {why}\n"
                f"  A container needs NET_ADMIN and /dev/net/tun. See lib/net.py.")
        netlib.up(ssh_port=a.ssh_port, forwards={a.ssh_port: 22, 8022: 8022})
        print(f"[run_qemu] tap {a.tap}, "
              f"{netlib.FORWARD_HOST}:{a.ssh_port} -> guest:22, "
              f"{netlib.FORWARD_HOST}:8022 -> guest:8022", flush=True)

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
        # `-qemu` HANDS EVERYTHING AFTER IT TO QEMU, and that is where the tap belongs.
        #
        # `-net-tap` is an emulator option and it did not work here: virtio_net.ko loaded
        # and never bound to a PCI device, so the guest had no interface, sent no
        # DHCPDISCOVER and took no lease -
        #
        #     qemubr0 172.30.0.1/24 UP, qemutap0 master qemubr0 UP
        #     dnsmasq running with dhcp-range=172.30.0.2
        #     /var/lib/misc/dnsmasq.leases: (no lease file)
        #
        # The arguments below are the ones every other system in this repository uses
        # (lib/vm.py build_args), verbatim, because they are the ones known to bind:
        #
        #   script=no,downscript=no  there is no /etc/qemu-ifup in a container, and qemu's
        #                            default is to run it - which fails after the guest is
        #                            created rather than before.
        #   romfile= (empty)         suppresses the iPXE option ROM. A value there is read
        #                            as a path to a firmware image.
        #   host_mtu=                the tap is at the container's MTU (1450); virtio-net
        #                            advertises 1500 and the guest then emits frames that
        #                            are silently dropped. This makes the guest learn it.
        "-qemu", "-qmp", f"unix:{os.path.join(D, 'qmp.sock')},server=on,wait=off",
        "-qemu", *qemu_net_args(a.tap),
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

    # THE BOOT STATE IS READ OFF THE DISK, NOT PARSED OUT OF THE CONSOLE.
    #
    # See SCRIPT's comment for why: the tty truncates commands that do not fit and wraps
    # replies (`running` arrives as `nning`). The guest wrote `sys.boot_completed` to
    # /metadata/b and the surfaceflinger state to /metadata/s; both are read back with the
    # same debugfs route the screenshot uses.
    booted = ""
    surface = ""
    shot = ""
    if a.evidence:
        meta = read_metadata(D, a.evidence)
        booted = meta.get("b", "").strip()
        surface = meta.get("s", "").strip()
        shot = meta.get("shot.png", "")
        if shot:
            shot = f"{shot} bytes in /metadata/shot.png"

    print(f"\n[run_qemu] sys.boot_completed       = {booted or '(empty)'}")
    print(f"[run_qemu] init.svc.surfaceflinger  = {surface or '(empty)'}")
    print(f"[run_qemu] screenshot               = {shot or '(none)'}")

    ok = booted == "1" and surface == "running" and bool(shot)

    # THE SCREENSHOT HAS TO LEAVE THE GUEST, OR THE RUN PROVES NOTHING TO ANYONE ELSE.
    #
    # A failure here does not change the verdict on its own - see read_metadata - but it is
    # reported, because a VERDICT of BOOTS with no picture is the exact claim this flow
    # exists to stop making.
    if a.evidence and ok and not os.path.isfile(a.evidence):
        print("[run_qemu] the boot succeeded but the picture could not be read back")
        ok = False

    print("\nVERDICT:", "ANDROID BOOTS" if ok else "see the log")
    if a.tap:
        # THE ADDRESS THE CALLER SHOULD USE. Every client in this repository names the
        # bridge address rather than 127.0.0.1 - a loopback destination cannot be DNATed to
        # a guest, because the rewrite changes the destination and not the source.
        print(f"[run_qemu] reach it at {netlib.FORWARD_HOST}:{a.ssh_port} (ssh) "
              f"or :8022")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
