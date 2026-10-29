"""Check a learned fingerprint table against frames it was not built from.

    verify.py <panes.json> <frames-dir>

Prints, for every frame, which pane the table thinks it is and how far off that is.

The point is the two numbers side by side: the distance to the best match, and the
distance to the runner-up. A table where those two are close is a table that will
mis-identify panes on frames slightly different from the ones it learned from.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from panefp import read_ppm, region_stats, distance  # noqa: E402


def main():
    meta = json.load(open(sys.argv[1]))
    frames_dir = sys.argv[2]
    region = meta["region"]
    table = meta["panes"]
    width, height = meta["size"]

    print("%-16s %-10s %8s %8s %8s" % ("frame", "best", "dist", "tol", "runner-up"))
    for name in sorted(os.listdir(frames_dir)):
        if not name.endswith(".ppm"):
            continue
        path = os.path.join(frames_dir, name)
        try:
            px = read_ppm(path)[2]
        except ValueError:
            continue
        means, hist, ink = region_stats(px, width, region[0], region[1], region[2], region[3])
        fp = {"means": means, "hist": hist, "ink": ink}

        scored = sorted(((distance(fp, e), n) for n, e in table.items()))
        best_d, best_n = scored[0]
        run_d, run_n = scored[1] if len(scored) > 1 else (None, None)
        tol = table[best_n]["tolerance"]
        mark = "ok" if best_d <= tol else "REJECTED"
        # How much of the tolerance the match used: close to 1.0 is a near miss.
        used = best_d / tol if tol else 0
        print("%-16s %-10s %8.1f %8d %8s  %s (%.0f%% of tol, next=%s %.1f)"
              % (name, best_n, best_d, tol, "%s %.1f" % (run_n, run_d),
                 mark, used * 100, run_n, run_d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
