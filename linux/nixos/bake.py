"""Bake a minimal NixOS base disk, by typing nixos-install at its serial console.

    python3 linux/nixos/bake.py --out out/nixos-base.qcow2

THE ONE LINUX HERE THAT IS INSTALLED AT A SHELL.

Every other distribution in this repository is driven through the screen: MX's minstall
and Mint's ubiquity are wizards, and linux/mx clicks them at measured coordinates. NixOS
has no such installer. It has `nixos-install`, and the documented way to install from a
live medium is to partition the disk yourself and run it. So the bake does the same
thing, with the commands written to the serial console instead of typed at a keyboard.

WHERE THIS COMES FROM. easysandbox-vm's bake/nixos/bake-nixos-minimal.sh has installed
NixOS this way for months, and three of its findings are load-bearing here:

    THE KERNEL IS READ FROM grub.cfg, not chosen by name. The ISO carries more than one
    kernel - boot/nix/store/<hash>-linux-6.18.59/bzImage and one for 7.3 - so any rule
    that picks "the newest" or "the first found" pairs a kernel with the wrong initrd.
    The image states the pairing in its own menu entry, and that line is what this uses.

    THE WHOLE INSTALL IS ONE COMMAND LINE ENDING IN poweroff. Install, report the exit
    status, sync, shut down. Nothing waits for the host to notice a marker and inject
    another keystroke - two attempts at that left a finished install sitting until the
    timeout.

    THE RESULT IS THE GUEST'S EXIT STATUS, not a phrase in the log. A failed install
    prints plenty of text; INSTALL_RC= is the only line that says whether it worked.

WHAT THIS DOES NOT DO

It does not install a desktop. The desktops are derived from this disk by
linux/nixos/derive.py, which boots it and runs nixos-rebuild - because partition, format
and unpack are the same for every flavour and nixos-install pays that cost each time.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from lib import compress, iso, live as livelib, serial, smoke, vm  # noqa: E402

NIXOS_TAG = "iso-nixos"
CONFIG_NAME = "configuration.nix"
CONFIG_PORT = 8000


def log(message: str) -> None:
    print(f"[nixos {time.strftime('%H:%M:%S')}] {message}", flush=True)


def boot_entry_from_grub(iso_path: str) -> tuple[str, str, str, str]:
    """The kernel and initrd that the ISO's default menu entry names.

    THE PAIRING IS THE IMAGE'S CLAIM, NOT AN INFERENCE. The ISO carries two kernels -
    6.18.60 and 7.3.1 - so any rule that picks one by name ("newest", "first found") pairs
    a kernel with another build's initrd. easysandbox-vm records exactly that mistake.

    TWO FORMATS, AND THE NEWER ONE IS WHY THIS RUN FAILED. Read from
    NixOS 26.05's own EFI/BOOT/grub.cfg:

        menuentry 'NixOS 26.05... Installer (Linux LTS)' --class installer {
          linux /boot//nix/store/<hash>-linux-6.18.60/bzImage ${isoboot} init=... \\
                nohibernate root=fstab loglevel=4 lsm=landlock,yama,bpf
          initrd /boot//nix/store/<hash>-initrd-linux-6.18.60/initrd
        }

    The initrd is on ITS OWN LINE, named by GRUB's `initrd` directive - not appended to
    the `linux` line as `initrd=...`, which is the form easysandbox-vm's older image used
    and the only form this function first understood. Run 41591542701 died on that
    difference at once: "no 'linux ... initrd=...' line", four minutes in, before any
    guest started.

    So both are read: an `initrd=` argument on the linux line if there is one, and
    otherwise the first `initrd <path>` line after it. The first menu entry is the
    default one, which is what a person pressing Enter at the boot menu gets.

    THE DOUBLE SLASH IS THE IMAGE'S. `/boot//nix/store/...` appears in grub.cfg because
    the path is built from a variable, and it is harmless - path lookups collapse it - but
    it is why the returned paths are not run through a normaliser.

    THE ENTRY ALSO NAMES init=, AND LEAVING IT OUT IS WHY RUN 41605220712 STOPPED DEAD.

    The boot stops in the initrd:

        [FAILED] Failed to start Find NixOS closure.
        Dependency failed for Initrd Default Target.
        You are in emergency mode.

    "Find NixOS closure" is the initrd looking for the path in `init=`. This bake boots
    the kernel directly instead of through the media's GRUB, so nothing expanded the
    menu entry - and the kernel command line it built named no init at all. Without one
    the initrd has no closure to mount and drops to a shell.

    It is returned here rather than hardcoded because the path carries the build hash of
    a specific NixOS release, and that hash changes with every channel update.
    """
    cfg = subprocess.run(
        ["7z", "e", "-so", iso_path, "EFI/BOOT/grub.cfg"],
        capture_output=True, text=True,
    )
    if cfg.returncode != 0 or not cfg.stdout:
        raise SystemExit(
            f"could not read EFI/BOOT/grub.cfg from {iso_path}: "
            f"{(cfg.stderr or '').strip()[:200]}"
        )

    kernel = ""
    initrd = ""
    init = ""
    for raw in cfg.stdout.splitlines():
        line = raw.strip()
        if not kernel and line.startswith("linux "):
            entry_line = line
            parts = line.split()
            if len(parts) < 2:
                continue
            kernel = parts[1]
            # The older form: initrd named as an argument on this same line.
            for part in parts:
                if part.startswith("initrd="):
                    initrd = part[len("initrd="):]
                elif part.startswith("init="):
                    init = part[len("init="):]
            continue
        if kernel and not initrd and line.startswith("initrd "):
            parts = line.split()
            if len(parts) >= 2:
                initrd = parts[1]
            continue
        # The first entry's block is what matters; a later one is a different kernel.
        if kernel and initrd and init:
            return kernel, initrd, init, entry_line

    if kernel and initrd and init:
        return kernel, initrd, init, entry_line
    raise SystemExit(
        f"no kernel/initrd/init set in {iso_path}'s grub.cfg: "
        f"kernel={'found' if kernel else 'missing'}, "
        f"initrd={'found' if initrd else 'missing'}, "
        f"init={'found' if init else 'missing'}. The image's layout changed and "
        f"this needs looking at rather than guessing."
    )


def extract(iso_path: str, wanted: str, dest: str) -> str:
    """Pull one file out of the ISO by its path suffix, and return where it landed.

    THE FILTER IS THE BASENAME, NOT THE WHOLE PATH. 7z treats its pattern as a wildcard
    and a value containing slashes matches nothing - measured here: passing
    "nix/store/<hash>-linux-6.18.60/bzImage" extracted zero files while "bzImage" got
    both of the image's two kernels. So the basename picks the candidates and the FULL
    suffix decides which one, which is the half that matters: the two kernels live at
    different store paths and only one is paired with the initrd this run needs.
    """
    leaf = os.path.basename(wanted.replace("\\", "/"))
    result = subprocess.run(
        ["7z", "x", "-y", "-r", f"-o{dest}", iso_path, leaf],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"7z could not extract {leaf} from {iso_path}: "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )

    suffix = wanted.replace("\\", "/").lstrip("./")
    found = []
    for root, _dirs, files in os.walk(dest):
        for name in files:
            path = os.path.join(root, name)
            if path.replace("\\", "/").endswith(suffix):
                found.append(path)
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit(
            f"{wanted} is named by the ISO's menu entry but was not in the extracted "
            f"tree under {dest}. What was extracted: "
            + (", ".join(os.path.join(r, f) for r, _d, fs in os.walk(dest) for f in fs)
               or "nothing")
        )
    raise SystemExit(
        f"{wanted} matched {len(found)} files under {dest}: {found}. The menu entry "
        f"names one store path and more than that cannot be what it meant."
    )


def partition_script() -> list[tuple[str, float]]:
    """The commands, and how long each needs, measured from a working run.

    ONE LINE PER STEP, AND A WAIT THAT IS THE COMMAND'S OWN TIME. There is nothing to
    poll: the console is a character stream, and waiting for a prompt string would be
    reading the screen for a verdict in text form. easysandbox-vm's numbers are used
    unchanged because the guest and the disk are the same shape.

    PARTITION, NOT THE BARE DISK. grub-install refuses to embed core.img in the MBR gap
    when a filesystem starts at sector 0 ("will not proceed with blocklists"), so
    parted leaves the first MiB free and the root is addressed by LABEL - a UUID only
    exists at install time and would pin the disk to one bake.
    """
    return [
        ("echo BAKE_BEGIN", 3),
        ("curl -fsS -o /tmp/c.nix http://10.0.2.2:%d/%s && wc -l /tmp/c.nix"
         % (CONFIG_PORT, CONFIG_NAME), 8),
        ("sudo sgdisk --zap-all /dev/vda >/dev/null 2>&1 || true", 2),
        ("sudo parted -s /dev/vda mklabel msdos", 3),
        ("sudo parted -s /dev/vda mkpart primary ext4 1MiB 100%", 3),
        ("sudo partprobe /dev/vda 2>/dev/null || true", 3),
        ("sudo mkfs.ext4 -F -L nixos /dev/vda1", 15),
        ("sudo mount /dev/vda1 /mnt", 5),
        ("sudo mkdir -p /mnt/etc/nixos", 3),
        ("sudo cp /tmp/c.nix /mnt/etc/nixos/configuration.nix", 3),
        # THE INSTALL, THE STATUS, THE SYNC AND THE SHUTDOWN IN ONE LINE. The
        # semicolons matter: a failure in nixos-install must still print INSTALL_RC
        # and still power off, or the run sits until the outer timeout with a finished
        # or failed install nobody can see.
        # THE VERDICT GOES TO A FILE AS WELL AS THE CONSOLE.
        #
        # Run 41608440365 finished the install - the log stops on "b", the first letter
        # of nixos-install's closing "booting the system..." - and then the machine was
        # gone, so INSTALL_RC never reached the serial console. The exit status is the
        # only thing that says whether the install worked, and it was the one line that
        # did not survive the shutdown.
        #
        # So it is written to the new system's own /mnt/easydiskbake-rc first, where it
        # is on the disk this bake is about to compress. install_status() reads that file
        # back through the qcow2 if the console did not carry it.
        #
        # The sleep before poweroff gives the serial drain time to flush; without it the
        # last line can be lost between the write and qemu noticing the machine stopped.
        ("sudo nixos-install --root /mnt --no-root-password --no-channel-copy 2>&1; "
         "echo INSTALL_RC=$? | sudo tee /mnt/easydiskbake-rc; "
         "sync; sleep 5; sudo poweroff -f", 40),
    ]


def serve_config(work: str, release: str) -> subprocess.Popen:
    """Serve configuration.nix to the guest over qemu's user network.

    WHY HTTP AND NOT THE SERIAL. The file is a hundred lines of Nix with quotes, braces
    and newlines. `type_text` cannot send a newline and a heredoc over a serial line
    needs every `$` and quote escaped twice. qemu's user netdev puts the host at
    10.0.2.2, so the guest curls the file - which is what easysandbox-vm does and the
    reason its bake needs no network beyond that.
    """
    srv = os.path.join(work, "srv")
    os.makedirs(srv, exist_ok=True)
    src = os.path.join(HERE, f"configuration-{release}.nix")
    if not os.path.isfile(src):
        raise SystemExit(f"no {src}")
    with open(src, "rb") as fh:
        body = fh.read()
    with open(os.path.join(srv, CONFIG_NAME), "wb") as fh:
        fh.write(body)
    return subprocess.Popen(
        [sys.executable, "-m", "http.server", str(CONFIG_PORT), "--bind", "0.0.0.0"],
        cwd=srv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/nixos-base.qcow2")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=6144)
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--disk-size", default="20G")
    p.add_argument("--timeout", type=int, default=3600,
                   help="seconds the whole guest may run")
    args = p.parse_args(argv)

    work = args.work or os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(work, exist_ok=True)

    log(f"out={args.out}")
    iso_path = os.path.join(work, "nixos.iso")
    if not os.path.isfile(iso_path):
        log(f"pulling {NIXOS_TAG} from ghcr")
        try:
            iso.fetch_tag(NIXOS_TAG, iso_path)
        except iso.IsoError as exc:
            raise SystemExit(
                f"{NIXOS_TAG} is not published: {exc}\n\n"
                f"Run iso-publish.yml for nixos first. Nothing here downloads from a "
                f"mirror at run time - see lib/iso.py."
            ) from exc
    log(f"iso {os.path.getsize(iso_path) >> 20} MiB")

    kernel_path, initrd_path, init_path, entry = boot_entry_from_grub(iso_path)
    log(f"the ISO's menu entry names kernel={kernel_path}")
    log(f"                                  initrd={initrd_path}")
    log(f"                                  init={init_path}")

    tree = os.path.join(work, "x")
    kernel = extract(iso_path, kernel_path.lstrip("/boot/"), tree)
    initrd = extract(iso_path, initrd_path.lstrip("/boot/"), tree)
    log(f"kernel {kernel}")
    log(f"initrd {initrd}")

    disk = os.path.join(work, "disk.qcow2")
    if os.path.exists(disk):
        os.unlink(disk)
    compress.run("create", "-f", "qcow2", disk, args.disk_size)
    log(f"disk created: {args.disk_size}")

    server = serve_config(work, "minimal")
    log(f"configuration served on :{CONFIG_PORT} (the guest reaches it at 10.0.2.2)")

    # THE SERIAL IS A PAIR OF FIFOS - see lib/serial.py for why, and for why this is
    # the only bake here that writes to a console instead of looking at a screen.
    ser = serial.Serial(work, "serial")
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        firmware_code="", firmware_vars="",
        vga="std", cpu="", smp_topology="",
        serial_pipe=ser.stdin.removesuffix(".in"),
        # THE COMMAND LINE THE MEDIA'S OWN ENTRY WOULD HAVE BUILT, MINUS GRUB.
        #
        # `init=` IS THE WHOLE FIX, AND I WROTE IT WITHOUT THE PREFIX ONCE. The first
        # attempt passed the closure path as a bare argument:
        #
        #     /nix/store/...-nixos-system-.../init edd=off nohibernate ...
        #
        # which the kernel treats as an unknown token. initrd-find-nixos-closure then
        # read /proc/cmdline, found no init=, and failed exactly as before - so the fix
        # looked like it had not worked, when it had never been applied.
        #
        # NO findiso=, AND THE INITRD'S OWN fstab SAYS WHY. It contains
        #
        #     /dev/disk/by-label/nixos-minimal-26.05-x86_64  /iso  iso9660
        #     /sysroot/iso/nix-store.squashfs  /nix/.ro-store  squashfs loop
        #
        # so the medium is found by the ISO's VOLUME LABEL, which this image sets to
        # exactly that string - verified by reading sector 16 of the ISO. The ${isoboot}
        # GRUB expands to `findiso=<path>` is for booting the ISO from a FILE on another
        # filesystem, which is not what attaching it as a cdrom does.
        #
        # console= is added because this runs headless and the entry names none.
        kernel_append=(
            f"init={init_path} "
            "edd=off nohibernate root=fstab loglevel=4 "
            "lsm=landlock,yama,bpf console=ttyS0,115200n8"
        ),
    )
    drives = [
        "-kernel", kernel,
        "-initrd", initrd,
        "-drive", f"file={iso_path},media=cdrom,readonly=on",
        "-drive", f"file={disk},if=virtio,format=qcow2",
    ]
    # stdout IS /dev/null ON PURPOSE. This guest's console is a chardev on a fifo
    # (Shape.serial_pipe), so qemu's own stdout carries nothing but warnings - and the
    # first version pointed it at console.log, which made an empty file that looked like
    # a guest saying nothing. The console is ser.sink, and ser prints every line.
    guest = vm.Guest(work, extra=drives, shape=shape, stdout=os.devnull)
    try:
        guest.start()
        ser.open(timeout=60)

        # THE SCREEN TOO. This installer is installed by typing, but it draws - and a
        # cancelled run's last frame says which step it had reached without reading the
        # console. See lib/live.py for why both artifacts are kept current.
        shots = os.path.join(work, "frames")
        os.makedirs(shots, exist_ok=True)
        snap = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "lib", "snapshots.py"),
             guest.paths.record, shots,
             os.environ.get("SNAPSHOT_INTERVAL", "5"), "2000"],
            stdout=open(os.path.join(shots, "snapshots.log"), "wb"),
            stderr=subprocess.STDOUT,
        )
        live = livelib.start(
            [shots], os.path.join(work, "install.mp4"),
            os.path.join(work, "last-frame.png"),
            every=float(os.environ.get("LIVE_ARTIFACT_SECONDS", "20")),
        )

        # 115 seconds for the live medium to reach a root shell. Measured: the ISO's
        # own boot takes a little under two minutes before it prints the prompt.
        log("waiting 115s for the live medium to reach a shell")
        time.sleep(115)
        # THE LAST COMMAND ENDS THE MACHINE, SO THE SERIAL STAYS OPEN UNTIL IT DOES.
        #
        # The loop's `wait` is the gap between commands, not a deadline - and the final
        # entry is `nixos-install ...; sync; sleep 5; poweroff`, which takes minutes.
        # Closing the writer after its 40-second gap gave qemu EOF on the serial chardev
        # and qemu exited there, mid-install: run 41609769569 shows the console stopping
        # on "bu" (the start of "building ...") and `the guest powered off` fifty-one
        # seconds later, with neither INSTALL_RC nor the disk fallback present because
        # the command never reached its own echo.
        #
        # So everything but the last line is sent with its gap, the last line is sent
        # without a further wait, and the drain thread keeps reading until the guest is
        # gone. ser.close() happens in the finally block.
        script = partition_script()
        for index, (line, wait) in enumerate(script):
            last = index == len(script) - 1
            ser.say(line, wait=0 if last else wait)

        log(f"waiting up to {args.timeout}s for the guest to power itself off")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if not guest.alive():
                log("the guest powered off")
                break
            time.sleep(10)
        else:
            log(f"=== THE GUEST DID NOT POWER OFF IN {args.timeout}s ===")
    finally:
        guest.stop()
        # CLOSED AFTER THE MACHINE, NOT BEFORE IT. Closing the writer earlier sends EOF
        # down qemu's serial chardev, which qemu treats as the console going away.
        ser.close()
        snap.terminate()
        try:
            snap.wait(timeout=5)
        except subprocess.TimeoutExpired:
            snap.kill()
        live.stop(final=True)
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    # THE CONSOLE IS COPIED TO out/console.log, WHICH IS WHAT THE WORKFLOW UPLOADS.
    #
    # ser.sink lives in the work directory beside the disk, and the workflow's artifact
    # path names out/console.log - so the guest's own words were never in the artifact,
    # and a failed run explained itself only through whatever happened to reach the
    # check-run's 65535-character budget. That is how run 41607302540 came to report
    # "exit code 1" and nothing else.
    console = os.path.join(os.path.dirname(os.path.abspath(args.out)) or ".", "console.log")
    try:
        with open(ser.sink, "rb") as src, open(console, "wb") as dst:
            dst.write(src.read())
        log(f"console: {console} ({os.path.getsize(console) >> 10} KiB)")
    except OSError as exc:
        log(f"could not copy the console to {console}: {exc}")
        console = ser.sink

    rc = install_status(console, disk)
    log(f"nixos-install exit status: {rc if rc is not None else '<never returned>'}")
    if rc != 0:
        log("=== THE INSTALL FAILED ===")
        first_error(console)
        return 1

    if not smoke.check_disk(disk, root_fs="ext4"):
        log("=== THE DISK FAILED ITS CHECKS; refusing to produce an artifact ===")
        return 1

    log("compressing")
    compress.compress(disk, args.out)
    log(f"BAKED nixos-base -> {args.out} ({os.path.getsize(args.out) >> 20} MiB)")
    return 0


def install_status(console: str, disk: str | None = None) -> int | None:
    """The guest's own exit status, from the console or from the disk it wrote.

    THE LAST ONE, because a retried install prints the line twice and it is the second
    that decided.

    THE DISK IS THE FALLBACK, AND RUN 41608440365 IS WHY IT EXISTS. That run finished
    nixos-install - the guest's last output is "b", the first letter of the closing
    "booting the system..." - and then the machine was gone with INSTALL_RC never
    reaching the serial port. The status line is the whole verdict, and it was the one
    line that did not survive `poweroff -f`.

    So the bake also writes it to /mnt/easydiskbake-rc in the NEW SYSTEM, which is on
    the disk about to be compressed. Reading it back needs the qcow2 mounted, which
    debugfs can do for ext4 without root.
    """
    try:
        with open(console, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        text = ""
    found = re.findall(r"INSTALL_RC=(\d+)", text)
    if found:
        return int(found[-1])

    if disk is None:
        return None

    # The console did not carry it. Ask the disk.
    import subprocess

    log("  the console carries no INSTALL_RC; reading it off the disk")
    for path in ("/easydiskbake-rc", "/mnt/easydiskbake-rc"):
        for tool in ("debugfs",):
            try:
                result = subprocess.run(
                    [tool, "-R", f"cat {path}", disk],
                    capture_output=True, text=True, timeout=120,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            match = re.search(r"INSTALL_RC=(\d+)", result.stdout or "")
            if match:
                log(f"  {path} on the disk says INSTALL_RC={match.group(1)}")
                return int(match.group(1))
    log("  the disk carries no INSTALL_RC either")
    return None


def first_error(console: str, context: int = 25) -> None:
    """Print the FIRST error with its surroundings.

    Not the last sixty lines: a Nix assertion or a failed mount sits near the top, and
    the tail is only the prompt coming back. Same reasoning as easysandbox-vm's
    bake_assert_installed.
    """
    try:
        with open(console, "rb") as fh:
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        log(f"(no console log at {console})")
        return
    index = None
    for i, line in enumerate(lines):
        if re.search(r"error:|ERROR|Traceback|failed", line):
            index = i
            break
    if index is None:
        for line in lines[-40:]:
            log(f"  [console] {line}")
        return
    start = max(0, index - 6)
    for line in lines[start:start + context + 6]:
        log(f"  [console] {line}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
