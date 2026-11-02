"""Build the bake image and the Android builder image, and push both.

    python3 lib/build_image.py                 # both
    python3 lib/build_image.py --only bake     # just the bake image

TWO IMAGES, TWO PURPOSES

    <repo>:latest           the bake image: qemu, xorriso, swtpm, OVMF, ffmpeg,
                            wimlib, aria2, oras, python3
    <repo>:android-builder  cmdline-tools, a JRE, unzip, zstd - and nothing that boots

They are separate because the second is 1.5 GB of SDK and JDK that only one workflow
calls, and the bake image is pulled by every bake. Combining them would make every
Windows bake carry an Android SDK it never opens.

WHY THE BUILD IS A SCRIPT

It was a `run:` block assembling docker tags with `$(date -u +%Y.%m.%d)`. The
post-build check was a nested quoted script - the same shape that fix_grub.py's
docstring lists four failures of. Here it is a list of argv, and a failure names the
command that failed.

`--progress=plain` because log-stream stamps every line: buildkit's default progress
output redraws one line with carriage returns, which fills the 65535-character budget
with a progress bar and pushes the useful output out.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def log(msg: str) -> None:
    print(msg, flush=True)


class Failed(RuntimeError):
    pass


# What the bake image says it carries. Each entry is a command whose exit status is the
# assertion; the bakes call all of these directly.
BAKE_CHECKS = [
    ["ffmpeg", "-version"],
    ["tesseract", "--version"],
    ["tesseract", "--list-langs"],
    ["debugfs", "-V"],
    ["genisoimage", "--version"],
    ["wimlib-imagex", "--version"],
    ["swtpm", "--version"],
    ["aria2c", "--version"],
    ["qemu-system-x86_64", "--version"],
    ["qemu-img", "--version"],
    ["xorriso", "-version"],
    ["7z"],
    ["python3", "--version"],
    # The firmware is a file rather than a binary on PATH, so it needs its own check.
    # Windows 11 will not install without it and its absence reads as a broken ISO.
    ["ls", "-l", "/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"],
]

# What the Android builder carries. avdmanager is the one tool the workflow cannot do
# without, and it fails with a Java stack trace when the JRE is missing rather than
# saying so.
#
# avdmanager --help exits non-zero and writes to stderr, so `sdkmanager --version` is
# the version check and avdmanager is checked by its mere existence - see the `command
# -v` style entry below, which is why this list is not only version flags.
BUILDER_CHECKS = [
    ["python3", "--version"],
    ["unzip", "-v"],
    ["zstd", "--version"],
    ["java", "-version"],
    ["sdkmanager", "--version"],
    ["sh", "-c", "command -v avdmanager"],
]


def _build(image: str, tag: str, dockerfile: str, *, stamp: str | None) -> None:
    log(f"building {image}:{tag}")
    tags = ["-t", f"{image}:{tag}"]
    if stamp:
        tags += ["-t", f"{image}:{stamp}"]

    result = subprocess.run(
        ["docker", "build", "--progress=plain", "-f", dockerfile, *tags, "."],
        check=False,
    )
    if result.returncode != 0:
        raise Failed(f"docker build for {dockerfile} failed ({result.returncode})")


def _verify(image: str, tag: str, checks: list[list[str]]) -> None:
    log(f"verifying what {image}:{tag} actually carries")
    failed = []
    for cmd in checks:
        probe = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", cmd[0], f"{image}:{tag}", *cmd[1:]],
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
        raise Failed(f"{image}:{tag} is missing: {', '.join(failed)}")


def _push(image: str, tags: list[str]) -> None:
    for tag in tags:
        subprocess.run(["docker", "push", f"{image}:{tag}"], check=True)
        log(f"  pushed {image}:{tag}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--only", choices=["bake", "android"], default=None,
                   help="build one image instead of both")
    args = p.parse_args(argv)

    registry = os.environ.get("REGISTRY", "ghcr.io")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not repository:
        raise Failed("GITHUB_REPOSITORY is not set")

    image = f"{registry}/{repository.lower()}"
    stamp = time.strftime("%Y.%m.%d", time.gmtime())

    try:
        if args.only in (None, "bake"):
            log("pulling the base")
            subprocess.run(["docker", "pull", "qemux/qemu:7.50"], check=True)
            _build(image, "latest", "Containerfile", stamp=stamp)
            _verify(image, "latest", BAKE_CHECKS)
            _push(image, ["latest", stamp])

        if args.only in (None, "android"):
            _build(image, "android-builder", "android/15/Containerfile.builder", stamp=None)
            _verify(image, "android-builder", BUILDER_CHECKS)
            _push(image, ["android-builder"])

        log("IMAGE_READY")
        return 0
    except Failed as exc:
        log(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
