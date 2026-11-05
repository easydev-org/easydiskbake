"""Add the developer toolchain to a macOS disk that already has Remote Login.

    python3 macos/15/dev.py --disk out/macos15-ssh.qcow2 --out out/macos15-dev.qcow2

WHY THIS STAGE USES SSH AND THE EARLIER ONES DID NOT

The ssh stage could not, because sshd was what it was creating. By this point the disk
answers on a forwarded port, so everything below is ordinary remote work: no keyboard, no
coordinates, no frames to read. That is the entire reason the stages are ordered
desktop -> ssh -> dev, and putting dev before ssh would mean driving a package manager
through a GUI.

WHAT IS INSTALLED, AND WHY IT IS THIS SHORT LIST

The same question as the Windows dev stage: what does a GitHub Actions runner need to be
useful? git and the compilers come with the Xcode command line tools, which Homebrew
itself requires, so they are not listed separately. Node and Python are, in the versions
a runner picks up when it looks on PATH.

WHY HOMEBREW AND NOT THE SYSTEM PACKAGES

macOS ships no package manager. Homebrew is what the ecosystem uses, and a disk without
one cannot install anything else later - which makes it the toolchain's prerequisite
rather than one of its entries.

THE ROOT PASSWORD IS NOT SET HERE. sudo works because the desktop stage's account is an
administrator, and this stage authenticates with that account's password over ssh. If
the account were not an admin, `brew install` would fail at the first write to
/usr/local and the log would say so.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from lib import compress, smoke  # noqa: E402

SSH_USER = os.environ.get("MACOS_USER", "docker")
SSH_PASSWORD = os.environ.get("MACOS_PASSWORD", "admin")

# The Homebrew installer is a script fetched over https and run with /bin/bash, which is
# what its own documentation has said for years. It is idempotent: a second run says
# "already installed" and exits 0.
BREW_INSTALL = (
    '/bin/bash -c "$(curl -fsSL '
    'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
)

# HOMEBREW IS NOT ON PATH IN A NON-INTERACTIVE SSH SHELL. Its installer writes to
# /opt/homebrew (arm64) or /usr/local (x86_64) and tells the user to add a shellenv line
# to a login file - which a `ssh host command` shell does not source. So the path is
# prefixed explicitly on every use rather than assumed.
BREW_PATH = '/opt/homebrew/bin:/usr/local/bin:$PATH'

# WHAT A RUNNER NEEDS ON PATH, as (command that proves it, how to install it).
PACKAGES = [
    ("git", "--version", None),
    ("node", "--version", "node"),
    ("python3", "--version", "python"),
    ("cmake", "--version", "cmake"),
    ("jq", "--version", "jq"),
    ("gh", "--version", "gh"),
]


def log(message: str) -> None:
    print(f"[macos-dev {time.strftime('%H:%M:%S')}] {message}", flush=True)


def ssh_base(port: int) -> list[str]:
    """The ssh command, with the two options a first connection always needs.

    StrictHostKeyChecking=no and UserKnownHostsFile=/dev/null because the host key is
    generated fresh by the install and there is no known_hosts entry for it anywhere; the
    alternative is an interactive prompt this has no terminal for.
    """
    return [
        "sshpass", "-p", SSH_PASSWORD, "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-p", str(port), f"{SSH_USER}@127.0.0.1",
    ]


def run(port: int, command: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    log(f"ssh> {command[:110]}")
    return subprocess.run(ssh_base(port) + [command],
                          capture_output=True, text=True, timeout=timeout)


def wait_for_ssh(port: int, seconds: int = 180) -> bool:
    """Wait until the port answers AND the account can actually log in.

    A BANNER IS NOT A LOGIN. sshd answers before the account is usable in some
    conditions - a locked account, a shell that is not yet registered - and finding that
    out from the first real command instead of here makes the failure look like a
    package problem.
    """
    import socket

    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                sock.settimeout(10)
                if not sock.recv(256).startswith(b"SSH-2.0"):
                    time.sleep(10)
                    continue
        except OSError:
            time.sleep(10)
            continue
        probe = run(port, "echo DEV_SSH_OK", timeout=60)
        if "DEV_SSH_OK" in (probe.stdout or ""):
            log("the account logs in")
            return True
        log(f"  sshd is up but the login failed: {(probe.stderr or '').strip()[:160]}")
        time.sleep(10)
    return False


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the ssh-stage disk")
    p.add_argument("--out", default=None)
    p.add_argument("--copy", action="store_true",
                   help="copy the input disk instead of moving it; the default is to "
                        "move, because the disk normally came from ghcr and the "
                        "registry protects it")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--ssh-port", type=int, default=2223)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--clean", action="store_true",
                   help="remove this run's traces and zero the free space before "
                        "exporting; without it the disk ships with its build history")
    args = p.parse_args(argv)

    out = args.out or args.disk.replace(".qcow2", "-dev.qcow2")
    work = args.work or os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(work, exist_ok=True)

    if not shutil_which("sshpass"):
        raise SystemExit(
            "sshpass is not installed. This stage authenticates with the account's "
            "password, so it needs a client that can answer the prompt without a "
            "terminal."
        )

    log(f"disk={args.disk} out={out}")
    # MOVED, NOT COPIED - the ssh disk came from ghcr, so the registry still holds it.
    import working_copy as wc

    disk = wc.working_copy(args.disk, os.path.join(work, "dev.qcow2"),
                           copy=args.copy, note=log)

    import opencore
    import macos_recovery
    from lib import machine, vm

    # THE RECOVERY IMAGE IS AN INPUT EVEN THOUGH THIS STAGE DOES NOT BOOT FROM IT. See
    # the longer note in ssh.py: opencore.build() extracts Apple's APFS driver out of it,
    # and a job inherits no other job's inputs. This is the last stage in the chain, so
    # without this it would fail after setup, desktop and ssh had all succeeded.
    log("=== recovery image ===")
    recovery = macos_recovery.fetch_published("15", work)
    log(f"  {recovery} ({os.path.getsize(recovery) >> 20} MiB)")

    log("=== OpenCore, firmware, APFS driver ===")
    assets = opencore.build("15", work)
    log(f"identity {assets['identity']['serial']} / {assets['identity']['mac']}")

    shape = machine.shape(assets, cpus=args.cpus, mem_mb=args.mem)
    shape.ssh_port = args.ssh_port
    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            "-drive", f"file={disk},if=none,format=qcow2,id=target",
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
            "-device", f"isa-applesmc,osk={machine.OSK_PLAIN}",
        ],
        shape=shape,
        stdout=os.path.join(work, "console.log"),
    ).start()

    ok = False
    try:
        log(f"waiting up to {args.boot_timeout}s for sshd")
        if not wait_for_ssh(args.ssh_port, args.boot_timeout):
            log("=== THE DISK DID NOT ANSWER ON SSH ===")
            log("  this stage needs the ssh-stage disk, not the desktop one")
            return 1

        log("=== Homebrew ===")
        brew = run(args.ssh_port, f"export PATH={BREW_PATH}; {BREW_INSTALL}", timeout=2400)
        # THE INSTALLER EXITS 0 WHEN IT IS ALREADY THERE, so the check is the tool, not
        # the exit status.
        present = run(args.ssh_port, f"export PATH={BREW_PATH}; brew --version", timeout=120)
        if present.returncode != 0:
            log("=== HOMEBREW IS NOT USABLE ===")
            log((brew.stdout or "")[-800:])
            log((brew.stderr or "")[-800:])
            return 1
        log(f"  {(present.stdout or '').splitlines()[0] if present.stdout else 'brew'}")

        for command, version_flag, formula in PACKAGES:
            have = run(args.ssh_port,
                       f"export PATH={BREW_PATH}; command -v {shlex.quote(command)} "
                       f"&& {command} {version_flag}")
            if have.returncode == 0 and (have.stdout or "").strip():
                first = (have.stdout or "").strip().splitlines()[0]
                log(f"  {command} already present: {first[:80]}")
                continue
            if formula is None:
                # git ships with the command line tools that Homebrew itself installed.
                log(f"  {command} is missing and has no formula here; "
                    f"the command line tools did not install")
                log("=== THE TOOLCHAIN IS INCOMPLETE ===")
                return 1
            log(f"=== {formula} ===")
            got = run(args.ssh_port,
                      f"export PATH={BREW_PATH}; brew install {shlex.quote(formula)}",
                      timeout=1800)
            if got.returncode != 0:
                log(f"  brew install {formula} failed: {(got.stderr or '')[-400:]}")
                log("=== THE TOOLCHAIN IS INCOMPLETE ===")
                return 1

        log("=== verifying every tool answers ===")
        for command, version_flag, _formula in PACKAGES:
            got = run(args.ssh_port,
                      f"export PATH={BREW_PATH}; {command} {version_flag}", timeout=300)
            if got.returncode != 0 or not (got.stdout or "").strip():
                log(f"  {command} does not answer: {(got.stderr or '')[:200]}")
                log("=== THE TOOLCHAIN IS INCOMPLETE ===")
                return 1
            log(f"  {command}: {(got.stdout or '').strip().splitlines()[0][:80]}")

        if args.clean:
            log("=== cleaning ===")
            run(args.ssh_port, "sudo rm -rf ~/Library/Caches/Homebrew ~/.ssh/known_hosts",
                timeout=300)
            run(args.ssh_port, "sudo dd if=/dev/zero of=/tmp/zero bs=1m 2>/dev/null; "
                               "sudo rm -f /tmp/zero", timeout=1800)

        ok = True
        return 0
    finally:
        guest.stop(acpi_first=ok)
        if ok:
            if not smoke.check_disk(disk, root_fs="apfs"):
                log("=== THE DISK FAILED ITS CHECKS ===")
                raise SystemExit(1)
            log("compressing")
            compress.compress(disk, out)
            log(f"BAKED macos15 dev -> {out} ({os.path.getsize(out) >> 20} MiB)")


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
