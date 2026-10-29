"""Learn what each installer pane looks like, from frames already captured.

    learn.py <frames-dir> <out-panes.json>

This is the offline half. It runs once, on a workstation, against frames from a real
run; the bake then never calls tesseract at all.

WHAT IT LOOKS AT

The frames a run already produced, plus the driver's log that says which pane each step
was on. For every group of frames that belong to the same pane it computes a
fingerprint over a region - and the region is chosen by measurement, not by taste:

  the size of an 8x8 grid of the whole screen, then keep the cells whose colour varies
  LEAST between frames of the same pane and MOST between different panes. That is the
  part of the screen that identifies the pane.

The result is a table of names, regions, fingerprints and a tolerance each, taken from
the observed spread: a pane whose own frames vary a lot gets a wide tolerance, a static
pane a narrow one.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from panefp import read_ppm, region_stats, distance  # noqa: E402

GRID = 8


def cell_means(path):
    """8x8 mean colour of the whole frame, flattened to GRID*GRID*3 numbers."""
    width, height, px = read_ppm(path)
    cw, ch = width // GRID, height // GRID
    out = []
    for gy in range(GRID):
        for gx in range(GRID):
            r = g = b = n = 0
            for y in range(gy * ch, (gy + 1) * ch, 4):
                row = y * width * 3
                for x in range(gx * cw, (gx + 1) * cw, 4):
                    o = row + x * 3
                    r += px[o]
                    g += px[o + 1]
                    b += px[o + 2]
                    n += 1
            out.extend([r // n, g // n, b // n] if n else [0, 0, 0])
    return width, height, out


def pane_name(text):
    """A short stable name for the pane a driver log line describes.

    LONGEST MATCH WINS, not first match. Every installer pane after the first carries
    "MX Welcome" in its OCR text - that window is still open behind the installer - so a
    first-match list starting with "welcome" classified almost every pane as the welcome
    window. Ordering by the length of the matched phrase, and putting the specific
    names first, is what makes the distinction possible at all.
