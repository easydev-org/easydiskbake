"""The last frame of a run, as one small picture.

    python3 lib/lastframe.py out/frames/shots out/last-frame.png

WHY THIS EXISTS

Run 41468749665 was cancelled before its recording was encoded, so the fallback upload
sent 494 MB of raw PPM: a hundred frames at 6 MiB each. Whoever wants to know which pane
the driver stopped on needs exactly one of them, and it took six minutes to download the
pile to look at it.

WHAT IT DOES

Finds the highest-numbered frame-*.ppm, converts that one with ffmpeg, and writes a PNG.
200-400 KiB for a 1920x1080 screenshot.

WHY ffmpeg AND NOT A HAND-WRITTEN PNG ENCODER

It is in the bake image - ffmpeg reads a PPM and writes a PNG without being told anything
about the format - and a converter written here would be a second thing to be wrong in a
step whose whole job is to survive when something else is already wrong.

PNG RATHER THAN JPEG, even though both are available: this is a screenshot of flat
colours and small text, and JPEG's ringing around every glyph is exactly the detail
somebody is trying to read. Measured on the same image, PNG was also the smaller of the
two at this size.

WHY THE FRAME IS CHOSEN BY NAME

snapshots.py writes frame-0000, frame-0001, ... and a directory that has been copied about
- by an artifact download, say - gives every file the same mtime. The highest number is
what the producer meant; the newest mtime is what the filesystem happened to do.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


# THE TWO NAMING SCHEMES THIS REPOSITORY USES, and the reason it is not one:
#
#     frame-0000.ppm      lib/snapshots.py, which every bake records through
#     boot-1793633015.ppm windows/11/ssh.py, which takes its own rather than starting a
#                         recorder process, and stamps them with the clock
#
# A prefix list is honest about that. Sorting by name across two schemes would put
# `boot-…` after `frame-…` for no reason except the alphabet.
# THREE NAMING SCHEMES, and each one is a different producer:
#
#     frame-0000.ppm      lib/snapshots.py, the timer that records every bake
#     boot-1793633015.ppm windows/11/ssh.py, which takes its own and stamps the clock
#     inst-003-after-...  macos/15/install.py's shot(), one per driver action
#
# The third has the action's name in the filename, which is why it is not numbered only:
# "inst-003-after-02-recovery-Continue.ppm" says more than "frame-0003".
PREFIXES = ("frame-", "boot-", "inst-")


def newest_frame(directory: str) -> str | None:
    """The frame with the largest number in its name, under either scheme.

    Not the newest mtime: a directory copied about - by an artifact download, say - gives
    every file the same one, and the number is what the producer meant.

    Clock-stamped names sort correctly as numbers because the stamp is seconds since the
    epoch, which only ever grows.

    A .PPM WITH A .JPG BESIDE IT IS BEING CONVERTED, SO IT IS SKIPPED. lib/snapshots.py
    writes `frame-0177.ppm`, converts it to `frame-0177.jpg`, then deletes the PPM. Between
    the write and the delete both exist, and this used to return the PPM - which then
    vanished underneath the caller:

        [lastframe] ffmpeg failed: Error opening input: No such file or directory
        Error opening input file .../frames/frame-0177.ppm.

    A bake that left both behind would log that line, and a caller with nothing else to
    fall back on would have lost the frame rather than the duplicate.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return None

    present = set(names)
    candidates = []
    for name in names:
        # .jpg AS WELL AS .ppm, because lib/snapshots.py converts each screendump and deletes
        # the PPM: a PPM is 3 MB and an hour of frames is 11 GiB, which filled a runner and
        # stopped macos26 mid-install. The name still starts with a prefix and a number; only
        # the extension changed.
        ext = os.path.splitext(name)[1]
        if ext not in (".ppm", ".jpg"):
            continue
        if ext == ".ppm" and (name[:-4] + ".jpg") in present:
            # The conversion finished and the delete has not happened yet. The JPEG is the
            # same picture and is not about to disappear.
            continue
        for prefix in PREFIXES:
            if not name.startswith(prefix):
                continue
            stem = name[len(prefix):-len(ext)]
            # `frame-0000` and `boot-1793633015` are numbers; `inst-003-after-...` starts
            # with one and then carries a label.
            head = stem.split("-", 1)[0]
            if head.isdigit():
                candidates.append((int(head), name))
            break

    if not candidates:
        return None
    return os.path.join(directory, max(candidates)[1])


