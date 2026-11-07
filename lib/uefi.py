"""Give a guest UEFI firmware, with its own writable NVRAM.

    from uefi import shape_with_uefi
    shape = shape_with_uefi(vm.Shape(cpus=4, mem_mb=4096), work, "debian")

WHY EVERY GUEST IN THIS REPOSITORY NOW USES UEFI

The first version of the Linux GUI bakes used SeaBIOS, because that is what the original
Windows and MX work used and it boots a live ISO perfectly well. Two things then measured
worse:

GARUDA REFUSES IT. Its installer opens with

    The system is not booted in UEFI mode, but in BIOS/CSM/Legacy (unsupported) mode

and the disk it produced on run after run would not boot: 4.48 GB written, then a black
screen forever. The installer had said so before it started.

EVERY MODERN DISTRIBUTION SHIPS A UEFI BOOT PATH FIRST. GPT partitioning, an EFI system
partition, and a bootloader installed to the ESP are what the images expect; the BIOS path
exists for old hardware and is the one that gets less testing.

WHAT IT COSTS. A UEFI-booted installer lays out the disk differently - an ESP instead of a
BIOS boot partition - so any pane that shows a partition summary looks different. The
coordinates in each driver were measured on the BIOS path and are re-measured where the
layout changed.

WHY THE VARS FILE IS A COPY. OVMF's CODE image is read-only and shared; the VARS image is
the machine's NVRAM, and it accumulates boot entries. A shared template would carry one
guest's boot order into the next run, which is the rule lib/vm.py states and this module
implements in one place instead of in every driver.

WHY THE PATHS ARE ARGUMENTS. The bake image ships OVMF at /usr/share/OVMF, and a developer
on a distribution that puts it under /usr/share/edk2 or /usr/share/qemu should not have to
patch a driver to run it.
"""

from __future__ import annotations

import os
import shutil

DEFAULT_CODE = "/usr/share/OVMF/OVMF_CODE_4M.fd"
DEFAULT_VARS = "/usr/share/OVMF/OVMF_VARS_4M.fd"


class UefiError(RuntimeError):
    """The firmware the caller asked for is not installed."""


def firmware_paths(code: str = "", vars_template: str = "") -> tuple[str, str]:
    """Where the read-only CODE image and the VARS template are."""
    return (code or os.environ.get("FIRMWARE_CODE") or DEFAULT_CODE,
            vars_template or os.environ.get("FIRMWARE_VARS") or DEFAULT_VARS)


def shape_with_uefi(shape, work: str, name: str, *,
                   code: str = "", vars_template: str = "") -> "shape":
    """Attach OVMF to `shape` in place, with <work>/<name>.vars as its NVRAM.

    THE VARS COPY IS REMOVED FIRST. A previous run's NVRAM carries its boot entries and its
    Secure Boot state, and after a failed install that means the firmware remembers a disk
    that no longer has a system on it - so the next run boots nothing, or boots the wrong
    thing, and the failure looks like a broken image.
    """
    code_path, tmpl = firmware_paths(code, vars_template)
    missing = [p for p in (code_path, tmpl) if not os.path.isfile(p)]
    if missing:
        raise UefiError(
            "=== UEFI FIRMWARE NOT FOUND ===\n"
            f"  missing: {', '.join(missing)}\n"
            "  install OVMF (edk2-ovmf / ovmf) or set FIRMWARE_CODE and FIRMWARE_VARS.\n"
            "  Every guest here boots on UEFI: a BIOS-only boot produces disks that the\n"
            "  installers themselves warn about - Garuda refuses Legacy mode outright.")

    os.makedirs(work, exist_ok=True)
    vars_copy = os.path.join(work, f"{name}.vars")
    if os.path.exists(vars_copy):
        os.unlink(vars_copy)
    shutil.copyfile(tmpl, vars_copy)

    shape.firmware_code = code_path
    shape.firmware_vars = vars_copy
    return shape


def describe(shape) -> str:
    """One line for the log, saying which firmware the guest got."""
    if not shape.firmware_code:
        return "firmware: SeaBIOS (no OVMF attached)"
    return f"firmware: UEFI {os.path.basename(shape.firmware_code)}"
