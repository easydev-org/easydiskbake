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

from lib import compress, smoke  # noqa: E402

SSH_USER = os.environ.get("MACOS_USER", "vmuser")
SSH_PASSWORD = os.environ.get("MACOS_PASSWORD", "admin")

# THE BREW RECIPE, THE PACKAGE LIST AND THE THREE FUNCTIONS THAT USED THEM lived here and
# are macos/15/steps/dev.yaml now, beside the steps that run them. What is left in this file
# is what a step cannot be: the machine, the disk, the recovery image and OpenCore.
#
# The Homebrew installer is a script fetched over https and run with /bin/bash, which is what
# its own documentation has said for years. It is idempotent: a second run says "already
# installed" and exits 0. That property is what lets the step run unconditionally.
WALK = os.path.join(HERE, "steps", "dev.yaml")


def _sha256(path: str) -> str:
    """The file's digest, streamed so a 28 GiB disk never has to fit in memory."""
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def log(message: str) -> None:
    print(f"[macos-dev {time.strftime('%H:%M:%S')}] {message}", flush=True)


# `wait_for_ssh` USED TO LIVE HERE AND IS GONE WITH THE BOOT.
#
# It was a socket-and-login loop spelled out in this file, and macos/15/steps/dev.yaml's
# `require_ssh:` step does the same thing through lib/session.py - one implementation, with its
# own timeout in the file where a reader can see it. Keeping a second copy here would have made
# "the disk answers ssh" a fact with two sources, and the one nothing called would have been the
# one that rotted.
#
# The `net` import below went with it; `smoke` stays, because checking the disk after the
# machine has stopped is the artifact's own verification and not a step.


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the ssh-stage disk")
    p.add_argument("--out", default=None)
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
    p.add_argument("--ssh-port", type=int, default=2222)
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

    # THE INPUT'S DIGEST, TAKEN BEFORE ANYTHING MOVES IT - see macos/15/desktop.py.
    input_digest = _sha256(args.disk)
    log(f"input  sha256 {input_digest[:16]}  ({args.disk})")
    with open(os.path.join(work, "input.sha256"), "w", encoding="utf-8") as fh:
        fh.write(input_digest + "\n")

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

    # THE MACHINE IS STARTED BY THE FILE'S `boot:` STEP, NOT HERE.
    #
    # What this stage keeps is what a step cannot be: the writable disk (or its overlay), the
    # recovery image OpenCore needs, the OpenCore assets, and the artifact. The boot, the ssh
    # wait, the toolchain and the shutdown are in macos/15/steps/dev.yaml.
    #
    # THE DRIVE LAYOUT USED TO BE SPELLED OUT HERE, by hand, and it was the fourth copy of
    # "what a macOS guest is" - lib/vm_session.py's _start_machine is the one that also carries
    # the qga device and the discard rule (TARGET_DRIVE vs TARGET_DRIVE_PLAIN, which is the
    # difference between a guest that boots a compressed disk and one that dies at HANDOFF TO
    # XNU). One description, and the stage says which disk.

    ok = False
    try:
        # THE WALKTHROUGH OWNS THE MACHINE: the boot, the ssh wait, the toolchain and the stop.
        #
        # `disk_path` IS PASSED SO A `python:` STEP CAN SEE IT, and `work` is the stage's own
        # directory because the Xcode .xip is kept there between the fetch and the copy.
        #
        # THE SHUTDOWN METHOD IS A FACT ABOUT THIS RUN and arrives as a var: an overlay is killed
        # (nothing it wrote is kept; the flatten is the artifact) and a real disk is closed with
        # `shutdown -h now`. See the two comments in dev.yaml.
        import steps as stepslib

        log(f"=== the walkthrough: {os.path.relpath(WALK, REPO)} ===")
        session = stepslib.MachineSession(
            "macos", disk, work,
            overlay=run_overlay, ssh_port=args.ssh_port,
            cpus=args.cpus, mem_mb=args.mem,
            # `copy: False` WHEN THERE IS NO OVERLAY: the disk is then a working copy this stage
            # already moved into `work`, and it is the file the artifact is made from.
            # SEE macos/15/ssh.py: `copy` would wrap this disk in a SECOND overlay, and every
            # write would then go into a diff that is deleted instead of published.
            boot_options={"assets": assets, "copy": False})
        ok_walk, ran = stepslib.run_file(
            WALK, qmp_path="", work=work, ssh_port=args.ssh_port,
            user=SSH_USER, password=SSH_PASSWORD, release=RELEASE,
            disk_path=disk, session=session,
            extra_vars={"args.shutdown": "kill" if run_overlay is not None else "ssh"},
        )
        log(f"{len(ran)} step(s) answered" if ok_walk
            else f"the walkthrough failed after {len(ran)} step(s)")
        if not ok_walk:
            return 1

        ok = True
        # THE WORK IS DONE; ONLY THE TEARDOWN AND THE COMPRESS REMAIN.
        #
        # Armed HERE rather than at startup because this is the line that makes the
        # sentence true. Before it, a SIGTERM means a cancel and must be obeyed.
        import entrypoint

        entrypoint.work_is_finished(
            "the dev walkthrough answered every step")
        return 0
    finally:
        # THE WALKTHROUGH STOPPED THE MACHINE, AND THAT IS THE ONLY STOP HERE.
        #
        # `shutdown: "{{ shutdown }}"` is the file's last step and names its method; run_file
        # stops the guest itself if the file fails before reaching it, so a crash does not leave
        # a qemu holding /dev/kvm. What this stage still owns is the artifact.
        if ok:
            if not smoke.check_disk(disk, root_fs="apfs"):
                log("=== THE DISK FAILED ITS CHECKS ===")
                raise SystemExit(1)
            log("compressing")
            if run_overlay is not None:
                run_overlay.flatten(out)
                # AND THEN IT IS RELEASED, WHICH IS ALSO THE CHECK: close() deletes the diff and
                # compares the base's size and mtime against what it recorded before qemu started.
                # After the flatten, because the flatten reads it.
                run_overlay.close()
            else:
                compress.compress(disk, out)
            log(f"BAKED macos15 dev -> {out} ({os.path.getsize(out) >> 20} MiB)")


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


if __name__ == "__main__":
    # ONE LATE SIGTERM IS SURVIVED, ONCE THE WORK IS FINISHED.
    #
    # Six runs have been killed by SIGTERM after their work was done and before their compress:
    # macOS cleans at 42913402988, 42919582582, 42928178260 and 42933089148, and Windows
    # vm-configure at 42959216978 and 42959948715 - the last of which had answered all 18 steps,
    # registered the logon task as Ready, and published nothing. The sender is not established
    # and every candidate has been excluded by measurement; see lib/entrypoint.py.
    #
    # INSTALLED HERE AND ARMED AT `ok = True`. Nothing is tolerated before that point, so a
    # cancel still stops the stage - the tolerant case is only a finished stage whose teardown
    # was interrupted.
    import entrypoint
    entrypoint.tolerate_one_sigterm()
    raise SystemExit(main(sys.argv[1:]))