"""
    t = text.lower()

    # The pane title appears right after "Help | Live Log", which makes it the most
    # reliable part of the line to key on.
    m = re.search(r"live log[,]?\s*([a-z][a-z ]{3,40})", t)
    haystack = m.group(1) if m else t

    best=None
    for key, name in (
        ("general instructions", "general"),
        ("terms of use", "terms"),
        ("installation options", "options"),
        ("installation confirmation", "confirm"),
        ("computer identity", "identity"),
        ("default user login", "user"),
        ("installation in progress", "progress"),
        ("localization defaults", "locale"),
        ("mx welcome", "welcome"),
    ):
        if key in haystack or key in t:
            if best is None or len(key) > len(best[0]):
                best = (key, name)
    return best[1] if best else None


def main():
    frames_dir, out_path = sys.argv[1], sys.argv[2]
    drive_log = os.path.join(frames_dir, "drive.log")

    # step number -> pane name, from the driver's own account of where it was.
    step_pane = {}
    if os.path.exists(drive_log):
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", open(drive_log, errors="replace").read())
        for line in text.splitlines():
            m = re.search(r"step (\d+): now at (.*)", line)
            if m:
                name = pane_name(m.group(2))
                if name:
                    step_pane[int(m.group(1))] = name
            m = re.search(r"step (\d+): (MX Welcome|Installer).*", line)
            if m and int(m.group(1)) not in step_pane:
                name = pane_name(m.group(2))
                if name:
                    step_pane[int(m.group(1))] = name

    print("panes seen in the log:", sorted(set(step_pane.values())))

    # Frame for each step: sNN.ppm, and sNN-after.ppm is the pane that step reached.
    by_pane = {}
    for fname in sorted(os.listdir(frames_dir)):
        m = re.match(r"s(\d+)(-after)?\.ppm$", fname)
        if not m:
            continue
        step = int(m.group(1))
        after = bool(m.group(2))
        # An "after" frame belongs to the pane the NEXT step reports.
        name = step_pane.get(step + 1 if after else step)
        if not name:
            continue
        path = os.path.join(frames_dir, fname)
        try:
            w, h, means = cell_means(path)
            by_pane.setdefault(name, []).append((path, w, h, means))
        except ValueError as e:
            print("  skip %s: %s" % (fname, e))

    for name, items in sorted(by_pane.items()):
        print("  %-10s %d frames" % (name, len(items)))
    if len(by_pane) < 2:
        print("not enough distinct panes to learn from")
        return 1

    # Which grid cells separate the panes? Score each cell by (variation across panes)
    # divided by (variation within a pane), and keep the best.
    scores = []
    for cell in range(GRID * GRID):
        within = 0.0
        between_means = []
        for name, items in by_pane.items():
            # items is a list of (path, cell_means); the means are element 1.
            vals = [it[3][cell * 3:cell * 3 + 3] for it in items]
            mean = [sum(v[k] for v in vals) / len(vals) for k in range(3)]
            between_means.append(mean)
            for v in vals:
                within += sum(abs(v[k] - mean[k]) for k in range(3))
        within /= max(1, sum(len(v) for v in by_pane.values()))
        between = 0.0
        for i in range(len(between_means)):
            for j in range(i + 1, len(between_means)):
                between += sum(abs(between_means[i][k] - between_means[j][k])
                               for k in range(3))
        scores.append((between / max(1.0, within), cell))

    scores.sort(reverse=True)
    best = [c for _, c in scores[:4]]
    print("best separating cells (row,col):",
          [(c // GRID, c % GRID) for c in best])

    first = next(iter(by_pane.values()))[0]
    width, height = first[1], first[2]
    cw, ch = width // GRID, height // GRID

    # Try candidate regions and keep the one that separates the panes best.
    #
    # The obvious choice - the bounding box of the four best cells - spans most of the
    # screen when those cells are scattered, and then the region includes the desktop
    # wallpaper, the clock and the progress bar. Every pane then fingerprints as
    # "welcome", with a tolerance so wide that it absorbs all the others.
    #
    # So several candidates are measured instead of one assumed: each single cell, and
    # the bounding box of the two and four best cells. The score is the smallest
    # distance between any two different panes divided by the largest distance within a
    # pane - a region where panes are far apart and each pane is self-consistent wins.
    candidates = []
    for c in best:
        candidates.append(([c], [c]))
    candidates.append((best[:2], best[:2]))
    candidates.append((best, best))
    # A horizontal band across the top third, where pane titles live.
    candidates.append((None, [r * GRID + c for r in range(0, 3) for c in range(GRID)]))

    def region_of(cells):
        xs = [(c % GRID) * cw for c in cells]
        ys = [(c // GRID) * ch for c in cells]
        return [min(xs), min(ys), max(xs) + cw, max(ys) + ch]

    def stats_for(region):
        per_pane = {}
        for name, items in by_pane.items():
            fps = []
            for path, _w, _h, _m in items:
                means, hist, ink = region_stats(read_ppm(path)[2], width,
                                                region[0], region[1], region[2], region[3])
                fps.append({"means": means, "hist": hist, "ink": ink})
            per_pane[name] = fps
        return per_pane

    best_region, best_score = None, -1.0
    for _label, cells in candidates:
        if not cells:
            continue
        region = region_of(cells)
        per_pane = stats_for(region)
        within = 0.0
        for fps in per_pane.values():
            for i in range(len(fps)):
                for j in range(i + 1, len(fps)):
                    within = max(within, distance(fps[i], fps[j]))
        apart = None
        names = sorted(per_pane)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                # Distance between the two panes' least unusual frames.
                a = per_pane[names[i]][0]
                b = per_pane[names[j]][0]
                d = distance(a, b)
                apart = d if apart is None else min(apart, d)
        if apart is None:
            continue
        score = apart / max(1.0, within)
        print("  region %-24s apart=%6.1f within=%6.1f score=%.3f"
              % (str(region), apart, within, score))
        if score > best_score:
            best_score, best_region = score, region

    region = best_region
    print("fingerprint region:", region, "score %.3f" % best_score)

    table = {}
    for name, items in sorted(by_pane.items()):
        fps = []
        for path, _w, _h, _m in items:
            means, hist, ink = region_stats(read_ppm(path)[2], width,
                                            region[0], region[1], region[2], region[3])
            fps.append({"means": means, "hist": hist, "ink": ink})
        # The pane's own spread decides its tolerance: four times the worst distance
        # between two of its own frames, and never less than 15.
        spread = 0.0
        for i in range(len(fps)):
            for j in range(i + 1, len(fps)):
                spread = max(spread, distance(fps[i], fps[j]))
        tol = max(15, round(spread * 4))
        # The reference is the frame closest to all the others - the least unusual one.
        ref = min(fps, key=lambda f: sum(distance(f, g) for g in fps))
        table[name] = dict(ref, tolerance=tol, frames=len(fps))
        print("  %-10s tolerance=%d (own spread %.1f)" % (name, tol, spread))

    meta = {"size": [width, height], "region": region, "grid": GRID, "panes": table}
    with open(out_path, "w") as f:
        json.dump(meta, f, indent=2)
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
