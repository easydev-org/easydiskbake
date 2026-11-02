"""Give the SSH disk a developer toolchain, and prove a compiler runs.

    python3 windows/11/devtools.py --ssh out/windows11-ssh.qcow2 --out out/windows11-dev.qcow2

WHERE THESE CHOICES COME FROM

easysandbox-windows has already run this four times and recorded what failed. Its
workflow publishes the result under the tag `dev` - base, then ssh, then this - and its
scripts/sdk_install.py and scripts/ps/msvc-check.ps1 are the measured version. The
reasoning below is theirs, kept because each line cost a run.

WHAT IT INSTALLS

    Microsoft.VisualStudio.2022.BuildTools   17.14.44, --override with the VCTools
                                             workload. The Windows SDK and CMake come
                                             with the workload.
    Microsoft.WindowsSDK.10.0.26100          the SDK on its own
    Microsoft.DotNet.SDK.10                  VCTools does not include a .NET SDK
    Microsoft.PowerShell                     PowerShell 7; LAST, because its MSI can drop
                                             the ssh session it is running over

WHY A PINNED WINGET VERSION AND NOT THE EVERGREEN BOOTSTRAPPER

Two other routes were tried and both failed:

    17.9.7, what winget resolves from a stale index, carries a manifest that caps the
    supported OS at 10.0.22000. This guest is 10.0.26200, so the installer marks every
    component "not applicable", installs nothing, and exits 1 - while printing a version
    range that reads like a configuration problem.

    aka.ms/vs/17/release/vs_BuildTools.exe got further, fetching v17.14, and then its
    extraction directory came out empty. That failure is still unexplained.

winget carries 17.14.44, whose manifest has no such cap. Pinning version and workload
together is one mechanism instead of two, and winget does the download and the elevation
itself.

WHY THE VERIFICATION COMPILES SOMETHING

A winget exit code says an installer ran, not that a compiler exists. The last step here
compiles and runs a C program through vcvars64, and prints `dotnet --version`. "The SDK
installed" and "the toolchain works" are different claims and only the second one matters
to whoever boots this disk.

WHY SSH MAKES THIS SHORT

windows/11/ssh.py types every character through QMP, so its commands are one line each.
Here sshd exists, so a script is a FILE: written on the host, copied with scp, run by
path. That is how the msvc check avoids escaping a C source through python, then
PowerShell, then cmd.
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

import vm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

SSH_PORT = 2222
SSH_USER = "docker"
SSH_PASSWORD = os.environ.get("WINDOWS_ADMIN_PASSWORD", "docker")

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
PACKAGES = [
    ("Microsoft.VisualStudio.2022.BuildTools", "17.14.44",
     "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools "
     "--includeRecommended",
     "MSVC, the Windows SDK and CMake, from a version whose manifest knows this OS"),
    ("Microsoft.WindowsSDK.10.0.26100", None, None, "the SDK on its own"),
    ("Microsoft.DotNet.SDK.10", None, None, "the .NET SDK, which VCTools does not include"),
    ("Microsoft.PowerShell", None, None,
     "PowerShell 7; last because its MSI can drop the ssh session"),
]


def ssh_base() -> list[str]:
    return [
        "sshpass", "-p", SSH_PASSWORD, "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-p", str(SSH_PORT), f"{SSH_USER}@127.0.0.1",
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
         "-P", str(SSH_PORT), local, f"{SSH_USER}@127.0.0.1:{remote}"],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"could not copy {os.path.basename(local)} to {remote}: "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )


def installed_packages() -> set[str]:
    """Ask winget what is on the machine, once, instead of per package.

    A package that is already installed is skipped rather than reinstalled: the BuildTools
    install alone is twenty minutes, and a re-run of this stage after a failure at the
    verification step should not pay for it again.
    """
    result = ssh_run("winget list --source winget", timeout=300)
    text = (result.stdout or "") + (result.stderr or "")
    found = set()
    for pid, _v, _o, _w in PACKAGES:
        # winget's table truncates long ids with an ellipsis, so the check is on the first
        # segment, which is distinctive enough for these four.
        head = pid.split(".")[0] + "." + pid.split(".")[1] if "." in pid else pid
        if pid in text or head in text:
            found.add(pid)
    return found


def install_package(pid: str, version: str | None, override: str | None) -> int:
    """Install one package.

    A package with an --override cannot also take --accept-package-agreements: override
    replaces winget's own switch set. And the override string cannot survive being passed
    through ssh -> cmd -> winget, where the quotes are eaten - so the whole command is
    written to a .bat, copied up, and run by path. Same reasoning as the msvc check.
    """
    base = ["winget", "install", "--id", pid, "--exact", "--source", "winget"]
    if version:
        base += ["--version", version]

    if override:
        script = ("@echo off\r\n"
                  + " ".join(base)
                  + " --accept-source-agreements --disable-interactivity"
                  + f' --override "{override}"'
                  + "\r\n")
        local = os.path.join(HERE, f"install-{pid.replace('.', '-')}.bat")
        with open(local, "w", newline="") as fh:
            fh.write(script)
        remote = f"C:/Users/{SSH_USER}/install-{pid.replace('.', '-')}.bat"
        try:
            scp_to(local, remote)
        finally:
            os.unlink(local)
        command = 'cmd /c "' + remote.replace("/", "\\") + '"'
    else:
        command = " ".join(base + ["--accept-source-agreements",
                                   "--accept-package-agreements",
                                   "--disable-interactivity"])

    result = ssh_run(command, timeout=INSTALL_TIMEOUT)
    for line in (result.stdout or "").splitlines()[-6:]:
        if line.strip():
            log(f"    {line[:150]}")
    return result.returncode


def wait_for_ssh(seconds: int, why: str = "") -> bool:
    import socket

    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", SSH_PORT), timeout=5) as sock:
                sock.settimeout(5)
                if sock.recv(64).startswith(b"SSH-"):
                    return True
        except OSError:
            pass
        time.sleep(5)
    log(f"sshd stopped answering{': ' + why if why else ''}")
    return False


def verify() -> bool:
    """Prove the toolchain works: a compiled program, a dotnet version, pwsh.

    Not the winget exit codes. An installer's exit code says a program ran.
    """
    failures = []

    log("asking winget what it installed")
    present = installed_packages()
    for pid, _v, _o, _w in PACKAGES:
        log(f"  {pid:45} {'installed' if pid in present else 'MISSING'}")
        if pid not in present:
            failures.append(pid)

    log("pwsh")
    result = ssh_run("pwsh -v", timeout=300)
    text = (result.stdout or "") + (result.stderr or "")
    # pwsh -v prints "PowerShell 7.6.9", not a bare number, and ssh's host-key warning
    # arrives on stderr AFTER the real answer - so the whole output is searched rather
    # than the last line.
    if not re.search(r"PowerShell [0-9]+[.][0-9]+", text):
        failures.append("pwsh")
        log("  no version in the output")
    else:
        log(f"  {[l for l in text.splitlines() if 'PowerShell ' in l][0].strip()}")

    log("dotnet")
    # THE FULL PATH. winget does not refresh PATH for sessions that already exist, so a
    # fresh ssh session sees the environment from before the install and reports the bare
    # name as not found even though the SDK is on disk and working.
    dotnet = r"C:\Program Files\dotnet\dotnet.exe"
    result = ssh_run(f'"{dotnet}" --version', timeout=300)
    text = (result.stdout or "") + (result.stderr or "")
    version = None
    for line in text.splitlines():
        if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+", line.strip()):
            version = line.strip()
            break
    if version:
        log(f"  {version}")
    else:
        failures.append("dotnet")
        log("  no version in the output")

    log("compiling and running a C program")
    script = os.path.join(HERE, "msvc-check.ps1")
    if not os.path.isfile(script):
        raise SystemExit(f"{script} is missing; the compiler check cannot run without it")
    remote = f"C:/Users/{SSH_USER}/msvc-check.ps1"
    scp_to(script, remote)
    result = ssh_run(
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {remote}", timeout=900)
    out = (result.stdout or "") + (result.stderr or "")
    # EVERY LINE, not the last six: the script reports where vswhere looked, what it found,
    # whether vcvars exists and the cl exit code, and truncating that left only a verdict
    # with no way to tell which part failed.
    for line in out.splitlines():
        text = line.strip()
        if text and not text.startswith("Warning: Permanently added"):
            log(f"    {text[:150]}")
    if "COMPILE_OK" in out:
        log("  compiled and ran: hello-from-msvc")
    else:
        failures.append("compile+run")

    if failures:
        log(f"FAILED: {', '.join(failures)}")
        return False
    log("VERIFIED: SDK, MSVC and .NET SDK present and working")
    return True


def clean() -> bool:
    """Remove what this run left on the disk, before it is exported.

    THE DISK IS THE DELIVERABLE. Everything the build put on it travels with it and is
    visible to whoever boots the image: downloaded installers, extraction directories, the
    test program, this run's helper scripts, shell history, event logs.

    Only caches, temp files, logs and history go. Installed products are untouched - in
    particular the Visual Studio payload cache, whose contents are what makes a VS repair
    or update work.

    THE LAST THING IT DOES IS ZERO THE FREE SPACE, and that is what decides the size of the
    artifact rather than a formality: `qemu-img convert` only skips clusters that are
    already zero. Deleting a file in NTFS marks its clusters free and leaves the bytes, so
    convert still stores them and the qcow2 comes out the same size. A file grown to fill
    the disk with zeros, then deleted, is what actually makes it smaller.
    """
    log("cleaning the guest")
    script = os.path.join(HERE, "clean.ps1")
    if not os.path.isfile(script):
        raise SystemExit(f"{script} is missing; the disk would ship with build traces")
    remote = f"C:/Users/{SSH_USER}/clean.ps1"
    scp_to(script, remote)

    result = ssh_run(
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {remote}", timeout=1800)
    out = (result.stdout or "") + (result.stderr or "")
    for line in out.splitlines():
        text = line.strip()
        if text and not text.startswith("Warning: Permanently added"):
            log(f"  {text[:150]}")

    # SILENCE IS NOT SUCCESS. The script ends with CLEAN_DONE; without the marker the step
    # failed and the disk still carries whatever was left.
    if "CLEAN_DONE" not in out:
        log("clean.ps1 did not report completion")
        return False
    log("clean complete")
    return True


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ssh", required=True, help="the disk windows/11/ssh.py produced")
    p.add_argument("--out", default="out/windows11-dev.qcow2")
    p.add_argument("--work", default=None)
    p.add_argument("--mem", type=int, default=8192,
                   help="MB; the BuildTools installer is what needs this")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--bootstate", default=None,
                   help="the .bootstate.tar.gz published with the disk; "
                        "without it the firmware has no boot entry")
    p.add_argument("--clean", action="store_true",
                   help="remove this run's traces and zero the free space before "
                        "exporting; without it the disk ships with its build history")
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
    log(f"copying {args.ssh} -> {disk}")
    copied = subprocess.run(["cp", "--reflink=auto", args.ssh, disk],
                            capture_output=True, text=True)
    if copied.returncode != 0:
        raise SystemExit(
            f"could not copy the disk: {(copied.stderr or copied.stdout).strip()[:300]}"
        )

    firmware_code = os.environ.get("OVMF_CODE", "/usr/share/OVMF/OVMF_CODE_4M.fd")
    firmware_vars = os.path.join(work, "OVMF_VARS.fd")
    template = os.environ.get("OVMF_VARS_TEMPLATE", "/usr/share/OVMF/OVMF_VARS_4M.fd")
    if not os.path.exists(firmware_vars):
        shutil.copyfile(template, firmware_vars)
    # THE FIRMWARE AND TPM STATE HAVE TO COME BACK BEFORE THE MACHINE STARTS.
    #
    # A published disk carries a complete Windows 11 and no boot entry: the one that
    # matters is written into OVMF's variables during setup, and a fresh template has
    # none of them. Started that way the firmware tries Boot0001..Boot0005, fails each,
    # and drops into its own shell - measured on run 41468968378.
    import bootstate
    # Same coupling as the ssh stage: the archive moves to the working disk's name.
    if args.bootstate:
        source = args.bootstate
        # A PATH THAT DOES NOT EXIST IS AN ERROR HERE, NOT A WARNING LATER.
        #
        # The workflow names this file and can name it wrongly - run 41480134822 passed
        # `out/windows11-gold.qcow2.bootstate.tar.gz` while the artifact downloads as
        # `windows11.qcow2.bootstate.tar.gz`. The old code silently skipped a missing
        # --bootstate and failed on the generic "no archive at <working disk>" message,
        # which pointed at the wrong directory.
        if not os.path.isfile(source):
            raise SystemExit(
                f"--bootstate {source} does not exist. It must be the archive as it "
                f"arrives - named after the PUBLISHED disk (windows11.qcow2), not after "
                f"the tag this stage pulled (windows11-gold.qcow2). Instead of guessing, "
                f"this stops: the archive beside {os.path.basename(source)} is what the "
                f"publisher actually wrote."
            )
    else:
        source = bootstate.archive_path(args.ssh)
    placed = bootstate.adopt(disk, source)
    log(f"bootstate {source} -> {placed}")
    restored = bootstate.restore(work, disk)
    if not restored:
        raise SystemExit(
            "no bootstate archive at "
            f"{bootstate.archive_path(disk)}. The disk alone will not boot: OVMF's "
            "variables hold the \"Windows Boot Manager\" entry and the TPM holds the "
            "state Windows sealed against it. Pass --bootstate <disk>.bootstate.tar.gz, "
            "or publish it beside the disk - see lib/bootstate.py."
        )
    log(f"bootstate restored into {work}")

    tpm = start_swtpm(work)

    shape = vm.Shape(
        cpus=args.cpus, mem_mb=args.mem, machine="q35",
        firmware_code=firmware_code, firmware_vars=firmware_vars,
        tpm_socket=tpm, vga="std", ssh_port=SSH_PORT,
    )
    guest = vm.Guest(work, extra=[], shape=shape,
                     stdout=os.path.join(work, "console.log")).start()

    ok = False
    try:
        # THE ARRIVAL CHECK IS SSHD, not a desktop. The previous stage already proved this
        # disk reaches one; what this stage needs is the service, and a TCP banner is a
        # better answer than a picture.
        log(f"waiting up to {args.boot_timeout}s for sshd")
        if not wait_for_ssh(args.boot_timeout, "the disk may not be the one ssh.py made"):
            return 1

        present = installed_packages()
        for pid, version, override, why in PACKAGES:
            if pid in present:
                log(f"=== {pid} already installed ===")
                continue
            log(f"=== {pid} ===")
            log(f"  {why}")
            code = install_package(pid, version, override)
            if code != 0:
                # NOT FATAL BY ITSELF. winget answers non-zero for a package that
                # installed but wants a restart, and the verification below asks the
                # machine directly - which is the claim that matters.
                log(f"  winget returned {code}; the machine is asked below")
            # Some MSIs restart a service or the guest. PowerShell 7 did.
            if not wait_for_ssh(300, pid):
                log(f"  continuing, but sshd is not answering after {pid}")

        if not verify():
            return 1

        if args.clean:
            if not clean():
                return 1
        else:
            log("not cleaning; the disk will carry this run's installers and logs")

        ok = True
        return 0
    finally:
        guest.stop(acpi_first=ok)
        stop_swtpm(work)
        if ok:
            log(f"exporting {args.out}")
            subprocess.run(["qemu-img", "convert", "-O", "qcow2", "-c",
                            "-o", "compression_type=zstd", disk, args.out], check=True)
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
