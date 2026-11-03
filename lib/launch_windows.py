"""Start a published Windows disk and photograph the screen. Nothing is installed.

    python3 lib/launch_windows.py --tag windows-base-20261102-155400 --artifacts out

WHAT THIS ANSWERS

    does the artifact in ghcr actually boot to a desktop

bake-windows publishes a disk and a bootstate archive; the ssh stage starts one and waits
for a desktop. That wait is nine minutes of silence, and when it fails it reports

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

import bootstate  # noqa: E402
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
                   help="the .bootstate.tar.gz; defaults to the one beside the disk")
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

    # --- 3. firmware and TPM state --------------------------------------------
    source_state = args.bootstate or bootstate.archive_path(source)
    if os.path.isfile(source_state):
        placed = bootstate.adopt(disk, source_state)
        note(f"bootstate {source_state} -> {placed}")
    restored = bootstate.restore(work, disk)
    if not restored:
        note(f"FAILED: no bootstate at {bootstate.archive_path(disk)}")
        note("  without OVMF's variables and the TPM, Windows will not start")
        return finish(1)
    note(f"bootstate restored into {work}")

    # --- 4. start it -----------------------------------------------------------
    firmware_code = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd")
    firmware_vars = os.path.join(work, "OVMF_VARS.fd")
    if not os.path.exists(firmware_vars):
        template = os.environ.get("OVMF_VARS_TEMPLATE",
                                  "/usr/share/OVMF/OVMF_VARS_4M.fd")
        shutil.copyfile(template, firmware_vars)

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
    # case that wants the pairing on purpose, and then the state has to travel with the
    # disk and be restored before every boot - see lib/bootstate.py.
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

    try:
        if wait_for == 0:
            note("not waiting for a desktop; going straight to the firmware shell")
        arrived = sample_until(guest, shots, wait_for, note, seen)

        # THE PROBE RUNS WHILE THE MACHINE IS STILL UP. It types into the firmware
        # shell, so it cannot happen after guest.stop() - and it used to: the first
        # version called it below the finally block, so the keyboard would have been
        # attached to a dead qemu. Run 41565960604 never got that far, because the
        # flag was also spelled without `args.` and raised NameError first.
        if not arrived and args.probe_shell:
            probe_firmware_shell(guest, shots, note)
    finally:
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
        return finish(0)

    note("the guest did not reach a desktop; the frames say where it stopped")
    return finish(1)


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


def sample_until(guest: "vm.Guest", shots: str, timeout: int, note, seen: list) -> bool:
    """Photograph the screen until a desktop appears or the timeout runs out.

    Returns True when the desktop test passes. Every kept frame's path is appended to
    `seen`, so the caller can read the last one whichever way this ends.
    """
    import desktop

    # EVEN "DO NOT WAIT" WAITS A LITTLE. The firmware needs about ten seconds to reach
    # its shell, and typing before the console is up sends keys to nothing.
    deadline = time.time() + max(timeout, 20)
    started = time.time()
    last_report = 0.0
    last_keep = 0.0

    while time.time() < deadline:
        if not guest.alive():
            note("qemu exited during the boot")
            return False
        shot = sshstage.take_shot(guest, shots)
        if shot:
            seen.append(shot)
            now = time.time()
            if now - last_keep >= SAMPLE_KEEP_SECONDS:
                last_keep = now
                note(f"  {int(now - started):4d}s  {sshstage.describe_shot(shot)}")
        else:
            note(f"  {int(time.time() - started):4d}s  no frame yet")

        if shot and desktop.is_desktop(shot):
            note(f"a desktop is on screen after {time.time() - started:.0f}s")
            return True

        if time.time() - last_report >= 60:
            last_report = time.time()
            note(f"waiting {time.time() - started:.0f}s for a desktop")
        time.sleep(12)

    return False


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
