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


def _publish(args) -> None:
    """Move the configured disk to `--out`, and say how big it is.

    A MOVE, NOT A COPY. This stage is the only reader `--disk` will ever have again, and the image
    is several GiB - a copy would read and write all of it on a runner where disk is the scarce
    resource, in a stage that has already spent its time booting a machine. `shutil.move` falls
    back to a copy when the two paths are on different filesystems, which is the case where a
    rename cannot work at all.
    """
    import shutil

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if out != os.path.abspath(args.disk):
        log(f"renaming {args.disk} -> {out}")
        shutil.move(args.disk, out)
    log(f"{out}: {os.path.getsize(out) // (1024 * 1024)} MiB")


def _stop(guest, args) -> None:
    """Stop the guest over ssh and swallow what cannot be fixed at this point.

    THE DISK THIS GUEST OWNS IS THE ONE THAT GETS PUBLISHED, so a kill would leave the last writes
    in the page cache of a filesystem somebody is about to boot. `acpi_first=False` because Linux
    honours ACPI and it is still a second path that can hang; the ssh command is the one that says
    when it is done.

    A FAILURE HERE IS LOGGED AND NOT RAISED. By the time this runs the walk has already answered
    every question the stage was asked, and turning a slow shutdown into a failed build would
    throw away a disk that is correct.
    """
    import vm_session

    try:
        # `share=False`: THE STAGE IS ABOUT TO EXIT, SO THERE IS NOTHING TO CLEAN UP AFTER.
        #
        # The container ends with the step and smbd goes with it - stopping it explicitly bought
        # nothing and cost a full run: `hostshare.stop()` walks its pidfile with a sleep between
        # each signal, and run ff85c1e was signalled INSIDE that walk:
        #
        #     File "lib/vm_session.py", line 596, in stop
        #         hostshare.stop()
        #     File "lib/hostshare.py", line 463, in stop
        #         time.sleep(0.5)
        #     === SIGNALLED: SIGTERM (15) ===
        #
        # macOS' vm_configure.py has passed `share=False` here since it needed the same thing,
        # for the same reason, in the same place.
        vm_session.stop(guest, acpi_first=False, family="linux",
                        password=args.password, share=False, overlay=None)
    except Exception as exc:                           # noqa: BLE001
        log(f"  could not stop the machine cleanly: {exc}")


def _note_signals() -> None:
    """Say which signal killed us, instead of leaving bash's 241 to be decoded later.

    241 IS 256 - 15: bash reports a process terminated by SIGTERM that way. Run 42861231538's
    vm-configure answered all fifteen steps and then died with it, five seconds after the last
    one, with no exception and no verdict - the last thing in the log was a successful step.

    THE NAME OF THE SIGNAL AND THE FRAME THAT WAS RUNNING are the two facts that turn "something
    killed us" into "we were here when it happened", and a handler costs nothing. macOS'
    vm_configure.py carries the same handler for the same reason and has never printed it, which
    is how this one differs from a copy of a fix.
    """
    import signal

    def explain(number, frame):
        try:
            name = signal.Signals(number).name
        except ValueError:
            name = str(number)
        print(f"=== SIGNALLED: {name} ({number}) ===", flush=True)
        import traceback
        # A PRINT FROM A SIGNAL HANDLER CAN RAISE, AND WHEN IT DID IT HID THE SIGNAL ITSELF.
        #
        # Run ff85c1e's handler died with `BrokenPipeError` and the traceback that followed was
        # about the handler rather than about the stack it was called from - the one thing this
        # exists to show. When stdout is a closed pipe there is nowhere to write, so the message
        # goes to stderr as well and neither write is allowed to raise.
        try:
            print("=== the stack when it arrived ===", flush=True)
            traceback.print_stack(frame)
            print("=== end of stack ===", flush=True)
        except OSError:
            try:
                import sys as _sys
                traceback.print_stack(frame, file=_sys.stderr)
            except OSError:
                pass
        raise SystemExit(128 + number)

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, explain)


def main(argv: list[str]) -> int:
    _note_signals()
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

    walk = args.walk or os.path.join(REPO, "linux", "kubuntu",
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

        # THE MACHINE IS STOPPED HERE, BEFORE THE `finally`, AND THE `finally` IS WHY.
        #
        # Run 42861231538 answered all fifteen steps and then died with `exit code 241` - 256 - 15,
        # the number bash prints for a process killed by SIGTERM - five seconds later, with
        # `log-stream: finished failure (1 pushes, 0 failures)` in the same second and NOT ONE LINE
        # after `15 step(s) answered`.
        #
        # Nothing after that line ever printed: not the shutdown, not the rename, not the size. So
        # the shutdown was still running when the wrapper that streams this step's output decided
        # the command was over and signalled the process tree. `shutdown -h now` over ssh takes
        # seconds, and every second of it was spent inside a `finally` that the harness was not
        # waiting for.
        #
        # Stopping here instead means the last thing this stage does before the harness can lose
        # interest is the work it exists to do, and each half says so as it happens.
        log("=== stopping the machine ===")
        _stop(guest, args)
        log("=== the machine is stopped ===")

        # AND THE OUTPUT IS MOVED HERE TOO, FOR THE SAME REASON THE SHUTDOWN IS.
        #
        # It was in the `finally`, which runs on the way out - that is, during the same window the
        # harness may already have stopped waiting. A 7 GiB rename is instant on one filesystem
        # and a full copy across two, so it is the other thing here that can take long enough to
        # matter. Both are now inside the block whose every line is printed.
        _publish(args)
        return 0
    finally:
        # THE MACHINE IS ALREADY STOPPED AND THE OUTPUT ALREADY MOVED on the success path - see
        # `_stop` and `_publish`, and the comment where they are called. What is left here is the
        # failure path, where the guest may still be running.
        if not ok and guest is not None:
            _stop(guest, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
