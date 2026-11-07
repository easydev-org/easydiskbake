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
import subprocess
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

# THE MENU BAR, AND WHY IT IS THE ONE TEST THAT SURVIVES EVERY WALLPAPER.
#
# MIN_VARIANCE_RATIO is measured on macOS 15 and it is BACKWARDS there. From run
# 41881240543's own recording, one frame every six seconds:
#
#     the wizard        drop -0.3   ratio 0.40   sat  0.00   top 228.5  bottom 228.8
#     the desktop       drop 27.3   ratio 1.56   sat  0.01   top 230.9  bottom 201.9
#
# and across all 349 frames: every frame that passed the brightness drop had a variance
# ratio BELOW 1.58, and every frame above 2.0 failed it. The test was written for a Windows
# desktop with a dark taskbar and a busy background; a white macOS setup sheet over a white
# desktop has the variance the other way round. Requiring ratio >= 2.0 on that machine is a
# test that can only be passed by the screen it is supposed to reject.
#
# What every macOS desktop has and no setup pane has is a menu bar: a dark, uniform strip
# across the very top, with the clock in it. Measured on the same frames:
#
#     the wizard         top 1.2% mean 227.3
#     the desktop        top 1.2% mean  39.7
#
# That is one band, one number, and a factor of nearly six - the same order of contrast the
# brightness drop was reaching for, on the region where it is actually reliable.
MENU_BAR = (0.000, 0.012)
MAX_MENU_BAR_MEAN = 120.0      # a dark strip; the lightest macOS menu bar is well under

# THE DOCK, WHICH IS WHAT THE macOS TEST ACTUALLY DECIDES ON NOW.
#
# The menu bar above is kept for is_macos_lock_screen, which uses its being ABSENT: a booted
# mac with nobody logged in draws wallpaper and a clock and no menu bar. It is no longer what
# decides "desktop", because on a pale wallpaper it measures 212 and reads as neither.
#
# The numbers are from _dock_stats' docstring: two desktops at std 42.4/42.6 and 16% coloured
# pixels, eight wizard panes at std 0.5 and 0%. The thresholds sit between with room on both
# sides - a Dock that has not finished animating in is still above 30, and a pane's flat
# footer is below 1.
DOCK_BAND = (0.885, 0.985)     # where the Dock sits on a 1920x1080 guest
DOCK_MIN_SPREAD = 20.0         # standard deviation of luminance across the band
DOCK_MIN_COLOURFUL = 0.03      # proportion of pixels whose channels differ by >40
DOCK_COLOUR_SPREAD = 40        # per-pixel "this is an icon, not grey UI" threshold
# AND THE SCREEN UNDER IT HAS TO BE LIT. A menu bar is dark and so is an unlit framebuffer,
# and the first version of this test passed every black frame in the recording for exactly
# that reason - 274 of 349, including the boot. The desktop's own body is bright (231 and 207
# in the bands above, mean 219) and a black screen is 0; the bracket separates them with an
# order of magnitude to spare on both sides.
MIN_DESKTOP_BRIGHTNESS = 120.0

