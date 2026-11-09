"""The two steps of the macOS dev stage that a shell cannot do.

    python: macos_dev_asset:fetch_xcode      download and verify the .xip on the host
    python: macos_dev_asset:copy_xcode       push it into the guest, and the installer script

WHY THESE ARE PYTHON AND NOT `ssh:`

`ssh:` runs a command IN the guest, and the guest cannot reach the download. Its internet is
this container's NAT, and this container's outbound HTTPS goes through a proxy the guest knows
nothing about - so a `curl` inside the guest would hang or 403, and the 2.7 GiB would be
attempted a second way. Downloading on the host keeps the proxy, the sha1 check and the retry
where they already work, and the guest only ever sees bytes that were verified.

WHY `copy_xcode` IS SEPARATE FROM `fetch_xcode`

Because they fail for completely different reasons and the log should say which. A download
that 404s is a pin that is stale; a copy that stalls is a guest that has no room. One step
would collapse those into "xcode failed".

WHY THE INSTALLER SCRIPT IS COPIED HERE TOO

`install-xcode.sh` lives in the repository because it is the part worth reading, and it is
copied beside the .xip so the guest runs the file that was read rather than a heredoc string
that drifted from it.
"""

from __future__ import annotations

import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO, os.path.join(REPO, "lib"), os.path.join(REPO, "macos", "15")):
    if path not in sys.path:
        sys.path.insert(0, path)

HERE = os.path.dirname(os.path.abspath(__file__))

# WHERE THE .XIP WAITS BETWEEN THE TWO STEPS. The work directory is the stage's own, so a
# resumed run finds it and iso.fetch_url verifies it instead of downloading again.
XIP_NAME = "Xcode.xip"
SCRIPT_NAME = "install-xcode.sh"


def log(message: str) -> None:
    print(f"[macos-dev {time.strftime('%H:%M:%S')}] {message}", flush=True)


def _work(driver) -> str:
    """The stage's work directory, which is where the download is kept."""
    work = getattr(driver, "work", "") or "."
    os.makedirs(work, exist_ok=True)
    return work


def _release(driver) -> str:
    return str(getattr(driver, "release", "15") or "15")


def fetch_xcode(driver, step) -> bool:
    """Download the pinned .xip on the HOST and verify it against Apple's sha1."""
    import xcode_asset

    release = _release(driver)
    dest = os.path.join(_work(driver), XIP_NAME)
    pin = xcode_asset.for_release(release)
    log(f"macOS {release} -> Xcode {pin['version']} ({pin['build']})")
    log(f"  {pin['why']}")
    log(f"  pinned sha1 {pin['sha1']}")
    # `iso.fetch_url` is resumable and verifies the hash; a present-and-correct file is
    # returned without touching the network, which is what a resumed bake wants.
    path = xcode_asset.fetch(release, dest)
    size = os.path.getsize(path)
    log(f"  {path} ({size >> 20} MiB) verified")
    return True


def copy_xcode(driver, step) -> bool:
    """Push the .xip and the installer script into the guest.

    THE SIZES ARE CHECKED ON BOTH SIDES. scp over a forwarded port through a container proxy
    is the sort of transfer that can finish "successfully" with a short file when a middlebox
    cuts it, and a truncated .xip fails at `xip -x` twenty minutes later with a message about
    a corrupt archive.
    """
    import session

    # READ OFF THE DRIVER, which is where lib/steps.py puts the facts of this run - the same
    # rule as `release` and `frames_dir`. A value here that disagreed with the walk's own
    # `ssh_port:` would be a second answer to one question.
    values = getattr(driver, "values", {}) or {}
    port = int(getattr(driver, "ssh_port", 0) or values.get("ssh_port") or 2222)
    user = str(values.get("user") or "docker")
    password = str(getattr(driver, "password", "") or values.get("password") or "admin")

    work = _work(driver)
    xip = os.path.join(work, XIP_NAME)
    if not os.path.isfile(xip):
        log(f"=== NO .XIP AT {xip} ===")
        log("  the fetch step did not run, or it wrote somewhere else")
        return False
    expected = os.path.getsize(xip)

    remote_xip = f"/Users/{user}/{XIP_NAME}"
    log(f"copying {xip} ({expected >> 20} MiB) -> {remote_xip}")
    # 3600 s: 2.7 GiB over a forwarded port is minutes, and a timeout that fires during a
    # working transfer is indistinguishable from a hung one.
    status, out = session.scp(xip, remote_xip, port=port, timeout=3600,
                              user=user, password=password)
    if status != 0:
        log(f"=== THE COPY FAILED (exit {status}) ===")
        for line in (out or "").splitlines()[:10]:
            log(f"    {line}")
        return False

    # THE INSTALLER SCRIPT GOES WITH IT, so the guest runs the file the repository holds.
    local_script = os.path.join(HERE, SCRIPT_NAME)
    remote_script = f"/Users/{user}/{SCRIPT_NAME}"
    log(f"copying {local_script} -> {remote_script}")
    status, out = session.scp(local_script, remote_script, port=port, timeout=300,
                              user=user, password=password)
    if status != 0:
        log(f"=== THE SCRIPT COPY FAILED (exit {status}) ===")
        return False

    # AND THE SIZE, READ BACK FROM THE GUEST. `stat -f%z` is BSD's syntax, which is what
    # macOS has; GNU's `stat -c` is not present.
    status, out = session.ssh(port, f"stat -f%z {remote_xip}", timeout=120, user=user,
                              password=password)
    got = (out or "").strip().splitlines()[-1:] or [""]
    try:
        got_bytes = int(got[0].strip())
    except (ValueError, IndexError):
        log(f"=== COULD NOT READ THE SIZE BACK: {out!r} ===")
        return False
    if got_bytes != expected:
        log(f"=== THE COPY IS SHORT: {got_bytes} of {expected} bytes ===")
        return False
    log(f"  the guest has {got_bytes >> 20} MiB, which matches the host")
    return True