def newest_frame_stable(directory: str, *, tries: int = 5) -> str | None:
    """newest_frame(), retried when the file it named disappeared before it was opened.

    THE RACE IS REAL AND IT IS ON A ONE-SECOND TIMER. lib/snapshots.py writes frame-N.ppm,
    converts it to frame-N.jpg and deletes the PPM, once a second, for the whole run. A
    caller that lists the directory can see the PPM, choose it, and lose it before ffmpeg
    opens it:

        [lastframe] ffmpeg failed: Error opening input: No such file or directory
        Error opening input file .../frames/shots/frame-1103.ppm

    which is what run 42000179043 logged every few minutes. Nothing was wrong with the
    recording - the frame had simply been converted, correctly, between the two calls.

    newest_frame() already skips a PPM that has a JPEG beside it, which covers the common
    case. It cannot cover this one: the JPEG does not exist yet at the moment of the listing.
    So the answer is to look again, and the second look finds the JPEG.

    Five tries with a short wait, because the conversion takes a fraction of a second; a
    disk that answers "gone" six times in a row has a different problem and saying so is
    more useful than looping.
    """
    import time as _time

    for attempt in range(tries):
        found = newest_frame(directory)
        if found and os.path.isfile(found):
            return found
        # A NAME IS NOT A FILE. The check above is what turns "listed a moment ago" into
        # "still there now"; without it the caller's ffmpeg is the thing that finds out.
        if attempt < tries - 1:
            _time.sleep(0.4)
    return None


def convert(source: str, dest: str) -> bool:
    """Turn one PPM into a PNG. True when it was written.

    THE FUNCTION macos/15/install.py CALLS AFTER EVERY SCREENSHOT. It did not exist: the
    driver called lastframe.convert() and got

        module 'lastframe' has no attribute 'convert'

    on every click of run 41493723813. The call is wrapped so a missing picture cannot
    stop a run, which is why that showed up as a warning rather than a failure - and also
    why nobody noticed the picture was never written.

    ffmpeg rather than a hand-written PNG encoder, for the reason in the module docstring:
    it reads a PPM and writes a PNG without being told anything about the format.
    """
    if not shutil.which("ffmpeg"):
        print("[lastframe] no ffmpeg", flush=True)
        return False

    # A FRAME THAT DISAPPEARS IS NOT AN ERROR, AND THIS IS THE ONE PLACE THAT CAN SAY SO.
    #
    # lib/snapshots.py converts frame-N.ppm to frame-N.jpg and unlinks the PPM, once a second
    # for the whole run. Any caller that named that PPM a moment earlier loses the race -
    # and every caller has its own way of picking a frame, so fixing the pickers one at a
    # time is how this came back three times:
    #
    #     newest_frame           skipped a PPM that already had a JPEG  (fixed, first)
    #     live._write_last_frame had its own selector                   (fixed, second)
    #     either of them         between the listing and ffmpeg's open   (this one)
    #
    # THE FALLBACK IS THE JPEG WITH THE SAME NAME, and it is the same picture - that is what
    # the conversion is. So: if the source is gone and a sibling with the same stem exists,
    # read that instead. Otherwise retry the whole read a few times, because the JPEG appears
    # before the PPM goes and the window between them is milliseconds.
    #
    # Only after that does it say anything, and what it says is "nothing at", not
    # "ffmpeg failed" - a missing frame is normal traffic in this directory, not a defect in
    # the encoder.
    import time as _time

    attempts = [source]
    stem = os.path.splitext(source)[0]
    for extension in (".jpg", ".jpeg", ".ppm"):
        candidate = stem + extension
        if candidate not in attempts:
            attempts.append(candidate)

    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    last_error = ""
    for attempt in range(3):
        for candidate in attempts:
            if not os.path.isfile(candidate):
                continue
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", candidate, dest],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return True
            last_error = (result.stderr or "").strip()[:200]
        _time.sleep(0.4)

    if not os.path.isfile(source) and not any(os.path.isfile(c) for c in attempts[1:]):
        # THE HONEST MESSAGE. Nothing was read because nothing was there.
        print(f"[lastframe] nothing at {os.path.basename(source)} "
              f"(and no sibling to fall back on)", flush=True)
        return False
    print(f"[lastframe] ffmpeg failed on {os.path.basename(source)}: {last_error}",
          flush=True)
    return False


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("usage: lastframe.py <frames-dir-or-ppm> <out.png>")

    source_arg, dest = argv[0], argv[1]
    if os.path.isdir(source_arg):
        # newest_frame_stable, NOT newest_frame: the recorder converts each PPM to JPEG and
        # deletes it once a second, so the frame named here can be gone by the time ffmpeg
        # opens it. See that function's docstring for the log line that produced this.
        source = newest_frame_stable(source_arg)
        if source is None:
            print(f"[lastframe] no frame-*.ppm or *.jpg in {source_arg}", flush=True)
            return 1
    elif os.path.isfile(source_arg):
        source = source_arg
    else:
        print(f"[lastframe] nothing at {source_arg}", flush=True)
        return 1

    # THE RESULT IS CHECKED AND DESCRIBED, WHICH USED TO BE UNREACHABLE.
    #
    # This block sat AFTER `return 0 if convert(...) else 1`, so it had never run: a
    # conversion that wrote a zero-byte file, or wrote nothing and returned True, reported
    # success with no message either way. Nothing depended on the message, which is why it
    # went unnoticed - a caller reading the log could not tell a picture from a failure.
    if not convert(source, dest):
        return 1

    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        print(f"[lastframe] {dest} was not written", flush=True)
        return 1

    print(f"[lastframe] {os.path.basename(source)} -> {dest} "
          f"({os.path.getsize(dest) >> 10} KiB)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
