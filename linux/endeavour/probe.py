"""Boot EndeavourOS and answer one question: what language does Calamares open in?

    python3 linux/endeavour/probe.py --iso out/iso/EndeavourOS-...iso --out out/probe

WHY THIS IS A PROBE AND NOT AN INSTALLER

The installer run for EndeavourOS has not been written yet, because there is one fact
about it that has to be settled first and cannot be settled from a reading of the image:
Calamares opens in Simplified Chinese on the machine this was developed on, while the
live session around it is in English and `LANG` is `en_US.UTF-8`.

Every explanation tried locally was disproved by a measurement:

    system locale            LANG=en_US.UTF-8, and `locale -a` has NO zh_CN at all
    container / guest clock  both Etc/UTC, not Asia/Shanghai
    geoip                    the mock was never contacted - dnsmasq logged no lookup
    Calamares config         no Shanghai string anywhere under /usr/share/calamares

and one claim was made on bad evidence: `ls /usr/share/calamares/modules/` returned
nothing, but the SAME listing a moment earlier showed `modules` in the directory. That
`ls` was run through `sudo -n`, which fails silently here, so the empty answer was the
sudo failure and not an empty directory.

So this probe does not argue from readings. It boots the medium, opens the installer, and
records what the screen says - in the environment CI actually uses, which is the only one
that matters.

WHAT IT LEAVES BEHIND

    frames/            a screenshot every SNAPSHOT_INTERVAL seconds
    language.txt       the OCR of the installer's first pane, and
                       the answers to the config questions below, taken WITHOUT sudo
    last-frame.png     the final screen

Everything is uploaded by the workflow, so the answer survives the runner.
"""

from __future__ import annotations

import argparse
import os
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

# THE WELCOME WINDOW'S OWN BUTTON, measured on this image at 1920x1080.
START_INSTALLER = (680, 425)
# THE INSTALLATION METHOD DIALOG that follows it. `Offline` is the right choice for a
# bake - no network, and the medium's own packages.
OFFLINE = (1069, 590)

DESKTOP_SECONDS = 240


