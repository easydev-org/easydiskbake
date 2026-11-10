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
# THE INSTALLER SITS ONE LEVEL UP, BESIDE THE STAGE, NOT IN steps/. `HERE` is
# macos/15/steps/ because that is where the extensions live; the script it copies is
# macos/15/xcode-install.sh, which is where a reader looks for a stage's shell code.
STAGE_DIR = os.path.dirname(HERE)

# WHERE THE .XIP WAITS BETWEEN THE TWO STEPS. The work directory is the stage's own, so a
# resumed run finds it and iso.fetch_url verifies it instead of downloading again.
XIP_NAME = "Xcode.xip"
# THE NAME ON THE DISK IS `xcode-install.sh`; the name it is copied AS can be anything, but
# two names for one file is how this failed the first time - the copy was looking for
# install-xcode.sh, which has never existed.
SCRIPT_NAME = "xcode-install.sh"


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


# THE TWO PACKAGES THAT REPLACE `brew install node python` ON AN INTEL DISK.
#
# Homebrew's installer refuses a non-arm64 macOS outright (install.sh: `if [[ "${UNAME_MACHINE}"
# != "arm64" ]]; then abort`), so each tool comes from the publisher that still ships an Intel
# build. A .pkg is what both of them ship for macOS; `installer -pkg ... -target /` is the
# system's own non-interactive installer and needs no GUI.
#
# (family, version, url, sha256, filename in the guest)
PACKAGES = {
    "node": {
        "version": "26.13.0",
        "url": "https://nodejs.org/dist/v26.13.0/node-v26.13.0.pkg",
        "sha256": "",  # filled by --check, see below
        "file": "/tmp/node.pkg",
        "what": "Node.js, official x64 .pkg - links node/npm/npx into /usr/local/bin",
    },
    "python": {
        "version": "3.13.9",
        "url": "https://www.python.org/ftp/python/3.13.9/python-3.13.9-macos11.pkg",
        "sha256": "",
        "file": "/tmp/python.pkg",
        "what": "Python, python.org macos11 .pkg - the Intel-capable build",
    },
}


def fetch_packages(driver, step) -> bool:
    """Download node and python on the HOST, into the stage's work directory.

    THE GUEST CANNOT REACH THEM. Its internet is this container's NAT, and this container's
    outbound HTTPS is what already works - so the download happens here and the next step
    copies the files in, exactly as the Xcode archive does.

    NO HASH IS PINNED, AND THAT IS A DELIBERATE DIFFERENCE FROM XCODE. These come from the
    publishers over https, at URLs that name an exact version - nodejs.org/dist/v26.13.0/ does
    not change its bytes and neither does python.org/ftp/python/3.13.9/. Xcode needed a pin
    because it came from an ARCHIVE (archive.org), where the file could be replaced; a
    publisher's own versioned URL is the identity. `--check` below prints the hashes so a pin
    can be added if that ever stops being true.
    """
    import iso

    work = _work(driver)
    for name, pin in PACKAGES.items():
        dest = os.path.join(work, f"{name}.pkg")
        log(f"{name} {pin['version']}: {pin['what']}")
        try:
            iso.fetch_url(pin["url"], dest, size=None)
        except Exception as exc:                        # noqa: BLE001
            log(f"=== COULD NOT FETCH {name}: {exc} ===")
            return False
        log(f"  {dest} ({os.path.getsize(dest) >> 20} MiB)")
    return True