# THE LOCK SCREEN, WHICH IS A BOOTED MAC WITH NOBODY LOGGED IN.
#
# desktop.py publishes a disk and never logs in, so the first boot of that disk stops here:
# wallpaper, clock, an account tile and a password field. It is not a failure and it is not a
# desktop - the wallpaper test says "not a desktop" correctly, because there is no menu bar,
# and a stage that only asks that question waits out its whole timeout on a healthy machine.
#
# Measured on the two frames this repository has:
#
#     lock screen    menu_bar 63.7   body  43.3
#     desktop        menu_bar 41.3   body 214.8
#     black screen   menu_bar  0     body   0
#
# So a lock screen is "the top strip is not dark enough to be a menu bar, AND the screen is
# not black". The second half matters: a booting guest with a dark logo reads menu_bar ~0 and
# body ~1, and must not be mistaken for a login window that wants a password typed into it.
LOCK_SCREEN_MIN_BODY = 15.0     # above the boot screen's ~1, below the lock screen's 43
LOCK_SCREEN_MAX_MENU = 200.0    # a menu bar is under 120; the wizard's white pane is 227


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

    # A JPEG IS CONVERTED FIRST, because lib/snapshots.py now writes those.
    #
    # The PPM is 3 MB per frame and an hour of a macOS install is 11 GiB of them, which filled
    # a runner and stopped macos26 mid-install. The frames are JPEG now, at 27 KB each - and
    # every measurement below wants RGB triples, not a particular container.
    #
    # Done here rather than at each caller because this is the single entry point: analyse(),
    # is_desktop(), newest_frame() and reached_desktop() all come through it, and a caller that
    # had to remember to convert would be a fourth place to get it wrong.
    if not data.startswith(b"P6"):
        import shutil
        import subprocess
        import tempfile

        if not shutil.which("ffmpeg"):
            return None
        with tempfile.NamedTemporaryFile(suffix=".ppm", delete=False) as tmp:
            ppm = tmp.name
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 "-i", path, "-pix_fmt", "rgb24", "-f", "image2", "-vcodec", "ppm", ppm],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                return None
            with open(ppm, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        finally:
            try:
                os.unlink(ppm)
            except OSError:
                pass

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
    menu_mean, _ = _band_stats(width, height, pixels, *MENU_BAR)

    return {
        "size": (width, height),
        "menu_bar_mean": sum(menu_mean) / 3.0,
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


def is_windows_desktop(path: str, *, explain: bool = False) -> bool:
    """Is this frame a Windows desktop?

    A bright top band over a dark, varied bottom band: the taskbar and the wallpaper. Three
    measurements, and each one was chosen because a single one passed something it should not
    have - see the constants above.

    THIS WAS HALF OF `is_desktop()`, WHICH ALSO ACCEPTED THE MACOS TEST. Keeping them apart
    matters in both directions:

        a macOS desktop must not be reported as a Windows one, because the Windows stages
        act on it - windows/11/ssh.py types a password when this returns True
        a Windows lock screen is bright and full-bleed and fails this test, which is correct
        and is why ssh.py has a separate "lit for sixty seconds" lock-page check below it
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
            f"sat={rise:+.2f} -> {'WINDOWS DESKTOP' if passed else 'not a windows desktop'}")
    return passed


def _dock_stats(width: int, height: int, pixels: bytes,
                step: int = 3) -> tuple[float, float, float]:
    """Brightness spread and colourfulness of the strip the Dock sits in.

    Returns (standard deviation of luminance, proportion of colourful pixels, mean).

    WHY THE DOCK AND NOT THE MENU BAR

    The menu bar was the test here for a long time and it does not survive a light wallpaper.
    Measured on the frame that made this function necessary - run 42294940588's own
    last-frame.png, which IS a desktop:

        menu_bar_mean = 212.0      threshold: <= 120.0

    Sequoia's default wallpaper is a pale gradient, the menu bar is translucent over it, and
    the strip reads 212 - a desktop failing a test whose comment says "the lightest macOS menu
    bar is well under 120". The same frame passes every visual check a person would apply:
    Finder's menu bar, the Dock, the wallpaper.

    WHAT ACTUALLY DIFFERS, measured across ten frames (two desktops, eight wizard panes):

        frame          dock std   dock range   coloured pixels
        CI desktop        42.4        245            15.8%
        local desktop     42.6        245            16.2%
        Terms              0.5          1             0.0%
        Transfer           0.5          1             0.0%
        Accessibility 26   0.5          1             0.0%
        Languages 26       0.5          1             0.0%
        Apple 26           0.5          1             0.0%
        FileVault 26       0.5          1             0.0%
        Age Range 26       0.5          1             0.0%
        Welcome (black)    5.4        105             0.0%

    Eight-five times apart in spread, and colourfulness is binary: a wizard's footer is one
    flat colour, and the Dock is ten coloured icons on a translucent bar. That is a much
    stronger separation than the menu bar ever had (212 vs 227, a factor of 1.07).

    THE BAND IS 88.5% TO 98.5% OF THE HEIGHT. On a 1080-high guest the Dock occupies roughly
    y=1000..1063; the band covers it and stops short of the very bottom edge, where the
    rounded corners of some panes draw a border.
    """
    y0 = int(height * DOCK_BAND[0])
    y1 = max(y0 + 1, int(height * DOCK_BAND[1]))
    x_step = max(1, width // 320)      # ~320 samples across, enough for a standard deviation

    values: list[float] = []
    colourful = 0
    for y in range(y0, y1, max(1, (y1 - y0) // 12 or 1)):
        base = y * width * 3
        for x in range(0, width, x_step):
            off = base + x * 3
            r, g, b = pixels[off], pixels[off + 1], pixels[off + 2]
            values.append((r + g + b) / 3.0)
            if max(r, g, b) - min(r, g, b) > DOCK_COLOUR_SPREAD:
                colourful += 1

    if not values:
        return 0.0, 0.0, 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return var ** 0.5, colourful / len(values), mean


# --- READING THE SCREEN, RATHER THAN MEASURING ITS COLOURS ---------------------------------
#
# WHY THE PIXEL TESTS WERE REPLACED
#
# Both macOS tests were fitted to one release's wallpaper and each broke on the other:
#
#     the menu-bar test   assumed a dark strip across the top. Sequoia's default wallpaper is
#                         a pale gradient and the menu bar is translucent over it, so run
#                         42294940588's own desktop frame measured 212 against a threshold of
#                         120 and was called "not a desktop" for thirty minutes.
#
#     the lock-screen test  assumed the login window has NO menu bar (it draws wallpaper
#                         there) and that the top strip is lighter than 120. On macOS 26 the
#                         login window's wallpaper is DARK - menu_bar 109 - while the desktop's
#                         menu bar is LIGHT - 216 - so the test answered backwards on both.
#
# A colour threshold is a fact about one wallpaper. The text on the screen is a fact about
# what the machine is doing, and it survives a wallpaper change, a theme change and a
# resolution change. So both decisions are made by reading now.
#
# THE TWO THINGS THE SCREEN SAYS, measured on thirteen frames across both releases:
#
#     the desktop        Finder's menu bar, which tesseract reads as the word "Finder".
#                        69 words on macOS 26's desktop, 62 on 15's - and never on anything
#                        else.
#
#     the login window   the password field's placeholder. NOT the whole frame: tesseract
#                        returns nothing at all for a login window (0 words, both releases)
#                        because the wallpaper behind the text defeats it. Cropping to the
#                        field and enlarging it is what makes the text readable.
#
#     frame              words    "Finder"    password field
#     macOS 26 login        0        no           yes
#     macOS 26 desktop     69        yes          no
#     macOS 26 wizard      28+       no           no
#     macOS 15 desktop     62        yes          no
#     macOS 15 wizard      28+       no           no
#     macOS 15 booting      0        no           no
#
# THE PASSWORD FIELD IS AT A FIXED PLACE because the login window is: the account tile sits
# at the bottom centre of a 1920x1080 screen and the field below it. That is a fact about
# macOS's login window layout, not about a wallpaper - the same kind of fact as the Setup
# Assistant's footer button, which the wizard drivers have always used.
PASSWORD_FIELD = (880, 960, 1045, 1000)     # x0, y0, x1, y1 at 1920x1080
PASSWORD_FIELD_SCALE = 6                     # the text is 12px tall; 6x is what tesseract reads

_OCR_CACHE: dict[str, list[str]] = {}


def ocr_words(path: str) -> list[str]:
    """Every word tesseract reads in this frame, lowercased.

    CACHED BY PATH, because reached_desktop() inspects the same five frames every five
    seconds and a tesseract run is about a second. The cache is bounded by the number of
    frames on disk in one run, which is a few hundred.

    THE FRAME MAY STILL BE BEING WRITTEN - lib/snapshots.py replaces a .ppm with a .jpg as it
    converts, and a half-written file makes tesseract return nothing, which would read as "no
    text here" for a frame that has plenty. A failure is therefore NOT cached.
    """
    if path in _OCR_CACHE:
        return _OCR_CACHE[path]
    try:
        proc = subprocess.run(["tesseract", path, "stdout", "--psm", "3"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    words = [w.lower() for w in proc.stdout.split()]
    if words:
        _OCR_CACHE[path] = words
    return words


def reads_password_field(path: str) -> bool:
    """Is a macOS login window's password field on screen?

    CROPPING AND ENLARGING IS THE WHOLE TRICK. On the full frame tesseract returns zero words:
    the wallpaper's texture is what it sees, and the placeholder inside a translucent pill is
    a few grey-on-grey pixels. Crop to the pill, enlarge it six times, invert or stretch the
    contrast, and the text appears.

    THE CURSOR SPLITS THE WORD, AND TESSERACT READS THE SPLIT AS A LETTER. Clicking the field
    puts an I-beam between "Pass" and "word", and the recognised text becomes

        Enter Pasdvord

    - measured on three preprocessings and four page-segmentation modes, every one of them
    agreeing. So the match allows for that: the fragments below cover the clean string, the
    cursor-split one, and the invariant halves.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return False
    if not os.path.exists(path):
        return False
    try:
        image = Image.open(path).convert("RGB")
        crop = image.crop(PASSWORD_FIELD)
        big = crop.resize((crop.width * PASSWORD_FIELD_SCALE,
                           crop.height * PASSWORD_FIELD_SCALE), Image.LANCZOS)
    except Exception:                                  # noqa: BLE001 - unreadable frame
        return False

    for prepare in (lambda im: ImageOps.invert(im.convert("L")),
                    lambda im: ImageOps.autocontrast(im.convert("L"))):
        tmp = path + ".pw.png"
        prepare(big).save(tmp)
        for psm in ("6", "8", "13"):
            try:
                proc = subprocess.run(["tesseract", tmp, "stdout", "--psm", psm],
                                      capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                continue
            text = "".join(proc.stdout.split()).lower()
            if "enterpassword" in text or "asdvord" in text:
                return True
            if "nter" in text and any(f in text for f in ("word", "vord", "sswd", "assw")):
                return True
    return False


def is_macos_desktop(path: str, *, explain: bool = False) -> bool:
    """Is this frame a macOS desktop? Decided by reading Finder's menu bar.

    A desktop draws "Finder" at the top left of its menu bar and nothing else in this pipeline
    does - not the Setup Assistant, not the login window, not the boot screen. That is a
    stronger statement than any colour measurement, because it is about what the machine is
    doing rather than about how its wallpaper reflects light.

    THE MENU BAR IS STILL CHECKED, AS A CHEAP GATE. A frame with no readable text anywhere
    cannot be a desktop, and skipping the image work for those is most of the savings.
    """
    words = ocr_words(path)
    passed = "finder" in words
    if explain:
        log(f"{os.path.basename(path)}: {len(words)} words, "
            f"finder={'yes' if 'finder' in words else 'no'} "
            f"-> {'MACOS DESKTOP' if passed else 'not a macos desktop'}")
    return passed


def is_macos_lock_screen(path: str, *, explain: bool = False) -> bool:
    """Is this frame the login window of a booted macOS?

    A LOCK SCREEN IS NOT A FAILURE, AND WAITING FOR ONE TO BECOME A DESKTOP NEVER WORKS.
    It is what desktop.py leaves behind - a finished system with nobody logged in - so the
    useful answer is not "not a desktop, keep waiting" but "type the password".

    READ THE PASSWORD FIELD. See reads_password_field for why the whole frame does not work:
    tesseract returns zero words for a login window on both releases, and the field's
    placeholder is the one piece of text that can be recovered.

    WHAT THIS REPLACED, AND WHY IT HAD TO GO

    It was `body >= 15 and menu > 120`: "the top strip is not a menu bar, and the screen is not
    black". Two things were wrong with that.

    The constant was never used. LOCK_SCREEN_MAX_MENU = 200 was defined and the expression
    tested MAX_MENU_BAR_MEAN = 120 instead, so the pane it was written to exclude - the
    wizard's white top strip at 227 - passed the test it was meant to fail.

    And the assumption is false on macOS 26. The login window's wallpaper is dark (menu_bar
    109) and the desktop's menu bar is light (216), so the comparison answered backwards:
    every login frame read "not a lock screen" and every desktop frame read "lock screen".
    """
    passed = reads_password_field(path)
    if explain:
        log(f"{os.path.basename(path)}: password field "
            f"{'present -> MACOS LOCK SCREEN' if passed else 'absent'}")
    return passed


def is_desktop(path: str, *, explain: bool = False) -> bool:
    """Is this frame a desktop of either kind?

    Kept for report and for callers that have no opinion - see is_windows_desktop() and
    is_macos_desktop() for the two tests and why they are separate.
    """
    if is_windows_desktop(path, explain=explain):
        return True
    return is_macos_desktop(path, explain=explain)


# THE DEFAULT FOR reached_desktop(), AND THE ONLY THING A CALLER NORMALLY SETS.
#
#   "macos"    the menu-bar test - macos/15/desktop.py and macos/15/ssh.py
#   "windows"  the taskbar test - windows/11/bake.py and windows/11/ssh.py
#   "any"      either; for a report, never for a decision
KINDS = {"windows": is_windows_desktop, "macos": is_macos_desktop, "any": is_desktop}


def newest_frame(directory: str) -> str | None:
    """The highest-numbered frame, or None. Frames are named frame-0000.ppm, ..."""
    try:
        names = sorted(
            f for f in os.listdir(directory)
            if f.startswith("frame-") and f.endswith((".ppm", ".jpg"))
        )
    except OSError:
        return None
    return os.path.join(directory, names[-1]) if names else None


def reached_lock_screen(directory: str, *, explain: bool = False) -> bool:
    """Has the newest frame become a macOS login window?

    ONE FRAME, NOT FIVE, AND THAT IS THE OPPOSITE OF reached_desktop. A desktop is a steady
    state, so looking at the last few rides out a frame caught mid-repaint. A login window is
    a moment to act on: by the time five frames have been checked and a password typed, the
    caller has spent five seconds of a real person's absence, and on a slow guest the window
    may already have dimmed again.

    OCR IS EXPENSIVE HERE and that is acceptable: this is called once per five seconds, and
    it stops being called at all once the password has been typed.
    """
    newest = newest_frame(directory)
    if newest is None:
        return False
    passed = is_macos_lock_screen(newest, explain=explain)
    if explain:
        log(f"{os.path.basename(newest)}: "
            f"{'MACOS LOCK SCREEN' if passed else 'not a lock screen'}")
    return passed


def reached_desktop(directory: str, *, kind: str = "any",
                    explain: bool = False) -> bool:
    """Has any of the last few frames looked like a desktop of `kind`?

    Looking at more than the newest matters: the screen is captured every few seconds
    and the desktop is stable, but the newest frame can be one taken mid-repaint or
    while the screen saver has blanked it.

    `kind` IS REQUIRED IN PRACTICE, even though it defaults to "any". A caller that knows
    which system it is booting should say so: the two tests are not equally good at
    rejecting the other system's screens, and a stage that guesses can wait out its whole
    timeout on a frame the wrong test happened to like. See KINDS.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}, not {kind!r}")
    try:
        names = sorted(
            f for f in os.listdir(directory)
            if f.startswith("frame-") and f.endswith((".ppm", ".jpg"))
        )[-5:]
    except OSError:
        return False

    test = KINDS[kind]
    for name in reversed(names):
        if test(os.path.join(directory, name), explain=explain):
            return True
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: desktop.py <frame.ppm> [...]")
    ok = True
    for target in sys.argv[1:]:
        ok = is_desktop(target, explain=True) and ok
    raise SystemExit(0 if ok else 1)
