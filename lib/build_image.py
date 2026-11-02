"""Build the bake image and push it to this repository's ghcr package.

    python3 lib/build_image.py

WHY THE BUILD IS A SCRIPT

It was a `run:` block invoking docker build with the tags assembled inline. Two things
about that were wrong: the `$(date -u +%Y.%m.%d)` tag was shell string work, and the
post-build check was a `docker run --entrypoint /bin/bash ... -c '...'` with a nested
quoted script - the shape that fix_grub.py's docstring lists four failures of.

THE POST-BUILD CHECK

It asserts what the image claims to carry, inside the image. A base image that loses a
package is caught here rather than twenty minutes into a bake: the Containerfile's own
RUN checks do the same thing at build time, and this repeats them against the pushed
image so a bad layer is caught after push rather than by the next bake.

`--progress=plain` because log-stream stamps every line: buildkit's default progress
output redraws one line with carriage returns, which fills the 65535-character budget
with a progress bar and pushes the useful output out.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def log(msg: str) -> None:
    print(msg, flush=True)


class Failed(RuntimeError):
    pass


# What the image says it carries. Each entry is a command whose exit status is the
# assertion; the bake calls all of these directly.
CHECKS = [
    ["ffmpeg", "-version"],
    ["tesseract", "--version"],
    ["tesseract", "--list-langs"],
    ["debugfs", "-V"],
    ["genisoimage", "--version"],
    ["wimlib-imagex", "--version"],
    ["swtpm", "--version"],
    ["qemu-system-x86_64", "--version"],
    ["qemu-img", "--version"],
    ["xorriso", "-version"],
    ["7z"],
    ["python3", "--version"],
    # The firmware is a file rather than a binary on PATH, so it needs its own check.
    # Windows 11 will not install without it and its absence reads as a broken ISO.
    ["ls", "-l", "/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"],
]


def main() -> int:
    registry = os.environ.get("REGISTRY", "ghcr.io")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not repository:
        raise Failed("GITHUB_REPOSITORY is not set")

    image = f"{registry}/{repository.lower()}"
    stamp = time.strftime("%Y.%m.%d", time.gmtime())

    log(f"building {image}:latest and :{stamp}")
    log("pulling the base")
    subprocess.run(["docker", "pull", "qemux/qemu:7.50"], check=True)

    log("building")
    result = subprocess.run(
        [
            "docker", "build",
            "--progress=plain",
            "-f", "Containerfile",
            "-t", f"{image}:latest",
            "-t", f"{image}:{stamp}",
            ".",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise Failed(f"docker build failed ({result.returncode})")

    log("verifying what the image actually carries")
    failed = []
    for cmd in CHECKS:
        probe = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", cmd[0], f"{image}:latest", *cmd[1:]],
            capture_output=True, text=True,
        )
        first = (probe.stdout or probe.stderr).splitlines()
        label = " ".join(cmd)
        if probe.returncode == 0:
            log(f"  ok   {label}: {first[0] if first else ''}")
        else:
            log(f"  FAIL {label}: {(probe.stderr or probe.stdout)[:200]}")
            failed.append(label)

    if failed:
        raise Failed(f"the image is missing: {', '.join(failed)}")

    log("pushing")
    for tag in ("latest", stamp):
        subprocess.run(["docker", "push", f"{image}:{tag}"], check=True)
        log(f"  pushed {image}:{tag}")

    log("IMAGE_READY")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failed as exc:
        log(f"FAILED: {exc}")
        raise SystemExit(1)
