"""Download a pinned installer image, verify it, and push it to this repo's ghcr.

    python3 lib/publish_iso.py --list
    python3 lib/publish_iso.py win11x64
    python3 lib/publish_iso.py --all

WHY THIS IS SEPARATE FROM THE BAKE

A bake used to download 7.2 GiB from Microsoft every time. That made a bake a
90-minute job whose first forty minutes were a download with its own failure modes,
and it meant a re-run cost the same as the first run even when nothing had changed.

Publishing the image once moves the download to its own job with its own failure
messages, and makes the bake a pull from a CDN. It also puts the verification
somewhere it can be re-run on its own: when Microsoft rotates a build, this fails and
says so, and the bake never starts.

WHY ghcr RATHER THAN AN ARTIFACT

An Actions artifact is capped and expires; a package version is pullable by anything
with a token, from anywhere. The macOS bake reached the same conclusion for its golden
disk - see dockur-macos/.github/workflows/publish-golden.yml - and the number there
was 11.8 GiB moving "between 106 seconds and not finished after 1085 seconds" through
the artifact store.

The tag is `iso-<name>`, in this repository's own package, alongside the bake image
(:latest) and the baked disks (:windows-base-<stamp>).

WHAT VERIFICATION MEANS, GIVEN THAT THE VENDOR ROTATES

A Windows ISO is not redistributable and Microsoft replaces it without notice, so a
pin is a claim with an expiry date. The checks, in order:

    exact byte count     catches a truncated transfer that exited 0
    sha256               catches a different build entirely
    ISO9660 at sector 16 catches an HTML error page served with 200 OK

When they fail, the message says what to change rather than what broke: the pin, not
the download.

WHAT IT DOES NOT DO

It does not verify the WIM index. That needs 7z and wimlib and is a property of how
the ISO is installed rather than of the bytes, so it stays in the bake where the
answer file it depends on lives - lib/wim.py.
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

import images  # noqa: E402
import iso  # noqa: E402


def log(msg: str) -> None:
    print(f"[publish] {msg}", flush=True)


class PublishError(RuntimeError):
    pass


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check(path: str, pin: dict) -> None:
    """The three checks, each catching a different failure."""
    size = os.path.getsize(path)
    log(f"  bytes: {size}")
    if pin.get("size") and size != pin["size"]:
        raise PublishError(
            f"size mismatch: got {size}, the pin says {pin['size']}.\n"
            f"The vendor has probably published a new build; update lib/images.py."
        )

    # ISO9660 primary volume descriptor: sector 16 starts with type 1, then "CD001".
    # An HTML error page served with 200 OK does not, and that failure otherwise looks
    # like a corrupt image.
    with open(path, "rb") as fh:
        fh.seek(16 * 2048)
        head = fh.read(8)
    if len(head) < 6 or head[1:6] != b"CD001":
        raise PublishError(
            f"not an ISO9660 image: sector 16 begins {head[:8]!r}.\n"
            f"The server returned something that is not the file."
        )
    log("  ISO9660 signature: found")

    if pin.get("sha256"):
        actual = sha256_of(path)
        if actual != pin["sha256"]:
            raise PublishError(
                f"sha256 mismatch:\n  expected {pin['sha256']}\n  actual   {actual}\n"
                f"The vendor has probably published a new build; update lib/images.py."
            )
        log("  sha256: matches")
    else:
        log("  sha256: not pinned (the release tag is the pin)")


def push(path: str, tag: str, repository: str, registry: str = "ghcr.io") -> None:
    """Push one file as a single-layer OCI artifact.

    oras rather than docker: the payload is a bare ISO, not a rootfs, and oras stores
    arbitrary files without inventing a Dockerfile for them. lib/iso.py pulls it back
    with three raw registry calls, so the bake image does not need oras at run time.
    """
    repo = repository.lower()
    log(f"pushing {os.path.basename(path)} -> {registry}/{repo}:{tag}")
    result = subprocess.run(
        [
            "oras", "push", f"{registry}/{repo}:{tag}",
            "--artifact-type", "application/vnd.easydev.iso",
            f"{path}:application/vnd.oci.image.layer.v1.tar",
            "--annotation", f"org.opencontainers.image.source={os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}",
            "--annotation", f"org.opencontainers.image.revision={os.environ.get('GITHUB_SHA', 'local')}",
            "--annotation", f"org.opencontainers.image.created={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        ],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        log(f"  {line}")
    if result.returncode != 0:
        raise PublishError(
            f"oras push failed ({result.returncode}):\n"
            f"{result.stdout[-1200:]}\n{result.stderr[-1200:]}"
        )


def publish_one(tag: str, pin: dict, repository: str, workdir: str) -> None:
    name = tag[len("iso-"):]
    dest = os.path.join(workdir, f"{name}.iso")
    log(f"=== {tag} ===")

    if os.path.exists(dest):
        log(f"  already downloaded: {dest}")
        try:
            check(dest, pin)
        except PublishError:
            log("  the cached download does not match the pin; fetching again")
            os.unlink(dest)
            iso.fetch_url(pin["url"], dest, sha256=pin.get("sha256"), size=pin.get("size"))
            check(dest, pin)
    else:
        log(f"  fetching {pin['url']}")
        # fetch_url verifies sha256 itself when one is pinned; the other two checks
        # are this module's job because they are about the container format rather
        # than the bytes.
        iso.fetch_url(pin["url"], dest, sha256=pin.get("sha256"), size=pin.get("size"))
        check(dest, pin)

    push(dest, tag, repository)

    # The image is multi-gigabyte and the runner disk is the scarce resource; the next
    # image in --all needs the space.
    os.unlink(dest)
    log(f"  removed {dest}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("names", nargs="*", help="tags to publish, e.g. iso-windows11")
    p.add_argument("--all", action="store_true", help="publish every pinned image")
    p.add_argument("--list", action="store_true", help="show the pins and exit")
    p.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""),
                   help="owner/repo to push into (default: $GITHUB_REPOSITORY)")
    # The workflow passes its choice through an environment variable rather than
    # branching in shell, so the selection lives in one place.
    p.add_argument("--which", default=os.environ.get("PUBLISH_WHICH"),
                   help="a tag or 'all'; overrides the positional names")
    args = p.parse_args(argv)

    table = images.all_images()

    if args.list:
        for tag, pin in sorted(table.items()):
            size = pin.get("size")
            print(f"{tag:34} {size if size else 'unpinned':>16}  {pin['url']}")
        return 0

    if args.which:
        wanted = list(table) if args.which == "all" else [args.which]
    elif args.all:
        wanted = list(table)
    else:
        wanted = args.names
    if not wanted:
        p.error("no images given; use --all, --which, or name some tags")

    unknown = [w for w in wanted if w not in table]
    if unknown:
        p.error(f"unknown image(s): {', '.join(unknown)}; known: {', '.join(sorted(table))}")

    if not args.repository:
        p.error("--repository (or $GITHUB_REPOSITORY) is required to push")

    workdir = tempfile.mkdtemp(prefix="isos-")
    failed = []
    for tag in wanted:
        try:
            publish_one(tag, table[tag], args.repository, workdir)
        except (PublishError, iso.IsoError) as exc:
            log(f"FAILED {tag}: {exc}")
            failed.append(tag)

    if failed:
        log(f"failed: {', '.join(failed)}")
        return 1
    log(f"published {len(wanted)} image(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
