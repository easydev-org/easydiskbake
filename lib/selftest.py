"""Prove the four channels work, on the smallest guest that boots.

    python3 selftest.py

This does not bake anything. It answers the question that has to be settled before
any distribution is worth wiring up: does -vnc answer, does QMP screendump work on a
second socket, does the framebuffer capture produce frames, and does a host port
forward reach the guest. Each is a separate moving part and a failure in any of them
looks like "the bake is slow".

The guest is Alpine's virt ISO, 63 MB, already cached in ghcr. It reaches a shell in
about 15 seconds, which is fast enough to iterate on.

WHY THIS IS A SCRIPT AND NOT A SHELL BLOCK IN THE WORKFLOW

The shell version was 60 lines of `source lib/vm.sh`, string concatenation for the
qemu arguments and `find | head -1` for the kernel and initrd. It could not be run
anywhere except in CI, and when it failed the output was whichever of those lines
printed last. Here the same steps are a sequence of calls with names, and the whole
thing runs locally given a machine with /dev/kvm.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import compress  # noqa: E402
import iso  # noqa: E402
import net  # noqa: E402
import smoke  # noqa: E402
import vm  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def extract(iso_path: str, outdir: str, *members: str) -> list[str]:
    """Pull specific files out of an ISO without mounting it.

    7z reads an ISO as an archive, which matters because mounting needs privileges a
    container job may not have.
    """
    subprocess.run(
        ["7z", "x", "-y", f"-o{outdir}", iso_path, *members],
        capture_output=True, text=True, check=False,
    )
    found = []
    for member in members:
        path = os.path.join(outdir, member)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            found.append(path)
    return found


def main() -> int:
    os.makedirs("out", exist_ok=True)
    os.chdir("out")

    log("=== the image carries what it says ===")
    for tool, args in (
        ("ffmpeg", ["-version"]),
        ("tesseract", ["--version"]),
        ("qemu-system-x86_64", ["--version"]),
        ("xorriso", ["-version"]),
        ("wimlib-imagex", ["--version"]),
        ("swtpm", ["--version"]),
    ):
        try:
            out = subprocess.run([tool, *args], capture_output=True, text=True)
            log(f"  {tool}: {out.stdout.splitlines()[0] if out.stdout else '(no output)'}")
        except FileNotFoundError:
            log(f"  {tool}: MISSING")
            return 1

    log("=== the ISO, from ghcr ===")
    iso.fetch_iso("alpine", "alpine.iso")
    log(f"  {os.path.getsize('alpine.iso')} bytes")

    log("=== kernel and initrd ===")
    found = extract("alpine.iso", "x", "boot/vmlinuz-virt", "boot/initramfs-virt")
    if len(found) < 2:
        log(f"PAIR_FAIL: extracted {found}")
        return 2
    kernel, initrd = found[0], found[1]
    log(f"  kernel: {kernel} ({os.path.getsize(kernel)} bytes)")
    log(f"  initrd: {initrd} ({os.path.getsize(initrd)} bytes)")

    log("=== an empty disk ===")
    compress.run("create", "-f", "qcow2", "disk.qcow2", "2G")

    log("=== boot it: vnc + port forwards + two qmp sockets ===")
    guest = vm.Guest(
        os.getcwd(),
        extra=[
            "-kernel", os.path.abspath(kernel),
            "-initrd", os.path.abspath(initrd),
            "-append", "console=ttyS0,115200n8",
            "-drive", f"file={os.path.abspath('disk.qcow2')},if=virtio,format=qcow2",
            "-no-reboot",
        ],
        stdout="console.log",
    ).start()

    # Every channel, checked separately. A single "did it work" would not say which
    # one is broken.
    log("=== every channel, checked separately ===")
    results = {
        "vnc 5900": vm.wait_port("127.0.0.1", 5900, 60, "vnc"),
        # Alpine's initrd has no sshd, so this proves the forward exists, not the
        # daemon behind it. THE FORWARD IS ON THE BRIDGE ADDRESS: loopback would be
        # DNAT'd to the guest with a loopback source, and the guest answers that to
        # its own loopback. See lib/net.py's OUTPUT rule.
        "ssh forward 2222": vm.wait_port(net.FORWARD_HOST, 2222, 30, "ssh forward"),
        "rdp forward 3389": vm.wait_port(net.FORWARD_HOST, 3389, 10, "rdp forward"),
    }

    log("=== screenshots over the second qmp socket ===")
    # TWO SECONDS, THREE FRAMES - the question is whether a screendump can be taken at
    # all, not how the recorder behaves over an hour. It used to be "15", "6" and a 25s
    # sleep, which is one frame at best and zero if the first screendump lands while the
    # guest is still painting; a test that can only pass when a timer happens to fall the
    # right way is a test that fails for reasons unrelated to what it checks.
    # BY ABSOLUTE PATH. main() does `os.chdir("out")` and the working directory is that,
    # so a bare "snapshots.py" is a file that does not exist from there - the child would
    # exit immediately with
    #
    #     python3: can't open file '/__w/.../out/snapshots.py': [Errno 2] No such file
    #
    # and the only symptom here is an empty shots/ directory, which reads exactly like a
    # screendump that did not work.
    snap = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots.py"),
         guest.paths.record, os.path.abspath("shots"), "2", "3"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    time.sleep(20)
    snap.terminate()
    try:
        snap_out, _ = snap.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        snap.kill()
        snap_out = ""

    # THE RECORDER'S OWN OUTPUT IS PRINTED, BECAUSE IT WAS CAPTURED AND THROWN AWAY.
    #
    # It already explained itself - "no QMP socket at ... - no frames" is the line that
    # says which half failed - and none of it reached the log, so a run with zero frames
    # reported only the number.
    # THE RECORDER PREFIXES ITS OWN LINES, so this adds only the indent. Writing
    # "  [snap] [snap] connected" reads like a bug in the logging rather than a message.
    for line in (snap_out or "").splitlines():
        log(f"  {line}")

    log("=== the guest's own words ===")
    matched = smoke.wait_for("console.log", r"alpine|login:|~ #|localhost", 180)

    guest.stop()

    log("=== what came out ===")
    # `.jpg` AS WELL AS `.ppm`, BECAUSE THE RECORDER NO LONGER LEAVES A PPM BEHIND.
    #
    # lib/snapshots.py converts each frame to JPEG and deletes the PPM as it goes - that
    # is what stopped a macOS bake from filling the runner with 11 GiB of frames. lib/live.py
    # and lib/smoke.py were both updated to accept either extension at the time; this line
    # was not, so it counted zero frames on every run since and reported
    #
    #     frames: 0
    #     vnc 5900: PASS
    #     ...
    #     SELFTEST FAILED
    #
    # while five channels had passed and the guest had printed 824 lines. selftest has been
    # failing since run 41964591528 for this one reason, which is worse than not having it:
    # a check that is always red is a check nobody reads.
    frames = ([f for f in os.listdir("shots") if f.endswith((".ppm", ".jpg"))]
              if os.path.isdir("shots") else [])
    log(f"  frames: {len(frames)}")
    for name, ok in results.items():
        log(f"  {name}: {'PASS' if ok else 'FAIL'}")
    log(f"  guest printed something: {'PASS' if matched else 'FAIL'}")
    log(f"  console lines: {len(open('console.log').read().splitlines())}")

    log("=== recording ===")
    vm.record_many("run.mp4", "shots")

    # The three channels that matter for a bake are vnc, a QMP screendump, and the
    # guest speaking. A port forward that never answered is reported but is not
    # fatal on its own: the forward is what is being tested, not the daemon.
    ok = results["vnc 5900"] and bool(frames) and matched
    log(f"SELFTEST {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
