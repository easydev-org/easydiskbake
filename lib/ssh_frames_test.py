"""The ssh stage's two pane matchers, against the frames they were measured on.

WHY THIS EXISTS AS A TEST AND NOT AS A COMMENT. Both matchers are read inside a CROP, and a
crop is the one part of a walkthrough that cannot be checked by reading the file: the words
look right, the region looks plausible, and whether tesseract finds anything in it is a fact
about a 474x415 bitmap. Full screen, the identical phrase is NOT found on the same frame -
'allow full disk access' is small text in a 1920-wide image and is discarded as noise.

The frames are the two that settle it, both from a run that worked (42473082131):

    remote-login-3-pane.png               the sheet with the switch OFF
    remote-login-5-full-disk-access.png   the same sheet with the switch ON

and one that did not - a local run where the query was doubled, the list said No Results and
the sheet never opened. That third frame is what makes the gate a gate: "the pane is up" has
to be FALSE there, or it cannot stop the clicks that follow it.

The frames are downloaded from the run, so this test skips - loudly - when they are absent
rather than passing on nothing.
"""

from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO, os.path.join(REPO, "lib")):
    if path not in sys.path:
        sys.path.insert(0, path)

import yaml

import steps as stepslib

WALK = os.path.join(REPO, "macos", "15", "steps", "ssh.yaml")

FRAMES = [
    # (label, path, pane is up, switch is on)
    ("sheet with the switch OFF", os.path.join(REPO, "testdata", "ssh",
                                              "remote-login-3-pane.png"), True, False),
    ("sheet with the switch ON", os.path.join(REPO, "testdata", "ssh",
                                             "remote-login-5-full-disk-access.png"), True, True),
    ("No Results, no sheet", os.path.join(REPO, "testdata", "ssh", "search-no-results.png"),
     False, False),
]


class FrameDriver:
    """Enough of Driver for _read: a frame on disk, and the crop-then-OCR path."""

    def __init__(self, image_path: str) -> None:
        self.ppm = image_path
        self.text_value = ""

    def resolution(self):
        from PIL import Image
        return Image.open(self.ppm).size

    def look(self, label: str = "look"):
        import assistant
        self.text_value = assistant.ocr_text(self.ppm) or ""
        return []

    def look_at(self, region, label: str = "look", scale: int = 2):
        from PIL import Image
        import assistant
        image = Image.open(self.ppm).convert("RGB")
        box = tuple(region)
        crop = image.crop(box)
        crop = crop.resize(((box[2] - box[0]) * scale, (box[3] - box[1]) * scale),
                           Image.LANCZOS)
        out = os.path.join(os.path.dirname(self.ppm) or ".", "_crop.ppm")
        crop.save(out)
        self.ppm = out
        self.text_value = assistant.ocr_text(out) or ""
        return []

    def says(self, word: str) -> bool:
        have = {"".join(c for c in w.lower() if c.isalnum())
                for w in self.text_value.split()}
        wanted = "".join(c for c in word.lower() if c.isalnum())
        return wanted in have


def matches(name: str, driver: FrameDriver) -> bool:
    document = yaml.safe_load(open(WALK))
    spec = next(s for s in document["steps"] if s["name"] == name)
    step = stepslib.Step(dict(spec), 0)
    driver.text_value = ""
    stepslib._read(driver, step, name)
    groups = [[g] if isinstance(g, str) else list(g) for g in step["until_any"]]
    return any(all(driver.says(w) for w in group) for group in groups)


def main() -> int:
    if not all(os.path.isfile(path) for _, path, _, _ in FRAMES):
        print("=== SKIPPED: the measured ssh frames are not in testdata/ssh ===")
        print("  fetch them with:")
        print("    gh run download 42473082131 -n macos15-ssh-remote-login-frames -D testdata/ssh")
        return 0

    failures = 0
    for name, which in (("the remote login pane is up", 2), ("the switch reads on", 3)):
        print(f"=== {name}")
        for row in FRAMES:
            label, path, expected = row[0], row[1], row[which]
            hit = matches(name, FrameDriver(path))
            if hit == expected:
                print(f"  ok   {label:28} {'PASS' if hit else 'miss'}")
            else:
                failures += 1
                print(f"  FAIL {label:28} got {'PASS' if hit else 'miss'}, "
                      f"expected {'PASS' if expected else 'miss'}")

    print()
    print("=== EVERY SSH MATCHER AGREES WITH ITS FRAME ===" if not failures
          else f"=== {failures} MATCHER(S) DISAGREE WITH THEIR FRAME ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
