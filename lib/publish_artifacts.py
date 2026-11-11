"""Publish every upstream artifact this repository needs, from one entry point.

    python3 lib/publish_artifacts.py --list
    python3 lib/publish_artifacts.py iso-windows11 iso-ubuntu
    python3 lib/publish_artifacts.py --group iso
    python3 lib/publish_artifacts.py --all

WHY THIS EXISTS

Three workflows published upstream files to ghcr and each was a near-copy of the others:

    iso-publish                the installer images        lib/publish_iso.py
    macos-assets-publish       OpenCore, firmware, vars    lib/publish_macos_assets.py
    macos-recovery-publish     Apple's recovery images     lib/macos_recovery.py

All three download a pinned URL, verify it and push it to this repository's package. The
differences are which table the pins come from and how the file is fetched; two of them
(publish_iso and publish_macos_assets) were so close that their main() bodies differed only
by the table's name.

WHAT IS SHARED AND WHAT IS NOT

Shared: the pins (lib/images.py - one table, so nothing can be pinned in two places), the
registry exchange (lib/iso.push), the verification rules, and the "one mirror should not
cost the other nineteen" keep-going behaviour.

NOT SHARED, AND THIS FILE DOES NOT TRY: how each artifact is obtained. An installer image is
one HTTP GET against a pinned URL. A macOS recovery image is a session cookie, a POST with
five opaque fields, a token exchange and then a GET - it cannot be one request because Apple
will not serve it as one. An AVD used to be a third kind - not a file at all but a directory of
raw images packed into a tar - and that publisher is gone with the emulator route. BlissOS is
an ordinary ISO from SourceForge, pinned by sha256 in lib/images.py and fetched by
android/blissos16/fetch_iso.py, so Android no longer needs a publisher of its own.

So this is a DISPATCHER: it owns the selection - which tag belongs to which publisher, what
"all" means, and the order - and each publisher keeps the code that only it can have.

THE TAG IS THE UNIT OF SELECTION, NOT THE WORKFLOW. A caller says `iso-windows11` or
`macos-asset-opencore`; the prefix decides who publishes it, and `--list` prints every tag
with the group it belongs to. That is what makes "publish everything" one command without a
list of repositories to keep in step.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)


def log(message: str) -> None:
    print(f"[artifacts {time.strftime('%H:%M:%S')}] {message}", flush=True)


# THE GROUPS, AND THE PREFIX THAT SELECTS EACH. Order matters twice: `iso-` must be tested
# before nothing else claims it, and a full run publishes the installer images first because
# every bake depends on them and they are the largest downloads.
GROUPS = {
    "iso":       {"prefix": "iso-",         "module": "publish_iso"},
    "macos":     {"prefix": "macos-asset-", "module": "publish_macos_assets"},
    "recovery":  {"prefix": "macos-recovery-", "module": "macos_recovery"},
}

# `macos-recovery-` ALSO STARTS WITH `macos-`, so the prefixes are tested longest-first.
# Without that ordering, `macos-recovery-sequoia` would be claimed by the macOS assets
# publisher and every recovery image would fail to match a pin it was never in.
PREFIX_ORDER = sorted(
    ((g, spec["prefix"]) for g, spec in GROUPS.items()),
    key=lambda pair: len(pair[1]), reverse=True,
)


def group_of(tag: str) -> str | None:
    for group, prefix in PREFIX_ORDER:
        if tag.startswith(prefix):
            return group
    return None


def catalogue() -> dict[str, str]:
    """Every publishable tag, mapped to its group.

    THE TABLES COME FROM lib/images.py AND NOT FROM THE PUBLISHERS. A module that lists its
    own pins is a second place to keep in step, and this repository has already had a pin
    that existed in one table and not the other.
    """
    import images

    found: dict[str, str] = {}
    for tag in images.all_images():
        group = group_of(tag)
        if group:
            found[tag] = group
    for tag in images.all_macos_assets():
        group = group_of(tag)
        if group:
            found[tag] = group
    for name in ("14", "15", "26"):
        tag = f"macos-recovery-{images.MACOS_RELEASES and _recovery_name(name)}"
        found[tag] = "recovery"
    return found


def _recovery_name(release: str) -> str:
    """'15' -> 'sequoia'. The release-to-name mapping lives in lib/macos_recovery.py."""
    import macos_recovery

    return macos_recovery.resolve(release)[0]


def publish_one(tag: str, group: str, repository: str, *, keep: bool) -> bool:
    """Hand one tag to its publisher. True when it published."""
    import importlib

    module = importlib.import_module(GROUPS[group]["module"])

    log(f"=== {tag} ({group}) ===")
    if group == "recovery":
        # A release NUMBER, because that is what macos_recovery takes and what it resolves
        # to a board id. The tag carries the name, so it is turned back into one.
        name = tag[len("macos-recovery-"):]
        import macos_recovery

        number = macos_recovery.ALIASES.get(name, name)
        return module.main(["--release", number, "--publish",
                            "--repository", repository]) == 0

    # iso and macos-asset: both take a tag positionally.
    argv = [tag, "--repository", repository]
    if keep:
        argv.append("--keep")
    return module.main(argv) == 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("tags", nargs="*", help="tags to publish, e.g. iso-windows11")
    p.add_argument("--all", action="store_true", help="publish every group")
    p.add_argument("--group", action="append", default=[],
                   choices=sorted(GROUPS), help="publish one group; may be repeated")
    p.add_argument("--list", action="store_true", help="show the tags and their groups")
    p.add_argument("--keep", action="store_true",
                   help="keep the downloaded files instead of deleting them after push")
    p.add_argument("--keep-going", action="store_true",
                   help="attempt every artifact even after one fails")
    p.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""),
                   help="owner/repo to push into (default: $GITHUB_REPOSITORY)")
    args = p.parse_args(argv)

    table = catalogue()

    if args.list:
        for tag, group in sorted(table.items()):
            print(f"{tag:34} {group}")
        return 0

    if args.tags:
        wanted = list(args.tags)
    elif args.group:
        wanted = [t for t, g in table.items() if g in args.group]
    elif args.all:
        wanted = list(table)
    else:
        p.error("no tags given; use --all, --group <name>, --list, or name the tags")

    # AN UNKNOWN TAG IS A FAILURE, NOT A SKIP. A caller that typed `iso-win11x64` wants to
    # know the pin does not exist; silently publishing nothing looks like success.
    unknown = [t for t in wanted if t not in table]
    if unknown:
        log(f"=== NO SUCH TAG: {', '.join(unknown)} ===")
        log(f"  known tags are listed by --list; {len(table)} of them")
        return 2

    # IN GROUP ORDER, NOT ARGUMENT ORDER, so a full run always fetches the installer images
    # before the things that are derived from them.
    order = {g: i for i, g in enumerate(GROUPS)}
    wanted.sort(key=lambda t: (order[table[t]], t))

    log(f"publishing {len(wanted)} artifact(s) to {args.repository}")
    failures: list[str] = []
    for tag in wanted:
        try:
            ok = publish_one(tag, table[tag], args.repository, keep=args.keep)
        except SystemExit as exc:                  # a publisher that calls sys.exit
            ok = exc.code in (0, None)
        except Exception as exc:                   # noqa: BLE001 - reported per artifact
            log(f"=== {tag} RAISED {type(exc).__name__}: {exc} ===")
            ok = False
        if not ok:
            failures.append(tag)
            log(f"=== {tag} FAILED ===")
            if not args.keep_going:
                log("  stopping; pass --keep-going to attempt the rest")
                break

    if failures:
        log(f"=== {len(failures)} OF {len(wanted)} FAILED: {', '.join(failures)} ===")
        return 1
    log(f"=== ALL {len(wanted)} PUBLISHED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
