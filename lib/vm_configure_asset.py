"""Fetch a file ON THE HOST, from a step in a walkthrough.

    - name: fetch the guest agent on the host
      python: vm_configure_asset:fetch_guest_agent
      python_screen: false
      timeout: 600

WHY A `python:` STEP AND NOT `script:`

`script:` COPIES A BODY INTO THE GUEST AND RUNS IT THERE. That is the right tool for something
the disk has to do, and the wrong one for something the CONTAINER has to do before it can copy
anything in - a download happens on the host, and a `curl` inside the guest would be a download
through the guest's own slow network to a file the host then has to fetch back.

`python_screen: false` BECAUSE IT NEVER LOOKS AT THE SCREEN. Without it the walkthrough is
classified as needing a GUI driver, and these stages run over ssh with no framebuffer at all -
the failure is a 180-second wait for a QMP socket that will never exist.

WHERE THE FILES GO

    <work>/guest-agent/<name>

The next step is always `scp:` from that path into the guest, so the two agree by construction
rather than by two literals that have to be kept in step.
"""

from __future__ import annotations

import os
import subprocess
import sys

# WHAT EACH PLATFORM NEEDS, AND WHY THEY ARE DIFFERENT FILES.
#
#   macOS    mav2287/mac-guest-agent - a native agent for macOS under QEMU. Apple's own does not
#            load here; see the note in macos/15/steps/vm-configure.yaml.
#   Windows  guest-agent/qemu-ga-x86_64.msi, EXTRACTED FROM THE virtio-win ISO.
#
# WHY NOT virtio-win-guest-tools.exe, WHICH IS WHAT THIS STARTED WITH
#
# It is a WiX Burn bundle with a `NetFxChainer` - it manages a .NET Framework prerequisite - and
# under /quiet it did not return at all on this guest: two runs sat in `install the guest agent`
# for ten and twelve minutes with no output, and the command itself never finished, so not even
# the Wait-Process deadline could report (runs 42797556702, 42800603925).
#
# The MSI IS THE THING THE BUNDLE WOULD HAVE INSTALLED, and the ISO carrying it is 56 MiB - the
# same order as the bundle's 31 MiB. The bake already pulls it (`iso-virtio-win` from ghcr) for
# the drivers, so this is a file the pipeline already has.
ASSETS = {
    "mac-guest-agent": {
        "url": "https://github.com/mav2287/mac-guest-agent/releases/latest/download/mac-guest-agent",
        "name": "mac-guest-agent",
        "executable": True,
    },
    # THE MSI, OUT OF THE ISO - see the note above. `member` says which path inside the archive,
    # so the same fetch() can either download a file or extract one.
    "qemu-ga-msi": {
        "from_iso": True,
        "member": "guest-agent/qemu-ga-x86_64.msi",
        "name": "qemu-ga-x86_64.msi",
        "executable": False,
    },
}

# THE SAME CACHE THE OTHER ASSET STEPS USE. A re-run of a stage should not re-download 60 MiB
# because the previous attempt failed at something else.
CACHE = os.path.join("out", "assets")


def log(message: str) -> None:
    print(f"[vm-configure] {message}", flush=True)


