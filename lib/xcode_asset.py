"""Fetch Xcode for a macOS disk, the one Apple asset that cannot be fetched from Apple.

    python3 lib/xcode_asset.py --release 15 --out /work/Xcode.xip
    python3 lib/xcode_asset.py --release 15 --list

WHY THIS IS NOT `brew install --cask xcode`

Because that is a download from Apple's developer portal, and the portal answers an
unauthenticated request with a redirect:

    $ curl -I https://download.developer.apple.com/Developer_Tools/Xcode_26.3/Xcode_26.3_Universal.xip
    HTTP/2 302
    location: https://developer.apple.com/unauthorized/

Measured on 2026-11-09 from this project's own network, and the "cookie trick" that used
to work (`GET developerservices2.apple.com/services/download?...` to mint an
`ADCDownloadAuth` cookie) now returns 403 there too. So an Apple ID is required, and this
repository has no credentials it is willing to carry for that - a CI job that needs a
person's Apple password and a 2FA code is a job that cannot run unattended.

WHAT IS USED INSTEAD, AND WHY IT IS NOT A RANDOM MIRROR

archive.org keeps a copy of Apple's own .xip, byte for byte, and the archive records a
sha1 per file. That number is the same one Apple's release index publishes, so it is an
identity check rather than a promise:

    xcodereleases.com    Xcode 26.3 Universal   sha1 cebf05ec2920111830af5e08a45f07961e36bfd6
    archive.org          Xcode_26.3_Universal.xip
                                               sha1 cebf05ec2920111830af5e08a45f07961e36bfd6

If they disagree the download is refused, which is the only thing that turns "a mirror"
into "Apple's file". See `PINNED` below for the two hashes side by side.

WHICH VERSION, AND WHY IT IS THE TOP OF A RANGE RATHER THAN "LATEST"

Apple raises Xcode's minimum macOS with almost every major release, and the installer
enforces it - it is a hard limit, not a recommendation:

    Xcode 26.3   requires macOS 15.6+     <- the last release that runs on Sequoia
    Xcode 26.4   requires macOS 26.2+

So a macOS 15 disk tops out at 26.3, and a 26 disk would want a later one. That is why
`--release` selects a version instead of the file naming one: the number is a fact about
the disk being baked, and putting it in the disk's own family is how the two 26 stages in
this repository already work.

WHY THE DOWNLOAD IS 2.7 GiB AND THE INSTALLED FORM IS ~40 GiB

The .xip is compressed. `xip --expand` writes the Xcode.app inside it, and a full Xcode
with its platforms is about 15 GiB before simulators. A 128 GiB disk has room; the dev
stage checks before it starts rather than filling the disk and failing at something
unrelated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import iso  # noqa: E402

# (release, version, build, sha1, bytes, filename)
#
# THE TWO NUMBERS ON EACH LINE ARE THE POINT. `sha1` is what archive.org records for the
# file it keeps AND what xcodereleases.com records for Apple's release; the download is
# refused unless the bytes match it. `bytes` is the size archive.org reports, and a
# truncated download is the failure a hash catches but a size catches sooner.
PINNED: dict[str, dict] = {
    "15": {
        "version": "26.3",
        "build": "17C529",
        "sha1": "cebf05ec2920111830af5e08a45f07961e36bfd6",
        "bytes": 2871807039,          # 2738 MiB, read from archive.org metadata
        "file": "Xcode_26.3_Universal.xip",
        "url": ("https://archive.org/download/xcode-26.3/"
                "Xcode_26.3_Universal.xip"),
        "why": "the last Xcode that installs on macOS 15 (it requires 15.6+)",
    },
}

# The command line tools are a SEPARATE, MUCH SMALLER download and are what Homebrew needs.
# They are not a substitute for Xcode - no SwiftUI, no simulator, no xcodebuild against an
# app - but a stage that must have SOMETHING to compile with can use them, and it is worth
# keeping their number here so the two are never confused.
CLT = {
    "15": {
        "file": "Command_Line_Tools_for_Xcode_26.3.dmg",
        "sha1": "6f3b952bb6bd9fa97b132d69b07c725d2ab80f29",
        "url": ("https://archive.org/download/xcode-26.3/"
                "Command_Line_Tools_for_Xcode_26.3.dmg"),
        "bytes": 878883722,            # 838 MiB, read from archive.org metadata
    },
}


def log(message: str) -> None:
    print(f"[xcode {message}]", flush=True)


def for_release(release: str) -> dict:
    """The pin for a disk's macOS release, and a refusal that says what to do.

    A MISSING RELEASE IS NOT AN ERROR TO WORK AROUND. The version that runs on a disk is a
    fact about that disk, and guessing one produces an installer that refuses to run after
    a 2.7 GiB download - which is the slowest possible way to learn it.
    """
    if release not in PINNED:
        raise SystemExit(
            f"no Xcode pin for macOS {release}. Known: {', '.join(sorted(PINNED))}.\n"
            f"Check https://xcodereleases.com/ for the newest release whose `requires` "
            f"matches macOS {release}, and add it with archive.org's sha1."
        )
    return PINNED[release]


def fetch(release: str, dest: str) -> str:
    """Download the pinned .xip and verify it against Apple's own sha1."""
    pin = for_release(release)
    log(f"macOS {release} -> Xcode {pin['version']} ({pin['build']}): {pin['why']}")
    if not os.path.isdir(os.path.dirname(os.path.abspath(dest))):
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    return iso.fetch_url(pin["url"], dest, sha1=pin["sha1"], size=pin["bytes"])


