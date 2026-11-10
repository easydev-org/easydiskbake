"""Launch a published disk, put a bootstrap in the share, and read back what it wrote.

    python3 lib/launch_selftest.py --family macos --disk out/macos15-dev.qcow2
    python3 lib/launch_selftest.py --family windows --disk out/windows11-dev.qcow2

WHAT THIS IS FOR, AND WHY IT IS ITS OWN RUN

The dev stage proves the toolchain is on the disk. It cannot prove the launcher works, because
the launcher only fires at LOGIN and the dev stage is already logged in - and anything the dev
stage writes to prove it would go onto the published disk.

So this starts the disk a second time, exactly as a person would: mount the share, drop a
bootstrap into it, log in, and look at what appeared. NOTHING HERE IS PUBLISHED. `~/hello.txt`
is written inside the guest and dies with the launch, which is the property that makes it a
test rather than a fixture.

THE CHAIN IT PROVES, IN ORDER

    the disk boots and answers ssh        (the stage before this one is still good)
    the share mounts                      (lib/hostshare.py, same code the disk was built with)
    ~/shared/bootstrap.sh is put there    (the file the launcher waits for)
    ~/shared/bootstrap.ps1  on Windows     (the same, in the shell Windows has)
    the launcher runs it at login         (launchd / Task Scheduler)
    ~/hello.txt exists and says so        (read back over ssh)

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
REQUIRED = ("hello.", "shell=", "share=readable")


def log(message: str) -> None:
    print(f"[selftest {time.strftime('%H:%M:%S')}] {message}", flush=True)


def read_hello(family: str, port: int, user: str, password: str) -> str:
    """What the guest's ~/hello.txt says, or "" if there is none.

    THE PATH IS SPELLED PER FAMILY because Windows has no `~`. Everything else about reading it
    is the same command.
    """
    import session

    if family == "windows":
        cmd = 'cmd /c "type C:\\Users\\%s\\hello.txt"' % user
    else:
        cmd = "cat ~/hello.txt"
    status, output = session.ssh(port, cmd, timeout=120, user=user, password=password)
    if status != 0:
        return ""
    return (output or "").strip()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--family", required=True, choices=("macos", "windows"))
    p.add_argument("--disk", default=None,
                   help="a local disk to boot; it is COPIED first, never opened in place")
    p.add_argument("--tag", default=None,
                   help="a published disk to pull instead of --disk")
    p.add_argument("--work", default=None)
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--user", default="docker")
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

    # THE SHARE FIRST, because the launcher waits for it: starting the guest before the server
    # is up means the launcher's one-minute wait is spent on nothing.
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
    # A MARKER FROM A PREVIOUS LAUNCH WOULD MAKE THE LAUNCHER SKIP, and the test would then
    # pass on the previous run's hello.txt.
    for stale in ("bootstrap.done", "bootstrap.log", "hello.txt"):
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
                "launchctl print gui/$(id -u)/org.easydiskbake.bootstrap 2>&1 | head -6",
                timeout=60, user=args.user, password=args.password)
            log(f"  the agent: launchctl print -> {status}")
            for line in (out or "").splitlines()[:6]:
                log(f"    {line[:140]}")

        log(f"waiting up to {args.login_timeout}s for the launcher to run it")
        deadline = time.time() + args.login_timeout
        text = ""
        while time.time() < deadline:
            text = read_hello(args.family, args.ssh_port, args.user, args.password)
            if text:
                break
            time.sleep(5)

        if not text:
            log("=== NO ~/hello.txt ===")
            log("  the launcher did not run, or it ran and the script failed; the share's")
            log("  bootstrap.log says which (it is on the host, in ~/shared)")
            for name in ("bootstrap.log",):
                path = os.path.join(share_dir, name)
                if os.path.isfile(path):
                    with open(path) as fh:
                        for line in fh.readlines()[-12:]:
                            log(f"    {line.rstrip()}")
            return 1

        log("~/hello.txt says:")
        for line in text.splitlines():
            log(f"    {line}")
        missing = [want for want in REQUIRED if want not in text]
        if missing:
            log(f"=== IT RAN BUT DID NOT PROVE: {', '.join(missing)} ===")
            return 1
        ok = True
        log("=== THE CHAIN WORKS: launcher -> share -> bootstrap -> ~/hello.txt ===")
        return 0
    finally:
        if not args.keep:
            boot_machine.stop(guest, acpi_first=ok)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