def fetch(key: str, work: str = "") -> str:
    """Download ASSETS[key] once and return its path.

    RESUMABLE AND IDEMPOTENT, because a stage that is re-run after a failure an hour later should
    not pay for the download again. `curl -C -` continues a partial file; the size check after it
    is what says whether the result is whole, since curl exits 0 for a resumed file it did not
    have to ask about.
    """
    if key not in ASSETS:
        raise SystemExit(f"unknown asset {key!r}; known: {', '.join(ASSETS)}")
    spec = ASSETS[key]
    target_dir = os.path.join(work or ".", "guest-agent")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, spec["name"])

    if os.path.isfile(target) and os.path.getsize(target) > 1024:
        log(f"{spec['name']} is already here ({os.path.getsize(target) >> 20} MiB)")
    else:
        log(f"fetching {spec['url']}")
        # `--retry` BECAUSE THIS RUNS THROUGH A PROXY THAT DROPS LONG TRANSFERS, which is the
        # reason every other download in this repository retries too.
        result = subprocess.run(
            ["curl", "-fsSL", "-C", "-", "--retry", "5", "--retry-delay", "5",
             "--connect-timeout", "30", "-o", target, spec["url"]],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(
                f"could not fetch {spec['url']}: {(result.stderr or '').strip()[:300]}")
        log(f"  {target} ({os.path.getsize(target) >> 10} KiB)")

    if spec["executable"]:
        os.chmod(target, 0o755)
    return target


def fetch_from_iso(key: str, iso_path: str, work: str = "") -> str:
    """Extract ASSETS[key]["member"] out of `iso_path` into the work directory.

    THE SAME ISO THE BAKE ALREADY HAS. `iso-virtio-win` is pulled for the drivers during install;
    the guest agent is inside the same archive, under guest-agent/. 7z reads an ISO as an archive
    with no privileges, which is what lib/winiso.py's extract_drivers already relies on.
    """
    spec = ASSETS[key]
    if not os.path.isfile(iso_path):
        raise SystemExit(f"no virtio-win ISO at {iso_path}")
    target_dir = os.path.join(work or ".", "guest-agent")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, spec["name"])
    if os.path.isfile(target) and os.path.getsize(target) > 1024:
        log(f"{spec['name']} is already extracted ({os.path.getsize(target) >> 10} KiB)")
        return target
    log(f"extracting {spec['member']} from {os.path.basename(iso_path)}")
    result = subprocess.run(
        ["7z", "e", "-y", f"-o{target_dir}", iso_path, spec["member"]],
        capture_output=True, text=True)
    if result.returncode != 0 or not os.path.isfile(target):
        raise SystemExit(
            f"could not extract {spec['member']} from {iso_path}: "
            f"{(result.stderr or result.stdout).strip()[:300]}")
    log(f"  {target} ({os.path.getsize(target) >> 10} KiB)")
    return target


def fetch_guest_agent(driver, step, **_kwargs) -> bool:
    """The walkthrough step. The file says which platform with `family:`.

    NO GUESSING. A step that does not say gets an error naming what it should have said, because
    the two assets are a Mach-O binary and a Windows installer and copying the wrong one fails in
    the guest with a message about a corrupt file rather than about a wrong walkthrough.
    """
    family = str(step.get("family") or "")
    work = getattr(driver, "work", "") or "."
    if family == "macos":
        key = "mac-guest-agent"
        path = fetch(key, work)
    elif family == "windows":
        # THE MSI OUT OF THE ISO. Which ISO is the bake's own choice, so this asks lib/images
        # for the artifact rather than hard-coding a URL a second time.
        import images
        import iso as isomod
        iso_path = os.path.join(work, "virtio-win.iso")
        if not os.path.isfile(iso_path):
            # THE WORK DIRECTORY OF A VM-CONFIGURE JOB MAY NOT HAVE IT - the bake's did. Pulling
            # it here is the same call the bake makes, so there is one description of where it
            # comes from.
            iso_path = isomod.fetch_iso("virtio-win", iso_path)
        path = fetch_from_iso("qemu-ga-msi", iso_path, work)
        log(f"  ready for the copy: {path}")
        return True
    else:
        raise SystemExit(
            f"{getattr(step, 'name', 'this step')}: `python: vm_configure_asset:fetch_guest_agent` "
            f"needs `family: macos` or `family: windows` on the step, got {family!r}")
    work = getattr(driver, "work", "") or "."
    path = fetch(key, work)
    # THE STEP AFTER THIS ONE IS AN `scp:` THAT NAMES THE PATH, so it is returned in the log
    # where a failing copy can be compared against it.
    log(f"  ready for the copy: {path}")
    return True
