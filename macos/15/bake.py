"""Bake a macOS release to a qcow2.

    OUT=out/macos15.qcow2 python3 macos/15/bake.py

WHY THIS IS MOSTLY NOT WRITTEN YET

The two hard parts of a macOS bake live in dockur-macos and neither is in lib/:

  1. The installer is not an ISO. It is a recovery image fetched from
     osrecovery.apple.com with a board identifier and opaque hex nonce fields, and
     the response is a CDN URL with an expiring token. src/install.sh records four
     distinct failure modes in that exchange.
  2. OpenCore. macOS is not installed by firmware; it is installed by an EFI payload
     that also carries the machine identity and the SMC key. src/boot.sh builds that
     image, and its size is why OVMF_VARS is per-resolution.

The good news, and the reason this file is short rather than 500 lines: the Setup
Assistant problem - which is what drive-ocr.py (2112 lines) and drive-coords.py (397
lines) exist to solve - has a cheaper answer. Write the marker:

    /private/var/db/.AppleSetupDone

dockur-macos/.../repair-setup-assistant.yml ended on exactly that, and its comment
says why it wins: "The marker Setup Assistant reads is unambiguous." It needs no
coordinates, no OCR and no per-release pane sequence.

So the port should NOT carry over the drivers. See ../README.md.

WHY THE STAGES BELOW ARE NOT IMPLEMENTED

Each unimplemented stage stops with a message rather than booting a disk that will
sit at a pane. A bake that half-works costs a 90-minute CI run to discover it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from lib import smoke, vm  # noqa: E402

RELEASE = os.environ.get("RELEASE", "15")
DISK_SIZE = os.environ.get("DISK_SIZE", "80G")
BOARD_ID = os.environ.get("BOARD_ID", "Mac-827FAC58A8FDFA22")


def log(msg: str) -> None:
    print(f"[bake-macos {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    out = os.environ.get("OUT")
    if not out:
        raise SystemExit("OUT must be set to the qcow2 to produce")

    work = os.path.dirname(os.path.abspath(out))
    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    os.chdir(work)

    # --- the recovery image -------------------------------------------------
    log("=== recovery image ===")
    recovery = os.path.join(work, "recovery.dmg")
    if not os.path.exists(recovery) or os.path.getsize(recovery) == 0:
        # NOT PORTED. Two requests: one for the session cookie, one for the payload,
        # with hex nonces and a board identifier, then a CDN URL with a token in it.
        # The whole exchange is in dockur-macos/src/install.sh and should be lifted
        # rather than reinvented - each of its error branches is a failure that took
        # a run to find.
        log("STOP: the osrecovery.apple.com exchange is not ported yet.")
        log("See dockur-macos/src/install.sh (lines ~94-215). Remove this guard when it is.")
        return 3

    # --- the disk -----------------------------------------------------------
    log("=== the disk ===")
    disk = os.path.join(work, "disk.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "disk.qcow2", DISK_SIZE], check=True)

    # --- firmware -----------------------------------------------------------
    log(f"=== firmware: OVMF at this release's resolution ({BOARD_ID}) ===")
    template = os.environ.get(
        "OVMF_VARS_TEMPLATE",
        os.path.join(HERE, "firmware", "OVMF_VARS-1920x1080.fd"),
    )
    vars_copy = os.path.join(work, "OVMF_VARS.fd")
    if not os.path.exists(vars_copy):
        if not os.path.isfile(template):
            log(f"STOP: no OVMF vars template at {template}")
            log("The GOP resolution is baked into it; a disk baked at one resolution")
            log("does not boot at another. See ../README.md.")
            return 3
        import shutil
        shutil.copyfile(template, vars_copy)

    # --- OpenCore -----------------------------------------------------------
    log("=== OpenCore ===")
    opencore = os.path.join(HERE, "opencore")
    if not os.path.isdir(opencore):
        log(f"STOP: macos/{RELEASE}/opencore/ is missing (Drivers/, Kexts/, config.plist).")
        log("The SMC key and machine identity are generated, not committed - see ../README.md.")
        return 3

    # --- boot ---------------------------------------------------------------
    log("=== boot: UEFI, OpenCore as the first disk ===")
    shape = vm.Shape(
        mem_mb=int(os.environ.get("VM_MEM", "8192")),
        machine="q35",
        firmware_code=os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd"),
        firmware_vars=vars_copy,
        vga=os.environ.get("VM_VGA", "vmware"),
    )
    guest = vm.Guest(
        work,
        extra=[
            "-device", f"isa-applesmc,osk={os.environ.get('APPLE_OSK', '')}",
            "-drive", "file=opencore.img,format=raw,if=none,id=oc",
            "-device", "ide-hd,bus=ide.0,drive=oc",
            "-drive", "file=disk.qcow2,if=virtio,format=qcow2",
        ],
        shape=shape,
        stdout=os.path.join(work, "console.log"),
    ).start()

    log("=== waiting for startosinstall ===")
    time.sleep(int(os.environ.get("INSTALL_SECONDS", "5400")))
    guest.stop()

    # --- the marker ---------------------------------------------------------
    log("=== drop the Setup Assistant marker ===")
    # Mounting the disk's system volume on the host and creating
    # /private/var/db/.AppleSetupDone replaces all of the OCR work. The volume is
    # APFS, so this needs apfs-fuse or a throwaway guest - which one is a decision
    # worth making before writing this step.
    log("NOT PORTED: needs an APFS writer on the host. See ../README.md.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
