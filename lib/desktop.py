"""Decide whether a Windows guest has reached its desktop, from a screenshot.

    from desktop import is_desktop
    is_desktop("/path/to/frame-0042.ppm")

WHY THIS REPLACES THE RDP CHECK

The arrival check used to be "is port 3389 answering RDP". It has two problems that no
amount of waiting fixes:

    it needs the guest's network stack, and the guest's NIC is virtio-net, which needs
    NetKVM in the *installed* system - not just in windowsPE. Nothing installs it there,
    so RDP may never come up no matter how well the install went.

    it needs OOBE to be finished *and* the RDP listener started. Two conditions behind
    the one it claims to test.

A desktop is visible the moment OOBE finishes, and the screen is already being
captured for the recording. So the check costs nothing extra and depends on nothing
that can be misconfigured.

WHY NOT "THE COLOURS LOOK LIKE THIS PICTURE"

That was the first idea and it does not survive contact:

    the reference desktop is a gradient, so "close enough" has no single threshold
    the Windows PE installer is also blue, and a blue-matched check cannot tell it
    from a desktop
    resolution and scaling move the values

What does survive is STRUCTURE. A Windows 11 desktop has a light upper half, a dark
lower half with a taskbar and icons, and therefore:

    a brightness gradient, top to bottom
    much more pixel variance at the bottom than at the top
    a saturation that rises with the darkness

Measured on a real desktop at 1280x750:

    top half   avg (143,183,212)  sat 0.33  var  1409
    bottom     avg ( 67,116,195)  sat 0.66  var 13812

    brightness falls 53, variance rises 9.8x, saturation rises 0.33

A Windows PE installer is a large area of near-uniform blue: the gradient is absent and
the variance ratio is near 1. A black screen has variance near zero at both ends. Both
fail all three tests.

THE THRESHOLDS ARE DELIBERATELY LOOSE

This decides when to stop waiting after an install that takes half an hour. A false
positive ships a disk that does not reach a desktop - expensive. A false negative
throws away a working one - just as expensive. The numbers below accept any desktop
with a light top and a busy dark bottom, including ones with a custom wallpaper,
because the test is on the gradient's *shape* rather than its colours.

A wallpaper that is dark at the top and light at the bottom would fail. That has not
been observed, and if it happens the fix is to look at the taskbar row specifically
rather than to loosen these.
"""

from __future__ import annotations

import os
import sys

# LIGHT TOP, DARK BUSY BOTTOM.
#
# Measured on a real Windows 11 desktop at 1280x800:
#
#     top half   avg (143,183,212)  sat 0.33  var  1409
#     bottom     avg ( 67,116,195)  sat 0.66  var 13812
#
#     brightness falls 53, variance rises 9.8x, saturation rises 0.33
#
# The thresholds ask for roughly a third of that, so a different wallpaper with the
# same shape still passes.
#
# A TASKBAR-BASED CHECK WAS TRIED AND IS NOT HERE. It looked for a uniform strip at the
# bottom with icons along its edge, which is true of some desktops and false of the two
# reference screenshots in this repository: both have pure wallpaper all the way down
# (variance 4000-5600 at every row below 84%, no uniform strip anywhere). A check that
# rejects the pictures it was built from is not a check.
#
# WHAT THIS DEPENDS ON: the guest must be at its desktop, not its Start menu. A run that
# left six Return presses queued opened the Start menu the moment the desktop appeared,
# and the resulting screen failed all three tests - correctly, because it was not the
# desktop any more. The fix for that is the key press (lib/bootkey.py now sends three
# ESCs, which cannot open a menu), not a wider judgement here.
TOP_BAND = (0.00, 0.20)
BOTTOM_BAND = (0.80, 1.00)

MIN_BRIGHTNESS_DROP = 15.0     # top mean minus bottom mean, per channel average
MIN_VARIANCE_RATIO = 2.0       # bottom variance / top variance
MIN_SATURATION_RISE = 0.10     # bottom saturation - top saturation


def log(msg: str) -> None:
    print(f"[desktop] {msg}", flush=True)


