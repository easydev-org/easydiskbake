"""Decide whether a macOS guest has reached the Setup Assistant, by reading its title.

    from assistant import is_assistant
    is_assistant("/path/to/frame-0007.ppm")

WHY OCR HERE, AFTER ARGUING AGAINST IT

The first screen of the wizard says "Select Your Country or Region", and that string
appears nowhere else in the macOS installer or on the desktop. A pixel test can tell a
light page from a dark one - measured, the wizard is 229 mean grey and every installer
pane is 23 to 28 - but it cannot tell the wizard from any other light page, and the point
of this check is to be a CHECK: a stage that fails should say why, and "the wizard is on
screen" is a stronger claim than "the screen is white".

So this reads. tesseract is already in the bake image (the Windows stage has used it for
the winget prompt since the first bake), and lib/ocr.py already settles the arguments:
--psm 3 rather than --psm 11, because sparse-text mode returns nothing on a frame that
demonstrably contains a list of country names.

WHY A PHRASE AND NOT ONE WORD

"Region" alone appears in other UI. "Select Your Country" is a phrase only the wizard's
first page carries, and tesseract returns it as separate words, so the match is on the
words in order rather than on the line as a whole - a title that wrapped or was recognised
with a stray space still matches.

A SECOND PHRASE IS ACCEPTED, because the wizard's pages after the first do not all say
this. "How Do You Want to Connect to the Internet?" and "Transfer Your Information to
This Mac" are both wizard-only and both mean the same thing: the installation finished and
the machine booted into it. The bake's question is "has the wizard started", not "which
page is it on".

WHAT A FAILURE LOOKS LIKE

The installer's own panes read as licence text, disk names and button labels - none of
which contain these phrases. A blank or half-written frame reads as nothing at all, and
that is a "no" rather than an error, because the caller is polling.
"""

from __future__ import annotations

import os
import shutil
import subprocess

# THE PHRASES THAT MEAN "THE WIZARD IS ON SCREEN", in lower case and without punctuation.
#
# Each one was read off a frame of this pipeline. The first is the page run 41606493103
# ended on.
WIZARD_PHRASES = (
    "select your country or region",
    "how do you want to connect to the internet",
    "transfer your information to this mac",
    "choose a wireless network",
    "sign in with your apple id",
)


def log(message: str) -> None:
    print(f"[assistant] {message}", flush=True)


def ocr_text(ppm: str) -> str:
    """Every word tesseract can read out of a frame, as one lower-case line.

    THE SAME ARGUMENTS lib/ocr.py USES, and for the same reason: --psm 3 is the automatic
    page segmentation, and --psm 11 (sparse text) returned nothing at all on the very
    frames this has to read.
    """
    if not ppm or not os.path.isfile(ppm):
        return ""
    if not shutil.which("tesseract"):
        log("tesseract is not installed, so the wizard cannot be recognised")
        return ""
    try:
        result = subprocess.run(
            ["tesseract", ppm, "stdout", "--psm", "3"],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"tesseract failed: {exc}")
        return ""
    return " ".join(
        " ".join(line.split()).lower()
        for line in (result.stdout or "").splitlines()
        if line.strip()
    )


def ocr_text_cropped(ppm: str, box: tuple[int, int, int, int],
                     scale: int = 3) -> str:
    """OCR one rectangle of a frame, enlarged. Same contract as ocr_text().

    WHY A CROP IS NECESSARY. Reading a whole 1920x1080 screendump gives the large labels
    and loses the small ones: Disk Utility's detail pane prints "Capacity: 268.4 MB" at
    about ten pixels tall, and tesseract's page segmentation treats it as part of the
    surrounding furniture. Enlarging the pane and reading only it makes the numbers
    legible, which is the difference between choosing the right disk and erasing the
    wrong one.

    NEEDS Pillow. It is in the bake image (lib/lastframe.py already imports it for the
    PPM-to-PNG conversion), and a missing import here returns "" rather than raising -
    this is diagnostic reading, not the thing that decides whether a run continues.
    """
    if not ppm or not os.path.isfile(ppm):
        return ""
    if not shutil.which("tesseract"):
        log("tesseract is not installed, so this cannot be read")
        return ""
    try:
        from PIL import Image
    except ImportError:
        # NO PILLOW, SO THE CROP IS DONE BY ffmpeg INSTEAD.
        #
        # The image has ffmpeg for the recordings anyway, and its crop filter takes the
        # same rectangle. This matters because a missing Pillow must not turn into
        # "cannot read the capacity" - run 41691829177 lost a bake to exactly that, and the
        # correct refusal it produced (do not erase a disk you cannot identify) still cost
        # a run that could have proceeded.
        log("Pillow is not installed; cropping with ffmpeg instead")
        return _ocr_cropped_ffmpeg(ppm, box, scale)

    try:
        with Image.open(ppm) as image:
            width, height = image.size
            left, top, right, bottom = box
            # Clamp rather than refuse: a caller passing a rect for a larger screen than
            # the guest actually has should get the part that exists, not nothing.
            left = max(0, min(left, width))
            right = max(0, min(right, width))
            top = max(0, min(top, height))
            bottom = max(0, min(bottom, height))
            if right <= left or bottom <= top:
                return ""
            crop = image.crop((left, top, right, bottom))
            crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
            # THE CROP GOES TO A FILE, because tesseract reads images from paths - it does
            # not decode a stream of bytes on stdin or it would need the format spelled
            # out. Temporary and removed immediately; the caller's frame is untouched.
            import tempfile
            handle, temp = tempfile.mkstemp(suffix=".png")
            os.close(handle)
            try:
                crop.save(temp)
                # PSM 6 IS "ASSUME A SINGLE UNIFORM BLOCK OF TEXT", which is what a detail
                # pane is. PSM 3 (used for whole pages) split this pane into columns and
                # joined them in the wrong order.
                result = subprocess.run(
                    ["tesseract", temp, "stdout", "--psm", "6"],
                    capture_output=True, text=True,
                )
            finally:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log(f"the cropped read failed: {exc}")
        return ""

    text = result.stdout or ""
    return " ".join(
        " ".join(line.split()).lower()
        for line in text.splitlines()
        if line.strip()
    )


