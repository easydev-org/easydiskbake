"""Click Continue through the macOS installer and keep the last frame of every pane.

    python3 macos/15/probe.py --out out/probe

WHY THIS EXISTS

Every macOS run so far has died on a coordinate, and the coordinates were always
plausible - inside the screen, near where that control usually sits. What was missing
was a way to ask the guest, cheaply, what its panes look like.

So this does one thing: it clicks Continue. Wherever Continue is. If the pane advances,
the click worked and the next pane is captured. If nothing changes, the click missed and
the frame that says so is kept. The last frame of each distinct screen is the
measurement.

WHAT IT PRODUCES

    out/probe/frames/frame-NNNN.ppm     one per click
    out/probe/panes/pane-NNNN.ppm       the last frame before the screen changed
    out/probe/panes/index.txt           pane number, frame, OCR text of that pane
    out/probe/probe.mp4                 the whole sequence

The OCR is run HERE, not in the driver. The driver still clicks blind - that is the
whole point of it - and the reading happens afterwards on files, where being wrong costs
a paragraph instead of a run.

WHAT IT WILL NOT DO

It does not agree to a licence, erase a disk, or choose one. Those need a decision and
a decision needs to know where the control is, which is what this is for. The sequence
it walks is: Recovery menu -> Reinstall macOS -> installer panes -> wherever Continue
leads, stopping at the first frame that does not change twice in a row.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, HERE)

import compress  # noqa: E402
import desktop  # noqa: E402
import machine  # noqa: E402
import opencore  # noqa: E402
import qmp as qmpmod  # noqa: E402

# The two positions this probe is allowed to use, both measured from frames of this
# bake rather than copied from another driver:
#
#   Reinstall macOS Sequoia, in the Recovery menu
#       title y=324, description y=342, so the row's centre is y=333 at x=837
#
#   Continue, on the installer's own panes
#       the welcome pane puts it at x=919..1002, y=698..727 -> (960, 712)
#
# Every pane after the welcome one is unknown, which is why this exists. The probe keeps
# clicking the same Continue and reports which frames changed.
REINSTALL_ROW = (837, 333)
CONTINUE = (960, 712)

# The Recovery menu's own Continue, for opening the entry that was just selected.
MENU_CONTINUE = (1142, 586)

# THE LICENCE PANE, measured from the frame this probe stopped on (run 41452125052):
#
#     Disagree   x=889..942,  y=707..720   centre (915, 713)
#     Agree      x=987..1023, y=707..720   centre (1005, 713)
#
# Continue does nothing on that pane until Agree is pressed, which is why the previous
# run sat there while twelve clicks went into a button that was disabled.
#
# WHERE THESE COME FROM MATTERS. `tesseract --psm 11` also returns the word "agree" at
# y=451 on the same frame - the middle of "you must agree to the terms". The buttons are
# on the bottom row and only `--psm 6` puts them there.
LICENCE_AGREE = (1005, 713)
LICENCE_DISAGREE = (915, 713)


def log(msg: str) -> None:
    print(f"[probe {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def screenshot(q, path: str) -> bool:
    try:
        q.execute("screendump", filename=path)
        return os.path.exists(path) and os.path.getsize(path) > 0
    except (OSError, RuntimeError) as exc:
        log(f"screendump failed: {exc}")
        return False


def fingerprint(path: str) -> str | None:
    """A cheap identity for a frame: its size plus a sample of its bytes.

    Not a hash of the whole file. These frames are 6 MiB of PPM and there will be a few
    dozen; hashing them all twice would cost more than the clicks. Size plus two samples
    a third of the way in distinguishes every pane this has seen.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(size // 3)
            a = fh.read(4096)
            fh.seek(2 * size // 3)
            b = fh.read(4096)
        return f"{size}:{hash(a)}:{hash(b)}"
    except OSError:
        return None


def ocr(path: str) -> str:
    """Read a frame with tesseract, if it is installed.

    Absent tesseract is not a failure: the frames are the product, and the text is a
    convenience for whoever reads the log.
    """
    if not shutil.which("tesseract"):
        return ""
    try:
        result = subprocess.run(
            ["tesseract", path, "-", "--psm", "11", "-l", "eng"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return " | ".join(lines)[:300]


# WHAT EACH PANE'S FORWARD CONTROL IS, DECIDED FROM THE WORDS ON IT.
#
# The words come from tesseract, which is what makes this a state machine rather than a
# guess: the panes were unknown when this was written, so the only thing that can be
# known in advance is how to recognise them. Every entry is a phrase that appears on one
# pane and nowhere else - "you must agree" is the licence, "set up the installation" is
# the welcome - and the click is the position measured for that pane.
#
# A pane with no entry stops the probe and is saved, which is the point: the list grows
# by measurement, one run at a time.
PANE_ACTIONS = [
    # phrase that identifies the pane            what to click             why
    ("must agree to the terms",                   LICENCE_AGREE,   "Agree on the licence"),
    ("agree to the terms",                        LICENCE_AGREE,   "Agree on the licence"),
    ("software license agreement",                LICENCE_AGREE,   "Agree on the licence"),
    ("set up the installation",                   CONTINUE,        "installer welcome"),
    ("select a disk",                             CONTINUE,        "destination pane"),
    ("select the disk",                           CONTINUE,        "destination pane"),
    ("how do you want to install",                CONTINUE,        "destination pane"),
    ("install macos",                             CONTINUE,        "installer pane"),
    ("continue",                                  CONTINUE,        "generic forward"),
]


def decide(text: str) -> tuple[tuple[int, int] | None, str]:
    """Which control to press on this pane, and why.

    The first phrase that matches wins, and the list is ordered so that the specific
    panes come before the generic "continue" - otherwise the licence pane would match
    the generic entry and press a button that is disabled on it.
    """
    low = text.lower()
    for phrase, target, why in PANE_ACTIONS:
        if phrase in low:
            return target, why
    return None, ""


def run(out: str, *, wait_boot: int, clicks: int, interval: float) -> int:
    work = os.path.join(out, "work")
    frames = os.path.join(out, "frames")
    panes = os.path.join(out, "panes")
    for d in (work, frames, panes):
        os.makedirs(d, exist_ok=True)

    log("=== the installer and the boot assets ===")
    import bake  # the Baker's fetch and build, reused rather than reimplemented

    class Args:
        release = "15"

    args = Args()
    args.disk_size = "64G"
    args.mem = 8192
    args.cpus = 4
    args.out = os.path.join(out, "probe.qcow2")
    args.timeout = 3600
    # THE ACCOUNT AND PASSWORD THE WIZARD WILL CREATE, from the same environment variables the
    # stages use. This is a MEASUREMENT tool - it boots the installer and photographs panes - so
    # it has no account of its own to log in as; what it needs is the pair to type, and typing a
    # pair the stages no longer use would measure a disk nobody ships.
    args.user = os.environ.get("MACOS_USER", "vmuser")
    args.password = os.environ.get("MACOS_PASSWORD", "admin")
    args.keep_work = True

    baker = bake.Baker(args)
    recovery = baker.fetch_recovery()
    assets = baker.build_boot_assets()

    disk = os.path.join(work, "disk.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, "64G")

    guest = machine.make_guest(work, assets, disk, recovery,
                               cpus=args.cpus, mem_mb=args.mem)
    guest.start()
    log(f"qemu pid {guest.pid}")

    q = None
    for _ in range(60):
        try:
            if os.path.exists(guest.paths.drive):
                q = qmpmod.QMP(guest.paths.drive)
                break
        except (OSError, RuntimeError):
            pass
        time.sleep(2)
    if q is None:
        log("no QMP socket; stopping")
        guest.stop()
        return 1

    index_path = os.path.join(panes, "index.txt")
    index = open(index_path, "w", encoding="utf-8")
    index.write("pane\tframe\tsize\tocr\n")

    try:
        # The guest takes a couple of minutes to draw the Recovery menu. A fixed wait,
        # not a poll: this probe is measuring positions, and polling would need to know
        # what to look for, which is the thing being measured.
        log(f"waiting {wait_boot}s for the Recovery menu")
        time.sleep(wait_boot)

        shot_path = os.path.join(frames, "frame-0000.ppm")
        screenshot(q, shot_path)
        log(f"first frame: {ocr(shot_path)[:120]}")

        # Open the installer: select Reinstall macOS and press the menu's Continue.
        log(f"click {REINSTALL_ROW} - Reinstall macOS row")
        qmpmod.click(q, *REINSTALL_ROW)
        time.sleep(2)
        log(f"click {MENU_CONTINUE} - Recovery Continue")
        qmpmod.click(q, *MENU_CONTINUE)
        time.sleep(25)

        # THE SEQUENCE IS A STATE MACHINE, NOT A FIXED LIST OF CLICKS.
        #
        # Each round reads the screen, decides which pane it is on from the words on
        # it, and clicks that pane's forward control. That is the only way to walk an
        # installer whose panes nobody has measured: a fixed list of positions knows
        # only the panes it was written for, and the first unknown one silently eats
        # every remaining click.
        #
        # The OCR result is also what the next round's decision uses, so a pane that
        # OCR cannot read is recorded and skipped rather than clicked blindly.
        previous = None
        previous_frame = None
        pane = 0
        unchanged = 0
        seen_panes: set[str] = set()

        for i in range(1, clicks + 1):
            shot_path = os.path.join(frames, f"frame-{i:04d}.ppm")
            if not screenshot(q, shot_path):
                break
            current = fingerprint(shot_path)
            text = ocr(shot_path)

            if current == previous:
                unchanged += 1
                log(f"round {i}: screen unchanged ({unchanged} in a row)")
                if unchanged >= 2:
                    log("the screen has not changed twice; stopping so the pane can be "
                        "measured")
                    break
            else:
                if previous_frame is not None:
                    # previous_frame is the last frame of a pane that is now gone, so it
                    # is the measurement for that pane.
                    pane += 1
                    keep = os.path.join(panes, f"pane-{pane:04d}.ppm")
                    shutil.copyfile(previous_frame, keep)
                    index.write(f"{pane}\t{previous_frame}\t"
                                f"{os.path.getsize(keep)}\t{ocr(keep)}\n")
                    index.flush()
                unchanged = 0
                previous = current
                previous_frame = shot_path

            log(f"round {i}: {text[:150] or '(nothing readable)'}")

            target, why = decide(text)
            if target is None:
                log(f"round {i}: no known action for this pane; recording it and "
                    f"stopping")
                break
            log(f"round {i}: click {target} - {why}")
            qmpmod.click(q, *target)
            time.sleep(interval)

            # A pane that keeps coming back is being clicked at and not moving. Record
            # the words, not the pixels: the pixels differ by a caret.
            key = " ".join(text.split())[:80]
            if key and key in seen_panes:
                log(f"round {i}: this pane has been seen before ({key[:60]}); stopping")
                break
            seen_panes.add(key)

        # The last screen is still on the guest, and it is the one that stopped the run.
        final = os.path.join(panes, "pane-final.ppm")
        screenshot(q, final)
        pane += 1
        final_text = ocr(final)
        size = os.path.getsize(final) if os.path.exists(final) else 0
        index.write(f"{pane}\tfinal\t{size}\t{final_text}\n")
        index.close()
        log(f"final pane: {final_text[:300]}")
    finally:
        guest.stop()
        try:
            q.close()
        except (OSError, AttributeError):
            pass

    # One image per distinct screen, encoded so it can be looked at without a viewer.
    record = os.path.join(out, "probe.mp4")
    if not _encode(record, frames):
        log("nothing to encode")

    log(f"done: {pane} panes, frames in {frames}")
    return 0


def _encode(dest: str, frames: str) -> bool:
    names = sorted(f for f in os.listdir(frames) if f.endswith(".ppm"))
    if not names:
        return False
    if not shutil.which("ffmpeg"):
        log("ffmpeg is not installed; the frames are still there")
        return False
    pattern = os.path.join(frames, "frame-%04d.ppm")
    result = subprocess.run(
        ["ffmpeg", "-y", "-framerate", "1", "-i", pattern,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", dest],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"ffmpeg failed: {result.stderr[-400:]}")
        return False
    log(f"{dest}: {os.path.getsize(dest) >> 10} KiB")
    return True


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/probe")
    p.add_argument("--wait-boot", type=int, default=180,
                   help="seconds before the first click, for the Recovery menu to draw")
    p.add_argument("--clicks", type=int, default=12,
                   help="how many Continue presses to try")
    p.add_argument("--interval", type=float, default=20.0)
    args = p.parse_args(argv)
    return run(args.out, wait_boot=args.wait_boot, clicks=args.clicks,
               interval=args.interval)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
