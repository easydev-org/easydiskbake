"""Every desktop pane matcher against every desktop frame we have.

WHY A MATRIX AND NOT A SPOT CHECK. A pane set is an ORDERED list matched against the screen,
and the bug this exists to catch is not "a matcher does not fire" - it is "a matcher fires on
somebody else's pane and, being declared earlier, wins". That is exactly what happened to
`data and privacy` versus `analytics`: both matched the Analytics screen, the privacy step is
earlier, so the analytics handler never ran and the baked disk kept Share Mac Analytics ticked
while the stage reported success.

A matcher firing on the wrong pane is invisible in a single-frame test. It shows up here as a
column with two hits, and the earlier one is the one that would run.
"""
import os
import subprocess
import sys

sys.path.insert(0, "/root/repos/easydiskbake/lib")

import yaml
import assistant
from PIL import Image

WALK = "/root/repos/easydiskbake/macos/15/steps/sequoia.yaml"
VIDEO = "/tmp/drec/install.mp4"
CACHE = "/tmp/drec/fr"

# (label, seconds into the recording) - chosen by looking at the frames, one per pane.
FRAMES = [
    ("country",       35),
    ("languages",     75),
    ("accessibility", 125),
    ("privacy",       155),
    ("account",       190),
    ("analytics",     512),
    ("look",          540),
    ("timezone",      490),
]


def frame_at(seconds: int) -> str:
    png = os.path.join(CACHE, f"m{seconds}.png")
    if not os.path.isfile(png):
        subprocess.run(["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", VIDEO,
                        "-frames:v", "1", "-y", png], check=True)
    return png


def words_of(png: str) -> set[str]:
    # A UNIQUE PPM PER CALL. tesseract reads the file it is given, and a single reused path
    # made every frame in the matrix report the same words - which showed up as matchers
    # firing on screens they had never seen.
    ppm = os.path.splitext(png)[0] + ".ppm"
    Image.open(png).convert("RGB").save(ppm)
    text = assistant.ocr_text(ppm) or ""
    return {"".join(c for c in w.lower() if c.isalnum()) for w in text.split()}


def main() -> int:
    if not os.path.isfile(VIDEO):
        print(f"=== SKIPPED: no recording at {VIDEO} ===")
        return 0
    document = yaml.safe_load(open(WALK))
    panes = [(s["name"], [s["when"]] if isinstance(s["when"], str) else s["when"])
             for s in document["steps"] if s.get("when")]

    columns = []
    for label, seconds in FRAMES:
        columns.append((label, words_of(frame_at(seconds))))

    width = max(len(n) for n, _ in panes)
    header = "".join(f"{label[:9]:>11}" for label, _ in columns)
    print(f"{'pane':{width}} {header}")
    print("-" * (width + 11 * len(columns)))

    wrong = []
    for name, matcher in panes:
        row = ""
        hits = []
        for label, have in columns:
            hit = all(w in have for w in matcher)
            row += f"{'HIT' if hit else '.':>11}"
            if hit:
                hits.append(label)
        print(f"{name:{width}} {row}   {','.join(hits)}")
        if len(hits) > 1:
            wrong.append((name, hits))

    print()
    if wrong:
        print("=== THESE PANES MATCH MORE THAN ONE SCREEN - the earlier one would win ===")
        for name, hits in wrong:
            print(f"  {name}: {', '.join(hits)}")
        return 1
    print("=== every pane matcher fires on one screen only ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
