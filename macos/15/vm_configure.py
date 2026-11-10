"""Configure the VM after ssh: the guest agent, the host share, and the login hook.

    python3 macos/15/vm_configure.py --disk out/macos15-ssh.qcow2 --out out/macos15-vm-configure.qcow2

WHAT CHANGED AND WHY THE NAME CHANGED WITH IT

This was `share.py`, because an SMB mount was the only thing it did. It now installs three things,
and the machine is what they have in common:

    the qemu guest agent     a shutdown that does not need a dialog answered
    an SMB share             a directory the person can put files in
    a login hook             something that runs those files at login

The agent is the reason for the rename. macOS answers an ACPI power button with a dialog and
waits (run 42762244256), so every stage that wanted a clean shutdown used ssh instead; the agent
is the supported route, and it has to be installed on a disk that already answers ssh - which is
exactly where this stage runs.

WHY IT IS STILL BEFORE THE TOOLCHAIN

It needs nothing installed. SMB is native, the launcher is /bin/sh, and the agent is one binary
copied in. Splitting it out means a failure here is about the machine rather than about a 2.7 GiB
Xcode download, and the two can be re-run independently.

WHAT IT INSTALLS, AND NOTHING ELSE

    an SMB share on the bridge, served by this container (lib/hostshare.py)
    the guest agent, from mav2287/mac-guest-agent
    a LaunchAgent that runs ~/shared/bootstrap.sh at login

No package manager, no compiler, no interpreter. If a step in macos/15/steps/vm-configure.yaml
needs something a stock macOS does not have, it is in the wrong file.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

# WHICH RELEASE THIS DISK IS. The stages in this directory are shared between
# macOS 15 and 26 - macos/26 keeps only the measurements that differ - so the
# recovery image and the OpenCore build have to be told which one they are
# working on. A constant here made macos26's desktop stage pull
# macos-recovery-sequoia, which is 15's image, and install Apple's APFS driver
# from it into a 26 boot.
RELEASE = os.environ.get("MACOS_RELEASE", "15")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
# AND THIS DIRECTORY, WHICH IS WHERE `machine`, `opencore` AND `macos_recovery` LIVE.
#
# `from lib import machine` asks lib/ for a module called machine. It is not there - it is
# macos/15/machine.py, beside this file - and the failure is
#
#     ImportError: cannot import name 'machine' from 'lib' (unknown location)
#
# which arrives AFTER the 16 GiB disk has been pulled, so the whole stage is spent on an import.
# macos/15/desktop.py carries the same note for the same reason; this file had the bug until
# run 42624293500.
sys.path.insert(0, HERE)

from lib import compress, net, smoke  # noqa: E402

SSH_USER = os.environ.get("MACOS_USER", "docker")
SSH_PASSWORD = os.environ.get("MACOS_PASSWORD", "admin")

# THE BREW RECIPE, THE PACKAGE LIST AND THE THREE FUNCTIONS THAT USED THEM lived here and
# are macos/15/steps/dev.yaml now, beside the steps that run them. What is left in this file
# is what a step cannot be: the machine, the disk, the recovery image and OpenCore.
#
# The Homebrew installer is a script fetched over https and run with /bin/bash, which is what
# its own documentation has said for years. It is idempotent: a second run says "already
# installed" and exits 0. That property is what lets the step run unconditionally.
WALK = os.path.join(HERE, "steps", "vm-configure.yaml")


def log(message: str) -> None:
    print(f"[macos-vmconf {time.strftime('%H:%M:%S')}] {message}", flush=True)


def wait_for_ssh(port: int, seconds: int = 180) -> bool:
    """Wait until the port answers AND the account can actually log in.

    A BANNER IS NOT A LOGIN. sshd answers before the account is usable in some
    conditions - a locked account, a shell that is not yet registered - and finding that
    out from the first real command instead of here makes the failure look like a
    package problem.
    """
    import socket

    import session

    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((net.FORWARD_HOST, port), timeout=10) as sock:
                sock.settimeout(10)
                if not sock.recv(256).startswith(b"SSH-2.0"):
                    time.sleep(10)
                    continue
        except OSError:
            time.sleep(10)
            continue
        # session.ssh, NOT A LOCAL `run()` HELPER. This file had one - sshpass and four options,
        # rebuilt here - and it was deleted with the rest of the brew code, which left
        # `wait_for_ssh` calling a name that no longer existed:
        #
        #     NameError: name 'run' is not defined
        #
        # raised after the recovery image and OpenCore had both been fetched, so the stage spent
        # its first minute on work that could not finish. lib/session.py is the shared client and
        # takes the password explicitly, which is what the WINDOWS_ADMIN_PASSWORD/MACOS_PASSWORD
        # difference needs anyway.
        status, output = session.ssh(port, "echo DEV_SSH_OK", timeout=60,
                                     user=SSH_USER, password=SSH_PASSWORD)
        if status == 0 and "DEV_SSH_OK" in (output or ""):
            log("the account logs in")
            return True
        log(f"  sshd is up but the login failed: {(output or '').strip()[:160]}")
        time.sleep(10)
    return False


def _note_signals() -> None:
    """Say which signal killed us, instead of leaving bash's 241 to be decoded later.

    241 IS 256 - 15: bash reports a process terminated by SIGTERM that way, and it is what the
    share stage printed twice, 0.6 seconds after `[disk] reading the MBR`, with no exception and
    no verdict from the check that was running. Whether that signal comes from the runner, from a
    process-group kill, or from something this repository does, the name of it is the next fact
    anybody needs - and a handler costs nothing.
    """
    import signal

    def explain(number, frame):
        try:
            name = signal.Signals(number).name
        except ValueError:
            name = str(number)
        print(f"=== SIGNALLED: {name} ({number}) ===", flush=True)
        # AND WHERE. A signal is delivered between bytecodes, so the frame that was running is
        # the code that was interrupted - which is the one fact that turns "something killed us"
        # into "we were here when it happened".
        import traceback
        print("=== the stack when it arrived ===", flush=True)
        traceback.print_stack(frame)
        print("=== end of stack ===", flush=True)
        raise SystemExit(128 + number)

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, explain)


def main(argv: list[str]) -> int:
    _note_signals()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the ssh-stage disk")
    p.add_argument("--out", default="out/macos15-vm-configure.qcow2")
    p.add_argument("--overlay", action="store_true",
                   help="treat --disk as a READ-ONLY base and write into a throwaway qcow2 "
                        "overlay; the base is never modified. See lib/overlay.py")
    p.add_argument("--copy", action="store_true",
                   help="copy the input disk instead of moving it; the default is to "
                        "move, because the disk normally came from ghcr and the "
                        "registry protects it")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--ssh-port", type=int, default=2223)
    p.add_argument("--boot-timeout", type=int, default=900)
    # NO --clean HERE ANY MORE, AND THE ONE THAT EXISTED WAS THE WRONG KIND ANYWAY.
    #
    # It ran `dd if=/dev/zero of=/tmp/zero bs=1m` - writing zeros to fill the free space, the
    # mechanism this repository abandoned everywhere else. lib/linclean.py measured what it
    # costs: one second of fstrim released 116 GiB on the same disk, and the zeroing that
    # followed allocated clusters back. On APFS it is worse than useless, because a
    # copy-on-write filesystem allocates new blocks before the old ones are released.
    #
    # Cleaning is lib/clean_step.py's job now, as the step after this one, the same way it is
    # for bake, derive and both Windows stages. It calls lib/macclean.py, which uses
    # `diskutil apfs trim` - discard, which writes nothing.
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

    run_overlay = None
    if getattr(args, "overlay", False):
        import overlay as overlaymod

        run_overlay = overlaymod.Overlay(args.disk, work, name="dev-overlay.qcow2")
        disk = run_overlay.open()
    else:
        disk = wc.working_copy(args.disk, os.path.join(work, "dev.qcow2"),
                               copy=args.copy, note=log)

    import opencore
    import macos_recovery
    import machine                     # macos/15/machine.py, via the path above
    from lib import vm

    # THE RECOVERY IMAGE IS AN INPUT EVEN THOUGH THIS STAGE DOES NOT BOOT FROM IT. See
    # the longer note in ssh.py: opencore.build() extracts Apple's APFS driver out of it,
    # and a job inherits no other job's inputs. This is the last stage in the chain, so
    # without this it would fail after setup, desktop and ssh had all succeeded.
    log("=== recovery image ===")
    recovery = macos_recovery.fetch_published(RELEASE, work)
    log(f"  {recovery} ({os.path.getsize(recovery) >> 20} MiB)")

    log("=== OpenCore, firmware, APFS driver ===")
    assets = opencore.build(RELEASE, work)
    log(f"identity {assets['identity']['serial']} / {assets['identity']['mac']}")

    shape = machine.shape(assets, cpus=args.cpus, mem_mb=args.mem)
    # THE AGENT'S CHANNEL, MADE BEFORE THE MACHINE SO QEMU CAN BIND IT. This stage is what
    # installs the agent, so it is the stage that has to give it somewhere to answer - and the
    # step that installs it checks the process, which only exists if the port does.
    #
    # ISA SERIAL, because Apple's own virtio agent never loads under QEMU and the third-party one
    # that does moved to ISA. See lib/vm.py's Shape.qga_transport.
    shape.qga_socket = os.path.join(work, "qga.sock")
    shape.qga_transport = "isa"
    if os.path.exists(shape.qga_socket):
        os.unlink(shape.qga_socket)
    shape.ssh_port = args.ssh_port
    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            "-drive", machine.TARGET_DRIVE.format(disk=disk),
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

        # THE TOOLCHAIN IS A FILE NOW: macos/15/steps/dev.yaml.
        #
        # What stood here - install Homebrew, walk PACKAGES, brew install each, verify each -
        # was a sequence of measurements and no decision. The measurements belong beside the
        # numbers they produced; the decision ("does this stage pass?") stays here.
        #
        # THE WALK RUNS OVER SSH, so it takes the headless driver lib/steps.py builds when a
        # file has no GUI steps. This stage never touches the screen: the disk arrived with a
        # working sshd.
        #
        # `disk_path` IS PASSED SO A `python:` STEP CAN SEE IT, and `work` is the stage's own
        # directory because the Xcode .xip is kept there between the fetch and the copy.
        log(f"=== the walkthrough: {os.path.relpath(WALK, REPO)} ===")
        import steps as stepslib
        ok_walk, ran = stepslib.run_file(
            WALK, qmp_path="", work=work, ssh_port=args.ssh_port,
            user=SSH_USER, password=SSH_PASSWORD, release=RELEASE,
            disk_path=disk,
        )
        log(f"{len(ran)} step(s) answered" if ok_walk
            else f"the walkthrough failed after {len(ran)} step(s)")
        if not ok_walk:
            return 1

        ok = True
        return 0
    finally:
        # THE SSH SHUTDOWN FIRST, BECAUSE macOS IGNORES THE ACPI POWER BUTTON.
        #
        # It receives `system_powerdown` and puts up the "Restart / Sleep / Cancel / Shut Down"
        # dialog, then waits for someone to answer - and nothing does on a headless guest. Five
        # minutes of waiting and then `killing it` (run 42762244256), which leaves a journal for
        # the next boot to replay. lib/vm_session.py's stop() sends `shutdown -h now` over ssh and
        # keeps ACPI as the fallback.
        import vm_session
        # `share=False`: THE STAGE IS ABOUT TO EXIT, SO THERE IS NOTHING TO CLEAN UP AFTER.
        #
        # The container ends with the step and smbd goes with it. Stopping it explicitly bought
        # nothing and cost this stage five runs: the SIGTERM that killed it arrived inside
        # hostshare.stop(), one line after `kill` returned (stack from run 42770251964), and
        # seven experiments - killpg, process groups, stale pids, subprocess vs os.kill, a
        # daemonised leader - found no sender inside this process. macos/15/ssh.py calls the same
        # stop() without a share and never had the problem.
        #
        # If a later stage in the SAME container needs 445, it starts its own smbd and start()
        # stops the previous one first. That is the case this cleanup was written for, and it is
        # not this one.
        vm_session.stop(guest, acpi_first=ok, family="macos", password=SSH_PASSWORD,
                        share=False, overlay=run_overlay)
        if ok:
            # SAY SO BEFORE AND AFTER, BECAUSE THIS IS WHERE THE PROCESS KEPT DISAPPEARING.
            #
            # Run 42765341998 printed `[disk] reading the MBR` and then `exit code 241` 0.6 seconds
            # later, with no exception and no verdict. 241 is what bash reports for a process that
            # was terminated by a signal, so the question is not "which check failed" but "who
            # signalled us" - and a line on either side of the call answers it.
            log("=== checking the disk ===")
            disk_ok = smoke.check_disk(disk, root_fs="apfs")
            log(f"=== the disk check returned {disk_ok} ===")
            if not disk_ok:
                log("=== THE DISK FAILED ITS CHECKS ===")
                raise SystemExit(1)
            log("compressing")
            if run_overlay is not None:
                run_overlay.flatten(out)
            else:
                compress.compress(disk, out)
            log(f"BAKED macos15 vm-configure -> {out} ({os.path.getsize(out) >> 20} MiB)")


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
