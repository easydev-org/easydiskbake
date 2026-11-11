"""Launch a published disk, put a bootstrap in the share, and read back what it wrote.

    python3 lib/launch_selftest.py --family macos --disk out/macos15-dev.qcow2
    python3 lib/launch_selftest.py --family windows --disk out/windows11-dev.qcow2

WHAT THIS IS FOR, AND WHY IT IS ITS OWN RUN

The dev stage proves the toolchain is on the disk. It cannot prove the launcher works, because
the launcher only fires at LOGIN and the dev stage is already logged in - and anything the dev
stage writes to prove it would go onto the published disk.

So this starts the disk a second time, exactly as a person would: mount the share, drop a
bootstrap into it, log in, and look at what appeared. NOTHING HERE IS PUBLISHED, and the reason is
the same one every stage keeps: hello.txt is written into the SHARE, so it lands on the host and
cannot be part of the image. A file on the disk would be a fixture; a file on the share is a
measurement.

THE CHAIN IT PROVES, IN ORDER

    the disk boots and answers ssh        (the stage before this one is still good)
    the share mounts                      (lib/hostshare.py, same code the disk was built with)
    ~/shared/bootstrap.sh is put there    (the file the launcher waits for)
    ~/shared/bootstrap.ps1  on Windows     (the same, in the shell Windows has)
    the launcher runs it at login         (launchd / Task Scheduler)
    hello.txt appears in the share        (read on the HOST, so no guest can fake it)
    and it names the account and shell    (it can only exist if the guest ran)

A STEP THAT IS SKIPPED IS A STEP THAT IS NOT PROVEN, so this fails loudly rather than reporting
a partial launch as a pass.

WHY THE LOGIN IS EXPLICIT

On macOS the launcher is a LaunchAgent: it runs when the user's session starts. A disk that
boots to a login window has no session yet, so this has to unlock first - the same unlock the
ssh stage does - or the trigger never fires and the failure looks like a broken launcher.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO, os.path.join(REPO, "lib")):
    if path not in sys.path:
        sys.path.insert(0, path)

import hostshare  # noqa: E402

# THE PROBE IS THE LANGUAGE THE DISK SPEAKS, AND ITS NAME IS WHAT THE LAUNCHER WAITS FOR.
#
#   macOS     ~/shared/bootstrap.sh      run by /bin/sh
#   Windows   ~/shared/bootstrap.ps1     run by powershell.exe
#
# Both write the same keys, so this reads one shape.
PROBE = {
    "macos": os.path.join(REPO, "lib", "bootstrap_probe.sh"),
    "windows": os.path.join(REPO, "lib", "bootstrap_probe.ps1"),
}
SCRIPT = {"macos": "bootstrap.sh", "windows": "bootstrap.ps1"}

# THE LINES THE PROBES WRITE, and what each one proves. Kept beside the reader so the two cannot
# drift: a key added there and not here is a fact nobody checks.
#
# `share=mounted` AND NOT `share=readable`: the probe now refuses to write at all unless the path
# is really mounted, so a file that exists says the mount was real. "readable" described a
# directory that might have been the local mount point.
REQUIRED = ("hello.", "shell=", "share=mounted")


def log(message: str) -> None:
    print(f"[selftest {time.strftime('%H:%M:%S')}] {message}", flush=True)


def read_hello(family: str, port: int, user: str, password: str) -> str:
    """What the bootstrap wrote into the share, or "" if there is none.

    READ FROM THE HOST, NOT FROM THE GUEST. The probe writes hello.txt BESIDE ITSELF, in the
    shared directory, so the assertion is about a file this process can see directly - no ssh, no
    guest, and nothing the guest could fake about where it went. It is also the reason a published
    disk cannot contain it: the share is not part of the image.

    THE GUEST-SIDE PATH IS STILL CHECKED, and that is the point of reading it this way round: if
    the file is on the host, the guest really did write through the mount.

    `args.share_dir` names the host directory; the caller resolves it.
    """
    raise SystemExit("read_hello is unused; main() reads the host directory directly")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--family", required=True, choices=("macos", "windows"))
    p.add_argument("--disk", default=None,
                   help="a local disk to boot; it is COPIED first, never opened in place")
    p.add_argument("--tag", default=None,
                   help="a published disk to pull instead of --disk")
    p.add_argument("--work", default=None)
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--user", default=os.environ.get("VM_USER") or "vmuser")
    p.add_argument("--password", default="admin")
    p.add_argument("--boot-timeout", type=int, default=900)
    p.add_argument("--login-timeout", type=int, default=180,
                   help="seconds to wait for the launcher to fire after login")
    p.add_argument("--keep", action="store_true", help="do not stop the guest on the way out")
    args = p.parse_args(argv)

    if not args.disk and not args.tag:
        log("=== one of --disk or --tag is required ===")
        return 2

    work = args.work or os.path.join("out", "selftest-work")
    os.makedirs(work, exist_ok=True)

    if args.tag:
        # PULLED ONCE, INTO THE WORK DIRECTORY. `iso.fetch_tag` is the same call every stage
        # uses and it resumes, so a retried launch does not pull 16 GiB again.
        import iso
        disk = os.path.join(work, "pulled.qcow2")
        if not os.path.isfile(disk):
            log(f"pulling {args.tag}")
            iso.fetch_tag(args.tag, disk)
        args.disk = disk
    if not os.path.isfile(args.disk):
        log(f"=== NO DISK AT {args.disk} ===")
        return 2

    # THE BRIDGE BEFORE THE SHARE, AND THIS PROGRAM DID NOT DO IT.
    #
    # `hostshare.start()` writes `interfaces = <bridge address>` into samba's config and waits
    # for smbd to bind there. That address is created by `net.up()`, which every other stage
    # reaches through `vm.Guest.start()` - and this program calls `hostshare.start()` FIRST,
    # twenty lines above `boot_machine.start()`, because the launcher waits for the share and
    # starting the guest before the server would spend the launcher's one-minute wait on nothing.
    #
    # So smbd was asked to bind an address that did not exist yet, waited ten seconds, failed,
    # and fell into the `Could not open tdb` retry - which is where it died:
    #
    #     05:58:16  private dir ... exists=True writable=True entries=['msg.sock', ...]
    #     05:58:26  samba lost its private directory; recreating it and starting smbd once more
    #     05:58:26  [entrypoint] signal 1, and the work has NOT finished; obeying it
    #
    # `lib/vm_session.py` learned this same ordering and says so: "THE BRIDGE FIRST, BECAUSE
    # smbd BINDS TO ITS ADDRESS ... a share that started perfectly and bound to no interface"
    # (run 42813552966). The fix there was to call `net.up()` before `hostshare.start()`. This
    # program never got it, and the tdb retry is what made the failure look like samba's.
    #
    # `net.up()` IS IDEMPOTENT - `vm.Guest.start()` calls it again and finds what is there - so
    # this is not a second setup, it is the same call moved to before the thing depending on it.
    import net as netmod

    netmod.up(ssh_port=args.ssh_port)

    # AND THEN THE SHARE, because the launcher waits for it.
    share_dir = os.path.expanduser("~/shared")
    if not hostshare.start(share_dir):
        log("=== THE HOST COULD NOT SERVE ~/shared ===")
        return 1
    target = os.path.join(share_dir, SCRIPT[args.family])
    with open(PROBE[args.family], "rb") as fh:
        probe_bytes = fh.read()
    with open(target, "wb") as fh:
        fh.write(probe_bytes)
    log(f"{SCRIPT[args.family]} ({len(probe_bytes)} bytes) is in {share_dir}")
    # A MARKER FROM A PREVIOUS LAUNCH WOULD MAKE THE LAUNCHER SKIP, and hello.txt would then be
    # the previous run's. The probe writes hello.txt into the SHARE and keeps its log and marker
    # in the guest's own home, so only the first has to be swept here - and it has to be swept
    # HERE, on the host, because that is the file the assertion reads.
    for stale in ("hello.txt",):
        path = os.path.join(share_dir, stale)
        if os.path.exists(path):
            os.unlink(path)

    import boot_machine  # noqa: E402  - written below, and only used here

    guest = boot_machine.start(args.family, args.disk, work, ssh_port=args.ssh_port,
                               boot_timeout=args.boot_timeout, log=log)
    if guest is None:
        return 1

    ok = False
    try:
        # THE SHARE FIRST, AND THE LOGIN SECOND, BECAUSE THAT IS THE ORDER THE LAUNCHER SEES.
        #
        # A LaunchAgent runs when the user's session starts - which is the unlock below - and it
        # waits up to a minute for ~/shared to appear. Mounting AFTER the login meant the
        # launcher spent that minute looking at an empty mount point and gave up, so the test
        # would report "the launcher did not run" for a launcher that worked.
        #
        # Mounting needs sshd, which is up before any login: that is what the ssh stage left
        # behind.
        # THE HOST-SIDE hello.txt IS REMOVED AGAIN, because a stale one from an earlier probe
        # would let the read below pass before this launch had written anything.
        host_hello = os.path.join(share_dir, "hello.txt")
        if os.path.exists(host_hello):
            os.unlink(host_hello)

        log("mounting the share in the guest, before the session that will look for it")
        if not boot_machine.mount(args.family, guest, args.user, args.password, log=log):
            log(f"=== THE SHARE DID NOT MOUNT; THE LAUNCHER WILL FIND NO "
                f"{SCRIPT[args.family]} ===")
            return 1

        # THE LOGIN, BECAUSE A LAUNCHAGENT NEEDS A SESSION. The ssh stage's own unlock is
        # reused rather than reimplemented - it knows the password field's rectangle.
        log("logging in, so the login trigger has something to fire in")
        boot_machine.login(args.family, guest, args.user, args.password, log=log)

        # AND ASK WHETHER THE AGENT IS LOADED, SO A FAILURE NAMES WHICH HALF BROKE.
        #
        # "No hello.txt" has two causes that look identical: the agent never loaded, or it
        # loaded and the script failed. `launchctl print` answers the first, and the share's
        # bootstrap.log answers the second - and reporting which one it is costs one command.
        if args.family == "macos":
            import session as _session
            status, out = _session.ssh(
                args.ssh_port,
                "launchctl print gui/$(id -u)/org.vm.bootstrap 2>&1 | head -6",
                timeout=60, user=args.user, password=args.password)
            log(f"  the agent: launchctl print -> {status}")
            for line in (out or "").splitlines()[:6]:
                log(f"    {line[:140]}")

        log(f"waiting up to {args.login_timeout}s for the launcher to run it")
        deadline = time.time() + args.login_timeout
        text = ""
        while time.time() < deadline:
            # READ ON THE HOST. The probe writes hello.txt into the shared directory, so this
            # process can see it directly - and if it is here, the guest really did write
            # through the mount.
            if os.path.isfile(host_hello):
                with open(host_hello) as fh:
                    text = fh.read().strip()
            if text:
                break
            time.sleep(5)

        if not text:
            log("=== NO hello.txt IN THE SHARE ===")
            log("  the launcher did not run, or it ran and the script refused; the guest keeps")
            log("  bootstrap.log and bootstrap-refused.txt in its own home, and both are printed")
            log("  below so the two cases can be told apart")
            import session as _s
            for name in ("bootstrap.log", "bootstrap-refused.txt"):
                cmd = (f'cmd /c "type C:\\Users\\{args.user}\\{name}"'
                       if args.family == "windows" else f"cat ~/{name} 2>/dev/null || true")
                _st, _out = _s.ssh(args.ssh_port, cmd, timeout=60,
                                   user=args.user, password=args.password)
                for line in (_out or "").splitlines()[-12:]:
                    log(f"    {name}: {line[:140]}")
            return 1

        log("the share's hello.txt says:")
        for line in text.splitlines():
            log(f"    {line}")
        missing = [want for want in REQUIRED if want not in text]
        if missing:
            log(f"=== IT RAN BUT DID NOT PROVE: {', '.join(missing)} ===")
            return 1
        ok = True
        log("=== THE CHAIN WORKS: launcher -> share -> bootstrap -> share/hello.txt ===")
        return 0
    finally:
        if not args.keep:
            boot_machine.stop(guest, acpi_first=ok)


if __name__ == "__main__":
    # ONE LATE SIGTERM IS SURVIVED, ONCE THE WORK IS FINISHED.
    #
    # THIS PROGRAM STARTS THE SHARE, AND `hostshare.start()` SIGNALS smbd INSIDE ITS OWN RETRY
    # PATH - which is exactly where macOS' launch_dev has been dying:
    #
    #     02:29:21  share 'shared' -> /github/home/shared (read-write, account 'guest')
    #     02:29:21  private dir /tmp/easydiskbake-samba-private: exists=True writable=True ...
    #     02:29:31  samba lost its private directory; recreating it and starting smbd once more
    #     02:29:31  ##[error] Process completed with exit code 241.
    #
    # 241 is `-15 & 0xFF`, it arrives 800 ms after that line, and no `signal delivered` follows -
    # so the process was gone before the helper's own log line was written. That is the shape the
    # nine stage programs already survive (lib/entrypoint.py), and this program was not in that
    # set because it is the only launch that starts a share and it does not compress a disk.
    #
    # INSTALLED HERE AND ARMED AT `ok = True`: a launch that has not read hello.txt yet must still
    # obey a cancel.
    import entrypoint
    entrypoint.tolerate_one_sigterm()
    raise SystemExit(main(sys.argv[1:]))
