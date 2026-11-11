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


# WHERE THE SIGNAL NOTE GOES. A dict rather than a module global so the handler, which runs with
# no arguments and no local state, can still find the directory the stage was told to use; main()
# fills it before any signal can arrive.
_ARTIFACTS: dict[str, str] = {}


def _install_signal_reporting() -> None:
    """Say so when a signal arrives, and from whom, instead of dying silently.

    RUN 42913402988'S CLEAN EXITED 241 WITH NO EXPLANATION. `241` is `-15 & 0xFF`: a Python
    process killed by SIGTERM. The last line it printed was `the guest powered off`, half a second
    before, and the work after it - the re-compress - never appeared. Two candidates (the share
    teardown signalling a reused pid, and the runner) and no way to tell them apart from a log
    that ends mid-sentence.

    A handler cannot save the stage's exit status, but it can name the signal, and `si_pid` names
    the sender - which is the difference between "something in this process did it" and "the
    runner did". It re-raises with the default action so the exit status is unchanged.
    """
    import signal

    def report(signum, frame):
        # A HANDLER GETS THE SIGNAL NUMBER, NOT THE SENDER - so name what can be named: the
        # parent (the runner's `bash -c`) and this process's group. If a later run prints a ppid
        # that is not the wrapper, the signal came from inside.
        try:
            sender = f" ppid={os.getppid()} pid={os.getpid()} pgid={os.getpgid(0)}"
        except OSError:
            sender = ""
        print(f"[clean-step] === SIGNALLED: {signal.Signals(signum).name} ({signum}) ==={sender}",
              flush=True)
        # THE VERDICT FILE IS THE STAGE'S OWN RECORD, AND IT IS WHAT THE WORKFLOW READS WHEN THE
        # EXIT CODE IS A SIGNAL. It is appended to on every note(), so the stages that already
        # ran are in it; naming the signal is what turns "241" into a sentence.
        try:
            path = os.path.join(_ARTIFACTS.get("dir", "out"), "clean-signal.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"{signal.Signals(signum).name} ({signum}){sender}\n")
        except OSError:
            pass
        # AND THE FIRST ONE IS SURVIVED. THE WORK IS DONE BY THE TIME THIS ARRIVES.
        #
        # Five runs have now been killed here, every one of them AFTER `the guest powered off`,
        # with the disk cleaned and only the record and the re-compress left:
        #
        #     42913402988   395/395      42922887078   378/378
        #     42919582582   419/418      42931268207   392/392
        #     42933089148   365/365      <- and this one with the signal sent from a child in its
        #                                    own session, which is measured not to be able to reach
        #                                    the parent
        #
        # The sender is not established, and every candidate has been excluded by measurement or
        # by the log. What IS established is that the work is finished when it arrives, so dying
        # here throws away a cleaned disk for no reason - and a clean that leaves its record and
        # its re-compress undone is indistinguishable from one that failed.
        #
        # A second signal is obeyed: a supervisor that repeats itself is not asking a question.
        # ONLY SIGTERM, AND ONLY WHEN THE DISK IS ALREADY CLEANED AND SHUT DOWN.
        #
        # SIGHUP and SIGINT mean the runner is going away and continuing would be rude; a SIGTERM
        # before the work is done means there is nothing to save. The tolerant case is exactly the
        # one this stage keeps dying in: a finished clean whose teardown is interrupted.
        state = _ARTIFACTS.setdefault("signals", 0) + 1  # type: ignore[arg-type]
        _ARTIFACTS["signals"] = state  # type: ignore[assignment]
        already_cleaned = bool(_ARTIFACTS.get("cleaned"))
        if signum == signal.SIGTERM and state == 1 and already_cleaned:
            print("[clean-step]   the clean is finished; recording it and continuing", flush=True)
            return
        print(f"[clean-step]   signal {state}"
              f"{'' if already_cleaned else ', and the clean has NOT finished'}; obeying it",
              flush=True)
        print(f"[clean-step]   signal {state}; obeying it", flush=True)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, report)
        except (OSError, ValueError):
            pass


