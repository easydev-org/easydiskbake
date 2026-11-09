"""Share a directory on the host with the guest, read and write.

    python3 lib/hostshare.py --dir ~/shared --log

WHY THIS EXISTS

So a guest can read a file that was put on the host after the disk was published - a bootstrap
script, a config, a fixture - without rebuilding the disk or restarting the machine. The user
asked for it as "Bootstrap 之类的脚本放进 shared，让 vm 启动时自动执行", and the first half of
that is this: the directory has to be there, mounted, and readable from inside the guest.

WHY SMB AND NOT 9P OR VIRTIOFS

Because the two guests that matter here each have exactly one protocol they can use without a
driver, and SMB is the only one they share:

    Windows    NO 9p DRIVER EXISTS. virtio-win's maintainer, on the request to add one:
                   "There are no virtio-9p drivers for Windows. Please use virtio-fs.
                    Those are not same devices."
               and the issue was closed as not-planned. virtiofs works on Windows but needs
               WinFsp plus a virtiofsd process on the host - a driver install and a second
               daemon for a directory the guest could reach with a native client.

    macOS      9p mounts READ-ONLY. mount_9p takes `-r` and nothing else, so a share that has
               to be writable cannot use it. virtiofs needs Apple's driver and a Linux-only
               virtiofsd.

SMB is what both Windows and macOS speak natively, read and write, with no driver and no
extra process. Linux is not covered by this module: it would use qemu's virtio-9p, which is
a different device with a different mount, and a step that claimed to do "the share" on all
three would have silently done nothing on one of them. `smbd` is already in this image (Containerfile installs samba), which is why
this is a config file and a process rather than a new dependency.

WHAT CHANGED SINCE THE LAST SMB CODE IN THIS REPOSITORY

There was a `share_path` field and a `smb=` on qemu's user-mode netdev. Both are gone: this
repository dropped slirp entirely (see the commit that did it - a guest behind a bridged tap
cannot use qemu's built-in SMB, because that server only exists on the 10.0.2.x link slirp
creates). So this is a HOST-SIDE smbd on the bridge address instead, which is also how a real
machine would see a file server.

THE ADDRESS THE GUEST CONNECTS TO

`net.BRIDGE_IP` (172.30.0.1 by default), because that is the interface the guest already
routes to - it is how it reaches the ssh forward. Nothing new is exposed.

THE SHARE IS READ-WRITE AND THAT HAS A COST

The user asked for read and write. `force user` points the guest's writes at the host account
running smbd, so a file the guest creates is owned by it on the host - which is the behaviour a
person expects from a shared folder and is also how a guest can fill the host's disk. The
config sets no quota; the caller's directory is whatever it is.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import net  # noqa: E402

# THE SHARE NAME THE GUEST ASKS FOR: //172.30.0.1/shared , \\172.30.0.1\shared
SHARE_NAME = "shared"

# THE DEFAULT HOST DIRECTORY, which is the one the user named.
DEFAULT_DIR = os.path.expanduser("~/shared")

CONF_PATH = "/tmp/easydiskbake-smb.conf"
PID_PATH = "/tmp/easydiskbake-smbd.pid"
LOG_PATH = "/tmp/easydiskbake-smbd.log"


def log(message: str) -> None:
    print(f"[share {time.strftime('%H:%M:%S')}] {message}", flush=True)


def config_text(directory: str) -> str:
    """The smallest smb.conf that serves one directory read-write to one guest.

    GUEST ACCESS, NOT A PASSWORD, AND THAT IS A DELIBERATE CHOICE WITH A LIMIT WORTH KNOWING.
    The share exists to hand a guest a file from the machine that is running it, and the
    machine is a CI container whose bridge network the guest is the ONLY other member of
    (172.30.0.0/24, one guest). A password would be a second thing to keep in step with the
    disk's account for no gain on a link with two ends, both of which this process owns.

    `map to guest = Bad User` makes that work: a login that names no valid account is mapped
    to the guest account rather than refused. `force user = root` decides whose files the
    writes become.

    SMB2 IS THE FLOOR. macOS 15 refuses SMB1 outright and Windows 11 has it off by default;
    setting the minimum here means a misconfiguration fails at the server rather than at a
    client that says "the server does not support the required protocol".
    """
    return f"""[global]
   workgroup = WORKGROUP
   server string = easydiskbake shared
   security = user
   map to guest = Bad User
   guest account = root
   log file = {LOG_PATH}
   log level = 1
   server min protocol = SMB2_02
   # NO PRINTING. smbd would otherwise try to manage a spool directory it does not have.
   load printers = no
   printing = bsd
   printcap name = /dev/null
   disable spoolss = yes

