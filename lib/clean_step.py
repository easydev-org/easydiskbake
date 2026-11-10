"""Clean a local disk that is not running: boot it, clean it over ssh, stop it.

    python3 lib/clean_step.py --disk out/windows11-ssh.qcow2 --kind windows

WHY THIS EXISTS WHEN lib/clean_disk.clean_running() DOES

Two different situations, and they are worth two entry points:

    clean_running()    called from inside a stage whose guest is ALREADY UP. No boot, no
                       shutdown - just the cleaning, over a connection that exists. This is
                       what windows/11/devtools.py and linux/*/bake.py use, because they
                       finish with the machine running.

    this script        a workflow STEP. The stage before it has exited and its guest is
                       gone, so the disk has to be started again. That boot is the price of
                       having the clean be a separate, visible, separately-failing step -
                       which is what it costs to see "clean the disk: failed" in the run
                       graph instead of a line buried in someone else's log.

THE DISK IS MODIFIED IN PLACE. This is the last step before compress and publish, so the
disk passed in IS the one that ships: cleaning a copy would mean the copy has to be moved
back afterwards, and a copy-back that failed would publish the dirty one.

NO BOOTSTATE IS NEEDED, ON ANY SYSTEM. Windows used to require one - the "Windows Boot
Manager" entry lived in OVMF's variables and nowhere else - and this stage restored it before
starting the machine. The bake now writes the Windows loader to the UEFI fallback path
\\EFI\\BOOT\\BOOTX64.EFI as well, so a fresh template's variables are enough and `--bootstate`
exists only so an older command line still parses.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import hostshare  # noqa: E402


def log(message: str) -> None:
    print(f"[clean-step {time.strftime('%H:%M:%S')}] {message}", flush=True)


# WHAT EACH SYSTEM NEEDS TO BE STARTED AND CLEANED. Only the differences, so a fourth system
# is one entry. `clean` is passed straight to clean_disk.clean_running().
SYSTEMS = {
    "windows": {"ssh_port": 2222, "clean": "windows", "bootstate": True},
    "linux": {"ssh_port": 2222, "clean": "linux", "bootstate": False},
    "freebsd": {"ssh_port": 2222, "clean": "freebsd", "bootstate": False},
    # macOS FORWARDS 2223, not 2222, because the number is per stage and a macOS run and a
    # Linux run can be in flight on the same host.
    #
    # ITS MACHINE IS BUILT BY macos/15/machine.py, NOT BY THE CODE BELOW. A macOS guest needs
    # OpenCore on its own boot image, the Apple SMC device with the OSK key, a Skylake CPU
    # model, its own firmware vars and the recovery image attached; `generic` below builds a
    # plain q35 virtio disk and cannot produce any of that. The `builder` key names the
    # function that does it, so there is exactly one description of this machine in the
    # repository and this file calls it rather than restating it.
    # 2222, THE PORT EVERY SYSTEM USES. It was 2223 here after the workflows were unified:
    # this table kept the old number, so the clean booted the disk and then connected to a
    # port nothing was forwarded to -
    #
    #     ssh: connect to host 127.0.0.1 port 2223: Connection refused
    #
    # for the whole 900-second budget, on a disk that had sshd running (run 42048455475's
    # enable Remote Login step had just proved it with SSH-2.0-OpenSSH_9.9 on 2222).
    "macos": {"ssh_port": 2222, "clean": "macos", "bootstate": False,
              "builder": "macos15"},
}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--disk", required=True, help="the qcow2 to boot, clean and keep")
    p.add_argument("--kind", required=True, choices=sorted(SYSTEMS))
    p.add_argument("--work", default=None,
                   help="where qemu writes its sockets and firmware state; defaults to the "
                        "disk's directory")
    p.add_argument("--bootstate", default=None,
                   help="a directory holding <disk>.bootstate.tar.gz; for Windows")
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--mem", type=int, default=6144)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--artifacts", default="out")
    args = p.parse_args(argv)

    if not os.path.isfile(args.disk):
        log(f"=== NO DISK AT {args.disk} ===")
        return 2

    spec = SYSTEMS[args.kind]
    work = os.path.abspath(args.work or os.path.dirname(os.path.abspath(args.disk)))
    os.makedirs(work, exist_ok=True)
    os.makedirs(args.artifacts, exist_ok=True)

    verdict = os.path.join(args.artifacts, f"clean-{args.kind}.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)
        try:
            with open(verdict, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    note(f"disk {args.disk} ({os.path.getsize(args.disk) >> 20} MiB)")

    # THE BOOTSTATE IS OPTIONAL NOW, AND THAT IS THE POINT OF --self-boot.
    #
    # A disk the bake wrote the UEFI fallback loader into does not need OVMF's variables: the
    # firmware finds \EFI\BOOT\BOOTX64.EFI on its own. Restoring an old bootstate anyway is
    # not merely unnecessary - it reinstates the very dependency the boot was made independent
    # of, and if the archive is from a different boot its entries point at partitions this disk
    # may not have.
    #
    # So it is used only when there is one AND the caller asked for it. `--bootstate` is how a
    # caller says "this disk needs it"; the workflow passes nothing for a self-booting disk.
    if spec["bootstate"] and args.bootstate:
        import bootstate

        # THE ARCHIVE HAS TO SIT BESIDE THE DISK UNDER THE DISK'S OWN NAME - that is the only
        # path restore() reads. `--bootstate` says where the artifact was downloaded to, and
        # adopt() does the rename.
        dest = bootstate.archive_path(args.disk)
        if not os.path.exists(dest):
            source_dir = args.bootstate or work
            found = [
                os.path.join(source_dir, n)
                for n in sorted(os.listdir(source_dir))
                if n.endswith(".bootstate.tar.gz")
            ] if os.path.isdir(source_dir) else []
            if not found:
                note(f"=== NO BOOTSTATE IN {source_dir} ===")
                note("  Windows boots by a boot entry stored in its firmware variables;")
                note("  without the archive the machine starts and finds nothing")
                return 2
            if len(found) > 1:
                note("=== MORE THAN ONE BOOTSTATE; REFUSING TO GUESS ===")
                for f in found:
                    note(f"  {os.path.basename(f)}")
                return 2
            bootstate.adopt(args.disk, found[0])

        note("restoring the firmware state")
        if not bootstate.restore(work, args.disk):
            note("=== THE BOOTSTATE COULD NOT BE RESTORED ===")
            return 2

    # THE MACHINE IS lib/vm_session.py's BUSINESS, AND SO IS THE SHARE.
    #
    # THIS FILE USED TO START THE GUEST ITSELF, and that is what broke it. Once the share stage
    # began installing a launchd job that mounts 172.30.0.1 at boot, a clean that served nothing on
    # that address booted a machine whose root daemon retried a mount that could never work - and
    # the clean timed out after 900 seconds with "No route to host" (run 42756290022) on a disk
    # whose sshd had answered in thirty seconds during the stage before.
    #
    # That is not a missing line here. It is three places starting a guest and one of them knowing
    # what the guest expects, so the machine and the environment around it are now one call.
    #
    # `copy=False`, BECAUSE CLEANING IS THE POINT. boot_machine copies so a self-test cannot touch
    # the artifact; this stage modifies the disk in place on purpose - it is the last step before
    # compress and publish, and cleaning a copy would mean moving it back.
    import vm_session

    guest, reached = vm_session.start(
        args.kind, args.disk, work,
        ssh_port=spec["ssh_port"], boot_timeout=args.boot_timeout,
        copy=False, share=hostshare.DEFAULT_DIR,
        cpus=args.cpus, mem_mb=args.mem, log=note)
    if guest is None:
        return 2

    ok = False
    try:
        # vm_session.start WAITED FOR SSHD ALREADY. `reached` false means the machine came up and
        # never answered - which is the case that needs a picture, not a retry.
        if not reached:
            note("=== THE DISK DID NOT ANSWER ON SSH ===")
            note("  it cannot be cleaned if it cannot be reached; the disk is unchanged")

            # A PICTURE OF WHERE IT STOPPED, BECAUSE THE SSH MESSAGE CANNOT SAY.
            #
            # "Connection timed out during banner exchange" is what qemu's user-mode network
            # produces whether the guest never booted, booted to a firmware shell, or booted and
            # never started sshd - three different problems with one message. The frame is the
            # only thing that distinguishes them, and without it this stage failed twice with
            # nothing to look at.
            try:
                import qmp as qmpmod
                import lastframe

                # `args.artifacts`, NOT A BARE `artifacts`. That name was never bound in
                # this function, so the one line that exists to explain a failed boot died
                # with
                #
                #     NameError: name 'artifacts' is not defined
                #
                # and took the clean with it - the frame it was about to take is the only
                # evidence of why the guest did not answer.
                os.makedirs(args.artifacts, exist_ok=True)
                ppm = os.path.join(args.artifacts, "clean-stopped.ppm")
                client = qmpmod.QMP(guest.paths.drive)
                try:
                    client.execute("screendump", filename=ppm)
                finally:
                    client.close()
                lastframe.convert(ppm, os.path.join(args.artifacts, "clean-stopped.png"))
                note(f"  the screen at that moment: {args.artifacts}/clean-stopped.png")
            except (OSError, RuntimeError) as exc:
                note(f"  and the screenshot failed too: {exc}")
            return 1

        import clean_disk

        ok = clean_disk.clean_running(spec["clean"], port=spec["ssh_port"],
                                     note=note, disk=args.disk)
        if not ok:
            note("=== THE CLEAN DID NOT FINISH ===")
            return 1
        return 0
    finally:
        # ACPI FIRST WHEN IT WORKED, AND LONG ENOUGH. The clean wrote gigabytes of zeros
        # seconds ago; a disk killed mid-write repairs itself on every later boot. 60s was
        # not enough and produced "the guest did not power off within 60s; killing it".
        # THE SSH SHUTDOWN FIRST - macOS ignores the ACPI power button and shows a dialog
        # instead. See lib/vm_session.py's SHUTDOWN_COMMAND.
        vm_session.stop(guest, acpi_first=ok, family=args.kind, password="admin")
        # AND THE SHARE, because a leftover smbd holds 172.30.0.1:445 and the next stage in this
        # container would find it serving a directory that is no longer the one it wants.
        hostshare.stop()

    if not ok:
        return 1

    # COMPRESS AGAIN, BECAUSE THE CLEAN LEFT THE IMAGE BIGGER THAN IT FOUND IT.
    #
    # THIS IS THE STEP THAT WAS MISSING, and it cost 12 GiB on every Linux disk. The order
    # used to be:
    #
    #     bake       compress 4453 MiB
    #     clean      boot it, write 13247 MB of zeros -> the qcow2 grows to 16536 MiB
    #     publish    push 16536 MiB
    #
    # Writing zeros INTO an already-compressed qcow2 allocates clusters, because qemu stores
    # the zero blocks like any other data - discard=unmap helps the fstrim pass but it cannot
    # help the zeros that are written afterwards. The image only shrinks when convert reads it
    # and skips the blocks that are zero, and nothing did that after the clean.
    #
    # So the disk is compressed here, in place. The compress is the same call the bakes use,
    # which is what makes the published size the size of what is actually in the disk.
    note("compressing the cleaned disk")
    import compress

    # THE RAW SIZE IS REPORTED FIRST, because the ratio is the only number that says whether
    # the clean did anything: a disk that compresses to the same size it had before was cleaned
    # and not improved.
    before = os.path.getsize(args.disk)
    compress.compress(args.disk, args.disk)
    after = os.path.getsize(args.disk)
    note(f"  {before >> 20} MiB -> {after >> 20} MiB")

    note(f"CLEANED {args.disk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
