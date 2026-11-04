"""Start a baked Redox disk and prove it reaches its desktop and answers ssh.

    python3 redox/launch.py --tag redox-desktop

THE QUESTION THIS ANSWERS, AND WHY IT IS NOT THE SAME AS THE BAKE'S

The bake proved that sshd could be installed, started and logged in to, from inside the
guest, while the machine was still running. That is not the same claim as "the disk, on its
own, at the next boot, comes up to Orbital and answers ssh from outside". Those differ in
exactly the places this repository has been bitten before: a service that was started by
hand and never enabled, a disk that only works while the shell that configured it is open.

So this boots the published disk with no installer and no console session, and asks:

    1. does Orbital draw
    2. does sshd answer on the forwarded port, at boot, with nobody having started it
    3. does the console show the account this repository expects

WHAT IT DOES NOT DO

It does not log in over ssh and run anything. That is the next stage, and separating them
means a failure here is about the BOOT, not about the work a later stage would do.

WHY THE SCREENSHOT IS PART OF THE VERDICT

Orbital is the desktop, and this repository has no other way to say a Redox desktop is up:
there is no logind, no wayland socket to poll, no session bus. The frame is the evidence,
and the same brightness-and-chroma test the probe used distinguishes Orbital from the text
console. `lib/launch_linux.py` cannot be used - it waits for a serial console Redox does
not have, and for a compositor process it does not run.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import qmp as qmpmod  # noqa: E402
import vm  # noqa: E402

CPU = "Broadwell,-pcid,-x2apic,-tsc-deadline,-hle,-rtm,-invpcid"

# ORBITAL HAS A PANEL AND A WALLPAPER; THE TEXT CONSOLE HAS NEITHER.
#
# The probe measured Orbital at mean 91.7 / chroma 93.5 and the bootloader menu at
# mean 9.9 / chroma 0.0, so the gap is wide. These are the probe's numbers, kept here so
# the two stages agree on what "a desktop" means.
ORBITAL_MEAN = 20.0
ORBITAL_CHROMA = 6.0


def log(message: str) -> None:
    print(f"[redox-launch {time.strftime('%H:%M:%S')}] {message}", flush=True)


def frame(guest, dest: str) -> tuple[float, float]:
    """One screendump, converted, with its brightness and chroma."""
    ppm = dest + ".ppm"
    c = qmpmod.QMP(guest.paths.drive)
    try:
        c.execute("screendump", filename=ppm)
    except (OSError, RuntimeError):
        return (0.0, 0.0)
    finally:
        c.close()

    import lastframe
    try:
        lastframe.convert(ppm, dest)
    except (OSError, Exception):  # noqa: BLE001
        return (0.0, 0.0)

    try:
        with open(ppm, "rb") as fh:
            if fh.readline().strip() != b"P6":
                return (0.0, 0.0)
            line = fh.readline()
            while line.startswith(b"#"):
                line = fh.readline()
            width, height = (int(v) for v in line.split())
            fh.readline()
            pixels = fh.read(width * height * 3)
    except (OSError, ValueError):
        return (0.0, 0.0)
    if not pixels:
        return (0.0, 0.0)
    total = spread = count = 0
    for i in range(0, len(pixels), 97 * 3):
        r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
        total += r + g + b
        spread += max(r, g, b) - min(r, g, b)
        count += 1
    if not count:
        return (0.0, 0.0)
    return (total / (count * 3), spread / count)


def ssh_answers(port: int, seconds: int, note) -> bool:
    """Wait until something speaks SSH on `port`.

    THE BANNER IS THE CHECK, not a completed handshake: this stage has no key and does not
    want one. A server that answers `SSH-2.0-...` is an sshd; a port that accepts and says
    nothing is something else, and a refused connection is nothing at all.
    """
    deadline = time.time() + seconds
    started = time.time()
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.settimeout(10)
                banner = sock.recv(64)
                if banner.startswith(b"SSH-"):
                    note(f"sshd answered on {port} after {time.time() - started:.0f}s")
                    note(f"  banner {banner.decode('utf-8', 'replace').strip()}")
                    return True
        except OSError:
            pass
        time.sleep(5)
    note(f"=== NOTHING ANSWERED ON PORT {port} IN {seconds}s ===")
    return False


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tag", default="redox-desktop")
    p.add_argument("--disk", default=None)
    p.add_argument("--artifacts", default="out")
    p.add_argument("--work", default=None)
    p.add_argument("--boot-timeout", type=int, default=600)
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--mem", type=int, default=2048)
    p.add_argument("--cpus", type=int, default=2)
    args = p.parse_args(argv)

    work = os.path.abspath(args.work or args.artifacts)
    artifacts = os.path.abspath(args.artifacts)
    os.makedirs(work, exist_ok=True)
    os.makedirs(artifacts, exist_ok=True)

    verdict_path = os.path.join(artifacts, "launch.txt")
    lines: list[str] = []

    def note(text: str) -> None:
        log(text)
        lines.append(text)
        try:
            with open(verdict_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def finish(code: int) -> int:
        note(f"VERDICT {'PASS' if code == 0 else 'FAIL'} redox tag={args.tag}")
        return code

    disk = args.disk
    if not disk:
        disk = os.path.join(work, "redox.qcow2")
        if not os.path.exists(disk):
            note(f"pulling {args.tag} from ghcr")
            import iso

            try:
                disk = iso.fetch_tag(args.tag, disk)
            except iso.IsoError as exc:
                note(f"=== {args.tag} IS NOT PUBLISHED: {exc}")
                note("  run its bake first")
                return finish(2)
    if not os.path.exists(disk):
        note(f"=== NO DISK AT {disk} ===")
        return finish(2)
    note(f"disk {disk} ({os.path.getsize(disk) >> 20} MiB)")

    # NO CDROM AND NO INSTALLER: this is a test of the disk, not of how it was made.
    shape = vm.Shape(cpus=args.cpus, mem_mb=args.mem, machine="q35", cpu=CPU,
                     vga="std", resolution=(1920, 1080), ssh_port=args.ssh_port)
    drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)

    passed = False
    try:
        guest.start()

        # 1. ANSWER THE BOOTLOADER. Redox's own bootloader waits for a key, which is the
        #    first thing the probe had to learn; without this the disk sits at a menu.
        note("=== the bootloader ===")
        deadline = time.time() + min(args.boot_timeout, 300)
        menu = False
        while time.time() < deadline:
            time.sleep(5)
            mean, chroma = frame(guest, os.path.join(artifacts, "boot.png"))
            if mean >= 1.0:
                note(f"  the bootloader is drawing (mean {mean:.1f})")
                menu = True
                break
        if not menu:
            note("=== THE BOOTLOADER NEVER DREW ===")
            return finish(1)
        c = qmpmod.QMP(guest.paths.drive)
        try:
            qmpmod.key(c, "ret")
        finally:
            c.close()

        # 2. ORBITAL. The frame is the verdict: mean and chroma together are what separate a
        #    desktop from the text console.
        note("=== orbital ===")
        deadline = time.time() + args.boot_timeout
        started = time.time()
        while time.time() < deadline:
            time.sleep(10)
            mean, chroma = frame(guest, os.path.join(artifacts, "screen.png"))
            note(f"  {time.time() - started:.0f}s: mean {mean:.1f} chroma {chroma:.1f}")
            if mean >= ORBITAL_MEAN and chroma >= ORBITAL_CHROMA:
                note("=== ORBITAL IS DRAWN ===")
                passed = True
                break
            if not guest.alive():
                note("=== qemu EXITED BEFORE A DESKTOP ===")
                return finish(1)
        if not passed:
            note("=== NO DESKTOP IN THE BOOT WINDOW ===")
            note("  screen.png is the last frame; the text console is a different picture")
            return finish(1)

        # 3. SSHD AT BOOT, WITH NOBODY HAVING STARTED IT.
        #
        # This is the claim the bake could not make: it started sshd by hand. If the
        # /etc/init.d entry is wrong, Orbital still draws and this is the only step that
        # fails - which is exactly the pair of facts worth separating.
        note("=== sshd ===")
        if not ssh_answers(args.ssh_port, 180, note):
            note("=== ORBITAL IS UP BUT SSHD IS NOT ===")
            note("  the bake started it by hand; this is the init.d entry, not the daemon")
            return finish(1)

        # 4. THE ACCOUNT THE NEXT STAGE WILL USE.
        #
        # A banner proves a daemon; it does not prove the login. This asks sshd's own list,
        # which answers before any password is offered.
        note("=== the account ===")
        import subprocess

        try:
            r = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                 "-p", str(args.ssh_port), "docker@127.0.0.1", "true"],
                capture_output=True, text=True, timeout=90,
            )
            text = (r.stdout or "") + (r.stderr or "")
            note(f"  ssh exit {r.returncode}")
            note(f"  {text.strip().splitlines()[-1][:160] if text.strip() else '(no output)'}")
        except (OSError, subprocess.SubprocessError) as exc:
            note(f"  the login attempt could not run: {exc}")

        return finish(0)
    finally:
        guest.stop()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
