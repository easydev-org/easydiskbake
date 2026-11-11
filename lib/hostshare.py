"""Share a directory on the host with the guest, read and write.

    python3 lib/hostshare.py --dir ~/shared --log

WHY THIS EXISTS

So a guest can read a file that was put on the host after the disk was published - a bootstrap
script, a config, a fixture - without rebuilding the disk or restarting the machine. The user
asked for it as "Bootstrap 之类的脚本放进 shared，让 vm 启动时自动执行", and the first half of
that is this: the directory has to be there, mounted, and readable from inside the guest.

WHY SMB AND NOT 9P OR VIRTIOFS

Because the two guests that matter here each have exactly one protocol they can use without a
driver, and SMB is the only one they share:

    Windows    NO 9p DRIVER EXISTS. virtio-win's maintainer, on the request to add one:
                   "There are no virtio-9p drivers for Windows. Please use virtio-fs.
                    Those are not same devices."
               and the issue was closed as not-planned. virtiofs works on Windows but needs
               WinFsp plus a virtiofsd process on the host - a driver install and a second
               daemon for a directory the guest could reach with a native client.

    macOS      9p mounts READ-ONLY. mount_9p takes `-r` and nothing else, so a share that has
               to be writable cannot use it. virtiofs needs Apple's driver and a Linux-only
               virtiofsd.

SMB is what both Windows and macOS speak natively, read and write, with no driver and no
extra process.

LINUX USES THE SAME SERVER, THROUGH ITS OWN KERNEL CLIENT. An earlier version of this file left
Linux out on the grounds that it "would use qemu's virtio-9p" - true of the design that put
`smb=` on qemu's user-mode netdev, which this repository no longer has. `share_serve:` runs a
real smbd on the bridge address, so a Linux guest mounts it with `mount.cifs` (cifs-utils is
not on a stock desktop, so that step installs it first) and nothing new is exposed. `smbd` is already in this image (Containerfile installs samba), which is why
this is a config file and a process rather than a new dependency.

WHAT CHANGED SINCE THE LAST SMB CODE IN THIS REPOSITORY

There was a `share_path` field and a `smb=` on qemu's user-mode netdev. Both are gone: this
repository dropped slirp entirely (see the commit that did it - a guest behind a bridged tap
cannot use qemu's built-in SMB, because that server only exists on the 10.0.2.x link slirp
creates). So this is a HOST-SIDE smbd on the bridge address instead, which is also how a real
machine would see a file server.

THE ADDRESS THE GUEST CONNECTS TO

`net.BRIDGE_IP` (172.30.0.1 by default), because that is the interface the guest already
routes to - it is how it reaches the ssh forward. Nothing new is exposed.

THE SHARE IS READ-WRITE AND THAT HAS A COST

The user asked for read and write. `force user` points the guest's writes at the host account
running smbd, so a file the guest creates is owned by it on the host - which is the behaviour a
person expects from a shared folder and is also how a guest can fill the host's disk. The
config sets no quota; the caller's directory is whatever it is.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import net  # noqa: E402

# THE SHARE NAME THE GUEST ASKS FOR: //172.30.0.1/shared , \\172.30.0.1\shared
SHARE_NAME = "shared"

# THE DEFAULT HOST DIRECTORY, which is the one the user named.
DEFAULT_DIR = os.path.expanduser("~/shared")

# THE SHARE HAS ONE ACCOUNT AND IT HAS NO PASSWORD.
#
# AN EMPTY PASSWORD IS THE POINT, NOT AN OVERSIGHT. Both guests mount with a session setup that
# carries no password, and neither can be talked out of it:
#
#     macOS    mount_smbfs asks on a TERMINAL. There is no terminal over ssh, and six runs
#              established that the URL, nsmb.conf (no `password` keyword at all), -N ("do not
#              prompt"), and a stdin redirect all end with a real NTLMv2 response computed from
#              an EMPTY password.
#     Windows  cannot reach the credential vault from a network logon at all -
#              CredWriteW answers ERROR_NO_SUCH_LOGON_SESSION - so it supplies the password in
#              its own command, and `net use \host\share "" /user:guest` is that.
#
# A password therefore has to be a thing neither client has to produce. Samba will only accept an
# empty one when `null passwords = yes` is set, which is written out below rather than inherited
# from a default, because it is a weaker setting and it should be visible.
#
# WHAT THIS COSTS: anyone who can reach 172.30.0.1:445 can read and write the shared directory.
# The only other member of that bridge is the one guest this process started, which is the same
# trade the account itself already made. It is a CI container on a private bridge, not a server.
SMB_USER = os.environ.get("HOSTSHARE_USER", "guest")
SMB_PASSWORD = os.environ.get("HOSTSHARE_PASSWORD", "")
PRIVATE_DIR = "/tmp/easydiskbake-samba-private"

CONF_PATH = "/tmp/easydiskbake-smb.conf"
PID_PATH = "/tmp/easydiskbake-smbd.pid"
LOG_PATH = "/tmp/easydiskbake-smbd.log"


def log(message: str) -> None:
    print(f"[share {time.strftime('%H:%M:%S')}] {message}", flush=True)


def config_text(directory: str) -> str:
    """The smallest smb.conf that serves one directory read-write to one guest.

    GUEST ACCESS, NOT A PASSWORD, AND THAT IS A DELIBERATE CHOICE WITH A LIMIT WORTH KNOWING.
    The share exists to hand a guest a file from the machine that is running it, and the
    machine is a CI container whose bridge network the guest is the ONLY other member of
    (172.30.0.0/24, one guest). A password would be a second thing to keep in step with the
    disk's account for no gain on a link with two ends, both of which this process owns.

    `map to guest = Bad User` makes that work: a login that names no valid account is mapped
    to the guest account rather than refused. `force user = root` decides whose files the
    writes become.

    SMB2 IS THE FLOOR. macOS 15 refuses SMB1 outright and Windows 11 has it off by default;
    setting the minimum here means a misconfiguration fails at the server rather than at a
    client that says "the server does not support the required protocol".
    """
    return f"""[global]
   workgroup = WORKGROUP
   server string = easydiskbake shared
   # A REAL SMB ACCOUNT, BECAUSE WINDOWS 11 REFUSES EVERYTHING ELSE.
   #
   # Three client-side policies now block what this share used to do, all measured on run
   # 9666f4f after the server had been made as permissive as samba allows:
   #
   #     System error 1937   Authentication failed because NTLM authentication has been
   #                         disabled.
   #
   # and behind it, documented by Microsoft:
   #
   #     Windows 11 24H2+     inbound NTLM blocked; SMB signing required
   #     Windows 10 1709+     "insecure guest logons" off by default
   #
   # `map to guest`, `restrict anonymous` and `ntlm auth = yes` all act INSIDE a session setup
   # that the client has already decided not to make. `security = share` was removed in samba 4
   # and would not start. There is no server-side setting that makes an anonymous share work
   # with a current Windows client - the switch is on the client, and this stage owns the client
   # too, so the honest fix is a credential both ends can use.
   #
   # SO THERE IS ONE ACCOUNT, named after the share, with a password the guest already knows
   # from the answer file. It is created in start() before smbd is launched.
   security = user
   passdb backend = tdbsam
   # THREE DIRECTORIES A CONTAINER DOES NOT HAVE, ALL OF THEM REQUIRED.
   #
   # `testparm` accepts the config and then says, in warnings that are easy to read past:
   #
   #     WARNING: lock directory /run/samba does not exist
   #     WARNING: pid directory /run/samba does not exist
   #
   # and `smbpasswd` answers "Can't load <conf>" for a file that testparm just parsed, because
   # the load fails on the missing directory rather than on the syntax. All three are pointed at
   # one writable directory under /tmp, 0700 because the passdb holds a password hash.
   private dir = {PRIVATE_DIR}
   lock directory = {PRIVATE_DIR}
   state directory = {PRIVATE_DIR}
   cache directory = {PRIVATE_DIR}
   pid directory = {PRIVATE_DIR}
   # THE PIDFILE THIS MODULE STOPS, BY NAME. `pid directory` alone tells smbd where a pidfile
   # WOULD go; without `pid file` it does not write one, and stop() then has nothing to read -
   # which is why the previous implementation reached for `pkill -f` and hoped.
   pid file = {PID_PATH}
   # AND THE PIPE DIRECTORY, WHICH smbd CREATES AT STARTUP AND smbpasswd NEVER TOUCHES.
   #
   # This is the one that only appears when smbd actually runs:
   #
   #     directory_create_or_exist: mkdir failed on directory /run/samba/ncalrpc
   #     Failed to create pipe directory /run/samba/ncalrpc - No such file or directory
   #
   # run 42735185661, after the account had been created successfully - so the password step and
   # the start step fail on DIFFERENT missing directories, and fixing the first only moves the
   # error to the second.
   ncalrpc dir = {PRIVATE_DIR}/ncalrpc
   # NOT `map to guest`. It was tried as a way round the password problem - an unknown name lands
   # on the guest account with no password checked - and it is no longer needed now that the real
   # account HAS no password. Keeping it would admit any name with any password, which is a wider
   # door than the problem requires.
   map to guest = Never
   guest account = root
   # `ntlm auth` LEFT AT ITS DEFAULT, WHICH IS `ntlmv2-only`.
   #
   # That is the secure setting and the one Windows 11 requires - but macOS's mount_smbfs answered
   #
   #     mount_smbfs: server rejected the connection: Authentication error
   #
   # with it in place (run 42737751721), and the share had already been shown to serve:
   # `the share account 'guest' exists`, `smbd is listening on 172.30.0.1:445`.
   #
   # `yes` ACCEPTS NTLMv1 AS WELL, and this is one of the few places where the weaker setting is
   # the correct one: the client is a throwaway VM on a private bridge, the password is `admin`,
   # and the alternative is a macOS client that cannot mount the share at all. It is written out
   # rather than left to a default so the next reader sees the trade rather than inheriting it.
   ntlm auth = yes
   # AND AN EMPTY PASSWORD IS A VALID ONE. Without this, smbpasswd stores the empty password and
   # smbd refuses every session setup that presents it - which is exactly what both guests do.
   null passwords = yes
   server min protocol = SMB2_10
   # SIGNING IS LEFT AT ITS DEFAULT, NOT TURNED OFF.
   # Windows 11 24H2 requires signing by default and refuses a server that cannot; turning the
   # requirement off is a client policy, but a server that offers signing is fine - so this
   # simply does not forbid it.
   server signing = auto
   log file = {LOG_PATH}
   # LOG LEVEL 3, BECAUSE THE FAILURE IS AUTHENTICATION AND LEVEL 1 SAYS NOTHING ABOUT IT.
   #
   # Two runs were spent on `mount_smbfs: server rejected the connection: Authentication error`
   # with the share demonstrably serving (`the share account 'guest' exists`, `smbd is listening
   # on 172.30.0.1:445`). At level 1 the server logs startup and nothing else, so the question
   # "which side refused, and why" could not be asked of the evidence.
   #
   # Level 3 adds the session setup: the dialect, the auth type offered, and the nt status. It is
   # also required to see whether `ntlm auth` had any effect at all.
   log level = 3
   # NO PRINTING. smbd would otherwise try to manage a spool directory it does not have.
   load printers = no
   printing = bsd
   printcap name = /dev/null
   disable spoolss = yes

