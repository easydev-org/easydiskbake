"""A script that runs at login on both disks, in the language each one already has.

WHAT IT IS

    ~/shared/bootstrap.sh      run by the macOS launcher
    ~/shared/bootstrap.ps1     run by the Windows launcher
    ~/shared/bootstrap.log     what happened
    ~/shared/bootstrap.done    written on success; delete it to run again

WHY NOT ONE PYTHON FILE, WHICH IS WHAT THIS STARTED AS

Because Python is a TOOLCHAIN, and the share now exists BEFORE the toolchain does. The stages
are being split - the share and its launcher are installed first, the developer tools second - so
a bootstrap that needed python3 would do nothing on a disk that has not had the second stage yet.
That is the one thing a login hook must not do: silently depend on something installed later.

So each disk uses the shell it was born with:

    macOS     /bin/sh          always present, no package manager involved
    Windows   PowerShell 5.1   bundled with Windows 11

TWO FILES INSTEAD OF ONE, AND THAT IS THE COST. The bodies cannot be identical - `echo` and
`Write-Output`, `/` and `\\`, `$HOME` and `$env:USERPROFILE`. Sharing them would mean a
compatibility layer that is longer than either script and that nobody can read at a glance.

WHAT THE PROBE FILES (lib/bootstrap_probe.sh / .ps1) WRITE

Whichever runs, it writes ~/hello.txt with the same keys, so the self-test reads one shape:

    hello from the bootstrap
    user=<account>
    shell=<sh|powershell>
    share=readable|MISSING
    stamp=<unix seconds>
"""

from __future__ import annotations

import argparse
import os
import sys

# THE NAME THE PERSON USES IS THE LANGUAGE THE DISK SPEAKS.
SCRIPT = {"macos": "bootstrap.sh", "windows": "bootstrap.ps1"}
LOG = "bootstrap.log"
MARKER = "bootstrap.done"

# WHERE EACH FAMILY PUTS THE LAUNCHER AND WHAT RUNS IT.
MACOS_LABEL = "org.vm.bootstrap"
WINDOWS_TASK = "vm-bootstrap"

LAUNCHER = "bootstrap-launcher"


def macos_plist(launcher: str) -> str:
    """The LaunchAgent that runs the launcher at login.

    `ProgramArguments` names `/bin/sh` and the launcher rather than the launcher alone: launchd
    runs a programme and does not consult a shebang.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{MACOS_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>{launcher}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <!-- ONE SHOT. A bootstrap that has to keep running is not a bootstrap. -->
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/{MACOS_LABEL}.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/{MACOS_LABEL}.err</string>
</dict>
</plist>
"""


MACOS_MOUNT_LABEL = "org.vm.smbmount"


def macos_mount_plist(script: str) -> str:
    """The LaunchDaemon that mounts the share at boot, as root.

    WHY THE MOUNT IS A DAEMON AND NOT PART OF THE AGENT

    A non-root `mount_smbfs` against Samba is refused with "Authentication error" while the same
    credential from root succeeds - see the long note in lib/hostshare.py's mount_commands, where
    two runs and the server's own log established it. The login hook runs as the user, so it
    cannot be the thing that mounts.

    Splitting them also puts each half where it belongs: a mount is a machine-wide resource and
    belongs to a daemon that runs at boot; a bootstrap belongs to the person who logged in and
    belongs to an agent that runs at login. The agent waits for the file the daemon makes
    visible, and neither one has to know when the other ran.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{MACOS_MOUNT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>{script}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <!-- RETRY ON FAILURE, BUT NOT IN A TIGHT LOOP.
         #
         # `SuccessfulExit false` alone means "restart whenever it exits non-zero", and the script
         # exits 1 after its own 60 seconds of retries - so a host with no share running (the
         # clean stage, a published disk someone booted without the container) becomes an endless
         # mount attempt every sixty seconds, for as long as the machine is up. ThrottleInterval
         # puts a floor under that: launchd will not start the job more than once per this many
         # seconds, so a machine with no server costs one attempt a minute and nothing else.
         #
         # NOT `KeepAlive true`: that would ALSO restart it after a successful mount, and the
         # script would find the share mounted and exit 0 - correct, but a process waking up to
         # say so forever. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>300</integer>
    <key>StandardOutPath</key>
    <string>/var/log/{MACOS_MOUNT_LABEL}.out</string>
    <key>StandardErrorPath</key>
    <string>/var/log/{MACOS_MOUNT_LABEL}.err</string>
</dict>
</plist>
"""