def log(msg: str) -> None:
    print(f"[endeavour-probe {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def facts(out: str) -> str:
    """Answer the config questions from inside the running guest, WITHOUT sudo.

    THE `sudo -n` VERSION OF THESE QUESTIONS WAS WHAT WENT WRONG LOCALLY. `-n` means "never
    prompt", and with no passwordless rule it fails immediately - so a command that was
    meant to read a directory printed nothing and looked like an empty directory. These
    read only world-readable files, so they need no privilege at all.
    """
    checks = [
        ("locale", "locale 2>&1 | head -4"),
        ("locales available", "locale -a 2>/dev/null | grep -ci zh || true"),
        ("LANG env", "echo \"LANG=$LANG LANGUAGE=$LANGUAGE LC_ALL=$LC_ALL\""),
        ("clock", "date; cat /etc/timezone 2>/dev/null; readlink -f /etc/localtime"),
        ("calamares dir", "ls /usr/share/calamares/ 2>&1 | head"),
        ("modules dir", "ls /usr/share/calamares/modules/ 2>&1 | head -20"),
        ("welcome.conf", "cat /usr/share/calamares/modules/welcome.conf 2>&1 | head -30"),
        ("settings", "grep -vE '^#|^$' /usr/share/calamares/settings_offline.conf 2>&1 | head -30"),
        ("etc calamares", "ls /etc/calamares/ 2>&1 | head"),
        ("shanghai refs", "grep -rl Shanghai /usr/share/calamares 2>/dev/null | head"),
        ("calamares cmdline", "ps -eo args | grep '[c]alamares' | head -3"),
        ("calamares env", "for p in $(pgrep -f 'lib/calamares'); do "
                          "tr '\\0' '\\n' < /proc/$p/environ 2>/dev/null | "
                          "grep -E '^LANG|^LANGUAGE|^LC_' | head -4; done"),
    ]
    lines = []
    for name, cmd in checks:
        try:
            result = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True,
                                    timeout=30)
            body = (result.stdout or "") + (result.stderr or "")
        except (OSError, subprocess.SubprocessError) as exc:
            body = f"(failed: {exc})"
        lines.append(f"--- {name} ---")
        lines.append(body.strip() or "(empty)")
        lines.append("")
    text = "\n".join(lines)
    path = os.path.join(out, "language.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    log(f"wrote {path}")
    return text


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=None)
    p.add_argument("--disk-size", default="32G")
    p.add_argument("--mem", type=int, default=4096)
    p.add_argument("--cpus", type=int, default=4)
    args = p.parse_args(argv)

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or (os.path.dirname(os.path.abspath(args.out)) + "/work")
    os.makedirs(work, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)

    disk = os.path.join(work, "endeavour-probe.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)

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
            work, "endeavour-probe"),
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
    log(f"qemu pid {guest.pid}")

    snap = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         guest.paths.record, frames, "5"],
        stdout=open(os.path.join(frames, "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )
    art = live.start([frames], os.path.join(args.out, "install.mp4"),
                     os.path.join(args.out, "last-frame.png"), every=30.0)

    try:
        drv_shots = os.path.join(work, "shots")
        os.makedirs(drv_shots, exist_ok=True)
        # THE SOCKET IS NOT THERE YET WHEN QEMU HAS JUST STARTED, AND ocr.Screen DOES NOT
        # WAIT FOR IT. Every driver in this repository has its own wait loop for exactly
        # this reason - `Driver.__init__` in linux/kubuntu/install.py is the reference -
        # and the first version of this probe skipped it:
        #
        #     File "lib/ocr.py", line 57, in __init__
        #       self.q = qmpmod.QMP(qmp_path)
        #     FileNotFoundError: [Errno 2] No such file or directory
        #
        # which is the probe failing before it photographed anything.
        deadline = time.time() + 180
        while not os.path.exists(guest.paths.drive):
            if time.time() > deadline:
                raise SystemExit(f"no QMP socket at {guest.paths.drive} after 180s")
            time.sleep(1)
        screen = ocr.Screen(guest.paths.drive, drv_shots, width=1920, height=1080)
        q = screen.q

        log(f"waiting {DESKTOP_SECONDS}s for the live desktop")
        time.sleep(DESKTOP_SECONDS)

        log("opening the installer")
        Q.click(q, *START_INSTALLER)
        time.sleep(3)
        Q.click(q, *START_INSTALLER)
        time.sleep(20)

        log("answering the installation-method dialog with Offline")
        Q.click(q, *OFFLINE)
        time.sleep(45)

        # THE QUESTION: what does the installer's own first pane say?
        ppm = screen.grab("calamares-welcome")
        words = screen.text(ppm) if ppm else []
        first = " | ".join(w[0] for w in words[:30])
        log(f"installer first pane: {first}")

        text = facts(args.out)
        header = ("=== THE INSTALLER'S FIRST PANE AS OCR READ IT ===\n"
                  f"{first}\n\n")
        with open(os.path.join(args.out, "language.txt"), "w", encoding="utf-8") as fh:
            fh.write(header + text)

        # A FRAME FROM INSIDE THE SESSION AS WELL: the QMP screendump does not include the
        # window decorations, and the point of this run is a picture someone can look at.
        with open(os.path.join(args.out, "welcome.ppm"), "wb") as fh:
            fh.write(b"")
        try:
            Q.QMP(guest.paths.drive).execute("screendump",
                                            filename=os.path.join(args.out, "welcome.ppm"))
            lastframe.convert(os.path.join(args.out, "welcome.ppm"),
                              os.path.join(args.out, "welcome.png"))
        except Exception as exc:                       # noqa: BLE001
            log(f"  could not save the welcome frame: {exc}")
    finally:
        guest.stop(acpi_first=False)
        snap.terminate()
        art.stop()

    log("=== PROBE DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
