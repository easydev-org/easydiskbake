"""Ask the shipped image what it can actually do, rather than trusting release notes.

    python3 lib/probe.py

THE QUESTION

`ffmpeg -f vnc -i localhost:5900` fails with "Unknown input format: 'vnc'". There are
four ways to record a guest and each is settled by one command against this image:

    1. does this ffmpeg have a vnc demuxer at all
    2. would a package provide one
    3. what input devices and display backends does this qemu have
    4. what is packaged for alternatives (x11grab, xvfb, xdotool)

The answers decided the recording design, so they are worth being able to re-run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def log(msg: str = "") -> None:
    print(msg, flush=True)


def run(cmd: list[str], *, timeout: int = 60) -> tuple[int, str]:
    """Run a command and return its exit code and combined output."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout}s"


def head(text: str, lines: int = 10) -> str:
    return "\n".join(text.splitlines()[:lines])


def main() -> int:
    log("=== 1. is there a vnc demuxer in this ffmpeg ===")
    code, out = run(["ffmpeg", "-hide_banner", "-demuxers"])
    vnc = [line for line in out.splitlines() if "vnc" in line.lower()]
    log("\n".join(vnc) if vnc else "  NO vnc demuxer")

    log()
    log("=== 1b. would a package provide it ===")
    run(["apt-get", "update", "-qq"], timeout=120)
    code, out = run(["apt-cache", "depends", "ffmpeg"])
    dep = [line for line in out.splitlines() if "vnc" in line.lower()]
    log("\n".join(dep) if dep else "  no vnc dependency")
    code, out = run(["apt-cache", "search", "libvncclient"])
    log(head(out, 5) if out else "  no libvncclient package")

    log()
    log("=== 2. ffmpeg input devices ===")
    code, out = run(["ffmpeg", "-hide_banner", "-devices"])
    started = False
    shown = 0
    for line in out.splitlines():
        if "Devices:" in line:
            started = True
        if started:
            log(f"  {line}")
            shown += 1
            if shown > 25:
                break

    log()
    log("=== 3. qemu display backends compiled in ===")
    code, out = run(["qemu-system-x86_64", "-display", "help"])
    log(head(out, 20))

    log()
    log("=== 3b. qemu's own gtk recorder option ===")
    code, out = run(["qemu-system-x86_64", "-display", "gtk,help"])
    log(head(out, 40))

    log()
    log("=== 3c. does the gtk display even exist ===")
    # -display none and -S: this only asks whether the backend loads, it does not need
    # a window. A 3-second bound because a gtk backend with no X server can hang.
    code, out = run(
        ["timeout", "3", "qemu-system-x86_64", "-display", "gtk",
         "-monitor", "none", "-serial", "none", "-S"],
        timeout=10,
    )
    log(f"  exit {code}: {head(out, 5) or '(no output)'}")

    log()
    log("=== 4. alternatives packaged for debian ===")
    for tool in ("Xvfb", "x11vnc", "xdotool"):
        path = shutil.which(tool)
        log(f"  {tool}: {'present at ' + path if path else 'absent'}")

    log()
    log("PROBE_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