[{SHARE_NAME}]
   comment = easydiskbake host share
   path = {directory}
   browseable = yes
   writable = yes
   # NOT `guest ok`. The account is what admits a client now, and it has no password - allowing
   # guest as well would be a second, wider door onto the same directory.
   guest ok = no
   force user = root
   create mask = 0666
   directory mask = 0777
   # FOLLOW SYMLINKS INTO THE SHARE, not out of it.
   follow symlinks = yes
   wide links = no
"""


def listening(host: str = "", port: int = 445, timeout: float = 2.0) -> bool:
    """Is something accepting SMB connections?

    A TCP connect and nothing more. The protocol handshake is the client's business, and a
    server that accepts and then refuses is a different failure - but one that a connect test
    is not the right place to diagnose.
    """
    import socket

    try:
        with socket.create_connection((host or net.BRIDGE_IP, port), timeout=timeout):
            return True
    except OSError:
        return False


def start(directory: str = DEFAULT_DIR, *, wait: float = 10.0) -> bool:
    """Write the config, start smbd, and wait for the port.

    THE DIRECTORY IS CREATED IF IT IS MISSING, because a share whose path does not exist is a
    share smbd accepts and then fails to serve on the first access - and the error arrives in
    the client, not here.
    """
    if not shutil.which("smbd"):
        log("=== smbd IS NOT INSTALLED IN THIS IMAGE ===")
        log("  Containerfile installs samba; an image without it cannot serve a share")
        return False

    directory = os.path.abspath(os.path.expanduser(directory))
    os.makedirs(directory, exist_ok=True)
    log(f"share {SHARE_NAME!r} -> {directory} (read-write, account {SMB_USER!r})")

    # THE CONFIG FIRST, BECAUSE `smbpasswd -c` READS IT.
    #
    # It was the other way round, and smbpasswd answered
    #
    #     Can't load /tmp/easydiskbake-smb.conf - run testparm to debug it
    #
    # for a file that had not been written yet. Run 42732853845.
    #
    # `private dir` IS TRANSFERABLE, NOT MACHINE-SPECIFIC. It is the tdb directory the passdb
    # writes into, and a container has no /var/lib/samba - an unset one makes smbd try to create
    # a directory under /var that it cannot write, which fails at startup rather than at the
    # password step. 0700 because it holds the password hash.
    os.makedirs(PRIVATE_DIR, exist_ok=True)
    os.chmod(PRIVATE_DIR, 0o700)

    with open(CONF_PATH, "w") as fh:
        fh.write(config_text(directory))

    if not _ensure_account():
        return False

    # STOP ONE THIS MODULE STARTED EARLIER. `smbd -D` daemonises, so the process is not a
    # child and cannot be waited on - which is why the pid file is written rather than a
    # Popen handle kept.
    stop()

    argv = ["smbd", "-D", "--no-process-group", "-s", CONF_PATH]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"=== smbd DID NOT START (exit {result.returncode}) ===")
        for line in ((result.stdout or "") + (result.stderr or "")).splitlines()[-6:]:
            log(f"    {line[:160]}")
        return False

    deadline = time.time() + wait
    while time.time() < deadline:
        if listening():
            log(f"  smbd is listening on {net.BRIDGE_IP}:445")
            _record_pid()
            return True
        time.sleep(0.5)
    log(f"=== smbd DID NOT BIND {net.BRIDGE_IP}:445 IN {wait:.0f}s ===")
    tail = ""
    if os.path.isfile(LOG_PATH):
        with open(LOG_PATH) as fh:
            tail = "".join(fh.readlines()[-6:])
    for line in tail.splitlines():
        log(f"    {line[:160]}")
    return False


def _ensure_account() -> bool:
    """Create the share's samba account, idempotently.

    THE ACCOUNT IS A UNIX USER FIRST. samba's `smbpasswd -a` refuses a name the system does not
    know, and a container's /etc/passwd has no `guest` - so `useradd` (or `adduser -D` on busybox)
    runs first, with no shell and no home, because nothing logs in as it.

    `-s` SETS THE SMB PASSWORD NON-INTERACTIVELY, twice because smbpasswd asks twice.
    """
    import subprocess

    existing = subprocess.run(["getent", "passwd", SMB_USER], capture_output=True, text=True)
    if existing.returncode != 0:
        added = subprocess.run(
            ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", SMB_USER],
            capture_output=True, text=True)
        if added.returncode != 0:
            log(f"=== COULD NOT CREATE THE {SMB_USER!r} UNIX USER ===")
            for line in (added.stderr or added.stdout or "").splitlines()[:4]:
                log(f"    {line[:150]}")
            return False

    # TWO EMPTY LINES, BECAUSE smbpasswd ASKS TWICE. It accepts an empty password and answers
    # rc=0; `null passwords = yes` in the config is what makes smbd honour it at session setup.
    created = subprocess.run(
        ["smbpasswd", "-a", "-s", "-c", CONF_PATH, SMB_USER],
        input=f"{SMB_PASSWORD}\n{SMB_PASSWORD}\n", capture_output=True, text=True)
    if created.returncode != 0:
        log(f"=== smbpasswd COULD NOT SET THE {SMB_USER!r} PASSWORD ===")
        for line in ((created.stderr or "") + (created.stdout or "")).splitlines()[:6]:
            log(f"    {line[:150]}")
        return False
    log(f"  the share account {SMB_USER!r} exists"
        + (" (no password)" if not SMB_PASSWORD else ""))
    return True


def log_tail(lines: int = 40) -> None:
    """Print the end of smbd's log, so an authentication failure can be read rather than guessed.

    CALLED WHEN A MOUNT IS REFUSED. The client's message is one sentence - "server rejected the
    connection: Authentication error" - and the server's log at level 3 says which half refused:
    a wrong password, an account missing from the passdb, a dialect the client would not take, or
    a session setup the client never sent.
    """
    if not os.path.isfile(LOG_PATH):
        log(f"  (no smbd log at {LOG_PATH})")
        return
    try:
        with open(LOG_PATH, errors="replace") as fh:
            tail = fh.readlines()[-lines:]
    except OSError as exc:
        log(f"  could not read {LOG_PATH}: {exc}")
        return
    log(f"  smbd log, last {len(tail)} line(s):")
    for line in tail:
        log(f"    {line.rstrip()[:190]}")


def _record_pid() -> None:
    """Write the pid of the smbd serving THIS config, so stop() does not have to search later.

    `smbd -D` daemonises, so the process that `subprocess.run` waited on is gone and its return
    code says nothing about the daemon. Samba's own `pid file` did not materialise here (it is
    written by the parent before the daemonise, and this container's /tmp is not where it looks
    first), so the pid is discovered once, at the moment the port is proven open, and kept.

    THE SEARCH IS NARROW: `pgrep -f` over the exact `-s <conf>` this module writes, not over
    "smbd" in general - a container may be running other samba instances and killing one of those
    would be somebody else's outage.
    """
    try:
        found = subprocess.run(["pgrep", "-f", f"smbd .*-s {CONF_PATH}"],
                               capture_output=True, text=True)
        pids = [int(x) for x in (found.stdout or "").split() if x.isdigit()]
    except (OSError, ValueError):
        return
    if not pids:
        log("  (no smbd pid recorded; stop() will have nothing to kill)")
        return
    # SAY WHOSE PID IT IS AND WHAT IT LOOKS LIKE, because stop() sends it a signal and a wrong
    # pid is a process this module did not start. Run 42766989537 died with `SIGNALLED: SIGTERM`
    # two milliseconds after `the guest powered off` - which is the moment stop() runs.
    try:
        with open(f"/proc/{pids[0]}/cmdline", "rb") as fh:
            looks_like = fh.read().replace(b"\0", b" ").decode(errors="replace")[:120]
    except OSError:
        looks_like = "<gone>"
    log(f"  smbd pid {pids[0]}: {looks_like}")
    if pids[0] == os.getpid() or "smbd" not in looks_like:
        log("=== REFUSING TO RECORD A PID THAT IS NOT smbd ===")
        return
    with open(PID_PATH, "w") as fh:
        fh.write(str(pids[0]))


def stop() -> None:
    """Stop the smbd this module started, and only that one.

    BY PID, IN A CHILD PROCESS. `kill` as a subprocess rather than `os.kill` in this one, because
    a wrong pid then kills the wrong process instead of the process doing the killing - and run
    42766989537 showed what that looks like from the inside: the stage printed
    `=== SIGNALLED: SIGTERM (15) ===` two milliseconds after `the guest powered off`, which is the
    moment this function runs.

    A PIDFILE IS STILL THE RIGHT HANDLE. It names the daemon this module started; `pkill -f` names
    "whatever matches the pattern", which is not something a reader can check. But a pidfile can
    be stale, a pid can be reused, and neither of those should be able to take the caller with it.
    """
    try:
        with open(PID_PATH) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return
    if pid <= 1:
        return
    # A stale pidfile is the case that matters: whatever now owns that number is not our smbd.
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            looks_like = fh.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        _unlink_pid()
        return
    # A PID THAT NAMES *THIS* PROCESS GROUP IS NEVER KILLED.
    #
    # `smbd -D --no-process-group` daemonises, so a reused pid is a real possibility, and the
    # cmdline test below accepts any string containing "smbd" - including
    # `bash -c "... smbd ..."`, which is exactly what a wrapper script looks like. Signalling
    # that takes the caller with it.
    #
    # Two things have already gone wrong this way. Run 42766989537 died with
    # `=== SIGNALLED: SIGTERM (15) ===` two milliseconds after `the guest powered off`, and run
    # 42913402988's clean exited 241 - which is `-15 & 0xFF`, a Python process killed by SIGTERM -
    # half a second after the same line, with the stage's remaining work (the re-compress) never
    # printed. Both moments are `stop()`'s.
    #
    # So the pid is compared against this process AND its group before anything is sent, and a
    # match is a refusal rather than a kill.
    if pid == os.getpid():
        log(f"  pid {pid} is THIS PROCESS; refusing to signal it")
        _unlink_pid()
        return
    # AND NOTHING IN THIS PROCESS'S OWN GROUP EITHER, WHICH IS THE CASE THAT KEPT HAPPENING.
    #
    # Run 42919582582 named it exactly:
    #
    #     [clean-step] === SIGNALLED: SIGTERM (15) === ppid=418 pid=419 pgid=418
    #
    # 419 is the clean, 418 is the `bash -c` that runs it, and 418 is the GROUP. So the signal
    # reached the group rather than one process - and a signal sent to a process group takes the
    # sender with it. Measured: `kill -TERM -<own pgid>` from a child kills the child too.
    #
    # `smbd -D --no-process-group` SHARES this group by design, so a correct smbd pid is
    # normally IN it - which is why this cannot be a blanket refusal. What it can refuse is a pid
    # whose cmdline does not look like the daemon, and that check is below. A reused pid that
    # now names our own bash - the one case where signalling would kill the stage - is caught
    # here.
    try:
        in_our_group = os.getpgid(pid) == os.getpgid(0)
    except OSError:
        in_our_group = False
    # EXACTLY "smbd ", NOT "smbd". The daemon's own cmdline is
    #
    #     smbd -D --no-process-group -s /tmp/easydiskbake-smb.conf
    #
    # with a space after the name, and a `bash -c "...smbd..."` wrapper does not have that in the
    # same position. A loose substring test is what let a reused pid look like ours.
    if in_our_group and not looks_like.lstrip().startswith("smbd "):
        log(f"  pid {pid} is in this process's group and is not smbd "
            f"({looks_like[:60]}); refusing to signal it")
        _unlink_pid()
        return
    if "smbd" not in looks_like:
        log(f"  pid {pid} is not smbd ({looks_like[:60]}); leaving it alone")
        _unlink_pid()
        return
    # SAY SO BEFORE AND AFTER THE SIGNAL, BECAUSE THIS IS THE ONLY PLACE IN THE PROCESS THAT
    # SENDS ONE AND THREE RUNS HAVE NOW DIED FIVE MILLISECONDS AFTER `the guest powered off` -
    # which is when this function runs:
    #
    #     42919582582   === SIGNALLED: SIGTERM (15) === ppid=418 pid=419 pgid=418
    #     42922887078   === SIGNALLED: SIGTERM (15) === ppid=371 pid=378 pgid=378
    #     42928178260   === SIGNALLED: SIGTERM (15) === ppid=356 pid=363 pgid=363
    #
    # If the next run prints `signalling smbd <pid>` and then the SIGNALLED line, the sender is
    # this function and the pid is wrong. If it prints the SIGNALLED line with no `signalling`
    # before it, the signal came from outside and this is not the place to look.
    for signal_number in ("TERM", "KILL"):
        log(f"  signalling smbd {pid} with SIG{signal_number} "
            f"(this process is {os.getpid()}, group {os.getpgid(0)})")
        done = subprocess.run(["kill", f"-{signal_number}", str(pid)], capture_output=True)
        log(f"  kill returned {done.returncode}")
        if done.returncode != 0:
            break
        time.sleep(0.5)
        if not os.path.exists(f"/proc/{pid}"):
            break
    _unlink_pid()


def _unlink_pid() -> None:
    try:
        os.unlink(PID_PATH)
    except OSError:
        pass


def mount_commands(host: str = "", user: str = "vmuser",
                   login_password: str = "admin") -> dict[str, str]:
    """What a guest types to mount this share, per family.

    THE PATH IS THE SAME ON BOTH GUESTS (`~/shared`), and so is the credential: the share's own
    account, which start() creates. Windows 11 cannot mount an anonymous share at all - see the
    note above `security = user` - so a username and password are not optional here.

        macOS      mount_smbfs, into a directory the account owns. A network mount cannot be
                   put at a path that does not exist, so the command makes it first.
        Windows    `net use` maps a UNC path to a DRIVE LETTER, and there is no `~` in cmd, so
                   the local directory is spelled out and the share lands on Z:.

    LINUX IS DELIBERATELY ABSENT. It would use qemu's virtio-9p instead - the kernel's own
    client, no daemon - but that is a different device with a different mount, and mixing the
    two behind one name would be a step that silently did nothing on one of the three.

    THE PASSWORD IS ON THE COMMAND LINE, which is acceptable here and nowhere else: the guest is
    a throwaway VM on a private bridge, the credential is `admin`, and the alternative - a
    credential file - is one more thing to copy in before the mount can be attempted.
    """
    host = host or net.BRIDGE_IP
    return {
        # macOS: THE PASSWORD GOES IN ON STDIN, BECAUSE THAT IS WHERE IT IS READ FROM.
        #
        # Three runs, three forms, and the space between the second and third is the answer:
        #
        #     mount_smbfs //user:pass@host/share   "Authentication error"        42744852729
        #     nsmb.conf + mount_smbfs -N           NT_STATUS_WRONG_PASSWORD      42746003442
        #     as root, password still in the URL   NT_STATUS_WRONG_PASSWORD      42748534325
        #
        # Every one of them reached the server, and the server said the same thing each time:
        #
        #     Auth: [SMB2,(null)] user []\[GUEST] with [NTLMv2] status [NT_STATUS_WRONG_PASSWORD]
        #     ntlm_password_check: NTLMv2 password check failed
        #
        # An NTLMv2 response WAS sent - a real one, 214 bytes - computed from the wrong password.
        # Not a wrong account: `smbclient -U guest%admin` against this config answers rc=0.
        #
        # So the password never reached the client, and both earlier explanations were wrong in
        # the same way. `nsmb.conf` cannot carry one (its man page lists no `password` keyword at
        # all), and the URL form is documented but does not deliver it in a non-interactive
        # session: with no tty, mount_smbfs falls back to asking on STDIN. First run: stdin was
        # the ssh channel, so it read EOF. Second and third: `sudo -S` had ALREADY consumed the
        # password line, so mount_smbfs again read EOF - which is why making it root changed
        # nothing at all.
        #
        # WHICH MEANS THE FIX IS TO FEED IT, and to feed it INSIDE the sudo: the outer printf
        # satisfies sudo, the inner one satisfies mount_smbfs. The URL keeps the account name,
        # and the password is passed both ways because a client that honours the URL should not
        # be broken by an extra line it never needs to read.
        #
        # ROOT IS KEPT even though it was not the cause: `/Library/LaunchDaemons` needs it for
        # the mount daemon, so the two halves behave the same, and a mount made by one account is
        # not a mount another has to reason about.
        # macOS: THE EMPTY PASSWORD IS NOW THE RIGHT ONE, SO THIS IS THE PLAIN FORM.
        #
        # Six runs went into discovering that mount_smbfs sends an empty password and cannot be
        # made to send anything else over ssh:
        #
        #     //user:pass@host/share          password ignored, session setup sent anyway
        #     nsmb.conf + -N                  no `password` keyword exists in that file at all
        #     root + stdin redirect           a pipe is not a tty, so nothing was read
        #     script(1) pty                   would have worked, and was no longer needed
        #
        # The account in start() now HAS no password, and `null passwords = yes` makes smbd
        # accept that, so the client's empty password is the correct credential rather than a
        # mistake to work around. `user:` with the colon and nothing after it is the man page's
        # own spelling for "this user, no password" - `//user@host` would mean "ask for one",
        # and with no tty that gives up before any session setup.
        #
        # NO SUDO: a mount needs root, and this channel has it through `login_password` the same
        # way macos/15/steps/ssh.yaml sets the host name - but the command itself no longer has
        # to smuggle a secret through an elevated shell, which is the part that kept going wrong.
        # SUDO IS STILL NEEDED, AND ONLY FOR THE MOUNT ITSELF. `mount_smbfs` mounts a filesystem
        # and that is a privileged operation; what sudo is NOT doing here is smuggling a password
        # to the client, which is what made the earlier forms fail. The account has no password,
        # so the stdin line sudo reads is the only secret in the command and nothing else wants it.
        "macos": (
            f"mkdir -p ~/shared && "
            f"printf '%s\\n' {shlex.quote(login_password)} | sudo -S -p '' "
            f"mount_smbfs -f 0666 -d 0777 //{SMB_USER}:@{host}/{SHARE_NAME} ~/shared"
        ),
        # WINDOWS: THE SAME ACCOUNT, THE SAME EMPTY PASSWORD, SUPPLIED PER SESSION.
        #
        # A network logon can neither reuse a stored credential nor write one: CredWriteW
        # answers ERROR_NO_SUCH_LOGON_SESSION - "Network logon sessions do not have an
        # associated credential set" - and Win32-OpenSSH hardcodes that logon type, so `cmdkey`
        # is not available to this channel at all (run 42748563969 measured the refusal).
        #
        # What a network logon CAN do is open an SMB session by supplying a credential, and the
        # `""` is a real argument: it passes the empty password the account now has. Every ssh
        # command supplies it again rather than inheriting it, because the session it made dies
        # with the command that made it.
        #
        # NO DRIVE LETTER: a mapping belongs to the logon session that created it, which is run
        # 42737785611 - `net use Z:` answered 0 and the next command could not find the path.
        # The share is addressed by UNC path while the session lives.
        "windows": (f'net use \\\\{host}\\{SHARE_NAME} "" /user:{SMB_USER} /persistent=no'),
        # LINUX: THE KERNEL'S OWN CIFS CLIENT, AND IT IS THE SAME SERVER THE OTHER TWO USE.
        #
        # The module's header used to say Linux was "deliberately absent" because it would use
        # virtio-9p. That was true of the design it described - qemu's user-mode `smb=` on the
        # slirp link - and the repository no longer has that: `share_serve:` starts a real smbd
        # on the bridge address (see `start()`), which Linux reaches as an ordinary SMB server.
        #
        # So 9p is not needed and is not used. `mount.cifs` is the kernel's client, present in
        # `cifs-utils` - which is NOT installed on a stock desktop, so the step that runs this
        # installs it first. The mount is the same shape as macOS': a directory the account
        # owns, and root for the mount itself.
        #
        # `vers=3.0` IS PINNED. A client and a server that negotiate their own can land on SMB1
        # (which current smbd disables) or on a dialect whose `null passwords` handling differs;
        # 3.0 is what this smbd offers and what the account's empty password was tested against.
        #
        # `password=` IS EMPTY AND THAT IS THE CREDENTIAL, exactly as on macOS: `_ensure_account`
        # creates the account with no password and the config sets `null passwords = yes`.
        "linux": (
            f"mkdir -p ~/shared && "
            f"printf '%s\\n' {shlex.quote(login_password)} | sudo -S -p '' "
            f"mount -t cifs //{host}/{SHARE_NAME} ~/shared "
            f"-o vers=3.0,user={SMB_USER},password=,uid=$(id -u),gid=$(id -g),forceuid,forcegid"
        ),
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--stop", action="store_true")
    p.add_argument("--status", action="store_true")
    args = p.parse_args(argv)

    if args.stop:
        stop()
        log("stopped")
        return 0
    if args.status:
        ok = listening()
        log(f"{'listening' if ok else 'not listening'} on {net.BRIDGE_IP}:445")
        return 0 if ok else 1
    if not net.bridge_exists():
        log(f"=== {net.BRIDGE_IP} IS NOT UP; THE GUEST COULD NOT REACH THIS SHARE ===")
        log("  net.up() creates it, and every Guest.start() calls that")
        return 1
    return 0 if start(args.dir) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
