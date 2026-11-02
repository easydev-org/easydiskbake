"""Bake a macOS 15 disk, unattended: recovery in, qcow2 out.

    python3 macos/15/bake.py --out out/macos15.qcow2

THE PIPELINE, AND WHICH PART IS HARD

    recovery image     from this repository's package (:macos-recovery-sequoia), which
                       publish-macos-recovery.yml fills. Apple issues one per request
                       and lib/macos_recovery.py drives that exchange; the bake does
                       not talk to Apple.
    OpenCore           lib/opencore.py: the four pinned assets from ghcr, the identity,
                       the config, Apple's APFS driver taken from the recovery image,
                       and the FAT boot image the firmware starts.
    install            macos/15/install.py: Disk Utility erases the target, then the
                       installer runs. Coordinates, not OCR.
    Setup Assistant    macos/15/drive.py: the wizard, also by coordinates.
    arrival            a desktop, judged by the same wallpaper test the Windows bake
                       uses (lib/desktop.py) - the wizard's last pane is a blue gradient
                       and so is the desktop, but only the desktop fills the screen.
    shutdown           ACPI, so macOS flushes the disk.

WHAT IS MACOS-SPECIFIC AND LIVES HERE

    the SMC device     isa-applesmc with Apple's key, without which macOS refuses to
                       believe it is on Apple hardware
    the firmware pair  OVMF code plus a writable vars copy; the GOP resolution is baked
                       into the template and the coordinates are measured at 1920x1080
    the disk           virtio-blk, which macOS sees through the recovery image's own
                       drivers - no driver CD, unlike Windows

WHY THERE IS NO SECOND BOOT

dockur-macos installs, then on a later run patches an APFS driver into the boot image
taken from the installed disk, because before the first boot that file does not exist.
This bake takes it from the recovery image instead, which is on disk from the start -
see lib/opencore.install_apfs_driver. So the disk that installs is the disk that boots,
with nothing to reconcile between the two.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import compress  # noqa: E402
import desktop  # noqa: E402
import images  # noqa: E402
import iso  # noqa: E402
import opencore  # noqa: E402
import smoke  # noqa: E402
import vm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# The release's marketing name, for the recovery tag. The bake takes the number.
RELEASES = {"14": "sonoma", "15": "sequoia", "26": "tahoe"}

# Apple's SMC key, obfuscated the way every hackintosh does it. This is not a secret or
# a credential: it is the value macOS reads from the SMC device to decide whether the
# firmware is Apple's, and it is in OSX-KVM, in dockur/macos and in Apple's own
# diagnostic tooling.
OSK_PLAIN = "ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc"


def log(msg: str) -> None:
    print(f"[bake-macos {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bake a macOS disk.")
    p.add_argument("--out", default="out/macos15.qcow2")
    p.add_argument("--release", default="15", choices=sorted(RELEASES))
    p.add_argument("--disk-size", default="64G")
    p.add_argument("--mem", type=int, default=8192,
                   help="MB; macOS needs 4 GB to install and more to be useful")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--timeout", type=int, default=5400,
                   help="seconds the installation may run before Setup Assistant")
    p.add_argument("--user", default=os.environ.get("MACOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MACOS_PASSWORD", "admin"))
    p.add_argument("--keep-work", action="store_true")
    return p.parse_args(argv)


class Baker:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.work = os.path.dirname(os.path.abspath(args.out)) or "."
        self.frames = os.path.join(self.work, "frames")
        self.disk = os.path.join(self.work, "disk.qcow2")
        os.makedirs(self.frames, exist_ok=True)

    # -- inputs ---------------------------------------------------------------

    def fetch_recovery(self) -> str:
        """The installer, from this repository's package.

        NO FALLBACK TO APPLE. A bake that silently fetched its own recovery image would
        be a second path with its own failure modes, and the useful message when the
        image is missing is "run publish-macos-recovery.yml" - not a 850 MiB download
        that hides the real problem until it fails later, somewhere else.

        The file name carries the release because a work directory can hold more than
        one and opencore.recovery_image() picks the first .dmg it finds.
        """
        name = RELEASES[self.args.release]
        dest = os.path.join(self.work, f"recovery-{name}.dmg")
        tag = f"macos-recovery-{name}"

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            log(f"{dest} already present ({os.path.getsize(dest) >> 20} MiB)")
            return dest

        log(f"pulling {tag} from ghcr")
        try:
            return iso.fetch_tag(tag, dest)
        except iso.IsoError as exc:
            raise SystemExit(
                f"{tag} is not in this repository's ghcr package: {exc}\n\n"
                f"Run publish-macos-recovery.yml for {self.args.release} first:\n"
                f"    gh workflow run publish-macos-recovery.yml "
                f"-f releases={self.args.release}\n"
                f"This bake does not request a recovery image from Apple itself. A "
                f"second source would be a second thing to verify, and the failure it "
                f"hides is exactly the one worth seeing."
            ) from exc

    def build_boot_assets(self) -> dict:
        """OpenCore, the firmware pair, the identity and boot.img.

        lib/opencore.py does all of it from the pins and the recovery image. This is a
        thin call because every decision inside it is documented there - the merge, the
        config edit, and the APFS driver that has to come out of the installer.
        """
        log("=== OpenCore, firmware, APFS driver ===")
        try:
            return opencore.build(self.args.release, self.work)
        except opencore.OpenCoreError as exc:
            raise SystemExit(f"could not build the boot assets: {exc}") from exc

    # -- the machine ----------------------------------------------------------

    def run(self) -> bool:
        log(f"out={self.args.out} release={self.args.release}")
        recovery = self.fetch_recovery()
        assets = self.build_boot_assets()
        log(f"identity {assets['identity']['serial']} / {assets['identity']['mac']}")

        if os.path.exists(self.disk):
            os.unlink(self.disk)
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", self.disk, self.args.disk_size],
            check=True,
        )
        log(f"disk created: {self.args.disk_size}")

        shape = vm.Shape(
            cpus=self.args.cpus,
            mem_mb=self.args.mem,
            machine="q35",
            firmware_code=assets["rom"],
            firmware_vars=assets["vars"],
            vga="vmware",
            # macOS reads the CPU's family and model out of CPUID and refuses to boot
            # on a model it does not recognise, so "host" - which every other guest
            # here uses - is not an option. Penryn is the oldest model the Sequoia
            # kernel still runs on, and it is what dockur/macos uses for exactly this.
            cpu="Penryn,kvm=on,vendor=GenuineIntel,+invtsc,+ssse3,+sse4.2,"
                "+popcnt,+avx,+aes,+xsave,+xsaveopt,check",
            # ICH9 emulates suspend-to-RAM and suspend-to-disk, and macOS asks for both
            # during boot on real hardware. Under this firmware the transitions hang
            # the guest at a black screen with nothing in the log.
            extra_args=[
                "-global", "ICH9-LPC.disable_s3=1",
                "-global", "ICH9-LPC.disable_s4=1",
            ],
        )

        # THE DISK ORDER IS THE DEVICE ORDER MACOS SEES.
        #
        #   boot.img   OpenCore, as a raw IDE disk. It has to be first: the firmware
        #              finds no OS on an empty pflash, falls to the first disk, and
        #              BOOT/BOOTx64.efi is what it starts.
        #   disk.qcow2 the target, virtio-blk. macOS has a virtio driver in its own
        #              recovery image, so unlike Windows this needs no driver CD.
        #   recovery   the installer, as USB media. macOS will not install from a
        #              virtio disk whose contents it must read *before* it has a
        #              filesystem driver for it, and it treats a USB volume as a
        #              bootable installer exactly as it does on real hardware.
        drives = [
            "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
            "-device", "ide-hd,bus=ide.0,drive=oc",
            "-drive", f"file={self.disk},if=virtio,format=qcow2",
            "-drive", f"file={recovery},media=disk,if=none,id=rec,readonly=on",
            "-device", "usb-storage,bus=xhci.0,drive=rec",
            "-device", f"isa-applesmc,osk={OSK_PLAIN}",
        ]

        console = os.path.join(self.work, "console.log")
        guest = vm.Guest(self.work, extra=drives, shape=shape, stdout=console).start()

        shots = os.path.join(self.frames, "shots")
        os.makedirs(shots, exist_ok=True)
        snapscript = os.path.join(REPO, "lib", "snapshots.py")
        if not os.path.exists(snapscript):
            raise SystemExit(f"snapshots.py is not at {snapscript}; the layout changed")
        snap = subprocess.Popen(
            [sys.executable, snapscript, guest.paths.record, shots,
             os.environ.get("SNAPSHOT_INTERVAL", "5"), "2000"],
            stdout=open(os.path.join(shots, "snapshots.log"), "wb"),
            stderr=subprocess.STDOUT,
        )

        # THE TWO DRIVERS RUN IN SEQUENCE, NOT CONCURRENTLY.
        #
        # install.py takes the guest from Recovery to Setup Assistant and ends when the
        # installer reboots. drive.py starts at Setup Assistant. Running both at once
        # would put clicks into a screen neither is looking at - which is the failure
        # mode of every "just click everything" driver.
        arrived = False
        try:
            installed = self.run_installer(guest)

            if installed:
                log("=== Setup Assistant ===")
                self.run_setup_assistant(guest)
                arrived = self.wait_for_desktop(guest)
        finally:
            guest.stop(acpi_first=arrived)
            snap.terminate()
            try:
                snap.wait(timeout=5)
            except subprocess.TimeoutExpired:
                snap.kill()

        log("=== encoding the recording ===")
        mp4 = os.path.join(self.work, "install.mp4")
        if vm.record_many(mp4, shots, fps=int(os.environ.get("RECORD_FPS", "1"))):
            log(f"recording: {mp4} ({os.path.getsize(mp4) >> 20} MiB)")
        else:
            log("recording: nothing to encode")

        if not arrived:
            log("=== THE GUEST DID NOT REACH A DESKTOP ===")
            self.tail(console)
            return False

        log("the guest reached its desktop; checking the disk")
        if not smoke.check_disk(self.disk, root_fs="apfs"):
            log("=== THE DISK FAILED ITS CHECKS; refusing to produce an artifact ===")
            return False

        log("compressing")
        compress.compress(self.disk, self.args.out)
        log(f"BAKED macos{self.args.release} -> {self.args.out} "
            f"({os.path.getsize(self.args.out) >> 20} MiB)")
        return True

    def run_installer(self, guest: vm.Guest) -> bool:
        """Recovery -> the reboot into the installed system, via macos/15/install.py.

        THE EXIT CODE IS THE RESULT. install.py returns non-zero when it gave up - a
        black screen that outlasted its patience, or an installer that never rebooted -
        and the first version ignored that: it treated "the script exited" as "the
        installation finished" and handed a black screen to the Setup Assistant driver.
        """
        script = os.path.join(HERE, "install.py")
        log(f"=== installation: {os.path.basename(script)} ===")
        # A thread because the guest must keep running while the sequence plays out,
        # and because a crash in the driver has to surface rather than hang a 90-minute
        # job. The timeout is the bake's own; install.py has its own patience inside it.
        result: dict[str, object] = {}

        def drive() -> None:
            try:
                subprocess.run(
                    [sys.executable, script,
                     "--qmp", guest.paths.drive, "--frames", self.frames,
                     # The disk is what tells "copying" from "hung" while the screen is
                     # black. Passed here because only the baker knows the path.
                     "--disk", self.disk],
                    check=True,
                )
                result["ok"] = True
            except subprocess.CalledProcessError as exc:
                result["ok"] = False
                result["error"] = f"exit status {exc.returncode}"
            except OSError as exc:
                result["ok"] = False
                result["error"] = str(exc)

        thread = threading.Thread(target=drive, daemon=True)
        thread.start()

        deadline = time.time() + self.args.timeout
        while thread.is_alive() and time.time() < deadline:
            if not guest.alive():
                log("qemu exited during installation")
                return False
            time.sleep(15)

        thread.join(timeout=30)
        if thread.is_alive():
            log(f"the installer driver did not finish within {self.args.timeout}s")
            return False
        if not result.get("ok"):
            log(f"the installer driver failed: {result.get('error')}")
            log("the Setup Assistant is NOT started: clicking a screen the installer "
                "never reached is how the previous run spent an hour on black frames")
            return False

        log("installation sequence complete")
        return True

    def run_setup_assistant(self, guest: vm.Guest) -> None:
        """The wizard, by coordinates, after the installer's reboot.

        A wait first: the new system boots, draws the Apple logo, and takes a couple of
        minutes to reach the first pane. drive.py has its own 120s wait for exactly
        this, and adding to it here would only make the two disagree.
        """
        script = os.path.join(HERE, "drive.py")
        log(f"=== Setup Assistant: {os.path.basename(script)} ===")
        try:
            subprocess.run(
                [sys.executable, script,
                 "--qmp", guest.paths.drive, "--frames", self.frames,
                 "--user", self.args.user, "--password", self.args.password],
                check=True,
                timeout=self.args.timeout,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            log(f"the Setup Assistant driver failed: {exc}")

    def wait_for_desktop(self, guest: vm.Guest) -> bool:
        """Wait until a macOS desktop is on screen.

        THE SAME WALLPAPER TEST AS THE WINDOWS BAKE, and deliberately not a new one.
        lib/desktop.py explains its three thresholds and why a colour match alone does
        not work. Sequoia's default wallpaper is a blue-to-purple gradient with the
        light end at the top, which is the shape those thresholds describe.

        WHAT THIS DEPENDS ON: that the wizard has finished. While it is up, the screen
        is a white pane and fails all three tests - correctly, because it is not a
        desktop. drive.py's last action is the one that dismisses the final pane.
        """
        deadline = time.time() + self.args.timeout
        started = time.time()
        last_report = started
        shots = os.path.join(self.frames, "shots")

        while time.time() < deadline:
            if not guest.alive():
                log("qemu exited before a desktop appeared")
                return False

            if desktop.reached_desktop(shots):
                elapsed = time.time() - started
                log(f"a macOS desktop is on screen ({elapsed:.0f}s)")

                # Same settle rule as Windows: a desktop is when the shell has drawn,
                # not when the disk is quiet. macOS writes hard for the first minute -
                # mds, the login item registry, the first Time Machine snapshot.
                settle = int(os.environ.get("DESKTOP_SETTLE_SECONDS", "30"))
                log(f"letting the guest settle for {settle}s before stopping it")
                time.sleep(settle)

                # Only the machine dying invalidates the result. Re-checking the
                # screen here would read a display sleep as a crash - the exact bug
                # the Windows bake had and fixed.
                if not guest.alive():
                    log("the guest exited while settling - not keeping the disk")
                    return False
                log("still running after settling; keeping the disk")
                return True

            if time.time() - last_report >= 60:
                last_report = time.time()
                frames = 0
                try:
                    frames = len([f for f in os.listdir(shots) if f.startswith("frame-")])
                except OSError:
                    pass
                log(f"waiting {time.time() - started:.0f}s: {frames} frames")

            time.sleep(5)

        log(f"timed out after {self.args.timeout}s")
        return False

    def tail(self, path: str, lines: int = 30) -> None:
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            log(f"(no console log at {path})")
            return
        for line in text.splitlines()[-lines:]:
            print(f"[console] {line}", flush=True)


def main(argv: list[str]) -> int:
    args = parse(argv)
    return 0 if Baker(args).run() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
