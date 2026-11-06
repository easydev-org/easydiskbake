"""Drive the RUNNING macOS guest through its QMP socket: type, click, screenshot.

    python3 macos/15/gui.py shot          save a frame to /work/gui/NNN.png
    python3 macos/15/gui.py key ret
    python3 macos/15/gui.py type 'admin'
    python3 macos/15/gui.py click 960 540
    python3 macos/15/gui.py combo meta_l spc

WHY THIS EXISTS SEPARATELY FROM drive.py

drive.py owns a boot: it starts the machine, runs a fixed sequence, and stops it. This attaches
to a machine that is ALREADY RUNNING and does one thing, so a person (or the next command) can
look at the screen between steps. Measuring a pane means: shot, click, shot, look - and a script
that insists on doing all of that in one run cannot be interrupted to look.

It changes nothing about the guest except what the operator asks for. There is no sequence here
and no verdict.
"""

from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

SOCK = os.environ.get("MACOS_QMP", "/work/serve/drive.sock.qmp")
OUT = os.environ.get("MACOS_GUI_OUT", "/work/gui")


def main(argv: list[str]) -> int:
    import qmp as qmpmod

    if not argv:
        print(__doc__)
        return 2
    cmd, *rest = argv
    os.makedirs(OUT, exist_ok=True)

    q = qmpmod.QMP(SOCK)
    try:
        if cmd == "shot":
            # A RUNNING NUMBER, so successive shots never overwrite and the order is obvious.
            existing = [f for f in os.listdir(OUT) if f.endswith(".ppm")]
            n = len(existing) + 1
            ppm = os.path.join(OUT, f"{n:03d}.ppm")
            q.execute("screendump", filename=ppm)
            import lastframe
            png = os.path.join(OUT, f"{n:03d}.png")
            lastframe.convert(ppm, png)
            print(png)

        elif cmd == "key":
            qmpmod.key(q, rest[0])
            print(f"key {rest[0]}")

        elif cmd == "combo":
            qmpmod.key_combo(q, *rest)
            print(f"combo {'+'.join(rest)}")

        elif cmd == "type":
            from lib import keys
            keys.type_into(q, rest[0])
            print(f"type {rest[0]!r}")

        elif cmd == "click":
            x, y = int(rest[0]), int(rest[1])
            qmpmod.click(q, x, y)
            print(f"click {x},{y}")

        elif cmd == "sleep":
            time.sleep(float(rest[0]))
            print(f"slept {rest[0]}")

        else:
            print(f"unknown {cmd}")
            return 2
    finally:
        q.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