def fetch_clt(release: str, dest: str) -> str:
    """The command line tools alone - 838 MiB against Xcode's 2.7 GiB."""
    if release not in CLT:
        raise SystemExit(f"no command-line-tools pin for macOS {release}")
    pin = CLT[release]
    return iso.fetch_url(pin["url"], dest, sha1=pin["sha1"], size=pin["bytes"])


def archive_files(identifier: str = "xcode-26.3") -> list[dict]:
    """What archive.org actually holds for one item, with its hashes.

    THIS IS HOW THE PINS ABOVE ARE CHECKED RATHER THAN TRUSTED. It is the same metadata
    endpoint the download is served from, so if the file were replaced this is where the
    new sha1 would appear first.
    """
    with urllib.request.urlopen(
            f"https://archive.org/metadata/{identifier}", timeout=60) as response:
        payload = json.load(response)
    out = []
    for entry in payload.get("files") or []:
        name = entry.get("name") or ""
        if name.endswith((".xip", ".dmg")):
            out.append({
                "name": name,
                "bytes": int(entry.get("size") or 0),
                "sha1": entry.get("sha1") or "",
                "md5": entry.get("md5") or "",
            })
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--release", default="15")
    p.add_argument("--out", default="")
    p.add_argument("--clt", action="store_true", help="the command line tools, not Xcode")
    p.add_argument("--list", action="store_true", help="print the pins and archive's hashes")
    args = p.parse_args(argv)

    if args.list:
        for release in sorted(PINNED):
            pin = PINNED[release]
            print(f"macOS {release}: Xcode {pin['version']} ({pin['build']})")
            print(f"  pinned sha1  {pin['sha1']}")
            print(f"  {pin['url']}")
        try:
            for entry in archive_files():
                print(f"  archive says {entry['name']}: {entry['bytes'] >> 20} MiB "
                      f"sha1 {entry['sha1']}")
        except Exception as exc:                        # noqa: BLE001
            print(f"  (archive.org metadata unavailable: {exc})")
        return 0

    dest = args.out or (CLT[args.release]["file"] if args.clt
                        else PINNED[args.release]["file"])
    path = fetch_clt(args.release, dest) if args.clt else fetch(args.release, dest)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
