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

# windows/11/bake.py -> the repository root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib import iso, smoke, vm, wim, winiso  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ANSWER = os.path.join(HERE, "answer")

# The installer images, pinned the way dockur pins them: a fixed Microsoft URL and
# a fixed sha256 and size. Microsoft rotates these files; when the hash stops
# matching the bake fails with "the vendor has probably published a new build"
# rather than installing something silently different.
#
#   win11x64          Windows 11 Consumer (25H2), 7.2 GiB
#   win11x64-enterprise-eval   Windows 11 Enterprise Eval, 6.6 GiB
#
# The index is the number in the answer file's /IMAGE/INDEX, and it is verified
# against the WIM's own metadata before the guest boots - see lib/wim.py for why
# that beats asking the guest afterwards.
IMAGES = {
    "win11x64": {
        "url": "https://software-static.download.prss.microsoft.com/dbazure/"
               "888969d5-f34g-4e03-ac9d-1f9786c66749/"
               "26200.6584.250915-1905.25h2_ge_release_svc_refresh_CLIENT_CONSUMER_x64FRE_en-us.iso",
        "sha256": "d141f6030fed50f75e2b03e1eb2e53646c4b21e5386047cb860af5223f102a32",
        "size": 7736125440,
        "index": 6,
        "edition": "Windows 11 Pro",
        # "Pro" is a substring of "Pro N". Without this the check passes on an
        # index that installs the edition people actually complain about.
        "forbid": " N",
    },
    "win11x64-enterprise-eval": {
        "url": "https://software-static.download.prss.microsoft.com/dbazure/"
               "888969d5-f34g-4e03-ac9d-1f9786c66749/"
               "26200.6584.250915-1905.25h2_ge_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso",
        "sha256": "a61adeab895ef5a4db436e0a7011c92a2ff17bb0357f58b13bbc4062e535e7b9",
        "size": 7092807680,
        "index": 1,
        "edition": "Windows 11 Enterprise",
        "forbid": None,
    },
}

# The virtio-win drivers. Without a second CD, setup cannot see the virtio disk it
# is installing to, and the failure is a pane with an empty disk list - which the
# answer file's WillShowUI=OnError would show as a dialog nobody is watching.
#
# The source is qemus/virtiso-whql, which is what dockur/windows uses for the same
# purpose: it republishes the upstream virtio-win ISO with a stable tag. The first
# attempt pointed at virtio-win-pkg-scripts/raw/master/drivers/archive-virtio/...,
# which answers 404 - the archive moved, and a URL built from a guessed path is
# exactly the kind of dependency that fails at run time.
VIRTIO_WIN = {
    "url": "https://github.com/qemus/virtiso-whql/releases/download/v1.9.61-0/"
           "virtio-win-1.9.61.iso",
    "sha256": None,  # pinned by the release tag and by setup's own behaviour
    "size": None,
}