def macos_mount_script(host: str, user: str, share: str, password: str = "",
                       home: str = "/Users/docker") -> str:
    """The script the daemon runs: wait for the network, then mount.

    IT RETRIES, because a daemon that runs at boot can start before this guest has an address -
    and a mount that failed once is not a mount that cannot work. `KeepAlive` restarts the job on
    a failed exit, so the retry and the daemon's supervisor point the same way.

    `password` IS ACCEPTED AND NOT USED. The share's account has no password - that is the whole
    reason this works - and the parameter stays so the caller can keep passing one fact rather than
    growing a special case.
    """
    return f"""\
#!/bin/sh
# Mount the host share. INSTALLED BY macos/15/steps/vm-configure.yaml, run by launchd as root at boot.
#
# ROOT, BECAUSE IT RUNS AT BOOT BEFORE ANYBODY HAS LOGGED IN. There is no user session to mount
# into and no keychain to unlock, so this is where a mount that has to exist at login belongs.
#
# THIS FILE CARRIES NO SECRET. The account it mounts as has no password, so there is nothing to
# keep here and nothing lost if a published disk is read.
set -u

SERVER="{host}"
SHARE="{share}"
USER="{user}"
MOUNT="{home}/shared"

mkdir -p "$MOUNT"

# ALREADY MOUNTED IS SUCCESS, not a second mount - `mount_smbfs` answers "File exists" otherwise,
# and a job that exits non-zero is a job launchd keeps restarting.
if mount | grep -q " on $MOUNT "; then
    echo "already mounted on $MOUNT"
    exit 0
fi

# THE ACCOUNT HAS NO PASSWORD, AND THAT IS WHAT MAKES THIS WORK.
#
# mount_smbfs sends an empty password and cannot be made to send anything else from a script: not
# from the URL, not from nsmb.conf (no `password` keyword exists there), not from a pipe (a pipe is
# not a tty, so the prompt reads EOF), and not through `-N` ("do not prompt"). Six runs reached the
# server with a genuine NTLMv2 response computed from nothing before this was understood.
#
# So the account itself carries no password and `null passwords = yes` makes smbd accept that - see
# lib/hostshare.py. `user:` with the colon and nothing after it is the man page's own spelling for
# "this user, no password".
i=0
while [ "$i" -lt 30 ]; do
    i=$((i + 1))
    if mount_smbfs -f 0666 -d 0777 "//$USER:@$SERVER/$SHARE" "$MOUNT" 2>&1; then
        echo "mounted $SERVER/$SHARE on $MOUNT"
        exit 0
    fi
    sleep 2
done

echo "=== COULD NOT MOUNT $SERVER/$SHARE ON $MOUNT ==="
exit 1
"""


def macos_launcher(host: str, user: str, share: str) -> str:
    """The macOS launcher, with the server it has to reach written into it.

    IT MOUNTS THE SHARE ITSELF, AND THAT IS THE PART THAT IS EASY TO GET WRONG.

    The share stage mounts ~/shared over ssh and proves it readable, and then the machine is
    stopped. At the next boot ~/shared is whatever the disk has - an empty directory in the home
    folder, because a network mount does not survive a reboot. A launcher that only waited for
    ~/shared to appear would wait a minute, find an empty directory, and exit 0. That reads as
    "the bootstrap ran and did nothing", which is the worst failure this file can have.

    So it mounts first, with no password at all: the share's account has none, and
    `null passwords = yes` lets smbd accept the empty one mount_smbfs always sends.
    """
    return f"""\
#!/bin/sh
# Run ~/shared/bootstrap.sh at login, once, and leave a log beside it.
#
# INSTALLED BY macos/15/steps/vm-configure.yaml. Editing this file on the disk is fine; the step only
# writes it when it is missing.
#
# NO PYTHON, AND THAT IS THE POINT: this runs on a disk that may not have the developer
# toolchain yet, because the share is installed before it.
set -u

SERVER="{host}"
SHARE="{share}"
SHARED="$HOME/shared"

# THE SCRIPT COMES FROM THE SHARE; THE LOG AND THE MARKER DO NOT.
#
# THAT SPLIT IS THE WHOLE SAFETY PROPERTY OF THIS FILE. When the share is not mounted, $SHARED is
# just a local empty directory on the disk - the mount point the mount command made before trying.
# Anything written there would be written to the IMAGE, and the image is what gets published. So
# the log and the marker live under $HOME, where a stray file costs nothing, and the share is only
# ever read from.
LOG="$HOME/{LOG}"
MARKER="$HOME/{MARKER}"
SCRIPT="$SHARED/{SCRIPT['macos']}"

say() {{
    echo "$@" >> "$LOG" 2>/dev/null || echo "$@"
}}

# WAIT FOR THE MOUNT, WHICH THE DAEMON DOES.
#
# The mount is `org.vm.smbmount`, a LaunchDaemon: a non-root mount_smbfs against Samba is refused,
# and this agent runs as the user. Two jobs, two triggers, and the handover is the file this waits
# for - so whichever of the two starts first, the other catches up.
i=0
while [ ! -f "$SCRIPT" ] && [ "$i" -lt 60 ]; do
    i=$((i + 1))
    sleep 1
done

# THE FALLBACK: NO SCRIPT, NO RUN.
#
# A disk is published with this launcher installed and nothing else. If the share is up but the
# person has not put a bootstrap there, or the mount failed, or the host is gone - this exits 0 and
# says so. It must never invent something to run, and it must never fail loudly enough to look like
# a broken disk: a login hook that does nothing is the correct behaviour for a machine nobody has
# configured yet.
[ -f "$MARKER" ] && exit 0
if [ ! -f "$SCRIPT" ]; then
    say "[$(date)] no $SCRIPT on the share; nothing to run"
    exit 0
fi

say "[$(date)] running $SCRIPT"
if /bin/sh "$SCRIPT" >> "$LOG" 2>&1; then
    # THE MARKER IS WRITTEN ONLY ON SUCCESS, so a failing bootstrap tries again next login.
    date > "$MARKER"
    say "[$(date)] ok"
else
    say "[$(date)] FAILED; it will run again at the next login"
fi
"""


