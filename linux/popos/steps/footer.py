"""Pop!_OS' footer button, found by its FILL because the window resizes on every pane.

WHY THIS IS A LOOKUP, WHEN EVERY OTHER DRIVER IN THIS REPOSITORY FORBIDS ONE

The rule is that the screen is read to decide WHETHER a step worked, never WHERE to click,
because a lookup can be talked into clicking a word in a paragraph - kubuntu lost eighty-seven
minutes to exactly that. This window is the exception, and it is measured, not assumed:

    the card SHRINKS AND GROWS with each pane. The footer's green button has been measured at

        Select a Language    y=774..807   centre 790
        Keyboard Layout      y=794..827   centre 810
        Install              y=794..827   centre 810
        Select a drive       y=798..817   centre 798  (a grey `Erase and Install`)
        Create User Account  y=794..827   centre 810

A single constant is wrong on most of them, and the failure is NOT silent - it is worse than
silent: a press ten pixels below the button lands on the wallpaper, nothing advances, and the
next step's clicks were aimed at a pane that never appeared. Four runs were lost that way
(42525650794, 42526614298, 42529279468, 42531166731), each ending with the disk at 0 MiB.

SO THE BUTTON IS FOUND BY ITS PIXELS. That is safe here for the reason the capacity check in
macos/15/steps/setup.yaml is safe: the instruction is not "go to this position", it is "press
the button in this pane's footer" - and unlike prose, the footer button is the only rounded
fill of its kind in the bottom right of the card. Its colour says which button it is: green
for the forward control (Select / Next / Erase and Install), grey while it is disabled.

THE LABEL IS NOT USED. tesseract reads these 14-px labels unreliably - measured, `Select` in
this footer came back as nothing at all on two panes - while the fill is 84 x 34 px of a single
colour. The colour is the more robust signal, and it is the one the installer itself uses.
"""

from __future__ import annotations

import time

# THE FOOTER STRIP OF THE CARD. Every pane measured has its button inside this box, and no
# other control of this size is drawn there.
FOOTER_BOX = (1240, 755, 1430, 845)

# The two fills the installer draws. ENABLED is a green (about 144,206,176); DISABLED is a
# near-neutral grey (about 137,137,137). The background is (48,48,48).
def _is_button(r: int, g: int, b: int) -> bool:
    """Whether this pixel is a footer button's FILL.

    THREE FILLS HAVE BEEN MEASURED, and the third is the one that cost this driver:

        green   (144,206,176)   Select / Next, enabled
        grey    (137,137,137)   the same controls while disabled
        salmon  (231,143,143)   `Erase and Install`, which is DESTRUCTIVE and is drawn in the
                                warning colour rather than the confirm colour

    The first version of this test accepted only green and grey, so the moment the drive card
    was selected - the only thing that makes `Erase and Install` available - the locator
    reported `NO FOOTER BUTTON ON THIS PANE` on a pane whose button was plainly on screen in
    salmon. The test is therefore SATURATION, not hue: a fill is a colour far enough from grey,
    or a light grey, and the card behind it is (48,48,48).
    """
    if r > 200 and abs(r - g) < 12 and abs(r - b) < 12:
        return False                      # white label text, not the fill
    spread = max(r, g, b) - min(r, g, b)
    if spread > 50 and max(r, g, b) > 120:
        return True                       # green, salmon, any saturated fill
    return 110 < r < 185 and abs(r - g) < 10 and abs(r - b) < 10   # the disabled grey