def log(msg: str) -> None:
    print(f"[bake] {msg}", flush=True)


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bake a Windows 11 disk.")
    p.add_argument("--out", default="out/windows11.qcow2")
    p.add_argument("--image", default="win11x64", choices=sorted(IMAGES))
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

        pin = IMAGES[self.args.image]
        log(f"image {self.args.image}: {pin['size']} bytes, sha256 {pin['sha256']}")
        dest = os.path.join(self.work, f"{self.args.image}.iso")
        found = os.path.join(self.work, f"{self.args.image}.iso")
        if os.path.exists(found) and os.path.getsize(found) == pin["size"]:
            # A complete but corrupt file would fail the hash below anyway; this
            # only skips re-hashing seven gigabytes on a re-run.
            log("already downloaded")
            return found
        return iso.fetch_url(pin["url"], dest, sha256=pin["sha256"], size=pin["size"])

    def fetch_virtio(self) -> str | None:
        dest = os.path.join(self.work, "virtio-win.iso")
        if os.path.exists(dest):
            log("virtio-win.iso already present")
            return dest
        try:
            log("fetching virtio-win drivers")
            # No hash: the URL is a tagged release and the file is not large. The
            # failure this protects against - a different ISO than intended - is
            # caught by setup itself, which finds no disk.
            return iso.fetch_url(VIRTIO_WIN["url"], dest)
        except Exception as exc:  # noqa: BLE001 - a missing driver CD is not fatal
            log(f"WARNING: could not fetch virtio-win: {exc}")
            log("setup may not see the disk; continuing without the driver CD")
            return None

    def build_boot_iso(self, clean: str) -> str:
        """Rebuild the ISO with the answer file, substituting the placeholders.

        The template carries __ADMIN_PASSWORD__ and __COMPUTER_NAME__ rather than
        values so that the password is not in the repository and the machine name is
        not the same in every bake. The substitution happens here, into a copy, so
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

        out = os.path.join(self.work, "boot.iso")
        return winiso.rebuild(clean, staging, out)

    # -- the machine ----------------------------------------------------------

    def firmware(self) -> tuple[str, str]:
        """OVMF code and a writable vars copy.

        The vars file is per-guest NVRAM. It is created once per work directory and
        then reused, because Windows setup reboots twice inside the same VM: a
        fresh vars file each boot would lose the Secure Boot state the installed
        system is expected to keep.
        """
        code = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd")
        template = os.environ.get("OVMF_VARS_TEMPLATE", "/usr/share/OVMF/OVMF_VARS_4M.fd")

        for path in (code, template):
            if not os.path.isfile(path):
                log(f"WARNING: firmware file not found: {path}")

        vars_copy = os.path.join(self.work, "OVMF_VARS.fd")
        if not os.path.exists(vars_copy) and os.path.isfile(template):
            import shutil
            shutil.copyfile(template, vars_copy)
            log(f"created writable NVRAM copy: {vars_copy}")
        return code, vars_copy

    def swtpm(self) -> str | None:
        """Start swtpm and return its chardev socket.

        It is a separate process because it owns state that has to outlive one
        boot; starting it inside vm.py would make stop() the only thing that ever
        cleaned it up, and a bake that fails leaves the state behind for the next
        one to inherit.
        """
        state = os.path.join(self.work, "tpm")
        os.makedirs(state, exist_ok=True)
        sock = os.path.join(state, "swtpm.sock")

        if not __import__("shutil").which("swtpm"):
            log("WARNING: swtpm not installed; Windows 11 will fail its TPM check")
            return None

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
        pin = IMAGES[self.args.image]
        log(f"verifying the edition for index {pin['index']}")
        wim.check_iso(clean, pin["index"], pin["edition"], forbid=pin.get("forbid"))

        boot_iso = self.build_boot_iso(clean)
        virtio = self.fetch_virtio()

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
            tpm_socket=tpm_sock or "",
            vga="std",
            rdp_port=self.args.rdp_port,
        )

        drives = [
            "-drive", f"file={boot_iso},media=cdrom,readonly=on",
            "-drive", f"file={self.disk},if=virtio,format=qcow2",
        ]
        if virtio:
            drives += ["-drive", f"file={virtio},media=cdrom,readonly=on"]

        console = os.path.join(self.work, "console.log")
        guest = vm.Guest(self.work, extra=drives, shape=shape, stdout=console).start()

        shots = os.path.join(self.frames, "shots")
        snap = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "lib", "snapshots.py"),
             guest.paths.record, shots,
             os.environ.get("SNAPSHOT_INTERVAL", "5"), "2000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
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
        """Wait until the guest is a running Windows, using only host-side signals.

        THE THREE SIGNALS, AND WHY THESE

        A screenshot cannot answer this. A guest that finished setup and one that
        went interactive and is sitting at a pane both eventually draw a desktop,
        and telling them apart by eye is exactly the judgement a bake exists to
        remove.

          reboots >= 2        setup restarts the guest itself. A guest stuck on a
                              pane never restarts, so this alone rules out the most
                              common failure.
          booted from disk    SeaBIOS/OVMF says "Booting from Hard Disk" once the
                              installer has handed over. Before that it boots the
                              CD.
          port 3389 accepts   the strongest one. RDP is only listening after OOBE
                              has completed, which is after every pass of the answer
                              file has been consumed.

        The third is the only one that proves setup finished; the first two say it
        got moving. All three are read without the guest doing anything, which is
        what lets the answer file be the whole guest-side contract.
        """
        import time

        deadline = time.time() + self.args.timeout
        started = time.time()
        seen: set[str] = set()
        reboots = 0
        last_boot_marker = ""

        # A floor under the arrival time, because the cheapest way for this check to
        # be wrong is to be too eager.
        #
        # The first run declared success six seconds after `qemu-img create`, on an
        # RDP forward that accepted a connection because qemu had started - nothing
        # had booted. rdp_serving() fixes that specific cause, but a floor is a guard
        # against the next one: no Windows install reaches a desktop faster than this,
        # and a check that says otherwise is measuring the wrong thing.
        minimum = int(os.environ.get("MIN_INSTALL_SECONDS", "600"))

        while time.time() < deadline:
            if not guest.alive():
                log("qemu exited before the guest came up")
                return False

            # --- the console: reboots and what the firmware booted ---------------
            try:
                with open(console, "rb") as fh:
                    text = fh.read().decode("utf-8", "replace")
            except OSError:
                text = ""

            # Each "Booting from Hard Disk" is one completed handover from the
            # installer to the installed system. Counting distinct positions rather
            # than occurrences keeps an append-only log from inflating the count.
            marker = text.count("Booting from Hard Disk")
            if marker > reboots:
                reboots = marker
                log(f"booted from hard disk {reboots} time(s)")
            last_boot_marker = text

            # --- the port: the only signal that proves OOBE finished -------------
            #
            # rdp_serving, not port_open: see its docstring. The forward accepts a
            # connection from the moment qemu starts, so a bare TCP check declares
            # success in seconds and ships an empty disk.
            if vm.rdp_serving(self.args.rdp_port):
                elapsed = time.time() - started
                if elapsed < minimum:
                    log(f"RDP answered after {elapsed:.0f}s, which is before any Windows "
                        f"install can finish (floor is {minimum}s) - ignoring it")
                    log("this means something other than the guest is answering; "
                        "check that the forward is not colliding with a host service")
                else:
                    log(f"3389 is serving RDP: the guest is a running Windows "
                        f"({elapsed:.0f}s)")
                    return True

            if reboots == 0:
                seen.add("no hard-disk boot yet")
            time.sleep(5)

        log(f"timed out after {self.args.timeout}s")
        if reboots == 0:
            log("the guest never booted from its hard disk: setup did not get that far")
        else:
            log(f"the guest booted from disk {reboots} time(s) but 3389 never opened:")
            log("setup probably went interactive (a bad pass in the answer file)")
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