def windows_launcher(host: str, user: str, share: str, password: str = "") -> str:
    """The Windows launcher, connecting with explicit credentials in its own session.

    WHY THE CREDENTIAL IS HERE AND NOT IN THE VAULT

    `cmdkey` cannot work from an ssh session, and the reason is Microsoft's own:

        CredWriteW -> ERROR_NO_SUCH_LOGON_SESSION
        "Network logon sessions do not have an associated credential set."

    Win32-OpenSSH hardcodes LOGON32_LOGON_NETWORK_CLEARTEXT as its logon type and there is no
    setting to change it - so a credential written from this stage could not exist, and a
    launcher that relied on one would do nothing at the next logon.

    WHAT DOES WORK IS THE SESSION. A network logon cannot REUSE a stored credential, but it can
    OPEN an SMB session by supplying one - and the launcher runs in a session of its own, which
    is exactly what it needs. `net use` here, then the UNC path, and both live inside this one
    process.

    THIS FILE CONTAINS THE SHARE PASSWORD. That is acceptable for the same reason the macOS
    daemon's does: a throwaway VM on a private bridge, a share whose password is `admin`. The
    Task Scheduler action points at this file under the user's own profile.
    """
    win = SCRIPT["windows"]
    # THE PATH IS BUILT HERE, WHERE A BACKSLASH IS JUST A BACKSLASH. See the note above.
    unc = "\\\\" + host + "\\" + share
    return f"""\
# Run the share's bootstrap.ps1 at logon, once, and leave a log beside it.
#
# INSTALLED BY windows/11/steps/vm-configure.yaml. Started by Task Scheduler through
# `powershell.exe -WindowStyle Hidden`, so no console appears.
#
# NO PYTHON: the share is installed before the toolchain on this disk.
#
# NO DRIVE LETTER AND NO VAULT. A drive letter belongs to the logon session that made it, and a
# vault entry cannot be written from an ssh session at all - see the note in lib/bootstrap.py.
# What this uses is the one thing a network logon can do: open an SMB session with an EXPLICIT
# credential, inside this process, and address the share by UNC path while it lives.

$SmbUser = '{user}'
$SmbPass = '{password}'
$Base    = '{unc}'
$Script  = '{unc}\\{win}'

# THE SCRIPT COMES FROM THE SHARE; THE LOG AND THE MARKER DO NOT.
#
# Anything this writes must not end up in the published image, and the share is a UNC path with no
# local directory that can be fallen into by accident - but the profile is still the right place
# for bookkeeping, because it is this disk's own and a stray file there costs nothing. The share is
# only ever read from.
$Log     = Join-Path $env:USERPROFILE '{LOG}'
$Marker  = Join-Path $env:USERPROFILE '{MARKER}'

function Say($message) {{
    Add-Content -Path $Log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message" `
        -ErrorAction SilentlyContinue
}}

# THE SESSION, EXPLICITLY. `/persistent:no` because nothing here survives the process, and
# saying so keeps a stale mapping out of the user's registry for the next logon to trip over.
net use '{unc}' $SmbPass /user:$SmbUser /persistent:no 2>&1 | Out-Null

# THE SHARE MAY NOT BE REACHABLE YET. A logon trigger can fire before the network is up, so this
# waits for the file rather than checking once - and re-opens the session on the way, because the
# first attempt can be the one that ran before there was an address.
$found = $false
for ($i = 0; $i -lt 60; $i++) {{
    if (Test-Path $Script) {{ $found = $true; break }}
    if ($i % 10 -eq 9) {{ net use '{unc}' $SmbPass /user:$SmbUser /persistent:no 2>&1 | Out-Null }}
    Start-Sleep -Seconds 1
}}

if (-not $found) {{
    # SAID OUT LOUD, because "the task ran and nothing happened" is otherwise indistinguishable
    # from "the task never ran".
    Say "no $Script on the share; nothing to run"
    exit 0
}}
# THE FALLBACK: NO SCRIPT, NO RUN.
#
# A published disk carries this launcher and nothing else. If the share is up but there is no
# bootstrap on it, or the session could not be opened, or the host is gone - this exits 0 and says
# so. It must never invent something to run, and never fail loudly enough to look like a broken
# disk: a login hook that does nothing is the correct behaviour for a machine nobody has set up.
if (Test-Path $Marker) {{ exit 0 }}

Say "running $Script"
try {{
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Script *>&1 |
        ForEach-Object {{ Say $_ }}
    $code = $LASTEXITCODE
}} catch {{
    Say "could not run it: $_"
    exit 0
}}

if ($code -eq 0) {{
    # THE MARKER IS WRITTEN ONLY ON SUCCESS, so a failing bootstrap tries again at the next logon.
    Set-Content -Path $Marker -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') -ErrorAction SilentlyContinue
    Say "ok"
}} else {{
    Say "FAILED (exit $code); it will run again at the next logon"
}}
"""


