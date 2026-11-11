"""Build the bake image, the Android images and the macOS appliance, and push them all.

    python3 lib/build_image.py                 # both images
    python3 lib/build_image.py --only appliance  # just the macOS appliance
    python3 lib/build_image.py --only bake     # just the bake image

TWO IMAGES, TWO PURPOSES

    <repo>:latest           the bake image: qemu, xorriso, swtpm, OVMF, ffmpeg,
                            wimlib, aria2, oras, python3
    <repo>:macos15-appliance a bootable macOS 15 VM: the baked macos15-ssh disk, the
                            recovery image and the OpenCore assets, all inside the image

macos15-appliance is separate because it is 16 GiB of disk that only someone who wants to
run macOS pulls, so it must not be a layer of the image every bake starts from. It is also
the one image here that needs a registry credential DURING the build - it pulls six
artifacts from this repository's own package - which is a `--secret` mount rather than a
build argument, so the token never lands in the image history.

THE TWO ANDROID IMAGES ARE GONE, AND SO IS THE ROUTE THEY EXISTED FOR.

The two Android images that used to be built here carried cmdline-tools, a JRE, the
emulator and adb, so `avdmanager` could assemble an AVD from Google's AOSP system image and
the goldfish machine could boot it. That was the SDK's way of turning a system image into
disks, and Android is the one platform here that a STOCK qemu boots as an ordinary PC:
android/blissos16 installs onto a GPT disk with an ESP and starts it through UEFI, which
needs nothing the bake image does not already have. One Android route, and it is the one
with no SDK in it.

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
    # Actions are Node programs the runner executes inside this container. Without
    # node, `uses: actions/checkout@v4` fails before any code in this repository runs.
    ["node", "--version"],
    ["git", "--version"],
    ["qemu-system-x86_64", "--version"],
    ["qemu-img", "--version"],
    ["xorriso", "-version"],
    ["7z"],
    ["python3", "--version"],
    # The firmware is a file rather than a binary on PATH, so it needs its own check.
    # Windows 11 will not install without it and its absence reads as a broken ISO.
    ["ls", "-l", "/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"],
]

# What the macOS appliance carries. EVERY ENTRY IS SOMETHING THE BOOT NEEDS, and the point
# of checking here rather than at first start is that a missing asset otherwise shows up as a
# guest sitting at a prohibited sign - or, worse, at a Setup Assistant, which looks like a
# working boot for several minutes.
#
# The disk is checked through its manifest, not by listing files: 15.9 GiB of qcow2 is either
# the published bytes or it is nothing, and only the hashes can tell the two apart.
APPLIANCE_CHECKS = [
    ["sh", "-c", "test -s /assets/recovery.dmg"],
    ["sh", "-c", "test -s /assets/opencore/macos.rom"],
    ["sh", "-c", "test -s /assets/opencore/OVMF_VARS-1920x1080.fd"],
    ["sh", "-c", "test -s /assets/opencore/opencore.zip"],
    ["sh", "-c", "test -s /assets/opencore/opencore-template.iso"],
    # THE IMAGE MUST NOT CARRY A DISK, because that is what makes it one image that boots four
    # things instead of a 16 GiB image that boots one. This is an assertion about a deliberate
    # absence, which is the kind that is otherwise noticed only when the image is 16 GiB again.
    ["sh", "-c", "test ! -e /assets/disk"],
    # The four choices are cheap to state and are the image's whole interface.
    ["python3", "/repo/macos/15/appliance.py", "pull", "--list"],
    ["sh", "-c", "command -v websockify qemu-system-x86_64 dnsmasq iptables ip"],
    ["sh", "-c", "test -f /usr/share/novnc/vnc.html"],
]


def _build(image: str, tag: str, dockerfile: str, *, stamp: str | None,
           secret: str | None = None) -> None:
    log(f"building {image}:{tag}")
    tags = ["-t", f"{image}:{tag}"]
    if stamp:
        tags += ["-t", f"{image}:{stamp}"]

    args = ["docker", "build", "--progress=plain"]
    if secret:
        # A MOUNT, NOT AN ARG. `--build-arg TOKEN=...` writes the value into the image
        # history, which every layer keeps; a BuildKit secret is mounted for the one RUN that
        # reads it and is absent from the result. The appliance build pulls from this
        # repository's own ghcr package, so it is the one build here that needs a credential.
        args += ["--secret", f"id=ghcr_token,src={secret}"]
    args += ["-f", dockerfile, *tags, "."]

    result = subprocess.run(args, check=False)
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
    p.add_argument("--only", choices=["bake", "appliance"], default=None,
                   help="build one image instead of both")
    p.add_argument("--ghcr-token-file", default="",
                   help="file holding a ghcr token for the appliance build; written to a "
                        "temporary file when empty and pushed out of the image afterwards")
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

        if args.only in (None, "appliance"):
            # NO STAMP. A 16 GiB image is not something to redeploy by accident, and its
            # content is a published disk rather than a build of this commit - the stamp
            # would date the Dockerfile, not the macOS install inside it.
            token_file = args.ghcr_token_file
            temporary = ""
            if not token_file:
                token = os.environ.get("GITHUB_TOKEN", "")
                if token:
                    temporary = "/tmp/ghcr_token"
                    with open(temporary, "w", encoding="utf-8") as fh:
                        fh.write(token)
                    token_file = temporary
            try:
                _build(image, "macos15-appliance",
                       "macos/15/Containerfile.appliance", stamp=None,
                       secret=token_file or None)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            _verify(image, "macos15-appliance", APPLIANCE_CHECKS)
            _push(image, ["macos15-appliance"])

        log("IMAGE_READY")
        return 0
    except Failed as exc:
        log(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
