"""Put Autounattend.xml into a Windows ISO without breaking its boot records.

    from winiso import rebuild
    rebuild("clean.iso", "windows/11/answer", "boot.iso")

WHY THE ISO HAS TO BE REBUILT AT ALL

Windows setup looks for \\Autounattend.xml at the ROOT of the boot medium and
nowhere else. It cannot be passed on the kernel command line (setup.exe is started
by bootmgr, not by a kernel we control), and there is no second-CD convention for
it. So the file has to be inside the ISO.

WHY xorriso AND NOT genisoimage

dockur/windows uses genisoimage and has to do three things by hand that are easy
to get subtly wrong:

  1. extract the El Torito boot image with `dd` at an offset reported by isoinfo,
     because genisoimage cannot inherit one;
  2. re-specify `-b boot.img -eltorito-alt-boot -eltorito-boot efisys.bin` and
     every `-no-emul-boot` / `-boot-load-size` flag, for both the BIOS and the
     UEFI entry;
  3. pick a different set of those flags for legacy media.

xorriso reads the source ISO's boot records and writes them back unchanged:

    -boot_image any keep

so the rebuilt image boots exactly like the original, and there is no boot image
lying around as a separate file that could be truncated. The bake image already
carries xorriso (see Containerfile), so this is strictly less code and strictly
fewer ways to fail.

It is also much faster: xorriso copies, genisoimage re-encodes.

WHY THIS IS PYTHON NOW

$OEM$ and $$ are literal directory names on the Windows side that mean something
to setup. In shell they have to be written "\\$OEM\\$/\\$\\$/Setup/Scripts" and two
of those backslashes are inside a double-quoted string inside a function inside a
script - the exact shape that fix_grub.py's docstring lists four failures of. In
Python the path is "$OEM$/$$/Setup/Scripts" and there is no layer to eat it.

WHAT IS NOT DONE

The ISO is not fully extracted and re-mastered. Only the answer file and the setup
scripts are added, so every other byte comes from the original image and its
layout is preserved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

# Kept as documentation of the convention this bake does NOT use any more.
#
# Setup reads $OEM$/$$/Setup/Scripts/SetupComplete.cmd if it exists, which is how
# dockur stages guest-side validation. This bake deliberately stages nothing: the
# edition is verified from the host with lib/wim.py before the guest boots, and the
# arrival is verified on port 3389. See wim.py's docstring for why that is better
# than asking the guest.
OEM_SCRIPTS = "$OEM$/$$/Setup/Scripts"


def log(msg: str) -> None:
    print(f"[iso] {msg}", flush=True)


class IsoError(RuntimeError):
    pass


def _xorriso(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("xorriso"):
        raise IsoError("xorriso is not installed; it is in the bake image (see Containerfile)")
    result = subprocess.run(["xorriso", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise IsoError(
            f"xorriso {' '.join(args)} failed ({result.returncode}):\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
    return result


def _stage(answer_dir: str) -> str:
    """Build the overlay tree that gets merged onto the ISO root.

    Only Autounattend.xml. There is deliberately no $OEM$ tree here: everything a
    guest script would have done is either already in the answer file (RDP, the
    account, the OOBE skips) or is verified from the host instead (the edition,
    via lib/wim.py; the arrival, via port 3389).

    A guest that needs no staged script is a guest with one fewer way to fail, and
    the failures it would have had are not lost - they moved to a place where they
    are cheaper to see.
    """
    overlay = tempfile.mkdtemp(prefix="winiso-")

    source = os.path.join(answer_dir, "Autounattend.xml")
    if not os.path.isfile(source):
        raise IsoError(f"no Autounattend.xml in {answer_dir}")
    shutil.copyfile(source, os.path.join(overlay, "Autounattend.xml"))

    log("overlay: Autounattend.xml (no $OEM$: the guest stages no scripts)")
    return overlay


def rebuild(source: str, answer_dir: str, out: str) -> str:
    """Merge the answer file into `source` and write `out`.

    The result is verified before returning: it must contain the answer file at
    its root and it must still have an El Torito boot record. Both failures are
    otherwise invisible until a VM boots to nothing and hangs, which is the most
    expensive way to find out.
    """
    if not os.path.isfile(source) or os.path.getsize(source) == 0:
        raise IsoError(f"source ISO missing or empty: {source}")

    # xorriso refuses to overwrite, and a stale output would be published as if
    # it were this build.
    if os.path.exists(out):
        os.unlink(out)

    overlay = _stage(answer_dir)
    try:
        log(f"rebuilding {source} -> {out}")
        result = _xorriso(
            "-indev", source,
            "-outdev", out,
            # The whole trick: inherit the source's BIOS and UEFI boot records
            # rather than re-specifying them.
            "-boot_image", "any", "keep",
            "-overwrite", "on",
            "-map", os.path.join(overlay, "Autounattend.xml"), "/Autounattend.xml",
            "-commit",
        )
        for line in result.stdout.splitlines():
            if line.strip():
                log(line)
    finally:
        shutil.rmtree(overlay, ignore_errors=True)

    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise IsoError("xorriso produced no output")

    _verify(out)
    log(f"ok: {os.path.getsize(out)} bytes, answer file at root, boot record preserved")
    return out


def _verify(path: str) -> None:
    listing = _xorriso("-indev", path, "-find", "/Autounattend.xml", "-exec", "lsdl", check=False)
    if "Autounattend" not in (listing.stdout + listing.stderr):
        raise IsoError(
            "THE REBUILT ISO DOES NOT CONTAIN /Autounattend.xml\n"
            "setup would sit at the first pane forever. Refusing to continue."
        )

    torito = _xorriso("-indev", path, "-report_el_torito", "plain", check=False)
    if "el torito" not in (torito.stdout + torito.stderr).lower():
        raise IsoError(
            "THE REBUILT ISO HAS NO EL TORITO BOOT RECORD\n"
            "a rebuild that dropped it produces an image nothing boots from, and the\n"
            "failure looks like the VM hung."
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 4:
        raise SystemExit("usage: winiso.py <clean.iso> <answer-dir> <out.iso>")
    rebuild(sys.argv[1], sys.argv[2], sys.argv[3])