def _label_centre(px, x0: int, y0: int, x1: int, y1: int):
    """The centre of the footer's LABEL, for a button drawn with no fill.

    The label is the only thing in the footer box that is lighter than the card: COSMIC's
    `Next` is (50,50,50) on (48,48,48) with the text at (135,135,135). Counting those pixels
    and taking their bounding box gives the button's own centre, because a label is centred in
    its button.

    NOTHING IS RETURNED WHEN THE BOX IS EMPTY OR THE LABEL IS TOO SMALL TO BE ONE - a single
    anti-aliasing pixel is not a button, and pressing it would press wallpaper.
    """
    pts = []
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b = px[x, y]
            if 100 < r < 200 and abs(r - g) < 12 and abs(r - b) < 12:
                pts.append((x, y))
    if len(pts) < 30:                     # a `Next` label is ~89 px at this size
        return None
    xs = sorted(p[0] for p in pts)
    ys = sorted(p[1] for p in pts)
    return ((xs[0] + xs[-1]) // 2, (ys[0] + ys[-1]) // 2)


def footer_button(driver) -> tuple | None:
    """The centre of the footer button, or None when this pane does not draw one."""
    ppm = driver.screen.grab(f"{driver.n:03d}-footer")
    driver.n += 1
    if not ppm:
        return None
    try:
        from PIL import Image
    except ImportError:
        driver.log("  Pillow is missing; the footer cannot be located")
        return None
    try:
        with Image.open(ppm) as image:
            px = image.convert("RGB").load()
    except OSError:
        return None

    x0, y0, x1, y1 = FOOTER_BOX
    # COUNT PER COLOUR FIRST. A button is one fill covering most of an 84x34 rectangle, so the
    # dominant colour's own extent IS the button - a raw bounding box of "any button-ish pixel"
    # also catches the wallpaper and the title bar, which is what made the first version report
    # a 190x63 box for an 84x34 button.
    from collections import Counter
    tally = Counter()
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b = px[x, y]
            if _is_button(r, g, b):
                tally[(r // 8 * 8, g // 8 * 8, b // 8 * 8)] += 1
    if not tally:
        # A BUTTON WITH NO FILL IS STILL A BUTTON, AND THE LABEL IS WHERE IT IS.
        #
        # COSMIC draws this pane's `Next` as the CARD'S OWN FILL (50,50,50 against a 48,48,48
        # background) with the label in grey - so there is no fill to find and `tally` is
        # empty. Measured on run e242215's frame: the whole footer box is 46..51 grey except
        # the label's 89 pixels at x 1354..1380, y 806..815.
        #
        # THE LABEL IS INSIDE THE BUTTON, so its centre is the button's centre. Falling back to
        # it is what makes a flat-styled footer pressable; without it the step reports
        # `NO FOOTER BUTTON ON THIS PANE` and returns False on a pane whose button is on screen.
        label = _label_centre(px, x0, y0, x1, y1)
        if label is not None:
            cx, cy = label
            driver.log(f"  no fill in the footer; pressing the label's centre ({cx},{cy})")
            return (cx, cy, cx - 40, cx + 40, cy - 12, cy + 12)
        return None
    colour, count = tally.most_common(1)[0]
    # A DISABLED BUTTON IS DRAWN AT THE CARD'S OWN FILL (about 50,50,50), so it is not a
    # distinct colour at all - it is the presence of a BORDER and the label that mark it. That
    # case is not handled here on purpose: a footer that is disabled cannot be pressed, and the
    # step before this one must select whatever enables it. Reporting None makes that visible
    # in the log instead of pressing wallpaper.
    if count < 600:                       # an 84x34 fill is ~2800 px; 600 is well below
        return None
    pts = [(x, y) for y in range(y0, y1) for x in range(x0, x1)
           if _is_button(*px[x, y])
           and (px[x, y][0] // 8 * 8, px[x, y][1] // 8 * 8, px[x, y][2] // 8 * 8) == colour]
    xs = sorted(x for x, _ in pts)
    ys = sorted(y for _, y in pts)
    return ((xs[0] + xs[-1]) // 2, (ys[0] + ys[-1]) // 2, xs[0], xs[-1], ys[0], ys[-1])


def press_footer(driver, step) -> bool:
    """Press this pane's footer button, wherever the pane has drawn it."""
    found = footer_button(driver)
    if found is None:
        driver.log("=== NO FOOTER BUTTON ON THIS PANE ===")
        return False
    cx, cy, left, right, top, bottom = found
    driver.log(f"  the footer button spans x={left}..{right} y={top}..{bottom}; "
               f"pressing ({cx},{cy})")
    driver.qmpmod.click(driver.q, cx, cy)
    time.sleep(float(step.get("settle", 3.0)))
    driver.look("after the footer")
    return True
