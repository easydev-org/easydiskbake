"""Identify a screen by its pixels, so nothing has to read it at run time.

    from panefp import fingerprint, match
    fp = fingerprint("/tmp/frame.ppm")
    name, score = match(fp, table)

A fingerprint is a small, tolerant description of a region:

  * a 4x4 grid of mean colours - the coarse layout, so a dark console and a light
    dialog never collide;
  * a 16-bucket luminance histogram, normalised - the tone, which distinguishes two
    panes that differ in how much text they carry rather than where it is;
  * the fraction of pixels that are "ink" (darker than the region's median by a margin)
    - how dense the text is. This is what separates a pane with a heading from one with
    a paragraph.

WHY NOT A HASH OF THE BYTES

A byte hash matches exactly or not at all, and every pane here changes slightly between
captures: the clock advances, the pointer moves, a progress percentage ticks over. A
byte hash would report "unknown pane" on every frame. The three numbers above move a
little when the pane is the same and a lot when it is different, which is exactly the
property needed.

WHY NOT OCR AT RUN TIME

Because it is slow and it is unreliable in precisely the cases that matter. Reading a
1280x800 frame cost eight to ten seconds per step, and tesseract returned nothing at all
for this installer's button row - the buttons are small light text on a coloured bar.
Two runs failed because a card title was not read on one frame out of fifteen.

OCR is still the right tool for *finding* the coordinates. It just should not run while
the bake is trying to get somewhere.
"""

import struct

GRID = 4            # 4x4 mean colours
LUMA_BUCKETS = 16   # luminance histogram


def read_ppm(path):
    """Parse a binary PPM (P6). Returns (width, height, list of (r,g,b) rows)."""
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"P6"):
        raise ValueError("not a P6 ppm: %r" % data[:2])

    # Header: P6 <w> <h> <maxval>, whitespace separated, comments allowed.
    fields = []
    i = 2
    while len(fields) < 3:
        while i < len(data) and data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            while i < len(data) and data[i:i + 1] != b"\n":
                i += 1
            continue
        start = i
        while i < len(data) and not data[i:i + 1].isspace():
            i += 1
        fields.append(int(data[start:i]))
    i += 1  # single whitespace after maxval

    width, height, maxval = fields
    if maxval != 255:
        raise ValueError("only 8-bit ppm is handled, got maxval %d" % maxval)
    pixels = data[i:]
    if len(pixels) < width * height * 3:
        raise ValueError("truncated ppm: %d bytes for %dx%d" % (len(pixels), width, height))
    return width, height, pixels


def region_stats(px, width, x0, y0, x1, y1):
    """Grid means, luminance histogram and ink fraction for one rectangle."""
    gw = max(1, (x1 - x0) // GRID)
    gh = max(1, (y1 - y0) // GRID)
    cells = [[[0, 0, 0, 0] for _ in range(GRID)] for _ in range(GRID)]
    hist = [0] * LUMA_BUCKETS
    lumas = []

    for y in range(y0, y1):
        row = y * width * 3
        gy = min(GRID - 1, (y - y0) // gh)
        for x in range(x0, x1):
            o = row + x * 3
            r, g, b = px[o], px[o + 1], px[o + 2]
            gx = min(GRID - 1, (x - x0) // gw)
            cell = cells[gy][gx]
            cell[0] += r
            cell[1] += g
            cell[2] += b
            cell[3] += 1
            luma = (r * 299 + g * 587 + b * 114) // 1000
            lumas.append(luma)
            hist[luma * LUMA_BUCKETS // 256] += 1

    means = []
    for gy in range(GRID):
        for gx in range(GRID):
            r, g, b, n = cells[gy][gx]
            means.extend([r // n, g // n, b // n] if n else [0, 0, 0])

    total = max(1, len(lumas))
    hist = [round(h * 1000 / total) for h in hist]

    # Ink: pixels noticeably darker than the region's median. A heading against a mostly
    # empty panel gives a small fraction; a paragraph gives a large one.
    lumas.sort()
    median = lumas[len(lumas) // 2]
    ink = sum(1 for l in lumas if l < median - 30)
    return means, hist, round(ink * 1000 / total)


def fingerprint(path, region):
    """Fingerprint one rectangle of a frame: (x0, y0, x1, y1) in pixels."""
    width, height, px = read_ppm(path)
    x0, y0, x1, y1 = region
    x1 = min(x1, width)
    y1 = min(y1, height)
    means, hist, ink = region_stats(px, width, x0, y0, x1, y1)
    return {"means": means, "hist": hist, "ink": ink,
            "size": [width, height], "region": list(region)}


def distance(a, b):
    """How far apart two fingerprints are, as a single number.

    Three contributions, weighted by how much each one can be trusted:

      colour    mean colour per grid cell; a big difference means a different screen
      tone      the luminance histogram; separates panes of similar layout
      ink       text density; the cheapest way to tell a dialog from a paragraph

    The weights are not arbitrary: colour is measured over the whole region and is the
    most stable, ink over single pixels and is the noisiest.
    """
    dc = sum(abs(x - y) for x, y in zip(a["means"], b["means"])) / max(1, len(a["means"]))
    dh = sum(abs(x - y) for x, y in zip(a["hist"], b["hist"])) / max(1, len(a["hist"]))
    di = abs(a["ink"] - b["ink"])
    return 1.0 * dc + 0.6 * dh + 0.8 * di


def match(fp, table, tolerance=None):
    """The closest named pane, if it is within tolerance.

    Returns (name, distance) or (None, best_distance). The tolerance is per-pane,
    because some panes are static and some have a clock ticking in the corner.
    """
    best_name, best_d, best_tol = None, None, None
    for name, entry in table.items():
        d = distance(fp, entry)
        tol = tolerance if tolerance is not None else entry.get("tolerance", 40)
        if best_d is None or d < best_d:
            best_name, best_d, best_tol = name, d, tol
    if best_name is not None and best_d is not None and best_d <= best_tol:
        return best_name, best_d
    return None, best_d
