"""The one bake skeleton every `linux/<distro>/install.py` shares.

WHAT THIS IS, AND WHAT IT REPLACES

There were twelve `install.py` files, 6094 lines between them, and a pairwise similarity of
57-79% measured by normalised line sets. Each one re-implemented the same five things: a
`Driver` wrapper over `lib/ocr.py` and `lib/qmp.py`, a `main()` that created the disk,
started the guest, ran a recording thread and cleaned up in a `finally`, a `wait_for_*` loop,
and a `walk_*()` function that was NEVER CALLED - the real walk is `steps/install.yaml`, and
`lib/steps.py` already drives it.

So the differences between two distributions were buried in ~250 lines of copies of each
other. What is genuinely per distribution is this, and only this:

    how the live medium boots   its own kernel+initrd extracted from the ISO and a cmdline,
                                or `-boot d` and let the medium's own GRUB run
    the disk filename           `<distro>.qcow2`
    the memory                  NixOS and Ubuntu need 8192 MiB, everything else 4096
    how "the desktop is up" is decided   a measured sleep, or an OCR marker

Everything else - the QMP plumbing, the signal handlers, the recording thread, the
post-mortem frame, the empty-disk check, the compression - is the same and lives here.

WHY THE `walk_*()` FUNCTIONS WERE DELETED RATHER THAN KEPT AS DOCUMENTATION

They described the pane sequence before that sequence moved into YAML. `steps/install.yaml`
is now the authority, and two descriptions of the same walk drift: the Python copy cannot be
executed, so it cannot be verified, so it is wrong the first time a coordinate changes. The
frames and reasons live in the YAML beside the numbers they justify, which is where a reader
looks anyway.

WHY `Driver` IS NOT DEFINED HERE

`lib/steps.py` already has one, with more than the twelve copies had: `look_at` for a cropped
read, `press` for a repeated key, `says` with the alphanumeric normalisation. A second
class with the same name in the same process was the reason `walk_*()` could not simply call
`steps.run_file` in the first place - the walker opens its own QMP client, so the bake's
driver has to be closed first. `run_bake` does that, once.

A BAKERY DESCRIBES ITSELF

```python
BAKE = Bakery(
    name="mint",
    kernel=("casper/vmlinuz", "casper/initrd.lz"),
    cmdline="boot=casper locale=en_US.UTF-8 quiet splash",
    desktop=After(seconds=180),                 # or DesktopWhen(any_of=["install linux mint"])
    mem_mb=4096,
)
```

and each distribution's `main()` is then `return livebake.cli(BAKE, sys.argv[1:])`.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import compress     # noqa: E402
import lastframe    # noqa: E402
import live         # noqa: E402
import ocr          # noqa: E402
import steps        # noqa: E402
import uefi         # noqa: E402
import vm           # noqa: E402


def log(name: str, message: str) -> None:
    print(f"[{name} {time.strftime('%H:%M:%S')}] {message}", flush=True)


# --- how the live desktop is judged ready ----------------------------------------------


@dataclass(frozen=True)
class After:
    """Wait a measured number of seconds and stop.

    USED WHERE OCR IS NOT TRUSTWORTHY ON THE LIVE MEDIUM. CachyOS, EndeavourOS, Manjaro and
    openSUSE all boot to a KDE session whose panel OCR reads inconsistently across runs, and
    each bake records the measurement it was given: ~170 s, ~200 s, ~180 s, ~180 s. A number
    that comes from a measured run is a decision; a marker that matches half the time is a
    coin flip that reads like a decision.
    """

    seconds: float


@dataclass(frozen=True)
class DesktopWhen:
    """Wait until the OCR of the frame contains any one of these phrases.

    THE PHRASES ARE THE DESKTOP'S OWN, NOT THE INSTALLER'S. The installer's window does not
    exist until the bake opens it, so there is nothing inside it to wait for - what proves
    the session is up is the panel, the menu button, the app grid. Each list below was
    measured on that image's own frame.
    """

    any_of: tuple[str, ...]


@dataclass(frozen=True)
class Bakery:
    """Everything that differs between two `linux/<distro>/install.py` files."""

    name: str
    # (member_in_iso, member_in_iso) for the kernel and initrd, or () to let the medium boot
    # itself through its own GRUB. See `_boot_args`.
    kernel: tuple[str, ...] = ()
    cmdline: str = ""
    # How the live desktop is judged ready. Either one.
    after: After | None = None
    desktop: DesktopWhen | None = None
    mem_mb: int = 4096
    cpus: int = 4
    disk_size: str = "64G"
    timeout: int = 5400
    user_var: str = ""
    password_var: str = ""
    # How the walk is expected to leave the disk. 64 MiB is the "nothing was written" floor
    # every bake here uses: a written system is gigabytes, a sparse image is kilobytes.
    min_disk_mb: int = 64

    def __post_init__(self) -> None:
        if (self.after is None) == (self.desktop is None):
            raise ValueError(
                f"{self.name}: set exactly one of `after` (a measured sleep) or "
                f"`desktop` (an OCR marker)"
            )
        if self.kernel and len(self.kernel) != 2:
            raise ValueError(
                f"{self.name}: `kernel` is (the kernel, the initrd) or empty for a medium "
                f"that boots itself - got {len(self.kernel)} member(s)"
            )

    @property
    def disk_name(self) -> str:
        return f"{self.name}.qcow2"


# --- the pieces of a bake ---------------------------------------------------------------


def _boot_args(bakery: Bakery, iso: str, work: str) -> tuple[list[str], str | None]:
    """The `-drive`/`-kernel`/`-initrd`/`-append` list, and the kernel path if one is used.

    TWO WAYS A LIVE MEDIUM BOOTS, AND THEY ARE NOT INTERCHANGEABLE.

    THE MEDIUM'S OWN GRUB (`kernel` is empty). NixOS, EndeavourOS, Manjaro, Pop!_OS and Zorin
    boot the way they boot for a person: the firmware runs the ISO's own menu entry, which is
    what sets up the live session's tmpfs and its overlay. Extracting the kernel here would
    bypass that entry and land in a session the installer does not expect.

    AN EXTRACTED KERNEL AND A MEASURED CMDLINE. Casper and dracut-based mediums (Ubuntu,
    Kubuntu, Mint, MX, Fedora, openSUSE, CachyOS) are started with the ISO's kernel pulled out
    of the image and given `-append` directly. That is not a shortcut: it is what lets the
    bake pass a cmdline the ISO's own menu does not have, and it makes the boot independent of
    which menu entry the firmware happens to highlight.

    THE CMDLINES CARRY LABELS THAT COME FROM THE ISO'S OWN GRUB CONFIG, NOT FROM A GUESS:
    `COS_202608`, `Fedora-WS-Live-43`, `openSUSE_Tumbleweed_KDE_Live`. They are constants in
    each Bakery for the same reason a coordinate is - they were read off the artifact.
    """
    args: list[str] = []
    kernel: str | None = None

    if not bakery.kernel:
        # Let the firmware run the medium's own menu. `-boot d` is not decoration: the target
        # disk is empty and a firmware's default order would find nothing bootable on it, so
        # naming the CD-ROM removes the difference between machines.
        args += ["-drive", f"file={iso},media=cdrom,readonly=on", "-boot", "d"]
        return args, None

    # POSITIONAL, NOT BY FILENAME. `kernel[0]` is the kernel and `kernel[1]` is the initrd,
    # because the filenames cannot be asked: CachyOS calls its initrd
    # `initramfs-linux-cachyos.img`, MX calls it `antiX/initrd.gz`, Fedora calls both
    # `linux` and `initrd` with no extension at all. Guessing from a substring would have
    # classified CachyOS' initramfs as a kernel.
    kpath = os.path.join(work, "vmlinuz")
    ipath = os.path.join(work, "initrd")
    if not (os.path.exists(kpath) and os.path.exists(ipath)):
        # 7z writes each member under its own basename, so `casper/vmlinuz` lands as
        # `work/vmlinuz` and `casper/initrd.lz` as `work/initrd.lz` - the second needs moving.
        for member in bakery.kernel:
            subprocess.run(
                ["7z", "e", "-y", f"-o{work}", iso, member],
                check=True, stdout=subprocess.DEVNULL,
            )
        for index, member in enumerate(bakery.kernel):
            produced = os.path.join(work, os.path.basename(member))
            wanted = kpath if index == 0 else ipath
            if os.path.exists(produced) and produced != wanted:
                os.replace(produced, wanted)
        log(bakery.name, "kernel and initrd extracted")

    args += [
        "-drive", f"file={iso},media=cdrom,readonly=on",
        "-kernel", kpath,
        "-initrd", ipath,
        "-append", bakery.cmdline,
    ]
    return args, kpath


def _wait_for_desktop(drv, bakery: Bakery, seconds: int) -> bool:
    """The live session is up. Both forms report the last frame when they fail."""
    if bakery.after is not None:
        gap = float(bakery.after.seconds)
        log(bakery.name, f"waiting {gap:.0f}s for the live desktop")
        deadline = time.time() + min(gap, seconds)
        while time.time() < deadline:
            drv.look("boot")
            time.sleep(10)
        drv.look("desktop")
        log(bakery.name, f"  screen: {drv.text()[:120]}")
        return True

    assert bakery.desktop is not None
    wanted = [w.lower() for w in bakery.desktop.any_of]
    log(bakery.name, "waiting for the live desktop")
    deadline = time.time() + seconds
    while time.time() < deadline:
        drv.look("boot")
        joined = drv.text()
        if any(w in joined for w in wanted):
            log(bakery.name, "the live session is up")
            return True
        time.sleep(10)
    log(bakery.name, "=== THE LIVE DESKTOP DID NOT APPEAR ===")
    log(bakery.name, f"  last screen: {' | '.join(w[0] for w in drv.words[:24])}")
    return False


def run_bake(bakery: Bakery, args) -> int:
    """One bake, start to finish. This is the whole of what the twelve files repeated."""
    import qmp as Q

    if not os.path.isfile(args.iso):
        raise SystemExit(f"=== NO ISO AT {args.iso} ===")
    log(bakery.name, f"iso {args.iso} ({os.path.getsize(args.iso) / 2**30:.2f} GiB)")

    work = args.work or os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)

    disk = os.path.join(work, bakery.disk_name)
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(bakery.name, f"disk created: {args.disk_size}")

    boot_args, _kernel = _boot_args(bakery, args.iso, work)

    guest = vm.Guest(
        work,
        extra=["-drive", f"file={disk},if=virtio,format=qcow2,discard=unmap", *boot_args],
        # UEFI, ALWAYS, AND THERE IS NO BIOS PATH. See lib/uefi.py: Garuda refuses Legacy
        # mode outright, and the disks these installers produce on the BIOS path are MBR/GPT
        # with a BIOS boot partition instead of the ESP their own images expect.
        shape=uefi.shape_with_uefi(
            vm.Shape(
                cpus=args.cpus, mem_mb=args.mem, machine="q35", vga="std",
                vnc_bind=os.environ.get("VNC_BIND", "127.0.0.1"),
            ),
            work, bakery.name,
        ),
        stdout=os.path.join(work, "console.log"),
    )

    def _stop(signum: int, _frame) -> None:
        log(bakery.name, f"signal {signum}: stopping the guest")
        try:
            guest.stop(acpi_first=False)
        except Exception:                              # noqa: BLE001
            pass
        os._exit(143)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _stop)

    # THE RECORDING RUNS WHILE THE BAKE DOES, so a cancelled run still uploads something.
    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    guest.start()
    log(bakery.name,
        f"qemu pid {guest.pid}, vnc {guest.shape.vnc_bind}:{5900 + guest.shape.vnc_display}")

    snap = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
         guest.paths.record, frames, os.environ.get("SNAPSHOT_INTERVAL", "2")],
        stdout=open(os.path.join(frames, "snapshots.log"), "wb"),
        stderr=subprocess.STDOUT,
    )
    art = live.start([frames], os.path.join(work, "install.mp4"),
                     os.path.join(work, "last-frame.png"),
                     every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "30")))

    ok = False
    try:
        # THE BAKE'S OWN DRIVER IS CLOSED BEFORE THE WALK OPENS ITS OWN. qemu serves one
        # client per socket, so `steps.run_file` cannot connect while this one holds it.
        drv = steps.Driver(guest.paths.drive, work)
        try:
            ready = _wait_for_desktop(drv, bakery, args.timeout)
        finally:
            drv.close()

        if ready:
            walk_path = os.path.join(REPO, "linux", bakery.name, "steps", "install.yaml")
            log(bakery.name, f"=== the walk: {os.path.basename(walk_path)} ===")
            ok, ran = steps.run_file(
                walk_path, guest.paths.drive, work=work,
                user=args.user, password=args.password,
                disk_path=disk,
            )
            log(bakery.name,
                f"{len(ran)} step(s) ran" if ok
                else f"the walkthrough failed after {len(ran)} step(s)")
    finally:
        # THE POST-MORTEM FRAME, EVEN WHEN THE WALK RAISED. A failed run's most useful
        # artifact is the last thing the screen showed, and the `finally` is the only place
        # that holds whether the failure was an exception or a `False`.
        try:
            ppm = os.path.join(work, "final.ppm")
            c = Q.QMP(guest.paths.drive)
            try:
                c.execute("screendump", filename=ppm)
            finally:
                c.close()
            lastframe.convert(ppm, os.path.join(work, "last-frame.png"))
        except Exception:                              # noqa: BLE001
            pass
        guest.stop(acpi_first=False)
        snap.terminate()
        art.stop()

    # A DISK WITH NOTHING ON IT IS NOT A SUCCESS.
    if ok and os.path.exists(disk):
        size_mb = os.path.getsize(disk) >> 20
        if size_mb < bakery.min_disk_mb:
            log(bakery.name,
                f"=== THE DISK IS EMPTY ({size_mb} MiB) - THE INSTALL DID NOT HAPPEN ===")
            ok = False

    if ok and os.path.exists(disk):
        log(bakery.name, f"compressing {disk} -> {args.out}")
        compress.compress(disk, args.out)
        log(bakery.name, f"{args.out}: {os.path.getsize(args.out) >> 20} MiB")
    return 0 if ok else 1


def cli(bakery: Bakery, argv: list[str]) -> int:
    """The argument parser, which is the same for every distribution.

    `--user` and `--password` read an environment variable named after the distribution when
    one is set (`MINT_USER`, `MINT_PASSWORD`), because the workflows pass them that way - a
    bake that ignored the environment would install an account the ssh stage cannot log in
    as.
    """
    prefix = bakery.name.upper().replace("-", "_")
    p = argparse.ArgumentParser(description=f"Install {bakery.name} from its Live ISO.")
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=None)
    p.add_argument("--disk-size", default=bakery.disk_size)
    p.add_argument("--mem", type=int, default=bakery.mem_mb)
    p.add_argument("--cpus", type=int, default=bakery.cpus)
    p.add_argument("--timeout", type=int, default=bakery.timeout)
    p.add_argument("--user", default=os.environ.get(f"{prefix}_USER", "docker"))
    p.add_argument("--password", default=os.environ.get(f"{prefix}_PASSWORD", "admin"))
    args = p.parse_args(argv)
    return run_bake(bakery, args)
