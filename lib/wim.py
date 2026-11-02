"""Check which Windows edition an ISO would actually install, before booting it.

    from wim import editions, assert_index

    editions("sources/install.wim")   -> [(1, "Windows 11 Home"), (6, "Windows 11 Pro"), ...]
    assert_index(wim, 6, "Windows 11 Pro")

WHY THIS EXISTS

Autounattend.xml selects an image by INDEX into sources/install.wim. An index off
by one installs a different edition, and the wrong edition is invisible everywhere
downstream: it boots, it reaches a desktop, and it looks exactly like the right one.
The only difference is a string in the registry that nobody reads.

There are three ways to catch that:

  a. ask the guest, after it is installed, what edition it is
     -> needs a script inside the guest, which is the coupling we are removing
  b. mount the finished NTFS volume and read the registry key
     -> needs ntfs-3g and hivex, neither of which is in the bake image, and
        reading a live registry hive offline is fragile
  c. read the WIM's own metadata, before any guest exists
     -> one command, zero guest involvement, and it fails at the earliest
        possible moment

(c) is what this module does. It is the check that makes "no scripts in the guest"
defensible: the thing the guest script was verifying becomes something the host can
verify better and earlier.

WHAT A WIM LOOKS LIKE

    wimlib-imagex info install.wim
    Index:                  1
    Name:                   Windows 11 Home
    Description:            Windows 11 Home
    ...

The Name is what setup shows and what the answer file's index resolves to. It is not
localized in the WIM metadata, which is why matching it is safe across en-US and
other media.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile


def log(msg: str) -> None:
    print(f"[wim] {msg}", flush=True)


class WimError(RuntimeError):
    pass


def find_install_image(iso: str) -> str:
    """Extract sources/install.wim (or .esd) out of an ISO.

    Only the one file is extracted: a full mount needs privileges a container may
    not have, and 7z reads from an ISO without mounting it. The WIM is 4-5 GiB, so
    this is the second-largest copy in the bake after the disk itself.
    """
    if not shutil.which("7z"):
        raise WimError("7z is not installed; it is in the bake image (see Containerfile)")

    workdir = tempfile.mkdtemp(prefix="wiminfo-")
    # Try install.wim first, then install.esd - Windows 11 media ships either, and
    # which one is not something the caller should have to know.
    for name in ("sources/install.wim", "sources/install.esd"):
        result = subprocess.run(
            ["7z", "x", "-y", f"-o{workdir}", iso, name],
            capture_output=True, text=True,
        )
        extracted = os.path.join(workdir, *name.split("/"))
        if result.returncode == 0 and os.path.getsize(extracted) > 0:
            log(f"extracted {name} ({os.path.getsize(extracted)} bytes)")
            return extracted

    raise WimError(f"no sources/install.wim or install.esd in {iso}")


def editions(wim: str) -> list[tuple[int, str]]:
    """Index and Name for every image in the WIM.

    wimlib-imagex rather than DISM: DISM needs Windows. wimlib reads the same
    metadata on Linux and is one package.
    """
    if not shutil.which("wimlib-imagex"):
        raise WimError(
            "wimlib-imagex is not installed; add `wimtools` to the Containerfile"
        )

    result = subprocess.run(
        ["wimlib-imagex", "info", wim],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise WimError(f"wimlib-imagex info failed: {result.stderr.strip()}")

    found: list[tuple[int, str]] = []
    index = None
    for line in result.stdout.splitlines():
        if line.startswith("Index:"):
            index = int(line.split(":", 1)[1].strip())
        elif line.startswith("Name:") and index is not None:
            name = line.split(":", 1)[1].strip()
            found.append((index, name))
            index = None

    if not found:
        raise WimError(f"no images found in {wim}")
    return found


def assert_index(wim: str, index: int, expect: str, *,
                 forbid: str | None = None) -> None:
    """Fail unless index `index` is the edition named `expect`.

    `forbid` exists for the case that actually goes wrong in practice: index 6 is
    "Windows 11 Pro" in the Consumer image and "Windows 11 Pro N" in some others.
    A substring match on "Pro" accepts both, so the caller can name the string that
    must NOT appear.
    """
    table = editions(wim)
    log("images in this WIM:")
    for i, name in table:
        marker = "  <-- selected" if i == index else ""
        log(f"  {i}: {name}{marker}")

    match = dict(table).get(index)
    if match is None:
        raise WimError(
            f"index {index} does not exist; the WIM has {len(table)} image(s)"
        )

    if expect.lower() not in match.lower():
        raise WimError(
            f"index {index} is {match!r}, which does not contain {expect!r}.\n"
            f"The answer file would install the wrong edition."
        )

    if forbid and forbid.lower() in match.lower():
        raise WimError(
            f"index {index} is {match!r}, which contains {forbid!r}.\n"
            f"This is the edition the index is meant to avoid."
        )

    log(f"index {index} verified: {match}")


def check_iso(iso: str, index: int, expect: str, *, forbid: str | None = None) -> str:
    """Convenience: extract, verify, clean up. Returns the edition name."""
    wim = find_install_image(iso)
    try:
        assert_index(wim, index, expect, forbid=forbid)
        return dict(editions(wim))[index]
    finally:
        shutil.rmtree(os.path.dirname(os.path.dirname(wim)), ignore_errors=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        raise SystemExit("usage: wim.py <install.wim|install.esd> [index expect [forbid]]")
    if len(sys.argv) == 3:
        for i, name in editions(sys.argv[1]):
            print(f"{i}\t{name}")
    else:
        assert_index(
            sys.argv[1],
            int(sys.argv[2]),
            sys.argv[3],
            forbid=sys.argv[4] if len(sys.argv) > 4 else None,
        )