def copy_packages(driver, step) -> bool:
    """Push both .pkg files into the guest, checking the size on the far side."""
    import session

    values = getattr(driver, "values", {}) or {}
    port = int(getattr(driver, "ssh_port", 0) or values.get("ssh_port") or 2222)
    user = str(values.get("user") or "docker")
    password = str(getattr(driver, "password", "") or values.get("password") or "admin")

    work = _work(driver)
    for name, pin in PACKAGES.items():
        local = os.path.join(work, f"{name}.pkg")
        if not os.path.isfile(local):
            log(f"=== NO PACKAGE AT {local} ===")
            return False
        expected = os.path.getsize(local)
        log(f"copying {name}.pkg ({expected >> 20} MiB) -> {pin['file']}")
        status, out = session.scp(local, pin["file"], port=port, timeout=1800,
                                  user=user, password=password)
        if status != 0:
            log(f"=== THE {name.upper()} COPY FAILED (exit {status}) ===")
            for line in (out or "").splitlines()[:6]:
                log(f"    {line}")
            return False
        got = _remote_size(port, user, password, pin["file"])
        if got != expected:
            log(f"=== {name}: {got} of {expected} bytes arrived ===")
            return False
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
    # A 2.7 GiB scp OVER A FORWARDED PORT DOES NOT SUCCEED ONCE.
    #
    # This repository already learned that for ghcr pulls - iso.py's BLOB_RETRIES exists because
    # run 41481670715 lost a 10.5 GiB transfer at 3.2 GiB. A single scp has the same shape and
    # no resume, so a drop means starting again. Three attempts, and the size is checked after
    # each: scp exits 0 on a transfer that it *thinks* completed, and comparing bytes is the
    # only thing that distinguishes that from a full copy.
    attempts = 3
    for attempt in range(1, attempts + 1):
        status, out = session.scp(xip, remote_xip, port=port, timeout=3600,
                                  user=user, password=password)
        if status != 0:
            log(f"=== THE COPY FAILED (exit {status}), attempt {attempt}/{attempts} ===")
            for line in (out or "").splitlines()[:6]:
                log(f"    {line}")
            continue
        got = _remote_size(port, user, password, remote_xip)
        if got == expected:
            log(f"  the guest has all {expected >> 20} MiB")
            break
        log(f"  the guest has {got if got is not None else '?'} of {expected} bytes; "
            f"retrying ({attempt}/{attempts})")
    else:
        log("=== THE COPY DID NOT ARRIVE IN FULL ===")
        return False

    # THE INSTALLER SCRIPT GOES WITH IT, so the guest runs the file the repository holds.
    local_script = os.path.join(STAGE_DIR, SCRIPT_NAME)
    if not os.path.isfile(local_script):
        log(f"=== THE INSTALLER IS NOT IN THE REPOSITORY: {local_script} ===")
        log("  it is macos/15/xcode-install.sh - beside the stage, not in steps/")
        return False
    remote_script = f"/Users/{user}/{SCRIPT_NAME}"
    log(f"copying {local_script} -> {remote_script}")
    status, out = session.scp(local_script, remote_script, port=port, timeout=300,
                              user=user, password=password)
    if status != 0:
        log(f"=== THE SCRIPT COPY FAILED (exit {status}) ===")
        for line in (out or "").splitlines()[:10]:
            log(f"    {line}")
        # 255 IS ssh's OWN "THE CONNECTION FAILED", not scp's "the file did not arrive", and
        # the two want different investigations. The xip copy two lines above returned 0, so
        # either that transfer was faster than 2.7 GiB over a forwarded port can be - which
        # means it did not really happen - or sshd stopped accepting after it.
        #
        # So ask the guest, rather than guessing: is it still reachable, and is the xip there?
        try:
            import session as _session
            status2, out2 = _session.ssh(port, f"ls -l /Users/{user}/Xcode.xip; df -h /",
                                         timeout=60, user=user, password=password)
            log(f"  the guest after the failed copy: ssh -> {status2}")
            for line in (out2 or "").splitlines()[:6]:
                log(f"    {line[:150]}")
        except Exception as exc:                    # noqa: BLE001
            log(f"  could not ask the guest: {exc}")
        return False

    return True


def _remote_size(port: int, user: str, password: str, remote: str) -> int | None:
    """The size of a file in the guest, or None.

    `stat -f%z` IS BSD'S SYNTAX, which is what macOS has; GNU's `stat -c` is not present there.
    """
    import session

    status, out = session.ssh(port, f"stat -f%z {remote}", timeout=120, user=user,
                              password=password)
    if status != 0:
        return None
    lines = (out or "").strip().splitlines()
    try:
        return int(lines[-1].strip()) if lines else None
    except ValueError:
        return None
