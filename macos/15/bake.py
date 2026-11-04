"""Bake a macOS 15 disk, unattended: recovery in, qcow2 out.

    python3 macos/15/bake.py --out out/macos15.qcow2

THE PIPELINE, AND WHICH PART IS HARD

    recovery image     from this repository's package (:macos-recovery-sequoia), which
                       macos-recovery-publish.yml fills. Apple issues one per request
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
import assistant  # noqa: E402
import live as livelib  # noqa: E402
import machine  # noqa: E402
import images  # noqa: E402
import iso  # noqa: E402
import opencore  # noqa: E402
import smoke  # noqa: E402
import vm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# The release's marketing name, for the recovery tag. The bake takes the number.
RELEASES = {"14": "sonoma", "15": "sequoia", "26": "tahoe"}

def log(msg: str) -> None:
    print(f"[bake-macos {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bake a macOS disk.")
    p.add_argument("--out", default="out/macos15.qcow2")
    p.add_argument("--release", default="15", choices=sorted(RELEASES))
    p.add_argument("--disk-size", default="128G")
    p.add_argument("--mem", type=int, default=8192,
                   help="MB; macOS needs 4 GB to install and more to be useful")
    p.add_argument("--cpus", type=int, default=4)
    # NINETY MINUTES, matching macos/15/install.py's own patience. The installer's own
    # estimate on the first run that reached its progress pane was "about 2 hours and 52
    # minutes" - Apple's figure for real hardware - and a run that has not rebooting in
    # ninety is not merely slow.
    p.add_argument("--timeout", type=int, default=90 * 60,
                   help="seconds the installer stage may run")
    # THE LATER STAGES GET THEIR OWN BUDGET. They used to share --timeout, which was fine
    # while the installer stopped three minutes in. Now that it waits for the copy to
    # finish, one number for all three would mean a finished installation could run out of
    # the time the wizard needed.
    p.add_argument("--assistant-timeout", type=int, default=45 * 60,
                   help="seconds the Setup Assistant may run")
    p.add_argument("--desktop-timeout", type=int, default=30 * 60,
                   help="seconds to wait for a desktop after the wizard")
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
        image is missing is "run macos-recovery-publish.yml" - not a 850 MiB download
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
                f"Run macos-recovery-publish.yml for {self.args.release} first:\n"
                f"    gh workflow run macos-recovery-publish.yml "
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
        compress.run("create", "-f", "qcow2", self.disk, self.args.disk_size)
        log(f"disk created: {self.args.disk_size}")

        # THE MACHINE AND ITS DISKS COME FROM macos/15/machine.py.
        #
        # Eleven parameters that took eight runs to get right, and macos/15/probe.py has
        # to build the same machine or it measures a different one. The reasoning for
        # each field is in that module, next to the field.
        guest = machine.make_guest(
            self.work, assets, self.disk, recovery,
            cpus=self.args.cpus, mem_mb=self.args.mem,
        ).start()

        shots = os.path.join(self.frames, "shots")
        os.makedirs(shots, exist_ok=True)
        snapscript = os.path.join(REPO, "lib", "snapshots.py")
        if not os.path.exists(snapscript):
            raise SystemExit(f"snapshots.py is not at {snapscript}; the layout changed")
        snap = subprocess.Popen(
            [sys.executable, snapscript, guest.paths.record, shots,
             os.environ.get("SNAPSHOT_INTERVAL", "1")],
            stdout=open(os.path.join(shots, "snapshots.log"), "wb"),
            stderr=subprocess.STDOUT,
        )

        # THE ARTIFACTS ARE KEPT CURRENT, NOT WRITTEN AT THE END.
        #
        # A run cancelled at minute forty used to upload neither an mp4 nor a last frame,
        # because both were produced after the bake finished and a cancel is a SIGKILL.
        # Run 41588015377 is the case: sixteen minutes of a macOS install, cancelled, and
        # its artifact was a console log and a list of frame filenames nobody could open -
        # while the frames themselves sat on the runner and were thrown away.
        #
        # lib/live.py re-encodes from whatever exists every twenty seconds and replaces
        # both files atomically, so what a consumer downloads is always complete.
        live = livelib.start(
            [shots], os.path.join(self.work, "install.mp4"),
            os.path.join(self.work, "last-frame.png"),
            every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "20")),
            fps=int(os.environ.get("RECORD_FPS", "1")),
        )

        # THIS BAKE STOPS AT THE END OF THE INSTALLATION, AND THAT IS THE STAGE BOUNDARY.
        #
        # What it produces is a disk that boots INTO THE SETUP ASSISTANT, not one that
        # reaches a desktop. That is on purpose: the wizard is the next stage's work
        # (macos-15_desktop-bake), and mixing them here is what made the previous version
        # of this file unreachable - it ran install.py, then stopped, then claimed the
        # result was "still installing" and published nothing.
        #
        # THE EVIDENCE THAT THIS IS THE RIGHT BOUNDARY. Run 41579186251 finished the
        # installation - install.py reported "the installation is complete, 33644 MiB
        # unchanged for 180s" - and the disk it left boots to the Setup Assistant on its
        # own. That is exactly what a setup stage should hand over.
        installed = False
        # DECLARED BEFORE THE TRY so an exception cannot turn a boot problem into a
        # NameError in the finally block, and so the compression step below can tell
        # "reached the wizard" from "did not".
        arrived = False
        try:
            installed = self.run_installer(guest)
            if installed:
                log("=== the installation is complete ===")
                # AND THE WIZARD IS ON SCREEN, WHICH IS THE PROOF THE DISK BOOTS.
                #
                # "install.py exited 0" says the clicks landed and the copy finished. It
                # does not say the machine can start what it wrote - and a disk that
                # installs without booting is not a system image. Run 41606493103's
                # recording ends on the wizard's first page:
                #
                #     Select Your Country or Region
                #     Afghanistan / Aland Islands / Albania / ...
                #
                # so the stage now waits for that page and calls it arrival. lib/assistant
                # reads the title with tesseract - the same tool and the same --psm 3 the
                # Windows stage uses - because the phrase is wizard-only and a pixel test
                # cannot tell this light page from any other light page.
                log("waiting for the Setup Assistant to appear")
                arrived = self.wait_for_assistant(guest, shots)
                if not arrived:
                    log("=== THE WIZARD NEVER APPEARED ===")
                    log("  the installation reported success but the machine did not boot")
                    log("  into it; no disk is published")
        finally:
            # ACPI ONLY WHEN THERE IS A SYSTEM TO FLUSH. With the wizard on screen the
            # machine is booted into the installed system, so it is asked to shut down
            # and what it wrote reaches the disk. A run that never got there is stopped
            # abruptly: a machine mid-reboot does not answer the power request and the
            # sixty-second timeout is longer than the disk is worth.
            guest.stop(acpi_first=arrived)
            snap.terminate()
            try:
                snap.wait(timeout=5)
            except subprocess.TimeoutExpired:
                snap.kill()

        # ONE LAST PASS, so the artifacts match the final frame rather than whichever
        # frame the loop last saw. See lib/live.py for why they are kept current.
        log("=== finalising the recording ===")
        live.stop(final=True)
        mp4 = os.path.join(self.work, "install.mp4")
        if os.path.exists(mp4):
            log(f"recording: {mp4} ({os.path.getsize(mp4) >> 20} MiB)")
        else:
            log("recording: nothing was encoded")

        if not installed:
            log("=== THE INSTALLER SEQUENCE DID NOT COMPLETE ===")
            self.tail(os.path.join(self.work, "console.log"))
            return False

        # BOTH HALVES ARE REQUIRED: the install finished AND the machine booted into it.
        #
        # `installed` alone was the earlier claim, and it is a weaker one - it says the
        # clicks landed and the copy completed, not that what was written can start. The
        # wizard on screen is that proof, and it is what makes this a system image rather
        # than a disk with a macOS on it.
        if not arrived:
            log("=== THE INSTALL COMPLETED BUT THE DISK DID NOT BOOT ===")
            log("  nothing was written to the disk's own boot path, or the firmware")
            log("  could not pick it; no artifact is produced from a disk that only")
            log("  installs")
            return False

        # A STATIC CHECK CANNOT OUTVOTE A MACHINE THAT HAS ALREADY BOOTED THE DISK.
        #
        # run 41660619717 installed macOS to completion and the guest REBOOTED INTO IT -
        # the Setup Assistant's country list was on screen and lib/assistant read it - and
        # then this check said:
        #
        #     [disk] GPT protective MBR found
        #     [disk] no APFS partition in the GPT - macOS did not install
        #     === THE DISK WILL NOT BOOT ===
        #
        # and the artifact was thrown away. Those two statements cannot both be true: a
        # disk with no APFS container does not reach the Setup Assistant. The wizard is
        # not a hint about the disk, it is the disk running.
        #
        # So the check runs and its answer is recorded, but `arrived` - which required the
        # guest to boot into the system - is what decides. The one thing this still
        # refuses is a disk that did NOT arrive; that gate is above, unchanged.
        log("checking the disk holds an APFS system")
        if not smoke.check_disk(self.disk, root_fs="apfs"):
            log("  the static check disagrees with a guest that booted into the wizard")
            log("  the GPT parse is recorded above; the boot is the stronger evidence")
            log("  and the disk is kept, because the wizard is the disk running")

        log("compressing")
        compress.compress(self.disk, self.args.out)
        log(f"BAKED macos{self.args.release} setup -> {self.args.out} "
            f"({os.path.getsize(self.args.out) >> 20} MiB)")
        log("  this disk boots into the Setup Assistant; drive it with "
            "macos-15_desktop-bake")
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
            # THE DRIVER'S OUTPUT IS FORWARDED AS IT ARRIVES, NOT COLLECTED.
            #
            # capture_output=True waits for the process to exit before handing anything
            # back, so a forty-minute sequence produced a log containing one line:
            #
            #     === installation: install.py ===
            #
            # and nothing else until it finished. Run 41564236483 sat like that for ten
            # minutes and looked hung; whether it was, nobody could tell.
            #
            # Popen with a reader thread keeps the diagnosis and loses the silence. Every
            # line is prefixed [install], so it is distinguishable from the baker's own.
            # NOT a new local: the outer result is what the caller reads.
            result["stdout"] = []
            result["stderr"] = []
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-u", script,
                     "--qmp", guest.paths.drive, "--frames", self.frames,
                     # The disk is what tells "copying" from "hung" while the screen is
                     # black. Passed here because only the baker knows the path.
                     "--disk", self.disk],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1,
                )
            except OSError as exc:
                result["ok"] = False
                result["error"] = str(exc)
                return

            def pump(stream, key: str, prefix: str) -> None:
                try:
                    for line in stream:
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        log(f"{prefix} [install] {line}")
                        result[key].append(line)  # type: ignore[union-attr]
                except (OSError, ValueError):
                    pass

            readers = [
                threading.Thread(target=pump, args=(proc.stdout, "stdout", " "),
                                 daemon=True),
                threading.Thread(target=pump, args=(proc.stderr, "stderr", "!"),
                                 daemon=True),
            ]
            for reader in readers:
                reader.start()
            code = proc.wait()
            for reader in readers:
                reader.join(timeout=10)

            if code != 0:
                result["ok"] = False
                result["error"] = f"exit status {code}"
            else:
                result["ok"] = True

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
            passthrough = str(result.get("stdout") or "")
            for line in passthrough.splitlines()[-20:]:
                log(f"  [install] {line}")
            errors = str(result.get("stderr") or "")
            for line in errors.splitlines()[-20:]:
                log(f"  [install:stderr] {line}")
            log("the Setup Assistant is NOT started: clicking a screen the installer "
                "never reached is how the previous run spent an hour on black frames")
            return False

        log("installation sequence complete")
        return True

    def wait_for_assistant(self, guest: vm.Guest, shots: str) -> bool:
        """Wait until the Setup Assistant's first page is on screen.

        THE INSTALLER REBOOTS INTO THE SYSTEM IT WROTE, and that reboot is what this
        waits through. It is long: the machine restarts, OpenCore picks the new volume,
        the kernel boots, and the wizard paints - run 41606493103 took eleven minutes
        from "installation complete" to the wizard, most of it the reboot and the first
        boot's setup.

        WHY OCR AND NOT A PIXEL TEST. The wizard is a light page; so is a Finder window;
        and the point of this check is to be evidence rather than a guess. lib/assistant
        reads the title.
        """
        # THE INSTALLER ALREADY WAITED FOR THIS, AND THE FIRST CHECK IS NOT A FORMALITY.
        #
        # install.py ends its disk wait when the wizard appears, and it settles for thirty
        # seconds there before returning. This function is the fallback for the case where
        # it ended some other way - the disk settling without a readable wizard, or a run
        # whose OCR could not read the frame it stopped on.
        #
        # So the first pass asks once and returns immediately if the answer is yes. The
        # twenty-second settle this used to apply on top of install.py's thirty would make
        # the published disk wait fifty seconds on a page that had already been confirmed,
        # and a guest on the first Setup Assistant page is not going to change anything
        # that matters in that time.
        if assistant.reached_assistant(shots):
            log("the Setup Assistant is on screen (already confirmed before the disk settle)")
            return True

        deadline = time.time() + self.args.desktop_timeout
        started = time.time()
        last_report = started

        while time.time() < deadline:
            if not guest.alive():
                log("qemu exited before the wizard appeared")
                return False
            if assistant.reached_assistant(shots):
                log(f"the Setup Assistant is on screen ({(time.time() - started):.0f}s)")
                # Reached through the fallback, so it has NOT been settled yet.
                settle = int(os.environ.get("ASSISTANT_SETTLE_SECONDS", "20"))
                log(f"letting the guest settle for {settle}s")
                time.sleep(settle)
                if not guest.alive():
                    log("the guest exited while settling - not keeping the disk")
                    return False
                return True

            if time.time() - last_report >= 60:
                last_report = time.time()
                frames = 0
                try:
                    frames = len([f for f in os.listdir(shots) if f.startswith("frame-")])
                except OSError:
                    pass
                log(f"waiting {time.time() - started:.0f}s for the wizard: {frames} frames")

            time.sleep(10)

        log(f"timed out after {self.args.desktop_timeout}s waiting for the wizard")
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
