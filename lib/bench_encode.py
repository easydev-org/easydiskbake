"""How big is an hour of recorded install, really?

    python3 lib/bench_encode.py out/frames

Estimates are not good enough here: the size of a screen recording depends almost
entirely on how much of the screen changes, and an installer is mostly a progress bar
that does not. So this measures, on frames the pipeline actually produced.

THE MATRIX

    fps    how many frames per second the recording is encoded at
    crf    quality; lower is bigger
    preset how much time the encoder spends deciding

Encoding a real hour would take an hour. Looping 40 captured frames out to an hour's
worth of frame count and scaling the measurement is exact, because x264's output size
is a function of the frame content and the settings, not of the wall clock.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def log(msg: str) -> None:
    print(msg, flush=True)


def encode(frames_dir: str, count: int, into: int, fps: int, crf: int,
           preset: str) -> tuple[int, int, str]:
    """Encode `count` source frames repeated `into` times, return (bytes, rc, err)."""
    out = f"t_{fps}_{crf}_{preset}.mp4"
    if os.path.exists(out):
        os.unlink(out)

    # -stream_loop repeats the input sequence; -frames:v bounds the output. Using the
    # frames-per-hour as the bound is what makes the result comparable across fps.
    target = fps * 3600
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-framerate", str(fps), "-start_number", "0",
            "-i", os.path.join(frames_dir, "frame-%04d.ppm"),
            "-stream_loop", str(into),
            "-frames:v", str(target),
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", out,
        ],
        capture_output=True, text=True,
    )
    size = os.path.getsize(out) if os.path.exists(out) else 0
    return size, result.returncode, result.stderr[-300:]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("frames", nargs="?", default="frames")
    args = p.parse_args(argv)

    if not os.path.isdir(args.frames):
        log(f"no frame directory at {args.frames}")
        return 1

    frames = sorted(
        f for f in os.listdir(args.frames)
        if f.startswith("frame-") and f.endswith(".ppm")
    )
    if not frames:
        log(f"no frames in {args.frames}")
        return 1

    first = os.path.getsize(os.path.join(args.frames, frames[0]))
    log(f"frames: {len(frames)}  (each {first} bytes raw)")
    log(f"SIZE_{first}_BYTES_PER_FRAME")
    log()

    # How many repeats make one hour at each fps: 3600*fps / len(frames), rounded up.
    matrix = [
        (1, 23, "ultrafast"),
        (1, 28, "ultrafast"),
        (1, 23, "medium"),
        (5, 23, "ultrafast"),
        (5, 28, "ultrafast"),
        (10, 28, "ultrafast"),
    ]

    log(f"{'fps':>4} {'crf':>4} {'preset':>10} {'hour_size':>12} {'per_day':>10}")
    for fps, crf, preset in matrix:
        into = max(1, -(-3600 * fps // len(frames)))
        size, rc, err = encode(args.frames, len(frames), into, fps, crf, preset)
        if rc != 0:
            log(f"{fps:>4} {crf:>4} {preset:>10}  FAILED: {err}")
            continue
        gb = size / (1024 ** 3)
        log(f"{fps:>4} {crf:>4} {preset:>10} {gb:>10.2f} GB {gb * 24:>8.1f} GB")

    log()
    log("BENCH_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
