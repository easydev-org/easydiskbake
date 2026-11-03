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
    )
    console = os.path.join(work, "console.log")
    guest = vm.Guest(work, extra=[], shape=shape, stdout=console).start()
    note(f"qemu started, waiting up to {args.boot_timeout}s for a desktop")

    arrived = False
    seen: list[str] = []
    started = time.time()
    try:
        arrived = sample_until(guest, shots, args.boot_timeout, note, seen)
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


def sample_until(guest: "vm.Guest", shots: str, timeout: int, note, seen: list) -> bool:
    """Photograph the screen until a desktop appears or the timeout runs out.

    Returns True when the desktop test passes. Every kept frame's path is appended to
    `seen`, so the caller can read the last one whichever way this ends.
    """
    import desktop

    deadline = time.time() + timeout
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