def _ocr_cropped_ffmpeg(ppm: str, box: tuple[int, int, int, int],
                        scale: int) -> str:
    """ocr_text_cropped() without Pillow: ffmpeg crops and scales, tesseract reads.

    The rectangle is in source pixels, and the scale is applied by the same filter that
    crops, so the result is the same picture Pillow would have produced.
    """
    import tempfile

    left, top, right, bottom = box
    width = max(1, (right - left) * scale)
    height = max(1, (bottom - top) * scale)
    # AT LEAST 4x, BECAUSE THE DECIMAL POINT IS THE THING THAT GETS LOST.
    #
    # Measured on a real frame: at 3x this reads "2684 MB" and at 4x and above it reads
    # "268.4 MB". A dropped decimal turns a 268 MB disk into a 2.6 GB one, which is still
    # refused by the 60..200 GB window - but only by luck, and the same loss on a real
    # target would read 6872 GB and be refused too. The number has to be right, not merely
    # on the safe side of a comparison.
    if scale < 4:
        scale = 4
    width = max(1, (right - left) * scale)
    height = max(1, (bottom - top) * scale)
    handle, temp = tempfile.mkstemp(suffix=".png")
    os.close(handle)
    try:
        crop = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", ppm,
             "-vf", f"crop={right - left}:{bottom - top}:{left}:{top},"
                    f"scale={width}:{height}:flags=lanczos",
             temp],
            capture_output=True, text=True,
        )
        if crop.returncode != 0:
            log(f"ffmpeg could not crop: {(crop.stderr or '').strip()[:160]}")
            return ""
        result = subprocess.run(
            ["tesseract", temp, "stdout", "--psm", "6"],
            capture_output=True, text=True,
        )
        text = result.stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"the ffmpeg crop failed: {exc}")
        return ""
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass

    return " ".join(
        " ".join(line.split()).lower()
        for line in text.splitlines()
        if line.strip()
    )


def normalise(text: str) -> str:
    """Lower case, punctuation to spaces, runs of spaces collapsed.

    OCR inserts and drops punctuation unpredictably - a colon becomes a semicolon, a
    question mark vanishes - and comparing on letters and spaces alone survives that
    without loosening the phrases themselves.
    """
    out = []
    for ch in text.lower():
        out.append(ch if ch.isalnum() else " ")
    return " ".join("".join(out).split())


def is_assistant(path: str, *, explain: bool = False) -> bool:
    """Does this frame carry a phrase only the Setup Assistant shows?"""
    text = normalise(ocr_text(path))
    if not text:
        if explain:
            log(f"{os.path.basename(path)}: nothing readable")
        return False
    for phrase in WIZARD_PHRASES:
        if phrase in text:
            if explain:
                log(f"{os.path.basename(path)}: read {phrase!r} -> SETUP ASSISTANT")
            return True
    if explain:
        log(f"{os.path.basename(path)}: no wizard phrase in {text[:160]!r}")
    return False


def reached_assistant(directory: str, *, explain: bool = False) -> bool:
    """Has any of the last few frames carried a wizard phrase?

    MORE THAN THE NEWEST, for the same reason lib/desktop.py looks at five: the wizard
    animates, and a frame taken mid-transition can read as nothing at all.
    """
    try:
        names = sorted(
            f for f in os.listdir(directory)
            if f.startswith("frame-") and f.endswith((".ppm", ".jpg"))
        )[-5:]
    except OSError:
        return False

    for name in reversed(names):
        if is_assistant(os.path.join(directory, name), explain=explain):
            return True
    return False


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("usage: assistant.py <frame.ppm> [...]")
    ok = True
    for target in sys.argv[1:]:
        ok = is_assistant(target, explain=True) and ok
    raise SystemExit(0 if ok else 1)
