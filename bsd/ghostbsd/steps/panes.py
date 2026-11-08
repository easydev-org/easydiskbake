"""GhostBSD's one measured exception: the desktop icon is found, not assumed.

WHY THIS IS NOT A COORDINATE LIKE EVERY OTHER STEP HERE

THE DESKTOP PUTS `Install GhostBSD` AND `Trash` IN EITHER ORDER, measured on two runs of
this same image at 1920x1080:

    run 42441278741   Computer (88,110)  Home (88,188)  Install GhostBSD (88,240)  Trash (88,340)
    run 42502676624   Computer (88,110)  Home (88,188)  Trash (88,240)  Install GhostBSD (88,340)

- the two icons swap rows between boots. A driver that pressed (88,240) unconditionally
double-clicked `Trash` on the second layout and opened Caja; the wizard never started, every
pane click after that landed on a file manager, and the run ended with

    === THE DISK IS EMPTY (0 MiB) - THE INSTALL DID NOT HAPPEN ===

That is exactly the kind of failure a fixed coordinate cannot survive: the screen is correct,
the number is not.

AND IT IS NOT A LOOKUP THAT CHOOSES A CLICK. Every other step in this repository forbids
reading the screen to decide WHERE to click, because a lookup can be talked into clicking a
word in a paragraph - that is what stalled kubuntu-gui for eighty-seven minutes. This is the
other case: the instruction is not "go here", it is "double-click the installer's icon", and
a coordinate cannot say that. The icon is identified by its own pixels - a saturated teal
disc, the only one on that desktop - so the click lands on the installer whichever row it is
drawn in.
"""

from __future__ import annotations

import time

# The two rows the installer's icon has been seen at; the search covers both.
INSTALL_ICON_ROWS = (240, 340)

# The MATE double-click interval, measured: 80 ms opens gbi and 500 ms does not. Two presses
# further apart than this are read as two single clicks, and a single click only selects.
DOUBLE_CLICK_GAP = 0.08

# The left column the icons are drawn in. Wider than the glyph so the scan cannot be fooled by
# something on the wallpaper, narrower than the first pane's window.
COLUMN = (30, 160)


def install_icon(driver, step) -> bool:
    """Find `Install GhostBSD` by its teal disc, double-click it, and report what happened."""
    ppm = driver.screen.grab("install-icon")
    centre = None
    if ppm:
        try:
            from PIL import Image
            with Image.open(ppm) as image:
                rgb = image.convert("RGB")
                px = rgb.load()
                rows = []
                for y in range(40, 420):
                    run = best = 0
                    for x in range(*COLUMN):
                        r, g, b = px[x, y]
                        # the teal of the disc, and nothing else on this desktop
                        if r < 110 and g > 95 and b > 95 and g > r + 30 and b > r + 25:
                            run += 1
                            best = max(best, run)
                        else:
                            run = 0
                    if best >= 10:
                        rows.append(y)
        except Exception as exc:                       # noqa: BLE001
            driver.log(f"  the installer icon could not be scanned: {exc}")
            rows = []
        if rows:
            # one band per icon: group the matching rows that are within a few pixels
            bands, cur = [], [rows[0]]
            for y in rows[1:]:
                if y - cur[-1] <= 8:
                    cur.append(y)
                else:
                    bands.append((cur[0], cur[-1]))
                    cur = [y]
            bands.append((cur[0], cur[-1]))
            top = min(bands, key=lambda band: band[1] - band[0])
            centre = (88, (top[0] + top[1]) // 2)
            driver.log(f"  the installer icon is at {centre} - found by its teal disc, "
                       f"bands {bands}")

    if centre is None:
        # NOTHING MATCHED. Fall back to the first measured row, so this behaves exactly as the
        # old fixed constant did rather than pressing nothing at all.
        driver.log("  the installer icon was not found by its colour; using the measured row")
        centre = (88, INSTALL_ICON_ROWS[0])

    x, y = centre
    driver.log(f"double-click {centre} - Install GhostBSD")
    import qmp as Q
    w, h = driver.resolution()
    Q._send(driver.q, "press", [
        {"type": "abs", "data": {"axis": "x", "value": Q.to_abs(x, w)}},
        {"type": "abs", "data": {"axis": "y", "value": Q.to_abs(y, h)}},
        {"type": "btn", "data": {"down": True, "button": "left"}},
    ])
    time.sleep(DOUBLE_CLICK_GAP)
    Q._send(driver.q, "release", [{"type": "btn", "data": {"down": False, "button": "left"}}])
    time.sleep(DOUBLE_CLICK_GAP)
    Q._send(driver.q, "press 2", [{"type": "btn", "data": {"down": True, "button": "left"}}])
    time.sleep(DOUBLE_CLICK_GAP)
    Q._send(driver.q, "release 2", [{"type": "btn", "data": {"down": False, "button": "left"}}])
    time.sleep(float(step.get("settle", 25.0)))
    driver.look("gbi welcome")
    return True
