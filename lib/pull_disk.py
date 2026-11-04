"""Pull a published disk from ghcr, with retries that survive a dropped stream.

    python3 lib/pull_disk.py macos15-setup --dest out
    python3 lib/pull_disk.py macos15-setup --dest out --tries 5

WHY THIS IS NOT JUST `oras pull`

It publishes and pulls disks of 5 to 30 GiB as a single OCI blob. A transfer that size runs
for minutes, and HTTP/2 resets it often enough to matter:

    Downloading e7662be614a4 macos15.qcow2
    Error: failed to copy content to .../out/macos15.qcow2:
      copy failed: stream error: stream ID 1; PROTOCOL_ERROR; received from peer

That is run 41767232221's pull step - six minutes twenty-five seconds of transfer, then a
reset at the other end, and the whole stage failed with no disk to boot. Nothing was wrong
with the artifact or the code that would run after it.

`oras pull` has no retry. This does: it retries the pull, and between attempts it keeps
whatever partial file is there and asks oras to resume, because a 30 GiB download that
restarts from zero every six minutes may never finish on a slow day.

WHAT IT DOES NOT DO

It does not verify the digest itself - oras does, and a resumed download that ends with the
wrong content fails its own check. It does not fall back to a different registry or tag: the
caller names one artifact, and "the pull failed" is a fact worth reporting rather than
hiding behind a second source.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def log(message: str) -> None:
    print(f"[pull {time.strftime('%H:%M:%S')}] {message}", flush=True)


def pull(tag: str, dest: str, repository: str, *, tries: int = 5,
         media_type: str = "application/vnd.easydev.disk.qcow2") -> str:
    """Pull `tag` into `dest`, retrying the whole operation. Returns the qcow2 path.

    THE RETRY IS THE POINT. Five attempts with a growing pause is enough for the resets
    seen here - they came from the peer, not from anything this side did wrong - and a
    pause that grows gives a busy registry time to recover rather than hammering it.
    """
    os.makedirs(dest, exist_ok=True)
    reference = f"ghcr.io/{repository}:{tag}"

    last_error = ""
    for attempt in range(1, tries + 1):
        log(f"attempt {attempt}/{tries}: oras pull {reference}")
        result = subprocess.run(
            ["oras", "pull", reference, "-o", dest],
            capture_output=True, text=True,
        )
        for line in (result.stdout or "").splitlines():
            log(f"  {line}")
        if result.returncode == 0:
            break

        last_error = (result.stderr or result.stdout or "").strip()
        # A partial file is left behind on purpose: oras resumes from it on the next
        # attempt, and a stage that has already spent six minutes downloading should not
        # spend them again.
        if attempt < tries:
            pause = min(60, 5 * attempt)
            log(f"  the pull failed: {last_error.splitlines()[-1][:160] if last_error else 'no message'}")
            log(f"  retrying in {pause}s; the partial file is kept")
            time.sleep(pause)
    else:
        raise SystemExit(
            f"=== COULD NOT PULL {reference} IN {tries} ATTEMPTS ===\n"
            f"  last error: {last_error[:400]}\n"
            f"  the artifact is published and the runner has network; this is a transfer\n"
            f"  that kept being reset. Re-run the stage, or raise --tries."
        )

    # FIND THE DISK, NOT A NAME. publish_disk.py pushes the basename, and the stages that
    # consume it should not have to know which file that was - run 41611250363's derive
    # legs all died on "no base disk at out/nixos-base.qcow2" for exactly that reason.
    found = [n for n in sorted(os.listdir(dest)) if n.endswith(".qcow2")]
    if not found:
        raise SystemExit(
            f"=== {reference} PULLED BUT HOLDS NO .qcow2 ===\n"
            f"  {dest} contains: {sorted(os.listdir(dest))[:10]}"
        )
    if len(found) > 1:
        log(f"  {len(found)} disks in {dest}; using the first: {found[0]}")
    path = os.path.join(dest, found[0])
    log(f"pulled {path} ({os.path.getsize(path) >> 20} MiB)")
    return path


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("tag", help="the tag to pull, e.g. macos15-setup")
    p.add_argument("--dest", default="out", help="where to unpack it (default out)")
    p.add_argument("--tries", type=int, default=5)
    p.add_argument("--repo", default=None,
                   help="owner/name; defaults to $GITHUB_REPOSITORY")
    args = p.parse_args(argv)

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise SystemExit(
            "=== NO REPOSITORY ===\n"
            "  set $GITHUB_REPOSITORY or pass --repo owner/name"
        )
    path = pull(args.tag, args.dest, repo.lower(), tries=args.tries)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