# WHAT EACH SYSTEM NEEDS TO BE STARTED AND CLEANED. Only the differences, so a fourth system
# is one entry. `clean` is passed straight to clean_disk.clean_running().
SYSTEMS = {
    # `tpm` IS TRUE FOR WINDOWS AND ONLY WINDOWS: the disk was installed against a TPM and a boot
    # without one stops at the hardware check.
    "windows": {"ssh_port": 2222, "clean": "windows", "bootstate": True, "tpm": True},
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
    _install_signal_reporting()
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
    _ARTIFACTS["dir"] = args.artifacts

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
    # `copy=False`, BECAUSE CLEANING IS THE POINT, AND IT IS THE ONLY STAGE THAT MEANS IT.
    #
    # Everything else now boots an OVERLAY (lib/overlay.py): a read-only parent plus a throwaway
    # child, which is why a self-test can be killed without a shutdown. This stage cannot use one
    # - what it writes IS the artifact, and it is the last step before compress and publish, so an
    # overlay would have to be flattened back and the flatten is the step that could fail after
    # the clean succeeded. So there is no overlay here, `stop` gets none, and the careful shutdown
    # path applies.
    # THE DISK IS CHECKED BEFORE IT IS BOOTED, AND THAT IS NOT PARANOIA.
    #
    # This stage is handed the artifact the previous stage just wrote - on macOS, `qemu-img
    # convert -c` finished 1.1 seconds before this runs (run 42772191424). An image that qemu
    # cannot read fails at BOOT, and the symptom is `No route to host` after 900 seconds, which
    # says nothing about the disk. `qemu-img check` either reads it or says why in one line.
    # THE IMAGE IS CHECKED WITH THE SAME BINARY THAT WROTE IT.
    #
    # `compress.qemu_img()` resolves the one this repository compresses with. It used to matter
    # for a reason that no longer applies: the image put the Android SDK ahead of /usr/bin on
    # PATH, so a bare `qemu-img` was a 2018 build that answered
    #
    #     Unsupported qcow2 feature(s): compression type
    #
    # about a disk this repository had just compressed with zstd (run 42775862720). Containerfile
    # no longer puts the SDK on PATH, so the bare name would work now too - but naming the binary
    # is still the right thing here, because this check exists to prove the disk can be opened and
    # it should use the same tool every other step does.
    import compress as compresslib
    import subprocess as _sp
    try:
        checked = _sp.run([compresslib.qemu_img(), "check", args.disk],
                          capture_output=True, text=True, timeout=600)
        note(f"qemu-img check -> {checked.returncode}")
        for line in ((checked.stdout or "") + (checked.stderr or "")).splitlines()[-6:]:
            note(f"    {line[:160]}")
    except (OSError, _sp.TimeoutExpired) as exc:
        note(f"  qemu-img check could not run: {exc}")

    import vm_session

    # THREE VALUES, BECAUSE start() RETURNS AN OVERLAY TOO. It gained one when verification
    # stages moved off `shutil.copyfile`; this caller was never updated and failed on the
    # assignment - before it could boot anything, which is why the run showed a 900-second wait
    # for a guest that had never been started:
    #
    #     ValueError: too many values to unpack (expected 2)
    #
    # `clean` is the one caller that passes `copy=False`, so the overlay it gets back is always
    # None. It is named anyway: unpacking it is what keeps this call honest if that changes.
    guest, reached, _overlay = vm_session.start(
        args.kind, args.disk, work,
        ssh_port=spec["ssh_port"], boot_timeout=args.boot_timeout,
        copy=False, share=hostshare.DEFAULT_DIR,
        cpus=args.cpus, mem_mb=args.mem, log=note,
        # A WINDOWS DISK REMEMBERS THE TPM IT WAS INSTALLED AGAINST, and a clean that boots it
        # without one is a different machine: the guest stops at the hardware check and the
        # symptom is a 900-second wait that reads as a hung guest.
        #
        # `tpm` IS ITS OWN KEY, NOT `bootstate`. They are two different facts - `bootstate` is
        # "this disk needs a saved firmware-variable archive to find its loader", and `tpm` is
        # "this guest was installed with a TPM and refuses to boot without one". Reusing the
        # first for the second would have been a coincidence that read as a design.
        boot_options={"tpm": True} if spec.get("tpm") else None,
        # NO DISCARD, BECAUSE THIS DISK IS ALREADY COMPRESSED AND WE ARE STILL READING IT.
        #
        # `discard=unmap` frees clusters as the guest TRIMs them. That is right for a stage that
        # has finished writing and is what makes a published disk smaller - but this stage boots
        # a zstd qcow2 that another stage produced, and handing blocks back while the guest boots
        # from compressed clusters leaves the kernel dead at
        #
        #     #[EB|LOG:HANDOFF TO XNU] _
        #
        # with no panic and nothing in the console after it (run 42778795463). machine.py already
        # records the same trade for the installer, which is why TARGET_DRIVE_PLAIN exists.
        #
        # AND RUN 42874167311 REPEATS THE SAME DEATH WITH THIS VALUE IN PLACE, which is why the
        # cause is not this flag - see the note above this call for what it actually is.
        discard=False)
    if guest is None:
        return 2

    ok = False
    try:
        # vm_session.start WAITED FOR SSHD ALREADY. `reached` false means the machine came up and
        # never answered - which is the case that needs a picture, not a retry.
        if not reached:
            note("=== THE DISK DID NOT ANSWER ON SSH ===")
            note("  it cannot be cleaned if it cannot be reached; the disk is unchanged")

            # THE CONSOLE, BECAUSE IT IS THE ONLY THING THAT SAYS WHERE THE GUEST STOPPED.
            #
            # qemu writes the firmware and kernel output to console.log whatever happens, and a
            # guest that never reached sshd has that output even if it never reached a shell. A
            # screenshot is nicer for a person and useless over ssh; this text can be read in the
            # job log, and it distinguishes "the firmware would not boot the disk" from "the
            # kernel booted and the network never came up" - which are the two candidates for
            # `No route to host` and want opposite fixes.
            console = os.path.join(work, "console.log")
            if os.path.isfile(console):
                note(f"  the console, last 60 lines of {console}:")
                try:
                    with open(console, errors="replace") as fh:
                        for line in fh.readlines()[-60:]:
                            note(f"    {line.rstrip()[:180]}")
                except OSError as exc:
                    note(f"    and the console could not be read either: {exc}")
            else:
                note(f"  no console.log at {console}")

            # AND WHAT THE DHCP SERVER SAW, BECAUSE "No route to host" HAS TWO CAUSES.
            #
            # The guest either never asked for an address, or it asked and was refused. The
            # console cannot tell them apart - every healthy macOS boot looks the same - and
            # neither can the ssh message. dnsmasq's own log can, and the two want opposite
            # fixes: a guest that never asked is a guest whose NIC never came up; a guest that
            # was refused is a lease or a range problem on this side.
            #
            # `lib/net.py` starts dnsmasq with `log-facility=/var/log/dnsmasq.vm.log` and
            # `--dhcp-range=A,A` for one address, so a refusal reads `no address available` and
            # silence reads nothing at all.
            for dhcp_log in ("/var/log/dnsmasq.vm.log",):
                if not os.path.isfile(dhcp_log):
                    note(f"  no dnsmasq log at {dhcp_log}")
                    continue
                try:
                    with open(dhcp_log, errors="replace") as fh:
                        lines = [line.rstrip()[:180] for line in fh.readlines()[-40:]]
                except OSError as exc:
                    note(f"  the dnsmasq log could not be read: {exc}")
                    continue
                note(f"  dnsmasq, last {len(lines)} lines of {dhcp_log}:")
                for line in lines:
                    note(f"    {line}")
                if not lines:
                    note("    (empty - NOTHING ASKED FOR AN ADDRESS)")

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
        # THE DISK IS CLEAN AT THIS LINE, BEFORE THE SHUTDOWN.
        #
        # The shutdown runs in `finally`, so a signal that arrives during it lands while `ok` is
        # already true but `CLEANED` has not been written - which is the window every one of the
        # five killed runs died in. Recording it here is honest: the guest has been cleaned.
        _ARTIFACTS["cleaned"] = True
        # NO `return 0` HERE. THE SUCCESS PATH HAS TO LEAVE THE `try` TO REACH THE REST.
        #
        # It used to return here, and that made everything below this block unreachable: the
        # re-compress and the `CLEANED` marker both live after the `try`/`finally`, so NO clean
        # in this repository had ever compressed its disk or written the marker its workflow
        # reads. Run 42925475964 is how it surfaced - the clean succeeded, the separate verdict
        # step ran for the first time, and it correctly reported
        #
        #     === THE CLEAN DID NOT FINISH: no CLEANED line ===
        #
        # about a clean that had finished, because the line was past a `return`.
        #
        # Falling out of the `try` runs `finally` (the shutdown, which needs `ok` and is why this
        # is a `finally` at all) and then continues.
        pass
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
    # THE MARKER GOES DOWN BEFORE THE RE-COMPRESS, NOT AFTER IT.
    #
    # `CLEANED` is what tells the workflow the disk is fit to publish, and the re-compress is
    # minutes of qemu-img that only makes it smaller. Writing the marker afterwards made the two
    # indistinguishable: run 42919582582 was killed (241) between `the guest powered off` and this
    # line, so a finished clean left no record and the workflow reported failure. The marker is
    # now true at the moment it is written - the guest has been cleaned and powered off above -
    # and the compress cannot take it back.
    note(f"CLEANED {args.disk} (marker written before the re-compress)")
    # FROM HERE ON THE DISK IS PUBLISHABLE, AND A SIGNAL MUST NOT THROW IT AWAY.
    _ARTIFACTS["cleaned"] = True
    note("compressing the cleaned disk")
    import compress

    # THE RAW SIZE IS REPORTED FIRST, because the ratio is the only number that says whether
    # the clean did anything: a disk that compresses to the same size it had before was cleaned
    # and not improved.
    before = os.path.getsize(args.disk)
    try:
        compress.compress(args.disk, args.disk)
    except Exception as exc:                              # noqa: BLE001
        # THE CLEAN IS ALREADY DONE BY HERE, AND A FAILED RE-COMPRESS MUST NOT UNDO IT.
        #
        # This runs after the guest has been cleaned and shut down, so the disk is correct at this
        # line - the compress only makes it smaller. Reporting failure would tell the workflow to
        # throw away a cleaned disk because a size optimisation did not run, and `compress()`
        # writes to a `.part` and renames, so a failure leaves the previous image intact.
        #
        # Run 42916758079 is why this is written down: the process was killed (241, `-15 & 0xFF`)
        # half a second after `the guest powered off`, before this line printed anything, and the
        # stage failed with the work already done.
        note(f"=== THE RE-COMPRESS DID NOT RUN: {exc} ===")
        note(f"  the disk is cleaned; it is simply {before >> 20} MiB instead of smaller")
        note(f"CLEANED {args.disk} (uncompressed)")
        return 0
    after = os.path.getsize(args.disk)
    note(f"  {before >> 20} MiB -> {after >> 20} MiB")

    note(f"CLEANED {args.disk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