def read_ppm(path: str) -> tuple[int, int, bytes] | None:
    """Parse a binary PPM (P6), which is what qemu's screendump writes.

    Returns (width, height, pixels) or None. None rather than an exception because the
    frames land on disk asynchronously: the newest one can be half-written when this
    runs, and that is not a failure of the guest.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None

    if not data.startswith(b"P6"):
        return None

    fields: list[int] = []
    i = 2
    while len(fields) < 3 and i < len(data):
        # Skip whitespace and comments.
        while i < len(data) and data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            while i < len(data) and data[i:i + 1] != b"\n":
                i += 1
            continue
        start = i
        while i < len(data) and not data[i:i + 1].isspace():
            i += 1
        try:
            fields.append(int(data[start:i]))
        except ValueError:
            return None
    i += 1

    if len(fields) != 3:
        return None
    width, height, maxval = fields
    if maxval != 255:
        return None

    pixels = data[i:]
    if len(pixels) < width * height * 3:
        return None   # truncated: a frame mid-write

    return width, height, pixels


def _band_stats(width: int, height: int, pixels: bytes,
                top: float, bottom: float, step: int = 4) -> tuple[tuple[float, float, float], float]:
    """Mean colour and variance over a horizontal band.

    `step` subsamples horizontally: a 1280-wide band is 1280 pixels per row and this
    runs on every frame, so 4 is a 16x saving and changes none of these statistics
    meaningfully.
    """
    y0 = int(height * top)
    y1 = max(y0 + 1, int(height * bottom))

    total = 0
    sums = [0, 0, 0]
    for y in range(y0, y1):
        base = y * width * 3
        for x in range(0, width, step):
            off = base + x * 3
            sums[0] += pixels[off]
            sums[1] += pixels[off + 1]
            sums[2] += pixels[off + 2]
            total += 1

    if total == 0:
        return (0.0, 0.0, 0.0), 0.0

    mean = (sums[0] / total, sums[1] / total, sums[2] / total)

    var = 0.0
    for y in range(y0, y1):
        base = y * width * 3
        for x in range(0, width, step):
            off = base + x * 3
            var += (pixels[off] - mean[0]) ** 2
            var += (pixels[off + 1] - mean[1]) ** 2
            var += (pixels[off + 2] - mean[2]) ** 2
    return mean, var / (total * 3)


def saturation(colour: tuple[float, float, float]) -> float:
    """HSV saturation: 0 for grey, 1 for a pure hue."""
    mx = max(colour)
    if mx <= 0:
        return 0.0
    return (mx - min(colour)) / mx


def _band_stats(width: int, height: int, pixels: bytes,
                top: float, bottom: float, step: int = 4) -> tuple[tuple[float, float, float], float]:
    """Mean colour and variance over a horizontal band.

    `step` subsamples horizontally: a 1280-wide band is 1280 pixels per row and this
    runs on every frame, so 4 is a 16x saving and changes none of these statistics
    meaningfully.
    """
    y0 = int(height * top)
    y1 = max(y0 + 1, int(height * bottom))

    total = 0
    sums = [0, 0, 0]
    for y in range(y0, y1):
        base = y * width * 3
        for x in range(0, width, step):
            off = base + x * 3
            sums[0] += pixels[off]
            sums[1] += pixels[off + 1]
            sums[2] += pixels[off + 2]
            total += 1

    if total == 0:
        return (0.0, 0.0, 0.0), 0.0

    mean = (sums[0] / total, sums[1] / total, sums[2] / total)

    var = 0.0
    for y in range(y0, y1):
        base = y * width * 3
        for x in range(0, width, step):
            off = base + x * 3
            var += (pixels[off] - mean[0]) ** 2
            var += (pixels[off + 1] - mean[1]) ** 2
            var += (pixels[off + 2] - mean[2]) ** 2
    return mean, var / (total * 3)


def analyse(path: str) -> dict | None:
    """The numbers is_desktop() decides on. Useful in a log, and testable on its own."""
    parsed = read_ppm(path)
    if parsed is None:
        return None
    width, height, pixels = parsed

    top_mean, top_var = _band_stats(width, height, pixels, *TOP_BAND)
    bot_mean, bot_var = _band_stats(width, height, pixels, *BOTTOM_BAND)

    return {
        "size": (width, height),
        "top_mean": top_mean,
        "bottom_mean": bot_mean,
        "top_var": top_var,
        "bottom_var": bot_var,
        "top_sat": saturation(top_mean),
        "bottom_sat": saturation(bot_mean),
        "brightness_drop": (sum(top_mean) - sum(bot_mean)) / 3.0,
        # A flat band has variance 0 and the ratio is infinite, which is a correct
        # answer and a useless log line. Both are floored at 1.0, so the ratio reads as
        # a large finite number and no caller has to handle inf.
        "variance_ratio": (bot_var / max(top_var, 1.0)),
        "saturation_rise": saturation(bot_mean) - saturation(top_mean),
    }


def is_desktop(path: str, *, explain: bool = False) -> bool:
    """Is this frame a Windows desktop?

    All three tests must pass. See the constants above for why each one is there and
    why the thresholds are loose.
    """
    stats = analyse(path)
    if stats is None:
        if explain:
            log(f"{os.path.basename(path)}: unreadable or still being written")
        return False

    drop = stats["brightness_drop"]
    ratio = stats["variance_ratio"]
    rise = stats["saturation_rise"]

    passed = (
        drop >= MIN_BRIGHTNESS_DROP
        and ratio >= MIN_VARIANCE_RATIO
        and rise >= MIN_SATURATION_RISE
    )

    if explain:
        log(f"{os.path.basename(path)}: drop={drop:+.1f} ratio={ratio:.1f}x "
            f"sat={rise:+.2f} -> {'DESKTOP' if passed else 'not a desktop'}")
    return passed


def newest_frame(directory: str) -> str | None:
    """The highest-numbered frame, or None. Frames are named frame-0000.ppm, ..."""
    try:
        names = sorted(
            f for f in os.listdir(directory)
            if f.startswith("frame-") and f.endswith(".ppm")
        )
    except OSError:
        return None
    return os.path.join(directory, names[-1]) if names else None


def reached_desktop(directory: str, *, explain: bool = False) -> bool:
    """Has any of the last few frames looked like a desktop?

    Looking at more than the newest matters: the screen is captured every few seconds
    and the desktop is stable, but the newest frame can be one taken mid-repaint or
    while the screen saver has blanked it.
    """
    try:
        names = sorted(
            f for f in os.listdir(directory)
            if f.startswith("frame-") and f.endswith(".ppm")
        )[-5:]
    except OSError:
        return False

    for name in reversed(names):
        if is_desktop(os.path.join(directory, name), explain=explain):
            return True
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: desktop.py <frame.ppm> [...]")
    ok = True
    for target in sys.argv[1:]:
        ok = is_desktop(target, explain=True) and ok
    raise SystemExit(0 if ok else 1)
