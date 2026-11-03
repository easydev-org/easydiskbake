"""Rename every workflow to {os}-{version}_{stage}-{action}.

WHY A SCRIPT AND NOT EIGHTEEN `git mv`s

Every reference has to move with the file: other workflows call each other with
`gh workflow run <name>`, the names appear in `gh run view` instructions in comments,
and a stale name is a 404 at dispatch time that reads like a missing permission. A
single table is the only place the old->new mapping exists, so the check that all
eighteen are covered is a comparison against the directory listing rather than a
memory.

THE RULE, as decided:

    {os}-{version}_{stage}-{action}

    macos-15_setup-bake        macOS from recovery to the Setup Assistant
    macos-15_desktop-bake      Setup Assistant to a desktop
    macos-15_setup-probe       photograph that first stage
    windows-11_desktop-bake    install Windows to a desktop
    windows-11_desktop-launch  boot a published Windows disk and look at it
    windows-11_ssh-bake        add OpenSSH to a desktop disk
    windows-11_ssh-launch      boot it and assert sshd answers
    windows-11_dev-bake        add the developer toolchain
    linux-nixos_gnome-bake     install NixOS and derive one desktop
    linux-ubuntu_desktop-bake  autoinstall
    linux-kubuntu_desktop-bake autoinstall, KDE flavour
    bsd-freebsd_bake           scripted bsdinstall, ssh and toolchain included
    bsd-openbsd_bake           install.conf, ssh and toolchain included

Utility workflows keep the same shape with a single-part version:

    iso-publish                push installer images to ghcr
    macos-recovery-publish     Apple's recovery images
    macos-assets-publish       the OpenCore assets
    selftest, image-build, bench-encode
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(HERE, ".github", "workflows")

# old stem -> (new stem, workflow name, what it does)
RENAMES: dict[str, tuple[str, str]] = {
    # --- macOS: the four stages the user asked to split -----------------------
    "bake-macos": ("macos-15_setup-bake", "macos-15_setup-bake"),
    "probe-macos": ("macos-15_setup-probe", "macos-15_setup-probe"),
    # --- Windows --------------------------------------------------------------
    "bake-windows": ("windows-11_desktop-bake", "windows-11_desktop-bake"),
    "launch-windows": ("windows-11_desktop-launch", "windows-11_desktop-launch"),
    "bake-windows-ssh": ("windows-11_ssh-bake", "windows-11_ssh-bake"),
    "bake-windows-dev": ("windows-11_dev-bake", "windows-11_dev-bake"),
    # --- Linux ----------------------------------------------------------------
    "bake-nixos": ("linux-nixos_bake", "linux-nixos_bake"),
    # --- Android --------------------------------------------------------------
    "bake-android": ("android-15_aosp-bake", "android-15_aosp-bake"),
    "launch-android": ("android-15_aosp-launch", "android-15_aosp-launch"),
    "publish-android": ("android-15_aosp-publish", "android-15_aosp-publish"),
    # --- utilities ------------------------------------------------------------
    "publish-iso": ("iso-publish", "iso-publish"),
    "publish-macos-recovery": ("macos-recovery-publish", "macos-recovery-publish"),
    "publish-macos-assets": ("macos-assets-publish", "macos-assets-publish"),
    "image": ("image-build", "image-build"),
    "selftest": ("selftest", "selftest"),
    "bench-encode": ("bench-encode", "bench-encode"),
}

# Files that are leaving: MX and the generic probe have no place in the agreed scope.
REMOVE = ("bake-mx", "probe")


def main(argv: list[str]) -> int:
    apply = "--apply" in argv

    present = {}
    for entry in sorted(os.listdir(WORKFLOWS)):
        if entry.endswith(".yml"):
            present[entry[:-4]] = os.path.join(WORKFLOWS, entry)

    print(f"workflows present: {len(present)}")
    missing = [k for k in present if k not in RENAMES and k not in REMOVE]
    unused = [k for k in RENAMES if k not in present]
    if missing:
        print(f"NOT IN THE TABLE: {missing}")
        return 1
    if unused:
        print(f"IN THE TABLE BUT ABSENT: {unused}")
        return 1

    for old, (new, name) in RENAMES.items():
        mark = "  " if old == new else "->"
        print(f"  {old:<26} {mark} {new}")
    for old in REMOVE:
        print(f"  {old:<26} -> (removed)")

    if not apply:
        print("\nnothing changed; pass --apply")
        return 0

    # Rename first, then rewrite references, so a name that maps to itself is not
    # rewritten twice.
    for old, (new, name) in RENAMES.items():
        src = present[old]
        dst = os.path.join(WORKFLOWS, new + ".yml")
        if src != dst:
            os.replace(src, dst)

    for old in REMOVE:
        os.unlink(present[old])

    # Every reference, in every file that survived, plus the non-workflow files that
    # name a workflow in a comment (lib/ci.py, the READMEs).
    rewritten = 0
    for root, _dirs, files in os.walk(HERE):
        if any(part in root for part in (".git", "__pycache__", "node_modules")):
            continue
        for name in files:
            if not name.endswith((".yml", ".yaml", ".py", ".md", ".sh")):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    body = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            original = body
            for old, (new, _name) in RENAMES.items():
                if old == new:
                    continue
                # `.yml` is part of a filename reference; the bare name is `gh workflow
                # run <name>` and a sentence. Both are the same string with or without
                # the suffix, so the suffix form is handled by the same substitution.
                body = body.replace(old + ".yml", new + ".yml")
                body = body.replace(f"name: {old}\n", f"name: {new}\n")
                body = body.replace(f"workflow run {old}", f"workflow run {new}")
                body = body.replace(f"workflow_dispatch {old}", f"workflow_dispatch {new}")
            if body != original:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
                rewritten += 1

    print(f"\nrenamed {len(RENAMES)} workflows, removed {len(REMOVE)}, "
          f"rewrote references in {rewritten} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
