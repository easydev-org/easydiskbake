"""Pull a published disk from ghcr, resuming instead of restarting.

    python3 lib/pull_disk.py macos15-setup --dest out
    python3 lib/pull_disk.py macos15-setup --dest out --tries 8

WHY THIS IS NOT `oras pull`, WHICH IS WHAT IT USED TO BE

`oras pull` has no resume. A transfer that is reset at the other end starts again from byte
zero, and a 28 GiB blob over a long-lived HTTP/2 stream is reset often enough that five
attempts can all fail without making net progress:

    attempt 1  03:39:28 -> 03:45:00   stream error: stream ID 1; PROTOCOL_ERROR
    attempt 2  03:45:05 -> 03:48:58   the same
    attempt 3  03:49:08 -> 03:55:03   the same
    attempt 4  03:55:18 -> ...        the same

Three attempts, five to six minutes each, and the file never got further than the first.

lib/iso.py's downloader does the thing that actually survives this: it writes to `<dest>.part`
and sends a `Range: bytes=<have>-` header on the next attempt, so the second try continues
where the first stopped. That is the same code path that pulls a 10 GiB Windows disk and the
20 GiB installer ISOs, and it is the one this uses now. `oras` is still what PUBLISHES; it is
only the pull that needed a different mechanism.

WHAT IT DOES NOT DO

It does not fall back to another tag or registry: the caller names one artifact, and "the
pull failed" is a fact worth reporting rather than hiding.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import iso  # noqa: E402


def log(message: str) -> None:
    print(f"[pull] {message}", flush=True)


def pull(tag: str, dest: str, *, tries: int = 5) -> str:
    """Pull `tag` into `dest`, resuming across resets. Returns the path written.

    The destination is a DIRECTORY here, because a tag can carry more than one file (a
    macOS boot asset is four) and the caller should not have to know which. The disk is
    found among what arrived.
    """
    os.makedirs(dest, exist_ok=True)

    # THE PATH IS FIXED, WHICH IS WHAT MAKES THE .part FILE USEFUL. A name that changed per
    # attempt would restart the transfer every time; publish_disk.py pushes the file's
    # basename, so `macos15-setup` lands as macos15-setup.qcow2 and its .part sits beside it.
    #
    # fetch_tag does its own completeness check against the manifest's size, including
    # discarding a file that is the wrong size - so a previous attempt's truncated dest is
    # handled there and this does not need to guess.
    candidate = os.path.join(dest, f"{tag}.qcow2")

    log(f"pulling {tag} -> {candidate}")
    try:
        iso.fetch_tag(tag, candidate)
    except iso.IsoError as exc:
        raise SystemExit(
            f"=== COULD NOT PULL {tag} ===\n"
            f"  {exc}\n"
            f"  The downloader resumed from a partial file on every attempt; if this is\n"
            f"  still failing, the registry is refusing the blob rather than resetting the\n"
            f"  stream, and the message above says which."
        ) from exc

    found = [n for n in sorted(os.listdir(dest)) if n.endswith(".qcow2")]
    if not found:
        raise SystemExit(
            f"=== {tag} PULLED BUT HOLDS NO .qcow2 ===\n"
            f"  {dest} contains: {sorted(os.listdir(dest))[:10]}"
        )
    path = os.path.join(dest, found[0])
    log(f"pulled {path} ({os.path.getsize(path) >> 20} MiB)")
    return path


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("tag", help="the tag to pull, e.g. macos15-setup")
    p.add_argument("--dest", default="out", help="where to unpack it (default out)")
    p.add_argument("--tries", type=int, default=5,
                   help="kept for compatibility; lib/iso.py has its own attempt count")
    args = p.parse_args(argv)

    path = pull(args.tag, args.dest, tries=args.tries)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
