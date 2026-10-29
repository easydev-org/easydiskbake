"""Group frames into panes by what they look like, then name each group.

    cluster.py <frames-dir> <out-panes.json> [--distance N]

Why not use the driver's log to label the frames

Because that is what produced a "welcome" group of fourteen frames whose own spread was
129.9 - nearly three times any other group's. Those fourteen frames were not one pane.
They were the desktop, the welcome window, and several installer panes where OCR had
read only the window title behind them. The log said "MX Welcome" for all of them,
because that window stays open behind the installer.

Grouping by pixels first removes that whole class of error: frames that look the same
end up together, whatever any log said about them. The name is then attached afterwards,
from whichever pane title appears most often inside the group.

The grouping is single-link with a distance threshold, which is enough for a dozen panes
that are visually far apart. It is not a general clustering algorithm and does not need
to be.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from panefp import read_ppm, region_stats, distance  # noqa: E402

GRID = 8


def coarse(path):
    """A cheap whole-frame summary, used only for grouping candidates."""
    width, height, px = read_ppm(path)
    cw, ch = width // GRID, height // GRID
    out = []
    for gy in range(GRID):
        for gx in range(GRID):
            r = g = b = n = 0
            for y in range(gy * ch, (gy + 1) * ch, 6):
                row = y * width * 3
                for x in range(gx * cw, (gx + 1) * cw, 6):
                    o = row + x * 3
                    r += px[o]
                    g += px[o + 1]
                    b += px[o + 2]
                    n += 1
            out.extend([r // n, g // n, b // n] if n else [0, 0, 0])
    return width, height, out


TITLES = (
    ("general instructions", "general"),
    ("terms of use", "terms"),
    ("installation options", "options"),
    ("installation confirmation", "confirm"),
    ("computer identity", "identity"),
    ("default user login", "user"),
    ("installation in progress", "progress"),
    ("localization defaults", "locale"),
    ("swap file", "progress"),
    ("mx welcome", "welcome"),
)


def title_in(text):
    """Every pane title that appears, most specific first."""
    t = text.lower()
    found = [name for key, name in TITLES if key in t]
    return found


def main():
    frames_dir, out_path = sys.argv[1], sys.argv[2]
    threshold = 25.0
    if "--distance" in sys.argv:
        threshold = float(sys.argv[sys.argv.index("--distance") + 1])

    frames = []
    for name in sorted(os.listdir(frames_dir)):
        if not name.endswith(".ppm"):
            continue
        path = os.path.join(frames_dir, name)
        try:
            width, height, means = coarse(path)
        except ValueError:
            continue
        frames.append({"name": name, "path": path, "means": means})
    if len(frames) < 2:
        print("not enough frames")
        return 1
    print("frames:", len(frames))

    # Single-link grouping on the coarse whole-frame distance.
    group = list(range(len(frames)))

    def find(i):
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    for i in range(len(frames)):
        for j in range(i + 1, len(frames)):
            d = distance({"means": frames[i]["means"], "hist": [0], "ink": 0},
                         {"means": frames[j]["means"], "hist": [0], "ink": 0})
            if d <= threshold:
                a, b = find(i), find(j)
                if a != b:
                    group[b] = a

    clusters = {}
    for i in range(len(frames)):
        clusters.setdefault(find(i), []).append(frames[i])
    print("clusters at threshold %.0f: %d" % (threshold, len(clusters)))

    # Name each cluster from the pane titles visible in its OCR text, if a log recorded
    # them; otherwise by position in the run, which is deterministic for an installer.
    drive_log = os.path.join(frames_dir, "drive.log")
    step_text = {}
    if os.path.exists(drive_log):
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", open(drive_log, errors="replace").read())
        for line in text.splitlines():
            m = re.search(r"step (\d+): now at (.*)", line)
            if m:
                step_text[int(m.group(1))] = m.group(2)
            m = re.search(r"step (\d+): ([A-Z].*)", line)
            if m and int(m.group(1)) not in step_text:
                step_text[int(m.group(1))] = m.group(2)

    named = {}
    for root, members in sorted(clusters.items(), key=lambda kv: min(
            int(re.search(r"s(\d+)", m["name"]).group(1)) if re.search(r"s(\d+)", m["name"]) else 999
            for m in kv[1])):
        votes = {}
        for m in members:
            s = re.search(r"s(\d+)", m["name"])
            if not s:
                continue
            step = int(s.group(1))
            # An "-after" frame shows the pane the NEXT step reports.
            for probe in (step + 1 if "-after" in m["name"] else step, step):
                for name in title_in(step_text.get(probe, "")):
                    votes[name] = votes.get(name, 0) + 1
        label = max(votes, key=votes.get) if votes else "pane%d" % len(named)
        n = 2
        base = label
        while label in named:
            label = "%s%d" % (base, n)
            n += 1
        named[label] = members
        print("  %-10s %2d frames: %s" % (label, len(members),
                                          ", ".join(m["name"] for m in members)[:80]))

    # The fingerprint region: the cells that separate the named clusters best.
    first = frames[0]
    width, height = first["means"][:0] or (0, 0), 0
    width, height, _ = coarse(frames[0]["path"])
    cw, ch = width // GRID, height // GRID

    scores = []
    for cell in range(GRID * GRID):
        between_means, within = [], 0.0
        for label, members in named.items():
            vals = [m["means"][cell * 3:cell * 3 + 3] for m in members]
            mean = [sum(v[k] for v in vals) / len(vals) for k in range(3)]
            between_means.append(mean)
            for v in vals:
                within += sum(abs(v[k] - mean[k]) for k in range(3))
        within /= max(1, sum(len(m) for m in named.values()))
        between = 0.0
        for i in range(len(between_means)):
            for j in range(i + 1, len(between_means)):
                between += sum(abs(between_means[i][k] - between_means[j][k]) for k in range(3))
        scores.append((between / max(1.0, within), cell))
    scores.sort(reverse=True)

    best_cells = [c for _, c in scores[:3]]
    xs = [(c % GRID) * cw for c in best_cells]
    ys = [(c // GRID) * ch for c in best_cells]
    region = [min(xs), min(ys), max(xs) + cw, max(ys) + ch]
    print("fingerprint region:", region)

    table = {}
    for label, members in named.items():
        fps = []
        for m in members:
            means, hist, ink = region_stats(read_ppm(m["path"])[2], width,
                                            region[0], region[1], region[2], region[3])
            fps.append({"means": means, "hist": hist, "ink": ink})
        spread = 0.0
        for i in range(len(fps)):
            for j in range(i + 1, len(fps)):
                spread = max(spread, distance(fps[i], fps[j]))
        tol = max(12, round(spread * 2.5))
        ref = min(fps, key=lambda f: sum(distance(f, g) for g in fps))
        table[label] = dict(ref, tolerance=tol, frames=len(fps))
        print("  %-10s tol=%3d spread=%5.1f frames=%d" % (label, tol, spread, len(fps)))

    json.dump({"size": [width, height], "region": region, "panes": table},
              open(out_path, "w"), indent=2)
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
