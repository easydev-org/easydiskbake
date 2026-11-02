"""Fetch the four pinned macOS boot assets, verify them, and push each to ghcr.

    python3 lib/publish_macos_assets.py --list
    python3 lib/publish_macos_assets.py macos-asset-opencore
    python3 lib/publish_macos_assets.py --all --repository owner/repo

WHY THESE ARE PUBLISHED AND THE RECOVERY IMAGE IS NOT

A macOS bake needs five inputs, and they fall into two classes that must not be
confused:

    the recovery image    Apple issues one per request, so there is nothing to pin.
                          lib/macos_recovery.py drives that exchange and
                          publish-macos-recovery.yml stores the result under
                          :macos-recovery-<name>.

    these four            fixed, versioned URLs. An OpenCore release, a template
                          release, and two OVMF files at a commit. They are inputs to
                          every macOS bake and they never change on their own, which
                          is exactly what makes a pin possible and a republish useful.

Putting the four in the package moves an 27 MiB transfer with its own failure modes out
of the bake's first minute, and - more importantly - makes the bake fail with "run
publish-macos-assets.yml" rather than with a GitHub CDN hiccup that looks like a broken
firmware.

WHY THE VERIFICATION DIFFERS PER FILE

publish_iso.py's checks are about ISO9660, because every image it handles is one. Here
that is one file out of four:

    zip        the local file header magic, 50 4b 03 04
    iso9660    the primary volume descriptor at sector 16, exactly as for an ISO
    firmware   the firmware volume header "_FVH" at offset 40 of each 8 KiB block,
               which is what makes the file an OVMF image rather than a raw blob of
               the right length

A file that is the right size and the right hash is already proven; these checks exist
to catch the one case where the pin itself is wrong - a URL that now serves an HTML
error page with a 200, captured into a pin by a careless re-derivation.

WHY ORAS AND NOT docker push

The payloads are bare files, not rootfs images. oras stores arbitrary files as OCI
artifacts, which is what publish_iso.py and publish_disk.py concluded for the same
reason; the bake pulls them back with lib/iso.py's three raw registry calls, so the
bake image needs no oras at run time.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci  # noqa: E402
import images  # noqa: E402
import iso  # noqa: E402


def log(msg: str) -> None:
    print(f"[publish-macos] {msg}", flush=True)


class PublishError(RuntimeError):
    pass


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_zip(path: str) -> None:
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic != b"PK\x03\x04":
        raise PublishError(
            f"not a zip: the file begins {magic.hex()}, expected 504b0304.\n"
            f"The server returned something that is not the release archive."
        )
    log("  zip signature: found")


def check_iso9660(path: str) -> None:
    with open(path, "rb") as fh:
        fh.seek(16 * 2048)
        head = fh.read(8)
    if len(head) < 6 or head[1:6] != b"CD001":
        raise PublishError(
            f"not an ISO9660 image: sector 16 begins {head[:8]!r}.\n"
            f"The server returned something that is not the template image."
        )
    log("  ISO9660 signature: found")


def check_firmware(path: str) -> None:
    """An OVMF image is a run of firmware volumes, each with a header.

    The file starts with 16 zero bytes and then "_FVH" at offset 40; that is a property
    of the Firmware Volume Header layout, not of this particular build. Checking it is
    what distinguishes the real file from an HTML 404 page of the same length - which
    is not hypothetical, because raw.githubusercontent serves one with a 404 status
    that a download helper can be written to ignore.
    """
    size = os.path.getsize(path)
    if size % 4096 != 0:
        raise PublishError(
            f"firmware image is {size} bytes, which is not a multiple of 4096.\n"
            f"An OVMF file is a whole number of fixed-size firmware volumes."
        )
    with open(path, "rb") as fh:
        head = fh.read(64)
    if head[40:44] != b"_FVH":
        raise PublishError(
            f"no firmware volume header at offset 40: found {head[40:44]!r}, "
            f"expected b'_FVH'.\n"
            f"This is not an OVMF image."
        )
    log(f"  firmware volume header: found ({size >> 10} KiB)")


CHECKS = {"zip": check_zip, "iso9660": check_iso9660, "firmware": check_firmware}


def check(path: str, pin: dict) -> None:
    """Size, then the container format, then the hash.

    Ordered cheapest first so a wrong file is rejected before a hash is computed, and
    the format check comes before the hash because it is the one that catches a pin
    that was derived from an error page.
    """
    size = os.path.getsize(path)
    log(f"  bytes: {size}")
    if pin.get("size") and size != pin["size"]:
        raise PublishError(
            f"size mismatch: got {size}, the pin says {pin['size']}.\n"
            f"The upstream release has probably moved; update lib/images.py."
        )

    kind = pin.get("kind")
    checker = CHECKS.get(kind)
    if checker is None:
        raise PublishError(
            f"unknown asset kind {kind!r}; known: {', '.join(sorted(CHECKS))}. "
            f"Add a check for it rather than publishing it unexamined."
        )
    checker(path)

    actual = sha256_of(path)
    if not pin.get("sha256"):
        raise PublishError(
            "no sha256 in the pin. Every macOS asset has a fixed URL, so there is "
            "nothing to stop a changed build being published under the same tag."
        )
    if actual != pin["sha256"]:
        raise PublishError(
            f"sha256 mismatch:\n  expected {pin['sha256']}\n  actual   {actual}\n"
            f"The upstream release has probably moved; update lib/images.py."
        )
    log("  sha256: matches")


def push(path: str, tag: str, repository: str, *, kind: str,
         registry: str = "ghcr.io") -> None:
    """Push one file as a single-layer OCI artifact.

    PUSH FROM THE FILE'S OWN DIRECTORY, WITH A BARE FILENAME - oras validates the path
    inside a `file:media:type` reference and rejects an absolute one. The flag it
    suggests is --disable-path-validation, which would turn off every path check it
    has; a bare filename satisfies the one that applies without that trade. Same
    reasoning as lib/publish_iso.py, which arrived at it the hard way.
    """
    repository = repository.lower()
    directory = os.path.dirname(os.path.abspath(path)) or "."
    filename = os.path.basename(path)

    log(f"pushing {filename} -> {registry}/{repository}:{tag}")
    result = subprocess.run(
        [
            "oras", "push", f"{registry}/{repository}:{tag}",
            "--artifact-type", f"application/vnd.easydev.macos.{kind}",
            f"{filename}:application/vnd.oci.image.layer.v1.tar",
            "--annotation", f"org.opencontainers.image.source="
                            f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}"
                            f"/{repository}",
            "--annotation", f"org.opencontainers.image.revision="
                            f"{os.environ.get('GITHUB_SHA', 'local')}",
            "--annotation", f"org.opencontainers.image.created="
                            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            "--annotation", f"dev.easydev.kind=macos-asset-{kind}",
        ],
        capture_output=True, text=True,
        cwd=directory,
    )
    for line in result.stdout.splitlines():
        log(f"  {line}")
    if result.returncode != 0:
        raise PublishError(
            f"oras push failed ({result.returncode}):\n"
            f"{result.stdout[-1200:]}\n{result.stderr[-1200:]}"
        )


def publish_one(tag: str, pin: dict, repository: str, workdir: str, *,
                keep: bool = False) -> None:
    """Fetch, verify, push. The file is only removed after a successful push.

    The same rule as publish_iso.py, and for the same recorded reason: the first
    version of that script unlinked in a straight line, so an oras failure deleted a
    verified download and the retry paid for it again.
    """
    name = tag[len("macos-asset-"):]
    suffix = {"zip": ".zip", "iso9660": ".iso", "firmware": ".fd"}.get(
        pin.get("kind"), ".bin")
    dest = os.path.join(workdir, f"{name}{suffix}")
    log(f"=== {tag} ===")

    if os.path.exists(dest):
        log(f"  already downloaded: {dest}")
        try:
            check(dest, pin)
            log("  the cached download still matches the pin")
        except PublishError as exc:
            log(f"  the cached download does not match the pin ({exc}); fetching again")
            os.unlink(dest)
            iso.fetch_url(pin["url"], dest, sha256=pin["sha256"], size=pin["size"])
            check(dest, pin)
    else:
        log(f"  fetching {pin['url']}")
        iso.fetch_url(pin["url"], dest, sha256=pin["sha256"], size=pin["size"])
        check(dest, pin)

    push(dest, tag, repository, kind=pin["kind"])

    if keep:
        log(f"  kept {dest}")
    else:
        os.unlink(dest)
        log(f"  removed {dest}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("names", nargs="*", help="tags to publish, e.g. macos-asset-opencore")
    p.add_argument("--all", action="store_true", help="publish every pinned asset")
    p.add_argument("--list", action="store_true", help="show the pins and exit")
    p.add_argument("--keep", action="store_true",
                   help="keep the downloaded files instead of deleting them after push")
    p.add_argument("--keep-going", action="store_true",
                   help="attempt every asset even after one fails")
    p.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""),
                   help="owner/repo to push into (default: $GITHUB_REPOSITORY)")
    p.add_argument("--which", default=os.environ.get("PUBLISH_WHICH"),
                   help="a tag or 'all'; overrides the positional names")
    args = p.parse_args(argv)

    table = images.all_macos_assets()

    if args.list:
        for tag, pin in sorted(table.items()):
            size = pin.get("size")
            print(f"{tag:34} {size if size else 'unpinned':>10}  {pin['kind']:9} "
                  f"{pin['url'][:64]}")
        return 0

    if args.which:
        wanted = list(table) if args.which == "all" else [args.which]
    elif args.all:
        wanted = list(table)
    else:
        wanted = args.names
    if not wanted:
        p.error("no assets given; use --all, --which, or name some tags")

    unknown = [w for w in wanted if w not in table]
    if unknown:
        p.error(f"unknown asset(s): {', '.join(unknown)}; "
                f"known: {', '.join(sorted(table))}")

    if not args.repository:
        p.error("--repository (or $GITHUB_REPOSITORY) is required to push")

    workdir = tempfile.mkdtemp(prefix="macos-assets-")
    failed = []
    for tag in wanted:
        try:
            publish_one(tag, table[tag], args.repository, workdir, keep=args.keep)
        except (PublishError, iso.IsoError, ci.Failed) as exc:
            log(f"FAILED {tag}: {exc}")
            failed.append(tag)
            # Stop at the first failure unless asked to continue. The four assets are
            # small (27 MiB total) so the disk argument does not apply here, but the
            # other one does: when the failure is a bad oras flag or a missing login,
            # the remaining three pushes fail the same way and say nothing new.
            if not args.keep_going:
                log("stopping at the first failure (--keep-going to attempt the rest)")
                break

    if failed:
        log(f"failed: {', '.join(failed)}")
        return 1
    log(f"published {len(wanted)} asset(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
