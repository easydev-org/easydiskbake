"""Start a published Windows disk and photograph the screen. Nothing is installed.

    python3 lib/launch_windows.py --tag windows-base-20261102-155400 --artifacts out

WHAT THIS ANSWERS

    does the artifact in ghcr actually boot to a desktop

bake-windows publishes a disk that carries its own boot loader; the ssh stage starts one
and waits for a desktop. That wait is nine minutes of silence, and when it fails it reports

    the guest did not reach a desktop

which is true of two very different situations:

    the firmware could not find a boot loader, and the guest is sitting in its own shell
    Windows booted, and the desktop test does not recognise what it drew

Run 41483694400 was the first of those and reported the second, because nobody was
looking at the screen - 75 frames were captured, 21 of them had a picture, and the
brightest one read

    BdsDxe: failed to load Boot000A "Windows Boot Manager" from HD(1,GPT,...)
      /EFI/Microsoft/Boot/bootmgfw.efi: Not Found
    >>Start PXE over IPv4.
    ...
    UEFI Interactive Shell v2.2

So this starts the disk, photographs it every few seconds, and says what the screen said
at the end. It does not install anything and does not write to the disk.

WHY SAMPLING RATHER THAN ONE PICTURE AT THE END

The interesting question is not only whether it arrived, but where it stopped when it did
not. A single frame at the timeout shows a black screen; a frame every twelve seconds
shows the firmware printing, showing its shell, and then going quiet - which is what
happened above and what no single picture would have said.

Frames are kept only every SAMPLE_KEEP_SECONDS in the artifact: the raw PPM set for that
run was 221 MB, and the point of the sequence is order rather than every instant.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(REPO, "windows", "11"))

import iso  # noqa: E402
import smoke  # noqa: E402
import vm  # noqa: E402

# The boot machinery is the ssh stage's, imported rather than copied: a second opinion
# about how this guest starts is a second thing to keep in step, and the eleven qemu
# parameters took eight runs to get right.
import ssh as sshstage  # noqa: E402

# One frame in this many is kept for the artifact. The raw set is ~3 MB per frame.
SAMPLE_KEEP_SECONDS = 30


def log(message: str) -> None:
    print(message, flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tag", default=None,
                   help="a published disk to pull from ghcr")
    p.add_argument("--disk", default=None,
                   help="a local disk to start instead of pulling one")
    p.add_argument("--bootstate", default=None,
                   help="IGNORED. The disk carries \\EFI\\BOOT\\BOOTX64.EFI, so nothing has "
                        "to be restored; kept so an existing command line still parses")
    p.add_argument("--allow-blank-firmware", action="store_true",
                   help="IGNORED. Every launch now starts with a fresh variable store, "
                        "because the disk carries its own loader; kept so the command line "
                        "that ran the original experiment still parses")
    p.add_argument("--artifacts", default="out")
    p.add_argument("--work", default=None)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--probe-after", type=int, default=0,
                   help="seconds to wait for a desktop before probing the firmware "
                        "shell. 0 means the whole boot timeout; -1 means do not wait "
                        "at all")
    p.add_argument("--probe-shell", action="store_true",
                   help="when the firmware drops into its own shell, type into it: list "
                        "the EFI partition and try the loader by hand. Answers whether "
                        "the file is unreadable or the boot entry is wrong")
    p.add_argument("--resolution", default="1920x1080",
                   help="the guest screen, as WxH. The recording is what this launch "
                        "produces, so it is taken at the size a desktop is actually "
                        "looked at - not at OVMF's 1280x800 default")
    p.add_argument("--tpm", action="store_true",
                   help="provide a TPM 2.0; off by default, which is how dockur-windows "
                        "runs Windows 11 - its answer file bypasses the check, and ours "
                        "does too")
    p.add_argument("--check-ssh", action="store_true",
                   help="after the desktop, assert that sshd answers on the forwarded "
                        "port. For a disk that came through bake-windows-ssh: a desktop "
                        "says nothing about whether the OpenSSH install worked")
    p.add_argument("--mem", type=int, default=6144)
    p.add_argument("--cpus", type=int, default=4)
    args = p.parse_args(argv)

    if not args.tag and not args.disk:
        raise SystemExit("one of --tag or --disk is required")

    os.makedirs(args.artifacts, exist_ok=True)
    work = args.work or os.path.join(args.artifacts, "launch-work")
    os.makedirs(work, exist_ok=True)
    shots = os.path.join(work, "shots")
    os.makedirs(shots, exist_ok=True)

    report = os.path.join(args.artifacts, "launch.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)

    def finish(code: int) -> int:
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        keep_last_frame(shots, os.path.join(args.artifacts, "screen.png"), note)
        encode_recording(shots, os.path.join(args.artifacts, "install.mp4"), note)
        return code

    # --- 1. the disk -----------------------------------------------------------
    if args.disk:
        source = args.disk
        note(f"using the local disk {source}")
    else:
        pulled = os.path.join(work, "windows11-gold.qcow2")
        try:
            iso.fetch_tag(args.tag, pulled)
        except iso.IsoError as exc:
            note(f"FAILED: could not pull {args.tag}: {exc}")
            return finish(1)
        source = pulled
        note(f"pulled {args.tag}: {os.path.getsize(source) >> 20} MiB")

    # NOTHING IS WRITTEN TO THE PUBLISHED DISK. qemu opens it read-write regardless, so
    # the working copy is the disk that gets started; a launch that modified the artifact
    # would make the next launch a test of something else.
    disk = os.path.join(work, "disk.qcow2")
    if os.path.abspath(source) != os.path.abspath(disk):
        note(f"copying {source} -> {disk}")
        shutil.copyfile(source, disk)

    # --- 2. what is on the disk, before starting it ----------------------------
    #
    # A disk that has no Windows boot loader cannot reach a desktop, and the firmware
    # says so in a way that is easy to miss under nine minutes of "waiting". Checking
    # the file is instant and names the problem.
    if smoke.check_disk(disk, root_fs="windows"):
        note("the disk holds an EFI boot loader")
    else:
        note("=== THE DISK HAS NO WINDOWS BOOT LOADER ===")
        note("  the firmware will fall through to PXE and its own shell; see above")
        note("  this is a bake failure, not a launch failure - nothing below can help")
        return finish(1)

    # --- 3. firmware ---------------------------------------------------------
    #
    # NOTHING IS RESTORED, EVER. The disk is self-booting: bake.py writes the Windows loader
    # to the UEFI fallback path \EFI\BOOT\BOOTX64.EFI, the firmware tries it when no boot
    # entry resolves, and a fresh variable store is then enough. That is the machine a person
    # who downloads the artifact gets, so it is the machine this launch tests.
    #
    # This used to require a bootstate archive and refuse to start without one, which made a
    # launch of a perfectly good disk fail on a missing artifact. `--allow-blank-firmware` is
    # still accepted - it was the flag that ran this experiment - but it now describes what
    # always happens rather than an alternative.
    firmware_code = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd")
    firmware_vars = os.path.join(work, "OVMF_VARS.fd")
    template = os.environ.get("OVMF_VARS_TEMPLATE", "/usr/share/OVMF/OVMF_VARS_4M.fd")
    shutil.copyfile(template, firmware_vars)
    note("booting with a fresh variable store; the disk carries \\EFI\\BOOT\\BOOTX64.EFI")
    if args.bootstate:
        note(f"--bootstate {args.bootstate} IGNORED for the same reason")
    if args.allow_blank_firmware:
        note("  (--allow-blank-firmware: this was the experiment; it is now the only path)")

    # DOCKUR-WINDOWS DOES NOT GIVE WINDOWS A TPM AT ALL, and it installs and boots
    # Windows 11. Its answer file bypasses the check in WinPE:
    #
    #     reg.exe add "HKLM\SYSTEM\Setup\LabConfig" /v BypassTPMCheck ...
    #     reg.exe add "HKLM\SYSTEM\Setup\LabConfig" /v BypassSecureBootCheck ...
    #
    # Ours does the same, AND provides a TPM. That is the difference: with a TPM present
    # Windows seals state against it, so the disk and the TPM become a pair that cannot
    # be separated - and every way of getting the pair wrong shows up as the firmware
    # failing to find a boot loader, or as CMD_INIT 0x9, rather than as anything about a
    # TPM.
    #
    # THE DEFAULT IS NO TPM, the way dockur-windows runs Windows 11. --tpm is for the
    # case that wants the pairing on purpose, and then the swtpm state this run creates is
    # the only one the guest has ever seen - which is the situation the flag is for.
    tpm = sshstage.start_swtpm(work) if args.tpm else ""
    note("starting WITHOUT a TPM, the way dockur-windows does"
         if not args.tpm else "starting WITH a TPM and the state from the archive")
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        firmware_code=firmware_code, firmware_vars=firmware_vars,
        tpm_socket=tpm, vga="std", ssh_port=2222,
        resolution=parse_resolution(args.resolution),
    )
    console = os.path.join(work, "console.log")
    # THE DISK HAS TO BE ATTACHED, AND FOR SIX RUNS IT WAS NOT.
    #
    # This stage copies the published disk, restores its firmware state, and then started
    # a machine with `extra=[]` - no drive at all. Every symptom that followed was read
    # as something else:
    #
    #     Boot000A "Windows Boot Manager" ... bootmgfw.efi: Not Found
    #     map -r  ->  BLK0 only, no FS0:
    #
    # The firmware could not find a boot loader because there was no Windows disk to
    # find it on: BLK0 was the firmware's own variable store. bake.py passes its drives
    # through `extra`, and this stage never did.
    drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=console).start()
    note(f"qemu started, waiting up to {args.boot_timeout}s for a desktop")

    arrived = False
    # DECLARED HERE, NOT INSIDE THE TRY. A name first bound in a try block is unbound
    # after an exception in it, and this one is read after the finally - so an early
    # failure would turn a boot problem into a NameError. None means "not asked".
    ssh_ok: bool | None = None
    seen: list[str] = []
    started = time.time()
    # HOW LONG TO WAIT BEFORE GIVING UP ON A DESKTOP.
    #
    # --probe-after exists because 900 seconds of a screen that has been black since
    # frame one tells nobody anything. The firmware reaches its shell in about ten
    # seconds; if there is no desktop by then, waiting is not going to produce one, and
    # the useful thing is to ask the shell directly.
    #
    #     --probe-after -1   do not wait at all: probe as soon as qemu is up
    #     --probe-after 60   give it a minute
    #     0 (default)        the whole --boot-timeout, the old behaviour
    wait_for = args.boot_timeout
    if args.probe_shell and args.probe_after:
        wait_for = args.probe_after if args.probe_after > 0 else 0

    kb = None
    try:
        if wait_for == 0:
            note("not waiting for a desktop; going straight to the firmware shell")
        shots_dir = os.path.join(args.artifacts, "frames")
        os.makedirs(shots_dir, exist_ok=True)
        # A KEYBOARD ONLY WHEN THERE IS TIME TO USE ONE. The wake happens a minute in; a
        # run that is going straight to the firmware shell has nothing to wake.
        if wait_for > 60:
            try:
                kb = sshstage.Keyboard(guest.paths.drive, shots)
            except (SystemExit, OSError) as exc:
                note(f"  no keyboard, so a lock page cannot be woken: {exc}")
        arrived = sample_until(guest, shots, wait_for, note, seen,
                               screen_dir=shots_dir, kb=kb)

        # THE PROBE RUNS WHILE THE MACHINE IS STILL UP. It types into the firmware
        # shell, so it cannot happen after guest.stop() - and it used to: the first
        # version called it below the finally block, so the keyboard would have been
        # attached to a dead qemu. Run 41565960604 never got that far, because the
        # flag was also spelled without `args.` and raised NameError first.
        if not arrived and args.probe_shell:
            probe_firmware_shell(guest, shots, note)

        # AND THE SSHD CHECK, FOR THE SAME REASON AND WITH THE SAME PREVIOUS MISTAKE.
        #
        # The forward is a property of the running qemu: 2222 is a host port that exists
        # only while this machine does. Run 41595773313 put this call after the finally
        # block, so `guest.stop()` had already run and the check reported
        #
        #     SSHD DID NOT ANSWER on 127.0.0.1:2222: [Errno 111] Connection refused
        #
        # about a disk that had booted, in 74 seconds, to a desktop. The block's own
        # comment said "it happens BEFORE the machine is stopped", which was true of the
        # sentence and false of the code.
        #
        # SO IT RUNS HERE, inside the try, before the finally that stops the machine.
        if arrived and args.check_ssh:
            ssh_ok = check_sshd(note) == 0
    finally:
        if kb is not None:
            kb.close()
        guest.stop(acpi_first=arrived)
        sshstage.stop_swtpm(work)

    note(f"=== {'A DESKTOP IS ON SCREEN' if arrived else 'NO DESKTOP'} ===")
    note(f"  {len(seen)} frames captured over {time.time() - started:.0f}s")

    # WHAT THE SCREEN SAID, not just that it never arrived. For a guest in the firmware
    # shell this is the whole diagnosis.
    evidence = read_screen(seen[-1]) if seen else ""
    if evidence:
        note("  the last frame reads:")
        for line in evidence.splitlines()[:12]:
            note(f"    {line}")

    if arrived:
        note("OK: the published disk reached its desktop")
        # THE RESULT OF THE CHECK THAT ALREADY RAN, not a second attempt. It cannot be a
        # second attempt: the machine is stopped by now, and the forward with it.
        if args.check_ssh:
            if ssh_ok:
                note("OK: sshd answered on the forwarded port")
                return finish(0)
            note("=== A DESKTOP, BUT NOT SSHD ===")
            note("  the boot is fine; what failed is the stage that installed OpenSSH")
            return finish(1)
        return finish(0)

    note("the guest did not reach a desktop; the frames say where it stopped")
    return finish(1)


def check_sshd(note) -> int:
    """Is sshd answering on the forwarded port? Returns a process exit code.

    THE BANNER, NOT A PORT SCAN. A TCP connect succeeds against anything listening, and
    the question is whether the guest is running OpenSSH - the first bytes it sends say
    so. lib/vm.py already has wait_port; this adds the part that reads them.

    A REFUSED CONNECTION IS NOT RETRIED HERE. sshd is started by the previous stage and
    enabled at boot; if it is not listening by the time the desktop is up, that is the
    answer, and waiting longer only delays it.
    """
    import socket

    import net

    # THE FORWARD IS ON THE BRIDGE ADDRESS, NOT ON LOOPBACK: DNAT rewrites the destination
    # and not the source, so a probe from 127.0.0.1 makes the guest answer its own loopback
    # and the connect hangs. lib/net.py records the measurement.
    host, port = net.FORWARD_HOST, 2222
    note(f"checking sshd on {host}:{port}")
    try:
        with socket.create_connection((host, port), timeout=20) as sock:
            sock.settimeout(20)
            banner = sock.recv(256).decode("utf-8", "replace").strip()
    except OSError as exc:
        note(f"=== SSHD DID NOT ANSWER on {host}:{port}: {exc} ===")
        note("  the desktop is up, so this is the ssh stage's result and not the boot's")
        return 1

    note(f"  banner: {banner}")
    if banner.startswith("SSH-2.0"):
        note("OK: sshd is answering on the forwarded port")
        return 0
    note("=== SOMETHING ANSWERED, BUT NOT SSHD ===")
    return 1


def parse_resolution(text: str) -> tuple[int, int] | None:
    """WxH -> (w, h), or None for the firmware default.

    A malformed value is fatal rather than ignored: it would silently fall back to
    1280x800 and produce a recording at a size nobody asked for, which is the thing
    this option exists to prevent.
    """
    if not text:
        return None
    try:
        width, height = (int(v) for v in text.lower().split("x"))
    except ValueError:
        raise SystemExit(f"--resolution {text!r} is not WxH, e.g. 1920x1080")
    if width < 640 or height < 480:
        raise SystemExit(f"--resolution {text!r} is too small for a desktop")
    return (width, height)


def probe_firmware_shell(guest: "vm.Guest", shots: str, note) -> None:
    """Type into the UEFI shell and report what it says.

    WHY THIS EXISTS. Run 41562919683 had every fact the disk could offer:

        the ESP at 2048..534527 LBA, GUID 34a2eca8-e45b-4e9e-9915-23053919245b
        mtools reads \\EFI\\Microsoft\\Boot\\bootmgfw.efi inside it
        the firmware's own Boot000A names that same GUID and range

    and the firmware still said "Not Found". Two explanations fit and they need
    different fixes: the file is not readable through the firmware's file protocol, or
    the boot entry's path is. Asking the shell separates them - `dir` uses the same
    protocol the boot entry uses, and running the loader by hand proves whether it can
    start at all.

    Typing is the ssh stage's Keyboard, reused: it already waits for the QMP socket and
    settles after every key.
    """
    import time as _time

    sys.path.insert(0, os.path.join(REPO, "windows", "11"))
    import ssh as sshstage  # noqa: E402

    try:
        kb = sshstage.Keyboard(guest.paths.drive, shots)
    except (SystemExit, OSError) as exc:
        note(f"  could not attach a keyboard: {exc}")
        return

    note("  probing the firmware shell")
    commands = [
        ("map -r", "list the mapped devices"),
        ("fs0:", "select the first filesystem"),
        (r"dir \EFI\Microsoft\Boot", "the directory the boot entry names"),
        (r"\EFI\Microsoft\Boot\bootmgfw.efi", "run it by hand"),
    ]
    try:
        for text, why in commands:
            note(f"    > {text}   ({why})")
            sshstage.type_text(kb, text)
            kb.key("ret", settle=6.0)
            shot = sshstage.take_shot(guest, shots, label="probe")
            if shot:
                reading = read_screen(shot)
                if reading:
                    for line in reading.splitlines()[:10]:
                        note(f"      {line}")
            _time.sleep(2)
    finally:
        kb.close()


def sample_until(guest: "vm.Guest", shots: str, timeout: int, note, seen: list,
                 screen_dir: str = "", kb=None) -> bool:
    """Photograph the screen until a desktop appears or the timeout runs out.

    Returns True when the desktop test passes. Every kept frame's path is appended to
    `seen`, so the caller can read the last one whichever way this ends.

    THE WAITING IS THE SSH STAGE'S LOOP, NOT A SECOND COPY OF IT. That loop already
    knows three things this one did not: that a lit screen which stops changing is a lock
    page and not a crash, that a keypress tells which, and that the account's password
    goes in only after the screen is known not to be a desktop. Run 41575708286 is what
    the old copy did with a lock page - grey 77 for 257 seconds and then a dark screen -
    so keeping one copy is the whole point. This function now only keeps the pictures.
    """
    # EVEN "DO NOT WAIT" WAITS A LITTLE. The firmware needs about ten seconds to reach
    # its shell, and typing before the console is up sends keys to nothing.
    timeout = max(timeout, 20)
    last_keep = 0.0

    def keep(shot, elapsed):
        nonlocal last_keep
        if not shot:
            note(f"  {int(elapsed):4d}s  no frame yet")
            return
        seen.append(shot)
        if elapsed - last_keep < SAMPLE_KEEP_SECONDS:
            return
        last_keep = elapsed
        # ONE PNG PER SAMPLE, NAMED BY ITS SECOND.
        #
        # The first version wrote a single out/screen.png, overwritten each time. That is
        # worse than nothing on a run that ends in a black screen: run 41574255802 was
        # cancelled at 190 seconds, and the file it left held the LAST frame - mean grey 0
        # - while the interesting ones, a lit screen at 49s and 159s, were overwritten.
        #
        # So every sample keeps its own file. The timeline survives whatever happens next,
        # and a cancelled run uploads the whole of it.
        note(f"  {int(elapsed):4d}s  {sshstage.describe_shot(shot)}")
        if screen_dir:
            try:
                import lastframe
                lastframe.convert(
                    shot, os.path.join(screen_dir, f"screen-{int(elapsed):04d}s.png"))
            except Exception:  # noqa: BLE001 - a picture is not worth a run
                pass

    return sshstage.wait_for_desktop(guest, shots, timeout, kb=kb,
                                     on_sample=keep, say=note)


def read_screen(path: str) -> str:
    """OCR what the last frame says, when tesseract is available.

    NOT A DECISION INPUT. Nothing in this repository decides anything from OCR - the
    macOS driver reads coordinates and the Windows driver reads pixels. This is for the
    report, after the fact, because a firmware shell printing "bootmgfw.efi: Not Found"
    is the failure stated in the guest's own words.
    """
    if not shutil.which("tesseract"):
        return ""
    try:
        import subprocess
        result = subprocess.run(["tesseract", path, "-", "--psm", "6"],
                                capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return ""
    return "\n".join(line for line in (result.stdout or "").splitlines() if line.strip())


def encode_recording(shots: str, dest: str, note) -> None:
    """The whole sampled sequence as an mp4. Never fatal.

    THE LAUNCH SAMPLES EVERY TWELVE SECONDS AND WAS THROWING THAT AWAY. Run 41562919683
    captured 74 frames over 905s and uploaded one of them - so a firmware shell that
    appeared at 12s and a black screen that appeared at 265s were indistinguishable in
    the artifact, and the only way to see the difference was to run it again.

    The recording is what explains a failure here. lib/vm.record_many() is the same
    encoder the bakes use, so the file plays like theirs.
    """
    try:
        import vm
        if vm.record_many(dest, shots, fps=int(os.environ.get("RECORD_FPS", "1"))):
            note(f"recording: {dest} ({os.path.getsize(dest) >> 20} MiB)")
        else:
            note("recording: nothing to encode")
    except Exception as exc:  # noqa: BLE001 - a recording is not worth a failure
        note(f"could not encode the recording: {exc}")


def keep_last_frame(shots: str, dest: str, note) -> None:
    """The newest frame as a PNG, for the artifact. Never fatal."""
    try:
        import lastframe
        lastframe.main([shots, dest])
        note(f"last frame: {dest}")
    except Exception as exc:  # noqa: BLE001 - a picture is not worth a failure
        note(f"could not write {dest}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