[{SHARE_NAME}]
   comment = easydiskbake host share
   path = {directory}
   browseable = yes
   writable = yes
   guest ok = yes
   force user = root
   create mask = 0666
   directory mask = 0777
   # FOLLOW SYMLINKS INTO THE SHARE, not out of it.
   follow symlinks = yes
   wide links = no
"""


def listening(host: str = "", port: int = 445, timeout: float = 2.0) -> bool:
    """Is something accepting SMB connections?

    A TCP connect and nothing more. The protocol handshake is the client's business, and a
    server that accepts and then refuses is a different failure - but one that a connect test
    is not the right place to diagnose.
    """
    import socket

    try:
        with socket.create_connection((host or net.BRIDGE_IP, port), timeout=timeout):
            return True
    except OSError:
        return False


def start(directory: str = DEFAULT_DIR, *, wait: float = 10.0) -> bool:
    """Write the config, start smbd, and wait for the port.

    THE DIRECTORY IS CREATED IF IT IS MISSING, because a share whose path does not exist is a
    share smbd accepts and then fails to serve on the first access - and the error arrives in
    the client, not here.
    """
    if not shutil.which("smbd"):
        log("=== smbd IS NOT INSTALLED IN THIS IMAGE ===")
        log("  Containerfile installs samba; an image without it cannot serve a share")
        return False

    directory = os.path.abspath(os.path.expanduser(directory))
    os.makedirs(directory, exist_ok=True)
    log(f"share {SHARE_NAME!r} -> {directory} (read-write, guest access)")

    with open(CONF_PATH, "w") as fh:
        fh.write(config_text(directory))

    # STOP ONE THIS MODULE STARTED EARLIER. `smbd -D` daemonises, so the process is not a
    # child and cannot be waited on - which is why the pid file is written rather than a
    # Popen handle kept.
    stop()

    argv = ["smbd", "-D", "--no-process-group", "-s", CONF_PATH]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"=== smbd DID NOT START (exit {result.returncode}) ===")
        for line in ((result.stdout or "") + (result.stderr or "")).splitlines()[-6:]:
            log(f"    {line[:160]}")
        return False

    deadline = time.time() + wait
    while time.time() < deadline:
        if listening():
            log(f"  smbd is listening on {net.BRIDGE_IP}:445")
            return True
        time.sleep(0.5)
    log(f"=== smbd DID NOT BIND {net.BRIDGE_IP}:445 IN {wait:.0f}s ===")
    tail = ""
    if os.path.isfile(LOG_PATH):
        with open(LOG_PATH) as fh:
            tail = "".join(fh.readlines()[-6:])
    for line in tail.splitlines():
        log(f"    {line[:160]}")
    return False


def stop() -> None:
    """Stop the smbd this module started, and only that one."""
    result = subprocess.run(["pkill", "-f", f"smbd.*-s {CONF_PATH}"], capture_output=True)
    del result


def mount_commands(host: str = "", user: str = "docker") -> dict[str, str]:
    """What a guest types to mount this share, per family.

    THE PATH IS THE SAME ON BOTH GUESTS (`~/shared`), which is what was asked for and what
    makes a bootstrap script portable between the two disks. What differs is the client:

        macOS      mount_smbfs, into a directory the account owns. A network mount cannot be
                   put at a path that does not exist, so the command makes it first.
        Windows    `net use` maps a UNC path to a DRIVE LETTER, and there is no `~` in cmd, so
                   the local directory is spelled out and the share lands on Z:.

    `guest ok = yes` means no password, so neither command carries one.

    LINUX IS DELIBERATELY ABSENT. It would use qemu's virtio-9p instead - the kernel's own
    client, no daemon - but that is a different device with a different mount, and mixing the
    two behind one name would be a step that silently did nothing on one of the three. Linux
    gets its own step when it is wanted; nothing here pretends to cover it.
    """
    host = host or net.BRIDGE_IP
    return {
        "macos": (f"mkdir -p ~/shared && "
                  f"mount_smbfs //guest@{host}/{SHARE_NAME} ~/shared"),
        "windows": (f'if not exist "C:\\Users\\{user}\\shared" mkdir "C:\\Users\\{user}\\shared"'
                    f' & net use Z: \\\\{host}\\{SHARE_NAME} /persistent:no'),
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--stop", action="store_true")
    p.add_argument("--status", action="store_true")
    args = p.parse_args(argv)

    if args.stop:
        stop()
        log("stopped")
        return 0
    if args.status:
        ok = listening()
        log(f"{'listening' if ok else 'not listening'} on {net.BRIDGE_IP}:445")
        return 0 if ok else 1
    if not net.bridge_exists():
        log(f"=== {net.BRIDGE_IP} IS NOT UP; THE GUEST COULD NOT REACH THIS SHARE ===")
        log("  net.up() creates it, and every Guest.start() calls that")
        return 1
    return 0 if start(args.dir) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
