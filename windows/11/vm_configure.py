"""Configure the VM after ssh: the guest agent, the host share, and the logon hook.

    python3 windows/11/vm_configure.py --ssh out/windows11-ssh.qcow2 --out out/windows11-vm-configure.qcow2

WHAT CHANGED AND WHY THE NAME CHANGED WITH IT

This was `share.py`, because an SMB credential was the only thing it installed. It now also puts
the qemu guest agent on the disk:

    the qemu guest agent     a shutdown that survives a guest whose sshd has died
    an SMB credential        a directory the person can put files in
    a logon task             something that runs those files at logon

Windows honours an ACPI power button, so it was never blocked the way macOS was - but the agent
is the only shutdown route that works when the network does not, and installing it here means every
later stage can stop the machine without relying on ssh still being up.

WHY IT IS STILL BEFORE THE TOOLCHAIN

It needs nothing installed. SMB is native to Windows and the launcher is PowerShell 5.1, which
ships with Windows 11. The agent is one MSI.

WHAT IT INSTALLS, AND NOTHING ELSE

    a credential for an SMB share on the bridge, served by this container (lib/hostshare.py)
    qemu-ga, from the virtio-win guest tools
    a Task Scheduler task at ONLOGON that runs ~/shared/bootstrap.ps1

No winget package, no compiler, no interpreter.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))

import net  # noqa: E402
import vm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# THE WALKTHROUGH THIS STAGE RUNS. Beside the stage, for the same reason as ssh.py's.
WALK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steps", "vm-configure.yaml")

SSH_PORT = 2222
SSH_USER = "docker"
SSH_PASSWORD = os.environ.get("WINDOWS_ADMIN_PASSWORD", "admin")

# THE FORWARDED PORT EXISTS ON THE BRIDGE ADDRESS AND NOWHERE ELSE. A probe to
# 127.0.0.1:2222 is DNAT'd to the guest with a loopback source, and the guest answers
# that to its own loopback, so the connect hangs instead of being refused. lib/net.py
# records the measurement.
SSH_HOST = net.FORWARD_HOST

# The workload is the slow part: a fresh machine takes about twenty minutes, and the
# allowance covers a cold Windows Update check on top.
INSTALL_TIMEOUT = 60 * 60


def log(msg: str) -> None:
    print(f"[devtools {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# (package id, version or None, override or None, why)
#
# THE ORDER IS THE POINT, not alphabetical convenience: PowerShell 7 goes last because its
# MSI restarts the session it is installed through, and everything after it would be
# running over a connection that no longer exists.
#
# THE FIFTH FIELD IS THE PROBE, AND IT IS WHAT REPLACED ASKING WINGET.
#
# Run 41778185290 skipped the .NET SDK entirely because `winget list` contained a package
# whose id starts with "Microsoft.DotNet", and nothing was on disk:
#
#     === Microsoft.DotNet.SDK.10 already installed ===
#     candidate: C:\Program Files\dotnet\dotnet.exe exists=False
#
# THE PACKAGE LIST AND ITS PROBES LIVED HERE and are windows/11/steps/dev.yaml now, beside the
# steps that run them. What is left of the reasoning is the part a reader needs while reading
# this file: winget INSTALLS, and it is not what decides whether to - run 41778185290 skipped
# the .NET SDK because `winget list` matched a different package whose id shared a prefix, and
# nothing was on disk.
#
# windows/11/probe.ps1 is still here for the steps that ask a package about itself.


def ssh_base() -> list[str]:
    return [
        "sshpass", "-p", SSH_PASSWORD, "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-p", str(SSH_PORT), f"{SSH_USER}@{SSH_HOST}",
    ]


def ssh_run(command: str, *, timeout: int = 1800) -> subprocess.CompletedProcess:
    log(f"ssh> {command[:110]}")
    return subprocess.run(ssh_base() + [command], capture_output=True, text=True,
                          timeout=timeout)


def scp_to(local: str, remote: str, *, timeout: int = 300) -> None:
    result = subprocess.run(
        ["sshpass", "-p", SSH_PASSWORD, "scp",
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-P", str(SSH_PORT), local, f"{SSH_USER}@{SSH_HOST}:{remote}"],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"could not copy {os.path.basename(local)} to {remote}: "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )


def probe(name: str) -> tuple[bool, str]:
    """Is this package on the machine, asked of the machine.

    THE SAME SCRIPT THE VERIFICATION USES, so "already installed, skipped" and "verified"
    cannot mean different things. A package that the probe says is present but the
    verification cannot use is a bug in one of them, and that is worth seeing.

    A probe that cannot RUN is not "present". Anything other than exit 0 means install.
    """
    local = os.path.join(HERE, "probe.ps1")
    if not os.path.isfile(local):
        raise SystemExit(f"{local} is missing; the install order cannot be decided without it")
    remote = f"C:/Users/{SSH_USER}/probe.ps1"
    scp_to(local, remote)
    result = ssh_run(
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {remote} {name}",
        timeout=300,
    )
    text = ((result.stdout or "") + (result.stderr or "")).strip()
    detail = ""
    for line in text.splitlines():
        if line.strip() and not line.startswith("Warning: Permanently added"):
            detail = line.strip()
    return (result.returncode == 0, detail)


def wait_for_ssh(seconds: int, why: str = "") -> bool:
    import socket

    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((SSH_HOST, SSH_PORT), timeout=5) as sock:
                sock.settimeout(5)
                if sock.recv(64).startswith(b"SSH-"):
                    return True
        except OSError:
            pass
        time.sleep(5)
    log(f"sshd stopped answering{': ' + why if why else ''}")
    return False


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ssh", required=True, help="the disk windows/11/ssh.py produced")
    p.add_argument("--out", default="out/windows11-vm-configure.qcow2")
    p.add_argument("--copy", action="store_true",
                   help="copy the input disk instead of moving it; the default is to "
                        "move, because the disk normally came from ghcr and the "
                        "registry protects it")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=8192,
                   help="MB; the BuildTools installer is what needs this")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--no-clean", dest="clean", action="store_false",
                   help="leave the disk as it is; the workflow runs lib/clean_step.py next")
    p.add_argument("--tpm", action="store_true",
                   help="provide a TPM 2.0; off by default, because the answer file "
                        "bypasses the check and the disk boots without one")
    p.add_argument("--bootstate", default=None,
                   help="IGNORED. The disk carries \\EFI\\BOOT\\BOOTX64.EFI and this "
                        "machine uses a fresh variable store; kept so an existing command "
                        "line still parses")
    args = p.parse_args(argv)

    if not os.path.isfile(args.ssh):
        log(f"no SSH disk at {args.ssh}")
        return 1
    if not shutil.which("sshpass"):
        raise SystemExit(
            "sshpass is not installed. The previous stage configures password auth, so "
            "this one needs a client that can answer the prompt without a terminal."
        )

    work = args.work or os.path.join(os.path.dirname(os.path.abspath(args.out)),
                                     "devtools-work")
    os.makedirs(work, exist_ok=True)

    # A REAL COPY, NOT A QCOW2 BACKING FILE.
    #
    # The obvious design is `qemu-img create -b <gold> -F qcow2`, which costs nothing until
    # something writes. It does not work against a disk this repository publishes, and the
    # error says why:
    #
    #     qemu-img: Unsupported qcow2 feature(s): compression type
    #     Could not open backing image to determine size.
    #
    # qcow2 refuses a compressed image as a backing file, and every disk here is published
    # with -o compression_type=zstd for size. So the reuse is a copy.
    #
    # `cp --reflink=auto` asks the filesystem for copy-on-write (btrfs, xfs with reflink)
    # and falls back to a byte copy where it cannot. On a CI overlay filesystem that is the
    # latter, which is why the free-space check in the workflow asks for the disk twice.
    disk = os.path.join(work, "disk.qcow2")
    # MOVED, NOT COPIED. The disk this stage starts from came from ghcr, so the
    # registry still holds it - a copy protects nothing and costs minutes on ext4.
    # --copy is here for the case where it IS the only copy.
    import working_copy as wc
    disk = wc.working_copy(args.ssh, os.path.join(work, "disk.qcow2"), copy=args.copy, note=log)

    firmware_code = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd")
    firmware_vars = os.path.join(work, "OVMF_VARS.fd")
    template = os.environ.get("OVMF_VARS_TEMPLATE", "/usr/share/OVMF/OVMF_VARS_4M.fd")
    # A FRESH TEMPLATE, ALWAYS - the disk carries its own loader at the UEFI fallback path
    # \EFI\BOOT\BOOTX64.EFI, so nothing has to be restored for it to start. See the longer
    # note in ssh.py; this stage had the same unconditional bootstate requirement and the
    # same failure when the artifact was not there.
    shutil.copyfile(template, firmware_vars)
    if args.bootstate:
        log(f"--bootstate {args.bootstate} IGNORED: the disk carries its own loader at "
            f"\\EFI\\BOOT\\BOOTX64.EFI and this machine uses a fresh variable store")

    # NO TPM BY DEFAULT, the same as bake.py and ssh.py.
    tpm = start_swtpm(work) if args.tpm else ""

    # THE AGENT'S CHANNEL - see the note in macos/15/vm_configure.py. Windows uses virtio-serial,
    # which is what qemu-ga expects.
    qga_socket = os.path.join(work, "qga.sock")
    if os.path.exists(qga_socket):
        os.unlink(qga_socket)
    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        firmware_code=firmware_code, firmware_vars=firmware_vars,
        tpm_socket=tpm, vga="std", ssh_port=SSH_PORT,
        qga_socket=qga_socket, qga_transport="virtio",
    )
    # THE DISK HAS TO BE ATTACHED - see the longer note in ssh.py. This stage had the
    # same `extra=[]`, so it started a machine with no drive and every later symptom
    # (bootmgfw.efi Not Found, no FS0 in the firmware map) described an empty machine.
    drives = ["-drive", f"file={disk},if=virtio,format=qcow2"]
    guest = vm.Guest(work, extra=drives, shape=shape,
                     stdout=os.path.join(work, "console.log")).start()

    ok = False
    try:
        # THE ARRIVAL CHECK IS SSHD, not a desktop. The previous stage already proved this
        # disk reaches one; what this stage needs is the service, and a TCP banner is a
        # better answer than a picture.
        log(f"waiting up to {args.boot_timeout}s for sshd")
        if not wait_for_ssh(args.boot_timeout, "the disk may not be the one ssh.py made"):
            return 1

        # THE TOOLCHAIN IS A FILE NOW: windows/11/steps/dev.yaml.
        #
        # What stood here - skip the packages that are already present, install the rest in
        # order, re-check sshd after each because an MSI can restart it, then verify - was
        # four measurements and no decision. The measurements belong beside the numbers they
        # produced; the decision ("does this stage pass?") stays here.
        #
        # THE WALK RUNS OVER SSH, so it takes the headless driver lib/steps.py builds when a
        # file has no GUI steps. This stage never touches the screen: the disk arrived with a
        # working sshd, and everything it does is a command.
        log(f"=== the walkthrough: {os.path.relpath(WALK, REPO)} ===")
        import steps as stepslib
        ok_walk, ran = stepslib.run_file(
            WALK, qmp_path="", work=work, ssh_port=SSH_PORT,
            user=SSH_USER, password=SSH_PASSWORD, release="11",
        )
        log(f"{len(ran)} step(s) answered" if ok_walk
            else f"the walkthrough failed after {len(ran)} step(s)")
        if not ok_walk:
            return 1

        # CLEANING IS ITS OWN WORKFLOW STEP, so this stage can be asked to skip it.
        #
        # --no-clean is what the workflow passes; lib/clean_step.py runs as the next step and
        # boots the disk to clean it. The cost is one extra boot; what it buys is a step in
        # the run graph that fails visibly, instead of a line inside this stage's log.
        #
        # The default is to clean, because an uncleaned dev disk carries its installers and
        # its payload caches into the published artifact.
        if args.clean:
            from lib.clean_disk import clean_running

            if not clean_running("windows", port=SSH_PORT, note=log):
                log("=== THE GUEST WAS NOT CLEANED; THE DISK IS NOT PUBLISHED ===")
                return 1
        else:
            log("not cleaning here; the next step does it (lib/clean_step.py)")

        ok = True
        return 0
    finally:
        # WINDOWS HONOURS THE ACPI POWER BUTTON, so this is a real shutdown on this family -
        # unlike macOS, where the same request opens a dialog. It is named rather than inferred:
        # `acpi` means "the power button", and a guest that ignores it will say so by not exiting
        # rather than by quietly becoming a kill.
        import vm_session
        vm_session.stop(guest, family="windows", method="acpi", share=True, log=log)
        if args.tpm:
            stop_swtpm(work)
        if ok:
            log(f"exporting {args.out}")
            # compress.compress(), for the same reason as windows/11/ssh.py: the bare
            # `qemu-img` on this image's PATH is the Android SDK's, and it cannot open the
            # compressed disk this stage is handed. Run 41586561730 failed here after a
            # successful installation with
            #
            #     qemu-img: Could not open '.../disk.qcow2':
            #     Unsupported qcow2 feature(s): compression type
            #
            # which read like a corrupt disk and was a shadowed binary.
            import compress

            compress.compress(disk, args.out)
            log(f"BAKED {args.out} ({os.path.getsize(args.out) >> 20} MiB)")


def start_swtpm(work: str) -> str:
    state = os.path.join(work, "tpm")
    os.makedirs(state, exist_ok=True)
    sock = os.path.join(state, "swtpm.sock")
    if not shutil.which("swtpm"):
        raise SystemExit("swtpm is not installed; Windows 11 will not boot without a TPM")
    subprocess.run(["swtpm", "socket", "--tpmstate", f"dir={state}",
                    "--ctrl", f"type=unixio,path={sock}", "--tpm2", "--daemon"],
                   check=True)
    return sock


def stop_swtpm(work: str) -> None:
    subprocess.run(["pkill", "-f", os.path.join(work, "tpm")], check=False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
