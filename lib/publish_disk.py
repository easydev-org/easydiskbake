"""Push a baked disk to this repository's ghcr package, tagged by time.

    python3 lib/publish_disk.py out/windows11.qcow2 --kind windows-base

WHAT IT DOES

    ghcr.io/easydev-org/easydiskbake:windows-base-<YYYYmmdd-HHMMSS>

A timestamp and no floating tag. "Which build is this" has to stay answerable, and a
disk is not something to redeploy by accident - a `latest` that moves would make both
of those untrue at once.

WHY oras AND NOT docker push

The payload is a bare qcow2, not a container image. oras stores arbitrary files as OCI
artifacts without inventing a Dockerfile for them; that is also what
publish-golden.yml in dockur-macos concluded, for an 11.8 GiB disk.

WHY THIS IS A SCRIPT AND NOT A `run:` BLOCK

The push is the last thing that happens to a 64 GiB artifact, and a shell block that
gets a quoting detail wrong fails there - after the expensive part succeeded. Every
argument here is a list element, and --annotation values are built in Python rather
than interpolated by a shell.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def utc_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def push(path: str, tag: str, repository: str, *, kind: str,
         registry: str = "ghcr.io", media_type: str = "application/vnd.easydev.disk.qcow2") -> str:
    if not os.path.isfile(path):
        raise ci.Failed(f"no disk at {path}")
    if not os.access(path, os.R_OK):
        raise ci.Failed(f"{path} is not readable")

    size = os.path.getsize(path)
    log(f"{path}: {size >> 20} MiB")

    annotations = [
        "--annotation", f"org.opencontainers.image.source="
                        f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                        f"{os.environ.get('GITHUB_REPOSITORY', repository)}",
        "--annotation", f"org.opencontainers.image.revision={os.environ.get('GITHUB_SHA', 'local')}",
        "--annotation", f"org.opencontainers.image.created="
                        f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "--annotation", f"org.opencontainers.image.title={kind}",
        "--annotation", f"dev.easydev.kind={kind}",
    ]

    reference = f"{registry}/{repository}:{tag}"
    log(f"pushing -> {reference}")
    result = subprocess.run(
        [
            "oras", "push", reference,
            "--artifact-type", media_type,
            f"{path}:{media_type}+zstd",
            *annotations,
        ],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        log(f"  {line}")
    if result.returncode != 0:
        raise ci.Failed(
            f"oras push failed ({result.returncode}):\n"
            f"{result.stdout[-1200:]}\n{result.stderr[-1200:]}"
        )

    log(f"published {reference}")
    return reference


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("disk", nargs="?", default="out/windows11.qcow2")
    p.add_argument("--kind", default="windows-base",
                   help="tag prefix; the stamp is appended (default windows-base)")
    p.add_argument("--tag", default=None,
                   help="exact tag, overriding --kind and the stamp")
    p.add_argument("--registry", default="ghcr.io")
    args = p.parse_args(argv)

    try:
        repository = ci.lowercase_repository()
        tag = args.tag or f"{args.kind}-{utc_stamp()}"
        reference = push(args.disk, tag, repository, kind=args.kind, registry=args.registry)

        # Written to $GITHUB_OUTPUT so the workflow can report it, and to the step
        # summary so a reader sees what exists without opening the package.
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(f"tag={tag}\n")
                fh.write(f"reference={reference}\n")

        ci.append_summary(
            "## Published\n\n"
            f"```\n{reference}\n```\n\n"
            f"- tag: `{tag}`\n"
            f"- kind: `{args.kind}`\n"
        )
        return 0
    except ci.Failed as exc:
        log(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
