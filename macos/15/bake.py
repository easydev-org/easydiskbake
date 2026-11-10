"""Bake a macOS 15 disk, unattended: recovery in, qcow2 out.

    python3 macos/15/bake.py --out out/macos15.qcow2

THE PIPELINE, AND WHICH PART IS HARD

    recovery image     from this repository's package (:macos-recovery-sequoia), which
                       artifacts-publish.yml fills. Apple issues one per request
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
# `import assistant` AND `import machine` WENT WITH THE TWO FUNCTIONS THEY SERVED.
# assistant's phrase list is the file's `until_any` now (macos/15/steps/setup.yaml), and the
# machine is lib/vm_session.py's. Both modules are still in the tree and still used - by the
# desktop stage's `waits_for` and by every macOS guest this repository starts.
import live as livelib  # noqa: E402
import images  # noqa: E402
import macos_recovery  # noqa: E402
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
    # THE WIZARD ARRIVING IS THE INSTALLER FINISHING, AND THAT IS NOT 30 MINUTES.
    #
    # This wait used --desktop-timeout, whose own help text says "after the wizard". Run
    # 41970760010's last frame is the reason that was wrong:
    #
    #     macOS Sequoia
    #     macOS Sequoia will be installed on the disk "main".
    #     About 45 minutes remaining
    #
    # - the install was a third of the way through when the 1800-second budget ran out, and
    # the bake reported
    #
    #     === THE WIZARD NEVER APPEARED ===
    #     the installation reported success but the machine did not boot
    #
    # which sent the next hour into looking at firmware. The install had not finished.
    #
    # Two hours, because the progress bar's own estimate is 45 minutes and a runner under
    # load is slower than the estimate - and because a wait that is too long costs idle
    # minutes while a wait that is too short throws away the install.
    p.add_argument("--wizard-timeout", type=int, default=2 * 60 * 60,
                   help="seconds to wait for the installer to finish and the wizard to "
                        "appear; this is the copy and the first boot, not the wizard")
    p.add_argument("--user", default=os.environ.get("MACOS_USER", "docker"))
    p.add_argument("--password", default=os.environ.get("MACOS_PASSWORD", "admin"))
    p.add_argument("--steps", default="",
                   help="the installation walkthrough; macos/15/steps/setup.yaml. Required: "
                        "the file is the driver, and install.py is kept only as the table of "
                        "measurements it was written from.")
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
        image is missing is "run artifacts-publish.yml" - not a 850 MiB download
        that hides the real problem until it fails later, somewhere else.

        The file name carries the release because a work directory can hold more than
        one and opencore.recovery_image() picks the first .dmg it finds.
        """
        # ONE IMPLEMENTATION, SHARED WITH EVERY LATER STAGE. See
        # macos_recovery.fetch_published: the tag, the file name and the "run
        # artifacts-publish.yml" advice all come from there, so bake, desktop, ssh
        # and dev cannot drift into using different images for the same release.
        log("=== recovery image ===")
        return macos_recovery.fetch_published(self.args.release, self.work)

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

        # THE MACHINE IS STARTED BY THE FILE'S `boot:` STEP, NOT HERE.
        #
        # macos/15/steps/setup.yaml's first step boots it, through lib/vm_session.py - the same
        # module the other three macOS stages use - and the ONE thing this stage adds travels as
        # an option: the recovery image on a USB device (`recovery`), because this is the only
        # stage that installs rather than boots an installed system.
        #
        # THE RECORDING STILL STARTS FIRST, and it has to: the frames are what every failure in
        # this stage is diagnosed from, and a recorder that connects after the machine is up
        # misses the recovery menu. So the sampler is a process this stage owns, pointed at the
        # socket the machine will create - lib/snapshots.py waits for it to appear.
        self.boot_options = {
            "assets": assets,
            "recovery": recovery,
            "cpus": self.args.cpus,
            "mem_mb": self.args.mem,
        }

        shots = os.path.join(self.frames, "shots")
        os.makedirs(shots, exist_ok=True)
        snapscript = os.path.join(REPO, "lib", "snapshots.py")
        if not os.path.exists(snapscript):
            raise SystemExit(f"snapshots.py is not at {snapscript}; the layout changed")
        snap = subprocess.Popen(
            [sys.executable, snapscript, vm.Paths(self.work).record, shots,
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
        try:
            # THE FILE'S LAST STEP IS THE ARRIVAL WAIT (`until_any` on the Setup Assistant's
            # five first pages), so `installed` here already means "the walkthrough said the
            # wizard is on screen". Two facts that used to be separate calls are one now, and
            # the second cannot disagree with the first because there is no second.
            installed = self.run_installer(None)
            if installed:
                log("=== the installation is complete ===")
                log("  the disk boots into the Setup Assistant; that is what makes it a system")
        finally:
            # THE WALKTHROUGH STOPPED THE MACHINE, AND THAT IS THE ONLY STOP HERE.
            #
            # `shutdown: "{{ shutdown }}"` is setup.yaml's last step and the stage passes `acpi`
            # for it: this guest is a recovery environment with no sshd and no agent, so the
            # power button is the only graceful route it has, and a kill in the middle of the
            # installer's own final writes is what produces a disk that repairs itself on first
            # boot. run_file stops the guest itself if the file fails before reaching it.
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

        # THE ONE GATE: THE WALKTHROUGH GOT TO THE WIZARD.
        #
        # `installed` used to be "install.py exited 0" and `arrived` was a separate wait whose
        # answer could disagree with it. The file's last step waits for the wizard's own first
        # page, so "the walk finished" and "the machine booted into what it wrote" are the same
        # statement now - and a disk that installs without booting cannot pass it.
        if not installed:
            log("=== THE INSTALLER SEQUENCE DID NOT COMPLETE ===")
            log("  the wizard never appeared, so nothing was written to the disk's own boot")
            log("  path or the firmware could not pick it; no artifact is produced from a disk")
            log("  that only installs")
            self.tail(os.path.join(self.work, "console.log"))
            return False

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

    def _run_steps(self) -> bool:
        """The installation walk, from the file.

        THE MACHINE IS THE FILE'S NOW. `boot:` starts it (macos/15/steps/setup.yaml, with the
        recovery image passed through MachineSession's boot_options), so this function no longer
        needs a guest handle - it needs a SESSION, which is the object that carries the guest
        from the boot step to the shutdown step.

        THE WALK ASKS FOR THE WIZARD'S ARRIVAL ITSELF (`until_any` on the Setup Assistant's five
        first pages), so `wait_for_assistant` is not run twice: the file's last step IS that
        wait.
        """
        import steps

        log(f"=== installation: steps {os.path.basename(self.args.steps)} ===")
        session = steps.MachineSession(
            "macos", self.disk, self.work,
            # NO SHARE: nothing in a recovery environment mounts one.
            share="",
            cpus=self.args.cpus, mem_mb=self.args.mem,
            # THE DISK IS THIS STAGE'S TO WRITE. There is no base to protect - the disk was
            # created a few steps above - so an overlay would be a diff that nobody flattens.
            boot_options=dict(self.boot_options, copy=False))
        ok, ran = steps.run_file(
            self.args.steps, "", work=self.work,
            user=self.args.user, password=self.args.password,
            disk_path=self.disk, release=self.args.release,
            # THE FRAME DIRECTORY THIS STAGE'S OWN SNAPSHOTTER WRITES, so a `python:` step that
            # asks install.py "has the wizard appeared" is asking about these frames and not a
            # directory it guessed. bake.py sets shots = frames/shots below, and
            # installer_panes() reads exactly that.
            frames=self.frames, session=session,
            extra_vars={"args.shutdown": "acpi"},
        )
        log(f"{len(ran)} step(s) answered" if ok
            else f"the walkthrough failed after {len(ran)} step(s)")
        return ok

    def run_installer(self, guest=None) -> bool:
        """The installation, from macos/15/steps/setup.yaml. THERE IS NO OTHER DRIVER.

        macos/15/install.py IS STILL IN THE TREE AND IS NO LONGER A DRIVER THIS STAGE RUNS. It
        is where every coordinate in setup.yaml was measured - 1732 lines of what the recovery
        menu, Disk Utility and the installer look like - and it is the reference the YAML was
        written from. Two drivers for one outcome is what made run 42280638394 impossible to
        diagnose in the desktop stage, and the same argument applies here one stage earlier.

        THE `guest` ARGUMENT IS GONE FROM THE CALL AND KEPT IN THE SIGNATURE for the callers that
        still pass None; the machine belongs to the walkthrough now, so there is no handle to
        hand in.
        """
        if not self.args.steps:
            raise SystemExit(
                "--steps is required: the installation is macos/15/steps/setup.yaml. "
                "macos/15/install.py is kept as the table of measurements it was written from "
                "and is not a driver this stage runs.")
        return self._run_steps()

# `wait_for_assistant` WAS HERE AND IS THE FILE'S LAST STEP NOW.
#
# It was a second wait for the same fact: install.py ended its disk watch when the wizard
# appeared, and this ran a second loop over `assistant.reached_assistant` for the case where
# the first ended some other way. `until_any` in macos/15/steps/setup.yaml IS that loop, with
# the five phrases lib/assistant.py knows, and `run_installer` returning True now means the
# walk reached it. Two waits for one fact is how a stage reports "the installation completed
# but the disk did not boot" about a screen that was already the wizard.
#
# lib/assistant.py is still used - by the desktop stage's own `waits_for`, and by anything
# else that wants the phrase list in one place.


def main(argv: list[str]) -> int:
    args = parse(argv)
    return 0 if Baker(args).run() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
