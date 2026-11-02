"""Bake a Windows 11 disk, unattended, with no GUI driver and no guest script.

    python3 bake.py                      # out/windows11.qcow2

WHY THIS EXISTS AT ALL

Every stage below the installer is already in lib/ and already proven by the MX
bake. The installer stage is the only one that is Windows', and - unlike MX - it
needs no driver: Autounattend.xml is consumed by setup itself, and the four passes
in answer/Autounattend.xml are the whole unattended mechanism.

WHAT IS WINDOWS-SPECIFIC AND LIVES HERE

    fetch      a vendor URL with a pinned sha256 (the ISO is not redistributable)
    verify     which edition the index in the answer file resolves to (lib/wim.py)
    rebuild    put the answer file at the ISO root (lib/winiso.py)
    firmware   UEFI + pflash vars + a TPM, which is a machine not a driver
    arrival    port 3389 opening, plus the boot log saying it left the CD

WHAT IS SHARED AND IS NOT HERE

    the VM, the recording, the disk checks, the compression: lib/vm.py, lib/smoke.py

WHY THERE IS NO SCRIPT INSIDE THE GUEST

The answer file is the entire guest-side contract. Two things used to need a staged
SetupComplete.cmd, and both are now checked from the host - better, and earlier:

    which edition was installed
        was: ask the guest for its caption after the fact
        is:  read install.wim's index metadata before the guest exists (lib/wim.py)

    did setup finish
        was: a BAKE_END marker on COM1
        is:  port 3389, which only listens after OOBE completes (wait_for_arrival)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

# windows/11/bake.py -> the repository root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib import bootkey, desktop, images, iso, smoke, vm, wim, winiso  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ANSWER = os.path.join(HERE, "answer")


def log(msg: str) -> None:
    print(f"[bake] {msg}", flush=True)


def frame_progress(shots: str, window: int = 3) -> tuple[int, bool]:
    """How many frames exist, and whether the last few differ.

    Windows setup writes nothing to the serial console, so `console N bytes` cannot
    distinguish a slow install from a frozen one - both sit at a constant size. The
    captured frames can: snapshots.py writes one every few seconds, and if the newest
    two are byte-identical the screen is not changing.

    Returning `changed=False` for a screen that is genuinely static (a progress bar
    that has not moved, a dialog waiting for a click) is the point. It is a hint, not a
    verdict: an installer partitioning a disk draws nothing for a while.
    """
    try:
        names = sorted(f for f in os.listdir(shots)
                       if f.startswith("frame-") and f.endswith(".ppm"))
    except OSError:
        return 0, False

    if len(names) < 2:
        return len(names), False

    # Compare the newest frame against one `window` frames back, clamped to the oldest
    # available. A frame count below the window still compares against frame 0, which
    # is what "did anything change since the beginning" means for a short run.
    older_index = max(0, len(names) - 1 - window)
    try:
        with open(os.path.join(shots, names[-1]), "rb") as a, \
             open(os.path.join(shots, names[older_index]), "rb") as b:
            return len(names), a.read() != b.read()
    except OSError:
        return len(names), False


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bake a Windows 11 disk.")
    p.add_argument("--out", default="out/windows11.qcow2")
    p.add_argument("--image", default="windows11", choices=sorted(images.WINDOWS))
    p.add_argument("--disk-size", default="64G")
    p.add_argument("--mem", type=int, default=8192, help="MB; Windows 11 wants 4 GB minimum")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--timeout", type=int, default=5400, help="seconds the installer may run")
    p.add_argument("--rdp-port", type=int, default=3389,
                   help="host port forwarded to the guest's 3389; the arrival check")
    p.add_argument("--answer", default=ANSWER)
    p.add_argument("--iso", default="", help="use an ISO already on disk instead of downloading")
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

    def fetch_iso(self) -> str:
        if self.args.iso:
            log(f"using the ISO on disk: {self.args.iso}")
            return os.path.abspath(self.args.iso)

        pin = images.pin_for_bake(self.args.image)
        tag = images.tag(self.args.image)
        dest = os.path.join(self.work, f"{self.args.image}.iso")

        if os.path.exists(dest) and os.path.getsize(dest) == pin["size"]:
            # A complete but corrupt file would fail the hash at the next step anyway;
            # this only skips re-hashing seven gigabytes on a re-run.
            log(f"{dest} already present, size matches the pin")
            return dest

        # ONE SOURCE: this repository's package, and nothing else.
        #
        # There is no fallback to Microsoft's URL. A fallback sounds helpful and is not:
        # when the image is missing from the package the cause is that publish-iso.yml
        # was never run or failed, and the useful outcome is a message saying so at
        # second ten instead of a 7.2 GiB download that hides the real problem - and
        # then fails later, in the bake, on a different step.
        #
        # The pin is verified on both sides. publish_iso.py checks it against the vendor
        # before pushing; lib/iso.py checks the layer size when pulling.
        log(f"pulling {tag} from ghcr")
        try:
            return iso.fetch_iso(self.args.image, dest)
        except iso.IsoError as exc:
            raise SystemExit(
                f"{tag} is not in this repository's ghcr package: {exc}\n\n"
                f"Run publish-iso.yml for {tag} first:\n"
                f"    gh workflow run publish-iso.yml -f which={tag}\n"
                f"This bake does not download from the vendor. A second source would\n"
                f"be a second thing to verify, and the failure it hides is exactly the\n"
                f"one worth seeing."
            ) from exc

    def fetch_virtio(self) -> str:
        """The driver CD, from ghcr. Not optional, and not from the vendor.

        Setup cannot see a virtio disk without these drivers, so a missing CD is not a
        warning - without it the install stops at a pane listing no disks, which the
        answer file's WillShowUI=OnError renders as a dialog nobody is watching.
        """
        dest = os.path.join(self.work, "virtio-win.iso")
        tag = images.tag("virtio-win")

        if os.path.exists(dest):
            log("virtio-win.iso already present")
            return dest

        log(f"pulling {tag} from ghcr")
        try:
            return iso.fetch_iso("virtio-win", dest)
        except iso.IsoError as exc:
            raise SystemExit(
                f"{tag} is not in this repository's ghcr package: {exc}\n\n"
                f"Run publish-iso.yml for {tag} first:\n"
                f"    gh workflow run publish-iso.yml -f which={tag}\n"
                f"Without the driver CD setup cannot see the disk it is installing to."
            ) from exc

    def build_answer_iso(self, virtio: str) -> str:
        """Build the small ISO that carries Autounattend.xml.

        The install ISO is NOT touched. Windows setup reads the answer file from the
        root of any removable drive, so a second CD is enough - see lib/winiso.py for
        why that beats remastering 7.2 GiB, and for the 448 KB ISO that proved it.

        The template carries __ADMIN_PASSWORD__ and __COMPUTER_NAME__ rather than
        values, so the password is not in the repository and the machine name is not
        the same in every bake. The substitution happens here, into a copy, so
        windows/11/answer/ stays a template.
        """
        staging = os.path.join(self.work, "answer")
        os.makedirs(staging, exist_ok=True)

        password = os.environ.get("WINDOWS_ADMIN_PASSWORD", "docker")
        name = os.environ.get("WINDOWS_COMPUTER_NAME", "sandbox")

        for entry in os.listdir(self.args.answer):
            source = os.path.join(self.args.answer, entry)
            if not os.path.isfile(source):
                continue
            with open(source, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            text = text.replace("__ADMIN_PASSWORD__", password).replace("__COMPUTER_NAME__", name)

            # Windows .cmd files must be CRLF: cmd.exe treats a bare LF as part of
            # the line in some paths, and a label such as ":state_done" followed by
            # LF is not recognised. XML has no such requirement, and rewriting it
            # to CRLF only risks a BOM or an encoding question for no gain.
            newline = "\r\n" if entry.lower().endswith((".cmd", ".bat")) else "\n"
            with open(os.path.join(staging, entry), "w", encoding="utf-8", newline=newline) as fh:
                fh.write(text)

        # The drivers ride on the answer CD, in $WinPEDriver$, because windowsPE will
        # not see a virtio disk without them - see lib/winiso.py.
        return winiso.answer_iso(
            staging, os.path.join(self.work, "answer.iso"), drivers=virtio,
        )

    # -- the machine ----------------------------------------------------------

    def firmware(self) -> tuple[str, str]:
        """OVMF code and a writable vars copy, or a refusal to start.

        The vars file is per-guest NVRAM. It is created once per work directory and
        then reused, because Windows setup reboots twice inside the same VM: a fresh
        vars file each boot would lose the Secure Boot state the installed system is
        expected to keep.

        A missing firmware file is fatal, not a warning. Without it qemu starts a
        SeaBIOS guest, Windows 11 refuses to install on it, and the symptom is a
        boot loop or "no bootable device" - which reads as a broken ISO. The earlier
        version logged a WARNING and carried on.
        """
        code = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd")
        template = os.environ.get("OVMF_VARS_TEMPLATE", "/usr/share/OVMF/OVMF_VARS_4M.fd")

        for path in (code, template):
            if not os.path.isfile(path):
                raise SystemExit(
                    f"UEFI firmware not found: {path}\n"
                    f"Windows 11 will not install on SeaBIOS, so this is fatal rather\n"
                    f"than a warning. The bake image installs `ovmf`; a host running\n"
                    f"this outside the image needs it too. Override with OVMF_CODE and\n"
                    f"OVMF_VARS_TEMPLATE."
                )

        vars_copy = os.path.join(self.work, "OVMF_VARS.fd")
        if not os.path.exists(vars_copy):
            import shutil
            shutil.copyfile(template, vars_copy)
            log(f"created writable NVRAM copy: {vars_copy}")
        return code, vars_copy

    def swtpm(self) -> str:
        """Start swtpm and return its chardev socket.

        It is a separate process because it owns state that has to outlive one
        boot; starting it inside vm.py would make stop() the only thing that ever
        cleaned it up, and a bake that fails leaves the state behind for the next
        one to inherit.

        A missing swtpm is fatal. Windows 11 checks for a TPM 2.0 during windowsPE
        and stops with "This PC can't run Windows 11"; the answer file's bypass keys
        are deliberately not set, because using them would hide exactly this kind of
        misconfiguration and install a system nobody can update.
        """
        state = os.path.join(self.work, "tpm")
        os.makedirs(state, exist_ok=True)
        sock = os.path.join(state, "swtpm.sock")

        if not __import__("shutil").which("swtpm"):
            raise SystemExit(
                "swtpm is not installed; Windows 11 will not install without a TPM 2.0."
            )

        subprocess.run(
            ["swtpm", "socket", "--tpmstate", f"dir={state}",
             "--ctrl", f"type=unixio,path={sock}", "--tpm2", "--daemon"],
            check=True,
        )
        log(f"swtpm on {sock}")
        return sock

    # -- the run --------------------------------------------------------------

    def run(self) -> bool:
        log(f"out={self.args.out}")

        clean = self.fetch_iso()

        # Before anything boots: does the index in the answer file resolve to the
        # edition this image is supposed to be? This is the check the guest script
        # used to do after the fact, moved to the host and to before the boot.
        pin = images.pin_for_bake(self.args.image)
        log(f"verifying the edition for index {pin['index']}")
        wim.check_iso(clean, pin["index"], pin["edition"], forbid=pin.get("forbid"))

        # virtio first: the answer CD carries the drivers, so it cannot be built until
        # the driver ISO is on disk.
        virtio = self.fetch_virtio()
        answer = self.build_answer_iso(virtio)

        if os.path.exists(self.disk):
            os.unlink(self.disk)
        subprocess.run(["qemu-img", "create", "-f", "qcow2", self.disk, self.args.disk_size], check=True)
        log(f"disk created: {self.args.disk_size}")

        code, vars_copy = self.firmware()
        tpm_sock = self.swtpm()

        shape = vm.Shape(
            cpus=self.args.cpus,
            mem_mb=self.args.mem,
            machine="q35",
            firmware_code=code,
            firmware_vars=vars_copy,
            tpm_socket=tpm_sock,
            vga="std",
            rdp_port=self.args.rdp_port,
        )

        # Three CDs at most, and the order matters only for the boot device: the
        # install ISO is first so the firmware boots it. The answer CD is small and
        # its only job is to be a removable drive with Autounattend.xml at the root.
        # THREE CDS AND ONE VIRTIO DISK - the same shape dockur/windows uses.
        #
        # The disk is virtio-blk. windowsPE ships no virtio driver, so setup cannot see
        # it on its own; the first full run stopped exactly there - console output grew
        # to 4273 bytes and then nothing for fifteen minutes, because setup's drive
        # list was empty and WillShowUI=OnError had put a dialog on the screen.
        #
        # VirtualBox's answer is not a different controller, it is $WinPEDriver$: setup
        # scans removable media for that directory and loads what it finds. The drivers
        # travel on the answer CD, which keeps the virtio disk at index 0 so the answer
        # file's DiskID stays correct.
        #
        # dockur/windows reaches the same place by another route: it builds a FAT32
        # image, mounts it as disk 0, and numbers the virtio disk 1. Both work; this one
        # needs no extra disk and no DiskID shift.
        drives = [
            "-drive", f"file={clean},media=cdrom,readonly=on",
            "-drive", f"file={answer},media=cdrom,readonly=on",
            "-drive", f"file={self.disk},if=virtio,format=qcow2",
            # The same drivers again, for the installed system to pick up network and
            # ballooning. Setup only reads $WinPEDriver$ during windowsPE.
            "-drive", f"file={virtio},media=cdrom,readonly=on",
        ]

        console = os.path.join(self.work, "console.log")
        guest = vm.Guest(self.work, extra=drives, shape=shape, stdout=console).start()

        # THE ISO WILL NOT BOOT WITHOUT A KEYPRESS.
        #
        # The El Torito boot image prints "Press any key to boot from CD or DVD..." and
        # waits about five seconds. Nothing presses it in a bake, so the guest falls
        # through to PXE and the run ends with four thousand bytes of firmware output
        # and no Windows at all - which is what the first full run produced:
        #
        #     BdsDxe: starting Boot0001 "UEFI QEMU DVD-ROM QM00001"
        #     BdsDxe: failed to start Boot0001 ...: Time out
        #     >>Start PXE over IPv4.
        #
        # It runs in a thread because wait_for_arrival must be watching at the same
        # time: the prompt is five seconds of the install, and the arrival check is the
        # whole rest of it.
        keypress = threading.Thread(
            target=bootkey.wait_for_boot_prompt,
            args=(console, guest.paths.drive),
            daemon=True,
        )
        keypress.start()

        shots = os.path.join(self.frames, "shots")
        os.makedirs(shots, exist_ok=True)
        snap_log = os.path.join(shots, "snapshots.log")

        # stdout and stderr go to a file, not DEVNULL. The first run's recording was
        # missing entirely and there was no way to tell why: snapshots.py says "no QMP
        # socket" on stderr and a discarded stderr is a silent failure.
        #
        # THE PATH NEEDS THREE dirnames: windows/11/bake.py -> windows/11 -> windows ->
        # the repository root, where lib/ lives. Two got `windows/lib/snapshots.py`,
        # which does not exist - and the first version of this check is what reported
        # it, in the only line that said the recording was broken.
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        snapscript = os.path.join(repo_root, "lib", "snapshots.py")
        if not os.path.exists(snapscript):  # fail loudly rather than record nothing
            raise SystemExit(f"snapshots.py is not at {snapscript}; the repository layout changed")

        snap = subprocess.Popen(
            [sys.executable, snapscript,
             guest.paths.record, shots,
             os.environ.get("SNAPSHOT_INTERVAL", "5"), "2000"],
            stdout=open(snap_log, "wb"), stderr=subprocess.STDOUT,
        )

        # Prove the recorder is producing frames before waiting fifteen minutes for a
        # guest. The prompt to boot from CD is answered in the first seconds, so by the
        # time this check runs there should already be a frame or two.
        time.sleep(20)
        try:
            early = [f for f in os.listdir(shots) if f.startswith("frame-")]
        except OSError:
            early = []
        if early:
            log(f"recording: {len(early)} frame(s) captured already")
        else:
            log("recording: NO FRAMES after 20s - the QMP screendump is not working")
            log(f"recording: see {snap_log}")
            try:
                with open(snap_log, "rb") as fh:
                    for line in fh.read().decode("utf-8", "replace").splitlines()[-10:]:
                        log(f"  {line}")
            except OSError:
                pass
        try:
            arrived = self.wait_for_arrival(console, guest)
        finally:
            guest.stop()
            snap.terminate()
            try:
                snap.wait(timeout=5)
            except subprocess.TimeoutExpired:
                snap.kill()

        # Encode the recording whether or not the bake succeeded.
        #
        # A failed run is exactly when the frames are worth watching: a setup pane
        # waiting for a click and a hung kernel are identical in a log and obvious in
        # a picture. Encoding only on success would throw away the one artifact that
        # explains a failure.
        #
        # RECORD_FPS defaults to 1 because frames are captured every SNAPSHOT_INTERVAL
        # seconds: encoding at 1 fps makes the video run SNAPSHOT_INTERVAL times
        # faster than real time, which is the point - a 90-minute install is worth
        # watching in 18 minutes, and the file stays small.
        log("=== encoding the recording ===")
        mp4 = os.path.join(self.work, "install.mp4")
        if vm.record_many(mp4, shots, fps=int(os.environ.get("RECORD_FPS", "1"))):
            log(f"recording: {mp4} ({os.path.getsize(mp4) >> 20} MiB)")
        else:
            log("recording: nothing to encode")

        if not arrived:
            log("=== THE GUEST DID NOT REACH A RUNNING SYSTEM ===")
            self.tail(console)
            return False

        log("the guest reached a running system; checking the disk")

        if not smoke.check_disk(self.disk, root_fs="gpt"):
            # Do not produce an artifact from a disk that failed its checks. The
            # previous run compressed an empty qcow2 and called it BAKED because this
            # result was only used as the exit code - the publish step then had a
            # file to push. A disk that fails here is not publishable at any size.
            log("=== THE DISK FAILED ITS CHECKS; refusing to produce an artifact ===")
            return False

        log("compressing")
        subprocess.run(
            ["qemu-img", "convert", "-O", "qcow2", "-c",
             "-o", "compression_type=zstd", self.disk, self.args.out],
            check=True,
        )
        log(f"BAKED windows11 -> {self.args.out} "
            f"({os.path.getsize(self.args.out) >> 20} MiB)")
        return True

    def wait_for_arrival(self, console: str, guest: vm.Guest) -> bool:
        """Wait until the guest shows a Windows desktop.

        WHY THE SCREEN, AND NOT RDP

        The check used to be "is port 3389 answering". It depends on the guest's NIC
        having a driver in the installed system, and the NIC is virtio-net: NetKVM
        reaches windowsPE through $WinPEDriver$ but nothing installs it into the
        finished disk. So a perfect install could wait forever for an RDP listener that
        was never going to start, and the bake would report a timeout on a good disk.

        A desktop is on screen the moment OOBE finishes, and the screen is being
        captured for the recording already. The check costs nothing extra and depends on
        nothing that can be misconfigured.

        lib/desktop.py explains what it looks for and why colour matching alone does
        not work: the installer is also blue.

        THE CONSOLE IS STILL READ, FOR DIAGNOSIS ONLY

        The firmware's own words are what explain a failure - "No bootable option", a
        PXE attempt, a boot loop through Boot0001..0007. They no longer decide
        anything: the desktop does.
        """
        import time

        deadline = time.time() + self.args.timeout
        started = time.time()
        last_report = started
        shots = os.path.join(self.frames, "shots")

        while time.time() < deadline:
            if not guest.alive():
                log("qemu exited before the guest came up")
                self.tail(console)
                return False

            # --- the screen: the only signal that means what it says -------------
            #
            # reached_desktop looks at the last few frames rather than the newest,
            # because a capture can land mid-repaint or while the screen has blanked.
            if desktop.reached_desktop(shots):
                elapsed = time.time() - started
                log(f"a Windows desktop is on screen ({elapsed:.0f}s)")
                return True

            # --- the console: for the record, and for the failure message --------
            try:
                with open(console, "rb") as fh:
                    text = fh.read().decode("utf-8", "replace")
            except OSError:
                text = ""

            # --- a heartbeat that can tell progress from a hang ------------------
            #
            # Console byte count alone cannot: Windows setup writes nothing to the
            # serial console, so a healthy install and a frozen one both show a
            # constant size. The frames can - see frame_progress.
            if time.time() - last_report >= 60:
                last_report = time.time()
                elapsed = time.time() - started
                frames, changed = frame_progress(shots)
                log(f"waiting {elapsed:.0f}s: {frames} frames, "
                    f"screen {'changing' if changed else 'static'}, "
                    f"console {len(text)} bytes")
                if frames == 0:
                    log("no frames are being captured at all; check snapshots.log")
                elif not changed and elapsed > 900:
                    log("the screen has not changed for a while - if this continues, "
                        "the installer is waiting for something (see install.mp4)")

            time.sleep(5)

        log(f"timed out after {self.args.timeout}s")
        self.tail(console)
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
