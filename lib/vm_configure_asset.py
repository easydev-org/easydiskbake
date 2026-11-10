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
import shutil
import subprocess
import sys

# ONE PLATFORM, BECAUSE THE OTHER ONE MOVED.
#
#   macOS    mav2287/mac-guest-agent - a native agent for macOS under QEMU. Apple's own does not
#            load here; see the note in macos/15/steps/vm-configure.yaml.
#   Windows  NOT HERE. qemu-ga is installed by Setup, from the answer file's specialize pass, out
#            of the MSI lib/winiso.py puts on the answer CD.
#
# WHY WINDOWS MOVED, IN ONE SENTENCE: the disk this stage is given has already booted once, and a
# first boot with no agent is a boot that cannot be shut down by one. It is also one fewer 7z call
# and one fewer 12 MB copy per re-run, and the bundle this repository started with -
# virtio-win-guest-tools.exe, a WiX Burn bootstrapper with a NetFxChainer - never returned under
# /quiet at all (runs 42797556702, 42800603925).
ASSETS = {
    "mac-guest-agent": {
        "url": "https://github.com/mav2287/mac-guest-agent/releases/latest/download/mac-guest-agent",
        "name": "mac-guest-agent",
        "executable": True,
    },
    # THE MSI OUT OF THE virtio-win ISO. `member` says which path inside the archive, so the
    # same fetch() can either download a file or extract one.
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


def fetch_driver(work: str, driver: str = "vioserial") -> str:
    """Extract one driver's files from the virtio-win ISO into guest-agent/<driver>/.

    A DIRECTORY, NOT A FLAT NAME: `pnputil /add-driver vioser.inf /install` reads the INF and
    finds its .sys and .cat beside it, so the three files have to arrive together.
    """
    target_dir = os.path.join(work or ".", "guest-agent", driver)
    if os.path.isdir(target_dir) and any(f.endswith(".inf") for f in os.listdir(target_dir)):
        return target_dir
    import images
    import iso as isomod

    iso_path = os.path.join(work, "virtio-win.iso")
    if not os.path.isfile(iso_path):
        iso_path = isomod.fetch_iso("virtio-win", iso_path)
    os.makedirs(target_dir, exist_ok=True)
    for version_dir in ("w11", "w10", "2k19"):
        member = f"{driver}/{version_dir}/amd64"
        result = subprocess.run(
            ["7z", "x", "-y", f"-o{target_dir}", iso_path, f"{member}/*"],
            capture_output=True, text=True)
        source = os.path.join(target_dir, *member.split("/"))
        if result.returncode != 0 or not os.path.isdir(source) or not os.listdir(source):
            continue
        for entry in os.listdir(source):
            shutil.move(os.path.join(source, entry), os.path.join(target_dir, entry))
        shutil.rmtree(os.path.join(target_dir, driver), ignore_errors=True)
        log(f"driver: {driver} -> {target_dir}")
        return target_dir
    raise SystemExit(f"no {driver} driver in {iso_path}")


def fetch_guest_agent(driver, step, **_kwargs) -> bool:
    """The walkthrough step. The file says which platform with `family:`.

    NO GUESSING. A step that does not say gets an error naming what it should have said, because
    the asset is a Mach-O binary and copying the wrong thing fails in the guest with a message
    about a corrupt file rather than about a wrong walkthrough.

    WINDOWS IS NOT HERE ANY MORE. Its agent is installed by Setup, from the answer file's
    specialize pass, with the MSI that lib/winiso.py copies onto the answer CD
    (`guest-agent/qemu-ga-x86_64.msi` out of the same virtio-win ISO the drivers come from). The
    extraction that used to live here was a SECOND copy of that same 7z call, and it ran on a
    disk whose first boot had already had no agent - see windows/11/steps/vm-configure.yaml.
    """
    family = str(step.get("family") or "")
    work = getattr(driver, "work", "") or "."
    if family == "windows":
        # THE MSI AND ITS DRIVER, BOTH OUT OF THE ISO. The bake no longer installs either: a
        # virtio-serial device during Setup is what broke three runs, so this stage does.
        import images
        import iso as isomod

        iso_path = os.path.join(work, "virtio-win.iso")
        if not os.path.isfile(iso_path):
            iso_path = isomod.fetch_iso("virtio-win", iso_path)
        path = fetch_from_iso("qemu-ga-msi", iso_path, work)
        drv = fetch_driver(work)
        log(f"  ready for the copy: {path}")
        log(f"  ready for the copy: {drv}/vioser.inf")
        return True
    if family != "macos":
        raise SystemExit(
            f"{getattr(step, 'name', 'this step')}: `python: vm_configure_asset:fetch_guest_agent` "
            f"needs `family: macos` or `family: windows`, got {family!r}")
    path = fetch("mac-guest-agent", work)
    # THE STEP AFTER THIS ONE IS AN `scp:` THAT NAMES THE PATH, so it is returned in the log
    # where a failing copy can be compared against it.
    log(f"  ready for the copy: {path}")
    return True
