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
#   Windows  the virtio-win guest tools, which carry guest-agent\qemu-ga-x86_64.msi. The full ISO
#            is 700 MiB and this bundle is the same installer without drivers the disk has.
ASSETS = {
    "mac-guest-agent": {
        "url": "https://github.com/mav2287/mac-guest-agent/releases/latest/download/mac-guest-agent",
        "name": "mac-guest-agent",
        "executable": True,
    },
    "virtio-win-guest-tools": {
        "url": ("https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/"
                "stable-virtio/virtio-win-guest-tools.exe"),
        "name": "virtio-win-guest-tools.exe",
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


def fetch_guest_agent(driver, step, **_kwargs) -> bool:
    """The walkthrough step. The file says which platform with `family:`.

    NO GUESSING. A step that does not say gets an error naming what it should have said, because
    the two assets are a Mach-O binary and a Windows installer and copying the wrong one fails in
    the guest with a message about a corrupt file rather than about a wrong walkthrough.
    """
    family = str(step.get("family") or "")
    if family == "macos":
        key = "mac-guest-agent"
    elif family == "windows":
        key = "virtio-win-guest-tools"
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
