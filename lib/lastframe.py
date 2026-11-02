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
PREFIXES = ("frame-", "boot-")


def newest_frame(directory: str) -> str | None:
    """The frame with the largest number in its name, under either scheme.

    Not the newest mtime: a directory copied about - by an artifact download, say - gives
    every file the same one, and the number is what the producer meant.

    Clock-stamped names sort correctly as numbers because the stamp is seconds since the
    epoch, which only ever grows.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return None

    candidates = []
    for name in names:
        if not name.endswith(".ppm"):
            continue
        for prefix in PREFIXES:
            if not name.startswith(prefix):
                continue
            stem = name[len(prefix):-len(".ppm")]
            if stem.isdigit():
                candidates.append((int(stem), name))
            break

    if not candidates:
        return None
    return os.path.join(directory, max(candidates)[1])


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("usage: lastframe.py <frames-dir-or-ppm> <out.png>")

    source_arg, dest = argv[0], argv[1]
    if os.path.isdir(source_arg):
        source = newest_frame(source_arg)
        if source is None:
            print(f"[lastframe] no frame-*.ppm in {source_arg}", flush=True)
            return 1
    elif os.path.isfile(source_arg):
        source = source_arg
    else:
        print(f"[lastframe] nothing at {source_arg}", flush=True)
        return 1

    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", source, dest],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[lastframe] ffmpeg failed: {(result.stderr or '').strip()[:300]}",
              flush=True)
        return 1

    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        print(f"[lastframe] {dest} was not written", flush=True)
        return 1

    print(f"[lastframe] {os.path.basename(source)} -> {dest} "
          f"({os.path.getsize(dest) >> 10} KiB)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
