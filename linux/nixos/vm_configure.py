"""Configure a Linux disk that already answers ssh: its name, the host share, a guest agent.

    python3 linux/fedora-workstation/vm_configure.py --disk out/fedora.qcow2 \
        --out out/fedora-vm-configure.qcow2 --ssh-port 2222

THIS IS macOS' STAGE FOUR, FOR THE LINUX FAMILY, AND IT DOES THE SAME THREE THINGS

    the machine's name        `vm`, set with the system's own tool  (macOS: scutil)
    the host share            ~/shared, mounted from the smbd `share_serve:` starts
    the qemu guest agent      a shutdown that is a request, not a power button

The division of labour is macOS' too, and it was not obvious until it was tried:

    this file   starts the machine, decides whether the stage passed, stops and publishes it
    the YAML    the work - every step in steps/vm-configure.yaml is `ssh:` or a host service

WHY THE WALKTHROUGH HAS NO `boot:` OR `shutdown:` STEP

Because those need a MachineSession, and the session is what this file owns. A YAML that started
the machine would have to be handed one, which makes the file un-runnable against any other disk -
and the reason `vm-configure` is a separate stage at all is that it can be pointed at a disk that
somebody else built.

WHY `copy=False` HERE AND `copy=True` IN THE SELF-TEST

The launch self-test boots a throwaway overlay because it must not touch the artifact it was
given. This stage is the opposite: what it writes is what gets published, so the guest has the
real disk and the shutdown is a real one. `lib/vm_session.stop()` sends `shutdown -h now` over
ssh for exactly that reason - a kill would leave the last writes in the page cache.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import net  # noqa: E402
import session  # noqa: E402

DEFAULT_USER = os.environ.get("SSH_USER") or "vmuser"
DEFAULT_PASSWORD = os.environ.get("SSH_PASSWORD") or "admin"


def make_hostname(prefix: str = "easyvm") -> str:
    """A name that is unique per build, so two disks on one network never collide.

    `easyvm-` PLUS EIGHT RANDOM CHARACTERS. The prefix says what the machine is; the suffix is
    what makes it unique - two bakes running at once, or two disks booted on the same bridge,
    would otherwise both answer to the same name and neither would be discoverable by name.

    LOWER-CASE HEX FROM `os.urandom`, NOT A COUNTER AND NOT TIME. A counter restarts at zero with
    every container, and a timestamp is guessable enough that two runners starting in the same
    second would agree. Eight hex characters is 32 bits, which is plenty for names that only have
    to be unique among the handful of machines one cluster runs at a time.

    THE LENGTH IS DELIBERATE: an RFC 1123 host label is at most 63 characters, and
    `easyvm-` plus eight is fifteen - short enough to read in a prompt, long enough to be unique,
    and valid as a DNS label with no escaping.
    """
    return f"{prefix}-{os.urandom(4).hex()}"


DEFAULT_HOSTNAME = os.environ.get("VM_HOSTNAME") or ""


def log(message: str) -> None:
    print(message, flush=True)


def wait_for_ssh(port: int, seconds: int = 900, *, user: str = "", password: str = "") -> bool:
    """Wait for sshd, and say what the last attempt said when it never comes."""
    import time

    user = user or DEFAULT_USER
    password = password or DEFAULT_PASSWORD
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        status, output = session.ssh(port, "echo DEV_SSH_OK", timeout=30,
                                     user=user, password=password)
        if status == 0 and "DEV_SSH_OK" in (output or ""):
            return True
        last = (output or "").strip().splitlines()[-1:] or [f"status {status}"]
        time.sleep(10)
    log(f"=== THE DISK NEVER ANSWERED ON SSH IN {seconds}s ===")
    log(f"  last attempt: {last[0] if last else 'nothing'}")
    return False


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--disk", required=True, help="the ssh-stage disk to configure")
    p.add_argument("--out", required=True, help="where the configured disk is written")
    p.add_argument("--work", default=None)
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--password", default=DEFAULT_PASSWORD)
    p.add_argument("--hostname", default=DEFAULT_HOSTNAME,
                   help="the machine's name; empty means easyvm-<8 random characters>")
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    # WHICH DISTRIBUTION'S WALKTHROUGH. The file differs by package manager and by nothing else,
    # and naming it here rather than deriving it from the disk is the same choice every stage in
    # this repository makes: the stage knows what it was asked to build.
    p.add_argument("--walk", default="", help="the steps/vm-configure.yaml to run")
    args = p.parse_args(argv)

    # ONE NAME FOR THIS BUILD, DECIDED HERE AND PRINTED SO THE LOG NAMES THE MACHINE IT MADE.
    hostname = args.hostname or make_hostname()
    log(f"this machine will be named {hostname!r}")

    work = args.work or os.path.join(REPO, "out", "vm-configure-work")
    os.makedirs(work, exist_ok=True)

    walk = args.walk or os.path.join(REPO, "linux", "nixos",
                                     "steps", "vm-configure.yaml")
    if not os.path.isfile(walk):
        log(f"=== NO WALKTHROUGH: {walk} ===")
        return 2

    import vm_session

    # `copy=False`: THE GUEST GETS THE REAL DISK, BECAUSE WHAT IT WRITES IS WHAT IS PUBLISHED.
    #
    # `boot_machine.start()` passes `copy=True`, which makes a throwaway overlay - correct for the
    # launch self-test, which must not touch the artifact it was handed, and WRONG here. Run 1 of
    # this stage used it and the log said so:
    #
    #     [overlay] wrapper /data/vmc-work/session-overlay.qcow2 (193 KiB)
    #     [overlay] base unchanged (size 7100170240, ...)
    #
    # - every step could have succeeded and the published disk would have been byte for byte the
    # one that came in. A stage whose whole purpose is to change a disk must not boot a copy of it.
    log(f"booting {args.disk} with the real disk attached (this stage writes what gets published)")
    guest, reached, overlay = vm_session.start(
        "linux", args.disk, work, ssh_port=args.ssh_port,
        boot_timeout=args.boot_timeout, copy=False, share="", log=log)
    if guest is None:
        log("=== THE MACHINE DID NOT START ===")
        return 1

    ok = False
    try:
        log(f"waiting up to {args.boot_timeout}s for sshd")
        if not wait_for_ssh(args.ssh_port, args.boot_timeout,
                            user=args.user, password=args.password):
            log("  this stage needs the ssh-stage disk, not the desktop one")
            return 1

        # THE WALKTHROUGH IS THE STAGE. Everything it does is `ssh:` or a host-side service, so
        # it runs headless - no screen, no frames, no coordinates.
        log(f"=== the walkthrough: {os.path.relpath(walk, REPO)} ===")
        import steps as stepslib
        ok_walk, ran = stepslib.run_file(
            walk, qmp_path="", work=work, ssh_port=args.ssh_port,
            user=args.user, password=args.password,
            disk_path=args.disk,
            extra_vars={"args.hostname": hostname},
        )
        log(f"{len(ran)} step(s) answered" if ok_walk
            else f"the walkthrough failed after {len(ran)} step(s)")
        if not ok_walk:
            return 1

        ok = True
        return 0
    finally:
        # THE SHUTDOWN IS PART OF THE STAGE, AND IT IS OVER SSH.
        #
        # This guest owns the real disk, so a kill would leave the last writes in the page cache
        # of a filesystem that is about to be published. `acpi_first=False` says "do not wait for
        # a power button first": Linux honours ACPI, and it is still a second path that can hang.
        try:
            vm_session.stop(guest, acpi_first=False, family="linux",
                            password=args.password, share=True, overlay=None)
        except Exception as exc:                       # noqa: BLE001
            log(f"  could not stop the machine cleanly: {exc}")

        if ok:
            # THE OUTPUT IS RENAMED, NOT COPIED, AND ONLY AFTER THE GUEST IS GONE.
            #
            # The disk is open for writing until qemu has exited, so the move cannot happen any
            # earlier - and it does not need to COPY: this stage is the only thing that will ever
            # read `--disk` again, and the image is 7 GiB. A copy would cost a full read and write
            # of that on a runner whose disk is the scarce resource, in a stage that has already
            # spent its time booting a machine.
            #
            # `os.replace` IS NOT A CROSS-DEVICE MOVE. When `--out` is on another filesystem the
            # rename cannot work and shutil.move copies instead - which is the correct fallback and
            # is why this is not a bare os.rename.
            import shutil
            out = os.path.abspath(args.out)
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            if out != os.path.abspath(args.disk):
                log(f"renaming {args.disk} -> {out}")
                shutil.move(args.disk, out)
            log(f"{out}: {os.path.getsize(out) // (1024 * 1024)} MiB")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
