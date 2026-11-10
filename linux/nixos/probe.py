"""Boot the NixOS graphical ISO and measure its Calamares, before a driver is written for it.

    python3 linux/nixos/probe.py --iso out/iso/nixos-graphical.iso --out out/probe

WHY A PROBE AND NOT AN INSTALLER

Every coordinate in this repository is measured from a frame of the image it drives, and the
NixOS graphical ISO has never been booted by anything here. Its Calamares is a different
build from Manjaro's, CachyOS's and EndeavourOS's:

    branding.desc      windowSize 800x520, windowExpanding normal
    settings.conf      welcome, locale, keyboard, users, packagechooser,
                       notesqml@unfree, partition, summary -> ... -> finished

- SEVEN panes, not the six or nine the other Calamares builds have, and `notesqml@unfree` is
a QML page that no other distribution in this repository shows. A walk written from the
other images' numbers would be wrong on at least those two pages.

SO THIS OPENS THE INSTALLER AND PHOTOGRAPHS EVERY PANE. It does not install anything: it
stops before the point of no return and leaves the frames as the evidence. The driver that
follows is written from them.

WHAT IT LEAVES BEHIND

    frames/                 one screenshot every SNAPSHOT_INTERVAL seconds
    panes.txt               the OCR of each pane it reached, in order
    screenshot-<pane>.png   one frame per pane, named by what the pane said
    last-frame.png          the final screen
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import compress     # noqa: E402
import lastframe    # noqa: E402
import live         # noqa: E402
import ocr          # noqa: E402
import qmp as Q     # noqa: E402
import uefi         # noqa: E402
import vm           # noqa: E402

# THE LIVE SESSION'S OWN LAUNCHER. NixOS' graphical ISO boots to a GNOME session with an
# `Install NixOS` icon on the desktop and a welcome window; both are tried, because which
# one is drawn first has not been measured on this image.
DESKTOP_ICON = (88, 240)

# Calamares' footer, expressed as a measurement rather than a click: the probe SWEEPS the
# bottom strip of the window and reports where the button actually is on each pane, which is
# the number the walk needs and the one that has been wrong most often in this repository.
FOOTER_X = 1367
FOOTER_SWEEP = range(740, 880, 8)

DESKTOP_SECONDS = 320


def log(msg: str) -> None:
    print(f"[nixos-probe {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Screen:
    def __init__(self, qmp_path: str, work: str) -> None:
        shots = os.path.join(work, "shots")
        os.makedirs(shots, exist_ok=True)
        deadline = time.time() + 180
        while not os.path.exists(qmp_path):
            if time.time() > deadline:
                raise SystemExit(f"no QMP socket at {qmp_path} after 180s")
            time.sleep(1)
        self.ocr = ocr.Screen(qmp_path, shots, width=1920, height=1080)
        self.q = self.ocr.q
        self.n = 0
        self.words: list[tuple] = []
        self.ppm = ""

    def look(self, label: str) -> str:
        self.n += 1
        self.ppm = self.ocr.grab(f"{self.n:03d}-{label}") or ""
        self.words = self.ocr.text(self.ppm) if self.ppm else []
        text = " ".join(w[0].lower() for w in self.words)
        log(f"  {label}: {' | '.join(w[0] for w in self.words[:22])}")
        return text

    def click(self, xy, why: str, settle: float = 3.0) -> None:
        log(f"click {xy} - {why}")
        Q.click(self.q, *xy)
        time.sleep(settle)

    def key(self, code: str, settle: float = 1.0) -> None:
        Q.key(self.q, code)
        time.sleep(settle)

    def close(self) -> None:
        try:
            self.ocr.close()
        except Exception:                              # noqa: BLE001
            pass


def save_png(ppm: str, dest: str) -> bool:
    if not ppm or not os.path.isfile(ppm):
        return False
    try:
        lastframe.convert(ppm, dest)
        return True
    except Exception:                                  # noqa: BLE001
        return False


def footer_rows(ppm: str) -> list[tuple[int, int, int]]:
    """Where the footer's buttons are drawn on this frame: [(y, r, g, b)] down one column.

    A MEASUREMENT, NOT A CLICK. Every pane in this repository's Calamares bakes has had its
    footer at a different height, and the number that matters is the one this image draws.
    """
    try:
        from PIL import Image
    except ImportError:
        return []
    if not ppm or not os.path.isfile(ppm):
        return []
    out = []
    try:
        with Image.open(ppm) as image:
            px = image.convert("RGB").load()
    except OSError:
        return []
    for y in FOOTER_SWEEP:
        r, g, b = px[FOOTER_X, y]
        # only report rows that are a fill (not the card's own 48,48,48 background)
        if (max(r, g, b) - min(r, g, b) > 40 and max(r, g, b) > 110) or 110 < r < 190:
            out.append((y, r, g, b))
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=None)
    p.add_argument("--disk-size", default="64G")
    p.add_argument("--mem", type=int, default=8192)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--panes", type=int, default=5,
                   help="how many panes to walk past before stopping (never past Summary)")
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)

    disk = os.path.join(work, "probe.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size} (never written - this probe stops before Summary)")

    guest = vm.Guest(
        work,
        extra=[
            "-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={args.iso},media=cdrom,readonly=on",
            "-boot", "d",
        ],
        shape=uefi.shape_with_uefi(
            vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                     vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1")),
            work, "nixos-probe"),
        stdout=os.path.join(work, "console.log"),
    )

    def _stop(signum: int, _frame) -> None:
        log(f"signal {signum}: stopping the guest")
        try:
            guest.stop(acpi_first=False)
        except Exception:                              # noqa: BLE001
            pass
        os._exit(143)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _stop)

    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    guest.start()
    log(f"qemu pid {guest.pid}, vnc {guest.shape.vnc_bind}:{5900 + guest.shape.vnc_display}")

    snap = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         guest.paths.record, frames, os.environ.get("SNAPSHOT_INTERVAL", "3")],
        stdout=open(os.path.join(frames, "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )
    art = live.start([frames], os.path.join(work, "install.mp4"),
                     os.path.join(work, "last-frame.png"),
                     every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "30")))

    notes = os.path.join(args.out, "panes.txt")
    reached: list[str] = []

    def record(pane: str, text: str, ppm: str, rows) -> None:
        name = re.sub(r"[^a-z0-9]+", "-", pane.lower()).strip("-") or f"pane{len(reached) + 1}"
        with open(notes, "a", encoding="utf-8") as fh:
            fh.write(f"=== {pane}\n{text[:2000]}\n")
            fh.write(f"footer rows: {rows}\n\n")
        save_png(ppm, os.path.join(args.out, f"screenshot-{name}.png"))
        reached.append(pane)
        log(f"  recorded {pane} -> screenshot-{name}.png")

    try:
        scr = Screen(guest.paths.drive, work)
        text = ""
        log(f"waiting up to {DESKTOP_SECONDS}s for the live desktop")
        deadline = time.time() + min(DESKTOP_SECONDS, args.timeout)
        while time.time() < deadline:
            text = scr.look("boot")
            if "nixos" in text or "install" in text:
                log("the live desktop is up")
                break
            time.sleep(15)
        else:
            log("=== THE LIVE DESKTOP DID NOT APPEAR ===")
            record("desktop-timeout", text, scr.ppm, footer_rows(scr.ppm))
            return 1

        record("live-desktop", text, scr.ppm, footer_rows(scr.ppm))

        # OPEN THE INSTALLER. GNOME's desktop draws an `Install NixOS` icon; the live
        # session also shows a welcome window whose button starts it. Both are tried in
        # order, and the probe records which one worked - that answer is the driver's.
        opened = False
        for attempt, (spot, label) in enumerate((
                ((960, 640), "welcome-window button"),
                (DESKTOP_ICON, "desktop icon"),
                ((960, 540), "welcome-window button (centre)"),
        ), start=1):
            scr.click(spot, f"{label} (attempt {attempt})", settle=6.0)
            scr.click(spot, f"{label} (attempt {attempt}, second press)", settle=25.0)
            text = scr.look(f"after-{label.replace(' ', '-')}")
            if "welcome" in text or "language" in text or "installer" in text:
                opened = True
                log(f"the installer opened from the {label}")
                break
            log(f"  not open yet ({label})")
        if not opened:
            log("=== THE INSTALLER DID NOT OPEN ===")
            record("installer-not-opened", text, scr.ppm, footer_rows(scr.ppm))
            return 1

        # WALK FORWARD WITHOUT EVER COMMITTING. Each pane is photographed with its footer
        # measured; the probe stops after --panes so it cannot reach Summary's Install.
        for index in range(1, max(1, args.panes) + 1):
            text = scr.look(f"pane-{index}")
            record(f"pane-{index}", text, scr.ppm, footer_rows(scr.ppm))
            if "summary" in text and "install" in text:
                log("reached Summary - stopping, as a probe must")
                break
            # THE FOOTER IS PRESSED AT THE HEIGHT THIS PANE DRAWS IT, measured above.
            rows = footer_rows(scr.ppm)
            y = rows[len(rows) // 2][0] if rows else 803
            log(f"  pressing the footer at ({FOOTER_X},{y}) for pane {index + 1}")
            scr.click((FOOTER_X, y), f"pane {index}: Next", settle=6.0)
            time.sleep(10.0)
        return 0
    finally:
        try:
            ppm = os.path.join(work, "final.ppm")
            c = Q.QMP(guest.paths.drive)
            try:
                c.execute("screendump", filename=ppm)
            finally:
                c.close()
            lastframe.convert(ppm, os.path.join(work, "last-frame.png"))
            save_png(ppm, os.path.join(args.out, "last-frame.png"))
        except Exception:                              # noqa: BLE001
            pass
        guest.stop(acpi_first=False)
        snap.terminate()
        art.stop()
        log(f"panes recorded: {', '.join(reached) or '(none)'}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
