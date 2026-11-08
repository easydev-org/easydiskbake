"""Does a click at this coordinate actually do anything? Ask the guest, not the log.

    python3 macos/15/probe_click.py --qmp /work/drive.sock.qmp --at 960,712

WHY THIS EXISTS

The first CI run of macos/15/steps/setup.yaml reached the installer's welcome pane and then
reported

    welcome continue: no change in 120s over 19 sample(s): the picture never moved
    brightness now 28.28

Nineteen samples, so the screen could be read; brightness 28, so the guest was drawing. What
that does not say is whether the coordinate was wrong, whether the click was swallowed by a
repaint, or whether the pane had moved. Those are three different bugs and a fingerprint cannot
tell them apart.

So this clicks and READS THE PANE EVERY TIME, printing what it said before and after, and it
tries the coordinate more than once - because this guest draws a pane in about three seconds and
a click that arrives during the repaint is swallowed (install.py's click() docstring records
run 41472163272 sending seventeen actions into a display that had not painted).

WHAT IT WILL NOT DO

It does not open its own QMP connection when one is handed to it. qemu's
`-qmp unix:...,server=on,wait=off` accepts ONE client, so a probe that connects while a driver
holds the socket gets

    File "lib/qmp.py", line 78, in _read_message
        chunk = self.sock.recv(65536)
    TimeoutError: timed out

which is what the first version of this file did - and it looked like the probe was broken
rather than the socket being busy.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

REPO = os.environ.get("REPO") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for path in (REPO, os.path.join(REPO, "lib"), os.path.join(REPO, "macos", "15")):
    if path not in sys.path:
        sys.path.insert(0, path)


def log(message: str) -> None:
    print(f"[probe {time.strftime('%H:%M:%S')}] {message}", flush=True)


def read_pane(q, work: str, stem: str) -> str:
    """One screendump, OCR'd whole. Returns the text, lowercased, one line."""
    import assistant
    import ocr

    os.makedirs(work, exist_ok=True)
    ppm = os.path.join(work, f"{stem}.ppm")
    reply = q.execute("screendump", filename=ppm)
    if isinstance(reply, dict) and "error" in reply:
        log(f"  screendump refused: {reply['error']}")
        return ""
    # grab()'s own settle, and the reason it exists: the file is written after the reply.
    time.sleep(1.5)
    if not os.path.exists(ppm):
        log(f"  no file at {ppm}")
        return ""
    text = assistant.ocr_text(ppm) or ""
    return " ".join(text.lower().split())


def brightness(q, work: str, stem: str) -> float:
    from PIL import Image

    os.makedirs(work, exist_ok=True)
    ppm = os.path.join(work, f"{stem}.ppm")
    q.execute("screendump", filename=ppm)
    time.sleep(1.5)
    try:
        pixels = Image.open(ppm).convert("RGB").tobytes()
    except (OSError, ValueError):
        return -1.0
    total = 0
    count = 0
    for index in range(0, len(pixels) - 2, 97 * 3):
        total += pixels[index] + pixels[index + 1] + pixels[index + 2]
        count += 3
    return (total / count) if count else 0.0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--qmp", required=True)
    p.add_argument("--at", default="960,712", help="x,y to click")
    p.add_argument("--tries", type=int, default=3)
    p.add_argument("--settle", type=float, default=8.0,
                   help="seconds to wait after a click before reading again")
    p.add_argument("--work", default="/work/probe")
    p.add_argument("--expect", default="",
                   help="words that mean the click worked; empty means 'the text changed'")
    args = p.parse_args(argv)

    x, y = (int(v) for v in args.at.split(","))
    log(f"click target ({x}, {y})")

    # A PAIR OF CONNECTIONS, BECAUSE THE PROBE MUST NOT STEAL THE DRIVER'S.
    #
    # qmp.QMP is one client for one socket. This probe is normally run against a guest nothing
    # else is driving, so it opens its own - and says so when the socket is busy rather than
    # waiting out a 30-second QMP greeting timeout with no explanation.
    import qmp as qmpmod

    try:
        q = qmpmod.QMP(args.qmp)
    except TimeoutError:
        log("=== THE QMP SOCKET IS BUSY ===")
        log("  another process already holds it. qemu's -qmp unix:... accepts ONE client, so")
        log("  stop that driver first, or run this against a guest nobody is driving.")
        return 2

    try:
        before = read_pane(q, args.work, "before")
        log(f"before: {before[:180] or '(nothing)'}")
        log(f"before: brightness {brightness(q, args.work, 'before-b'):.1f}")

        for attempt in range(1, args.tries + 1):
            log(f"attempt {attempt}: clicking ({x}, {y})")
            qmpmod.click(q, x, y)
            time.sleep(args.settle)
            after = read_pane(q, args.work, f"after-{attempt}")
            log(f"after {attempt}: {after[:180] or '(nothing)'}")
            log(f"after {attempt}: brightness {brightness(q, args.work, f'after-b-{attempt}'):.1f}")

            if args.expect:
                moved = all(w in after for w in args.expect.lower().split())
            else:
                moved = after != before
            if moved:
                log(f"=== THE CLICK WORKED ON ATTEMPT {attempt} ===")
                return 0
            log(f"  attempt {attempt}: nothing changed")
            before = after
        log("=== THE CLICK DID NOTHING, EVERY TIME ===")
        log("  the coordinate, or the pane, is not where the file says it is")
        return 1
    finally:
        try:
            q.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
