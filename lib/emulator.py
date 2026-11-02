"""Run the Android emulator with the arguments a sandbox needs.

    from emulator import Emulator
    emu = Emulator(avd="sandbox", work="/tmp/run").start()
    emu.wait_for_boot()
    emu.stop()

WHY THIS EXISTS AND WHAT IT DOES NOT REPLACE

Android does not have an installer that writes a disk. Google ships a system image
whose contents *are* the disks - system.img, vendor.img, userdata.img - and a kernel
and ramdisk that boot them. So there is no bake step in the sense the other platforms
have: no installer to drive, no partition table to create, no progress to watch for
half an hour.

What there IS, and what this module drives, is the emulator: the process that turns
those images into a running device.

WHY THE EMULATOR AND NOT lib/vm.py

`lib/vm.py` builds a qemu command line for a `q35` machine. The Android system image is
built for Google's `ranchu` machine and its `goldfish` device family - framebuffer,
pipe, audio, battery - which upstream qemu does not implement. Booting a ranchu image
on q35 stops in the kernel because there is no display device and no goldfish_pipe.

So the launcher differs. Everything downstream of it does not: the emulator speaks QMP
on a socket, so lib/qmp.py, lib/ocr.py and lib/snapshots.py drive it exactly as they
drive a qemu guest.

WHAT IS SHARED WITH THE OTHER PLATFORMS

    lib/images.py       the system image is pinned there like every other input
    lib/publish_iso.py  publishes it to this repository's package
    lib/qmp.py          QMP client: screendump, input, keys
    lib/snapshots.py    frame capture, unchanged
    lib/vm.py           record_many() to encode the frames
    lib/ci.py           kvm check, oras, login
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

QEMU_SOCKET = "emulator-qmp.sock"


def log(msg: str) -> None:
    print(f"[emulator] {msg}", flush=True)


class EmulatorError(RuntimeError):
    pass


def build_args(avd: str, work: str, *, api: int = 35, abi: str = "x86_64",
               memory_mb: int = 4096, cores: int = 4,
               gpu: str = "swiftshader_indirect", login: bool = False,
               wipe_data: bool = True) -> list[str]:
    """The emulator argv. Pure, so it can be asserted without running it.

    Taken from easysandbox-android/entrypoint.sh, which is the version of this that
    has been run against real guests. The flags that matter and why:

        -no-window              headless; the screen is captured over QMP, not shown
        -no-audio               no audio device; nothing consumes it
        -no-boot-anim           the boot animation is 20 seconds of the same frame
        -no-snapshot            never resume a saved state: a bake must start cold,
                                and a snapshot would hide a broken system image
        -accel on               KVM. Without it an Android boot is tens of minutes.
        -gpu swiftshader_indirect   software rendering. The alternative needs a host
                                GPU, and a CI runner has none.
        -wipe-data              conditional, and the condition is the whole design

    WHY -wipe-data IS A PARAMETER NOW

    It used to be unconditional, with the reasoning that a bake must start cold and a
    snapshot would hide a broken image. That is right for the FIRST boot and wrong for
    the second, and the two are different jobs:

        bake      the first boot. -wipe-data, because the point is to run a known-clean
                  device and leave a /data that a later boot can be shown to have reused.

        verify    a boot of the artifact the bake just produced. WITHOUT -wipe-data,
                  because wiping would make it indistinguishable from a fresh AVD - the
                  one thing the check exists to rule out.

    A verify that passed with -wipe-data would prove only that the system image boots,
    which is true of the input. It would say nothing about whether the bake's output
    carries state, which is what makes it an artifact rather than a copy.
    """
    if not shutil.which("emulator"):
        raise EmulatorError(
            "the emulator is not on PATH.\n"
            "It is in the bake image at /opt/android-sdk/emulator; a host running\n"
            "this needs `sdkmanager emulator platform-tools`."
        )

    qmp = os.path.join(work, QEMU_SOCKET)

    args = [
        "emulator", "-avd", avd,
        "-no-window", "-no-audio", "-no-boot-anim", "-no-snapshot",
    ]
    if wipe_data:
        args.append("-wipe-data")
    args += [
        "-gpu", gpu,
        "-accel", "on",
        "-memory", str(memory_mb),
        "-cores", str(cores),
        "-port", "5554",
        # QMP REACHES QEMU THROUGH -qemu, NOT AS A TOP-LEVEL OPTION.
        #
        # The emulator is Google's qemu fork with its own argument parser in front of
        # qemu's. Its parser does not know -qmp:
        #
        #     unknown option: -qmp
        #     please use -help for a list of valid options
        #
        # `-qemu <args>` passes everything after it straight through to the qemu
        # underneath, which is where -qmp lives. Everything downstream of this line -
        # lib/qmp.py, lib/snapshots.py - is unchanged: the socket is a real QMP socket,
        # it is only the route to it that differs.
        # -no-metrics and -no-keys keep the emulator from making network calls of its
        # own; a bake should not depend on Google's telemetry endpoints being up.
        #
        # THE ORDER MATTERS: everything after -qemu goes to qemu, so these have to be
        # before it or the emulator never sees them.
        "-no-metrics",
        "-no-keys",
        # LAST, because -qemu hands the rest of the vector to qemu underneath.
        "-qemu", "-qmp", f"unix:{qmp},server=on,wait=off",
    ]
    return args


class Emulator:
    """A running emulator, with the paths that belong to it."""

    def __init__(self, avd: str, work: str, **shape):
        self.avd = avd
        self.work = os.path.abspath(work)
        self.shape = shape
        self.log_path = os.path.join(self.work, "emulator.log")
        self.qmp = os.path.join(self.work, QEMU_SOCKET)
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Emulator":
        os.makedirs(self.work, exist_ok=True)
        try:
            os.unlink(self.qmp)
        except FileNotFoundError:
            pass

        args = build_args(self.avd, self.work, **self.shape)
        log(f"avd {self.avd}, gpu {self.shape.get('gpu', 'swiftshader_indirect')}")
        log(f"qmp {self.qmp}")

        # The emulator writes a lot to stdout and it is the only place a boot failure
        # explains itself, so it goes to a file that is kept with the recording.
        self.proc = subprocess.Popen(
            args,
            stdout=open(self.log_path, "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "ANDROID_AVD_HOME": os.environ.get("ANDROID_AVD_HOME", "/opt/android-avd")},
        )
        log(f"pid {self.proc.pid}")

        deadline = time.time() + 120
        while time.time() < deadline:
            if os.path.exists(self.qmp):
                log("qmp socket is up")
                return self
            if self.proc.poll() is not None:
                raise EmulatorError(
                    f"the emulator exited with {self.proc.returncode} before opening "
                    f"QMP.\nLast lines of {self.log_path}:\n{self._tail()}"
                )
            time.sleep(2)

        raise EmulatorError(
            f"no QMP socket at {self.qmp} after 120s.\n{self._tail()}"
        )

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def adb(self, *args: str, timeout: int = 30) -> str:
        """Run adb against this emulator and return its output, stripped.

        A non-zero exit is returned as "FAILED:<stderr>" rather than raised, because
        most callers are polling and a command that fails before the framework is up is
        expected rather than exceptional.
        """
        result = subprocess.run(
            ["adb", "-s", "emulator-5554", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return f"FAILED:{(result.stderr or result.stdout or '').strip()}"
        return (result.stdout or "").strip()

    def mark(self, token: str) -> bool:
        """Write a token into the guest's /data, where it survives a shutdown.

        THIS IS WHAT MAKES THE ARTIFACT PROVABLY NOT A COPY OF ITS INPUT. /data lives in
        userdata-qemu.img, which the emulator creates on the first boot and which the
        bake publishes. A verify run without -wipe-data reads the token back; a disk
        that had been re-wiped would not have it, and a disk that was never written
        would not either. The value is the bake's own timestamp, so two runs cannot be
        confused for each other.
        """
        result = self.adb("shell", "sh", "-c",
                          f"echo '{token}' > /data/local/tmp/bake-gold && "
                          f"sync && cat /data/local/tmp/bake-gold")
        if result == f"FAILED:" or not result.endswith(token):
            log(f"could not write the gold marker: {result[:200]}")
            return False
        log(f"gold marker written: {token}")
        return True

    def read_mark(self) -> str:
        """Read the token back, or "" when it is not there."""
        result = self.adb("shell", "cat", "/data/local/tmp/bake-gold")
        if result.startswith("FAILED:"):
            return ""
        return result.strip()

    def wait_for_boot(self, seconds: int = 900) -> bool:
        """Poll sys.boot_completed. This is the whole arrival check.

        Android has no serial login prompt and no desktop to photograph: the boot
        animation runs whether or not the system works, and the launcher appears on a
        device that is half broken. sys.boot_completed is the property the framework
        sets when it has started, and it is what easysandbox-android waits on too.

        adb can answer before the device is listed, so the first calls fail with
        "device not found"; that is expected and not logged.
        """
        started = time.time()
        deadline = started + seconds
        last_report = started

        while time.time() < deadline:
            if not self.alive():
                log("the emulator exited while waiting for boot")
                return False

            try:
                state = self.adb("getprop", "sys.boot_completed")
            except (subprocess.TimeoutExpired, FileNotFoundError):
                state = ""

            if state == "1":
                log(f"sys.boot_completed after {time.time() - started:.0f}s")
                return True

            if time.time() - last_report >= 60:
                last_report = time.time()
                log(f"waiting {time.time() - started:.0f}s: boot_completed={state or '-'}")

            time.sleep(5)

        log(f"never booted within {seconds}s")
        return False

    def stop(self) -> None:
        """Ask the guest to power off, then make sure it did.

        A SIGTERM to the emulator's process group kills a machine that is mid-write,
        and the /data partition of an AVD is the thing this bake is about. `adb emu
        kill` is the emulator's own shutdown: it flushes the disk images and exits.

        The signals stay as the fallback, because a guest that is hung at the kernel
        must not be able to hang the bake - so this is "polite, bounded, then firm".
        """
        if self.proc is None:
            return

        if self.alive():
            try:
                self.adb("emu", "kill", timeout=30)
            except (subprocess.TimeoutExpired, OSError):
                pass
            try:
                self.proc.wait(timeout=60)
                log("stopped cleanly (adb emu kill)")
                return
            except subprocess.TimeoutExpired:
                log("the emulator did not exit within 60s; sending SIGTERM")

        try:
            os.killpg(os.getpgid(self.proc.pid), 15)
        except ProcessLookupError:
            return
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), 9)
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=10)
        log("stopped")

    def _tail(self, lines: int = 25) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            return "(no log)"
        return "\n".join(text.splitlines()[-lines:])