def launcher_body(family: str, host: str = "", user: str = "", share: str = "",
                  password: str = "") -> str:
    """The launcher text for one family.

    THE SERVER IS AN ARGUMENT BECAUSE THE LAUNCHER HAS TO REACH IT ON ITS OWN. Nothing it relies
    on survives a reboot except the credential and this file, so the address has to be in the file.
    """
    if not host:
        import net
        host = net.BRIDGE_IP
    user = user or "guest"
    share = share or "shared"
    if family == "macos":
        return macos_launcher(host, user, share)
    if family == "windows":
        return windows_launcher(host, user, share, password)
    raise SystemExit(f"no bootstrap launcher for {family!r}; known: macos, windows")


def windows_ps1(launcher: str) -> str:
    """The script that registers the logon task.

    POWERSHELL 5.1, WHICH WINDOWS 11 HAS. The task runs the launcher through
    `powershell.exe -WindowStyle Hidden`, so no console flashes on the desktop.

    `/RL LIMITED` AND NOT `/RL HIGHEST`: the hook does not need elevation - it writes to the
    user's own profile and to the share - and a task that asked for it would fail to start for a
    user who was not in the administrators group.
    """
    return f"""\
# Register the logon task that runs the share's bootstrap once.
#
# INSTALLED BY windows/11/steps/vm-configure.yaml.
#
# NO PYTHON: the share is installed before the toolchain on this disk.
$ErrorActionPreference = 'Stop'

$launcher = "{launcher}"
$action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""

schtasks /Create /F /TN "{WINDOWS_TASK}" /TR $action /SC ONLOGON /RL LIMITED | Out-String | Write-Output

if ($LASTEXITCODE -ne 0) {{
    Write-Output "SCHTASKS_FAILED: $LASTEXITCODE"
    exit 1
}}
Write-Output "BOOTSTRAP_TASK_REGISTERED"
"""


def launcher_name(family: str) -> str:
    """The launcher's file name: `.sh` on macOS, `.ps1` on Windows."""
    return f"{LAUNCHER}.sh" if family == "macos" else f"{LAUNCHER}.ps1"


def write_launcher(where: str, family: str, host: str = "", user: str = "",
                   share: str = "", password: str = "") -> str:
    """Write the launcher for `family` into `where`, and return its path."""
    os.makedirs(where, exist_ok=True)
    path = os.path.join(where, launcher_name(family))
    with open(path, "w") as fh:
        fh.write(launcher_body(family, host=host, user=user, share=share,
                               password=password))
    return path


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--write", metavar="DIR", help="write the launcher into DIR (needs --family)")
    p.add_argument("--family", choices=("macos", "windows"))
    p.add_argument("--macos-plist", metavar="LAUNCHER", help="print the LaunchAgent")
    p.add_argument("--windows-task", metavar="LAUNCHER", help="print the schtasks script")
    args = p.parse_args(argv)

    if args.write:
        if not args.family:
            p.error("--write needs --family")
        print(write_launcher(args.write, args.family))
        return 0
    if args.macos_plist:
        print(macos_plist(args.macos_plist))
        return 0
    if args.windows_task:
        print(windows_ps1(args.windows_task))
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
