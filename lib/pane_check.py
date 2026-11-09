"""Match a walkthrough's panes against REAL frames, with no guest and no QEMU.

    python3 lib/pane_check.py macos/15/steps/sequoia.yaml /tmp/mvid/f129/*.png

WHY THIS EXISTS

The desktop stage's failure mode is not a crash: it is a pane that no handler matches, or two
handlers that match the same screen. Both are questions about OCR and about coordinates, and
neither needs a running machine to answer - a recording's frames are the same pictures the
driver read.

The specific bug this was written for: the account pane's body says

    Allow computer account password to be reset with your Apple Account.
    You must sign in with your Apple Account in the next step to use this feature.

so `says("sign", "apple")` was TRUE on both panes, and the walk answered one as the other. That
was found only after a 40-minute CI run. Here it is one command.

WHAT IT REPORTS, PER FRAME

    the first pane whose `when` matches, with its title words
    every OTHER pane that also matched — because a second match is the bug above
    the pixels it read, when the pane declares a region

IT CANNOT TELL YOU the pane was ANSWERED, only that it was RECOGNISED. Answering needs a click;
recognising is what decides whether the click is on the right pane at all.
"""

from __future__ import annotations

import argparse
import os
import sys

REPO = os.environ.get("REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO, os.path.join(REPO, "lib"), os.path.join(REPO, "macos", "15")):
    if path not in sys.path:
        sys.path.insert(0, path)


def log(message: str) -> None:
    print(f"[pane] {message}", flush=True)


def words_in(path: str, region=None, scale: int = 2) -> set[str]:
    """The words on a frame, whole or in a crop, normalised the way the driver normalises."""
    import assistant

    if region:
        text = assistant.ocr_text_cropped(path, tuple(region), scale=scale) or ""
    else:
        text = assistant.ocr_text(path) or ""
    return {"".join(c for c in w.lower() if c.isalnum()) for w in text.split()}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("walkthrough")
    p.add_argument("frames", nargs="+")
    p.add_argument("--quiet", action="store_true", help="only frames with 0 or 2+ matches")
    a = p.parse_args(argv)

    import steps as stepslib

    walk = stepslib.load(a.walkthrough)
    if not walk.panes:
        log(f"{a.walkthrough} is a sequence, not a pane set; nothing to match")
        return 2

    log(f"{a.walkthrough}: {len(walk.panes)} pane(s), {len(a.frames)} frame(s)")

    ambiguous = 0
    unmatched = 0
    for frame in sorted(a.frames):
        hits = []
        for step in walk.panes:
            when = step["when"]
            want = when if isinstance(when, list) else [when]
            have = words_in(frame, step["region"],
                            int(step.get("scale", stepslib.DEFAULT_CROP_SCALE)))
            if all("".join(c for c in w.lower() if c.isalnum()) in have for w in want):
                hits.append(step.name)

        name = os.path.basename(frame)
        if not hits:
            unmatched += 1
            if not a.quiet:
                log(f"  {name}: (nothing matches)")
        elif len(hits) > 1:
            ambiguous += 1
            log(f"  {name}: AMBIGUOUS — {', '.join(hits)}")
        elif not a.quiet:
            log(f"  {name}: {hits[0]}")

    print()
    log(f"unmatched {unmatched}, ambiguous {ambiguous}")
    # AN AMBIGUOUS FRAME IS A BUG IN THE WALKTHROUGH and this exits non-zero for it. An
    # unmatched frame is not: a boot screen, a desktop and a spinner are all legitimately none
    # of the panes.
    return 1 if ambiguous else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
