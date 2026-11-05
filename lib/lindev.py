"""Install the build toolchain on a running Linux guest, over ssh.

    python3 linux/dev.py --disk out/ubuntu.qcow2 --kind ubuntu
    python3 lib/lindev.py --port 2222 --kind ubuntu

WHY THIS IS A STAGE AND NOT PART OF THE BAKE

The bake installs a system. It used to also install git, gcc, build-essential, python3,
pip, curl, jq and unzip through the subiquity `packages:` list, which made two things
harder than they should be:

    the bake stopped being a picture of a distribution
        A gold disk should answer "what does this release look like" before it answers
        "what does this pipeline need on it". Mixing the two means a bake failure and a
        broken toolchain look the same from the outside.

    a toolchain change needed a re-bake
        Adding a package to `packages:` is a forty-minute re-install. Adding it here is a
        ten-minute job against a disk that already exists, and the tag it pulls is the one
        the bake published.

WHAT IS STILL IN THE BAKE, AND WHY IT HAS TO BE

    openssh-server      the ssh stage has to have something to talk to
    the desktop         the desktop is the system, not a tool added to it
    the display driver  X comes up or it does not; this is not optional software

Everything below is a tool on top of a working system, which is what this stage is for.

WHY apt update AND upgrade RATHER THAN A PINNED SNAPSHOT

A gold disk is a starting point, and a starting point with a year-old libc is a disk whose
first useful act is to be upgraded. The upgrade is also the thing that would otherwise
happen unattended on its first night - doing it here means it happens once, under a
timeout, with a log.

THE UPGRADE IS ALLOWED TO FAIL AND THE INSTALL IS NOT. Ubuntu's repositories move, and an
`apt-get upgrade` that cannot fetch one package refuses the whole transaction; what this
stage exists to guarantee is the toolchain, so a failed upgrade is reported and the
package installs are still attempted. `DEBIAN_FRONTEND=noninteractive` and
`-o Dpkg::Options::=--force-confold` because there is no tty to answer a conffile prompt
and the config file that is already there came from the release.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)


def log(message: str) -> None:
    print(f"[tools {time.strftime('%H:%M:%S')}] {message}", flush=True)


# THE PACKAGES, GROUPED BY WHAT NEEDS THEM.
#
# A flat list would be shorter and would answer "what is installed" without answering "why",
# and the second question is the one that decides whether a package can be removed.
PACKAGES: list[tuple[str, list[str]]] = [
    # A GitHub Actions runner needs a shell, a compiler and a version control client. These
    # three are the floor.
    ("shell and build", [
        "build-essential", "gcc", "g++", "make", "pkg-config",
        "git", "curl", "wget", "ca-certificates",
    ]),
    # The runner itself is a Node program, and every action that ships as JavaScript needs
    # the major version the Actions runner expects. NodeSource would give a newer one at the
    # cost of a repository this disk does not need; ubuntu's own nodejs is enough for the
    # runner and for `npm ci` against a lockfile.
    ("node", ["nodejs", "npm"]),
    # Python is both a tool and a runtime other tools assume exists as `python`.
    ("python", ["python3", "python3-pip", "python3-venv", "python-is-python3"]),
    # Unpacking, inspection and the small tools a build script reaches for.
    ("archives and tools", ["unzip", "zip", "xz-utils", "jq", "file", "less", "rsync"]),
]

# FREEBSD CALLS THE SAME THINGS BY OTHER NAMES, and `build-essential` does not exist there
# at all - the base system ships a C compiler as part of `base`, which is why the bake does
# not need one. What pkg has to add is the pieces the base system leaves out.
PACKAGES_FREEBSD: list[tuple[str, list[str]]] = [
    ("shell and build", [
        "git", "curl", "wget", "gmake", "pkgconf", "gcc", "ca_root_nss",
    ]),
    ("node", ["node22", "npm-node22"]),
    ("python", ["python3", "py311-pip"]),
    ("archives and tools", ["unzip", "zip", "xz", "jq", "rsync", "gnugrep", "gawk"]),
]


def apt(command: str, *, seconds: int = 1800) -> tuple[int, str]:
    """One apt transaction, with the two options a tty-less install needs.

    DEBIAN_FRONTEND=noninteractive   no debconf dialog can appear
    --force-confold                  a conffile prompt keeps the file that is there

    The second is not cosmetic: without it, `grub-efi` or `sudo` on an upgrade stops at
    "Configuration file ... has been modified. Install the package maintainer's version?"
    and waits for an answer that never comes. The disk then hangs with dpkg holding its
    lock, and every later apt call blocks behind it.
    """
    return subprocess.run(
        ["sudo", "env", "DEBIAN_FRONTEND=noninteractive",
         "apt-get", "-y", "-q",
         "-o", "Dpkg::Options::=--force-confold",
         "-o", "Dpkg::Options::=--force-confdef"] + command.split(),
        capture_output=True, text=True, timeout=seconds,
    ).returncode, ""


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=2222, help="the forwarded ssh port")
    p.add_argument("--kind", default="ubuntu",
                   choices=["ubuntu", "kubuntu", "nixos", "freebsd"],
                   help="which package manager and which package names")
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--skip-upgrade", action="store_true",
                   help="install the toolchain without an apt upgrade first")
    args = p.parse_args(argv)

    import session
    import vm

    if args.kind == "nixos":
        # NIXOS IS NOT APT AND ITS PACKAGES ARE DECLARATIVE. A stage that ran apt on it would
        # be editing a file the next `nixos-rebuild` overwrites, and the toolchain there is a
        # configuration change rather than a package list - that belongs in the derive stage,
        # where the configuration is generated.
        log("nixos installs its toolchain through configuration.nix, not apt; nothing to do")
        return 0

    freebsd = args.kind == "freebsd"
    groups = PACKAGES_FREEBSD if freebsd else PACKAGES

    log(f"waiting for ssh on {args.port}")
    started = time.time()
    if not session.wait_for_ssh(args.port, args.boot_timeout, log):
        log("=== THE GUEST NEVER ANSWERED ===")
        return 1
    log(f"ssh after {time.time() - started:.0f}s")

    def run(what: str, command: str, *, seconds: int = 1800,
            required: bool = True) -> bool:
        log(f"  {what}")
        status, out = session.ssh(args.port, command, timeout=seconds)
        tail = [l for l in (out or "").splitlines() if l.strip()][-3:]
        for line in tail:
            log(f"    {line[:150]}")
        if status != 0:
            log(f"    exit {status}")
            if required:
                log(f"=== {what} FAILED ===")
            return False
        return True

    # THE LIST FIRST. Every later transaction needs a current index, and doing it explicitly
    # makes "the mirror was unreachable" its own failure rather than the first package
    # install's.
    if freebsd:
        # `pkg update` REPORTS FAILURE WHEN NOTHING CHANGED, which is not a failure. The
        # exit status is ignored and the install below is what decides - the same shape as
        # the apt upgrade.
        run("pkg update", "sudo pkg update -f 2>&1 | tail -3", seconds=900,
            required=False)
    else:
        if not run("apt update", "sudo apt-get update -q", seconds=900):
            return 1

        if not args.skip_upgrade:
            # ALLOWED TO FAIL. A repository that has moved on can refuse the whole
            # transaction over one package, and the toolchain is what this stage guarantees.
            run("apt upgrade", "sudo apt-get -y -q "
                "-o Dpkg::Options::=--force-confold "
                "-o Dpkg::Options::=--force-confdef upgrade", seconds=3600,
                required=False)

    failed = 0
    for group, names in groups:
        log(f"  {group}: {' '.join(names)}")
        if freebsd:
            command = "sudo env ASSUME_ALWAYS_YES=yes pkg install -y " + " ".join(names)
        else:
            command = ("sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "
                       "-o Dpkg::Options::=--force-confold "
                       "-o Dpkg::Options::=--force-confdef " + " ".join(names))
        status, out = session.ssh(args.port, command, timeout=3600)
        tail = [l for l in (out or "").splitlines() if l.strip()][-2:]
        for line in tail:
            log(f"    {line[:150]}")
        if status != 0:
            # ONE GROUP CAN FAIL WITHOUT STOPPING THE REST, and the verdict at the end is
            # what decides. `npm` is not on every release; the compiler is.
            log(f"    exit {status}; continuing")
            failed += 1

    # THE VERDICT ASKS THE GUEST WHAT IT HAS, not what apt said it did.
    # THE COMPILER IS `cc` ON FREEBSD AND `gcc` ON LINUX; both are asked for by the name
    # the platform actually uses, because `command -v gcc` on a stock FreeBSD answers no
    # until the port is installed - and the base system's compiler is `cc`.
    tools = (["cc", "gmake", "git", "node", "python3", "jq", "unzip", "rsync"]
             if freebsd else
             ["gcc", "g++", "make", "git", "node", "npm", "python3", "pip3", "jq",
              "unzip", "rsync"])
    probe = (
        "for c in " + " ".join(tools) + "; do "
        "  printf '%s=' $c; command -v $c >/dev/null 2>&1 && echo yes || echo no; "
        "done"
    )
    status, out = session.ssh(args.port, probe, timeout=180)
    have = {}
    for line in (out or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            have[k.strip()] = v.strip() == "yes"

    log(f"  toolchain: {sum(have.values())}/{len(have)} present")
    missing = [k for k, v in have.items() if not v]
    if missing:
        log(f"  missing: {', '.join(missing)}")

    # gcc AND git ARE THE FLOOR. A disk handed on as a gold image that cannot compile
    # anything is not the disk this stage claims to produce.
    for required in (("cc", "gmake", "git") if freebsd else ("gcc", "make", "git")):
        if not have.get(required):
            log(f"=== {required} IS NOT INSTALLED ===")
            return 1

    log("READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
