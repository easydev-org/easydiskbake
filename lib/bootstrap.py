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
MACOS_LABEL = "org.easydiskbake.bootstrap"
WINDOWS_TASK = "easydiskbake-bootstrap"

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


MACOS_MOUNT_LABEL = "org.easydiskbake.smbmount"


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
    <!-- AND AGAIN IF IT COMES DOWN, because a share that unmounted is a share that is gone. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>/var/log/{MACOS_MOUNT_LABEL}.out</string>
    <key>StandardErrorPath</key>
    <string>/var/log/{MACOS_MOUNT_LABEL}.err</string>
</dict>
</plist>
"""


def macos_mount_script(host: str, user: str, share: str, password: str,
                       home: str = "/Users/docker") -> str:
    """The script the daemon runs: wait for the network, then mount.

    IT RETRIES, because a daemon that runs at boot can start before this guest has an address -
    and a mount that failed once is not a mount that cannot work. `KeepAlive` restarts the job on
    a failed exit, so the retry and the daemon's supervisor point the same way.
    """
    return f"""\
#!/bin/sh
# Mount the host share. INSTALLED BY macos/15/steps/share.yaml, run by launchd as root at boot.
#
# ROOT, AND THAT IS THE WHOLE REASON THIS IS A DAEMON. `mount_smbfs` against Samba refuses a
# non-root caller with "Authentication error" while accepting the same credential from root.
#
# THE PASSWORD IS IN THIS FILE. That is only defensible because the file is 0600 and owned by
# root on a throwaway VM whose share password is `admin`; there is no keychain here to use
# instead, because a keychain belongs to a login session and this runs before there is one.
set -u

SERVER="{host}"
SHARE="{share}"
USER="{user}"
PASSWORD="{password}"
MOUNT="{home}/shared"

mkdir -p "$MOUNT"

# ALREADY MOUNTED IS SUCCESS, not a second mount - `mount_smbfs` answers "File exists" otherwise,
# and a job that exits non-zero is a job launchd keeps restarting.
if mount | grep -q " on $MOUNT "; then
    echo "already mounted on $MOUNT"
    exit 0
fi

i=0
while [ "$i" -lt 30 ]; do
    i=$((i + 1))
    if mount_smbfs -f 0666 -d 0777 "//$USER:$PASSWORD@$SERVER/$SHARE" "$MOUNT" 2>&1; then
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

    So it mounts first. The credential is already on the disk - the share stage wrote
    ~/Library/Preferences/nsmb.conf - which is what makes this possible without a prompt.
    """
    return f"""\
#!/bin/sh
# Run ~/shared/bootstrap.sh at login, once, and leave a log beside it.
#
# INSTALLED BY macos/15/steps/share.yaml. Editing this file on the disk is fine; the step only
# writes it when it is missing.
#
# NO PYTHON, AND THAT IS THE POINT: this runs on a disk that may not have the developer
# toolchain yet, because the share is installed before it.
set -u

SERVER="{host}"
SHARE="{share}"
SHARED="$HOME/shared"
SCRIPT="$SHARED/{SCRIPT['macos']}"
LOG="$SHARED/{LOG}"
MARKER="$SHARED/{MARKER}"

say() {{
    echo "$@" >> "$LOG" 2>/dev/null || echo "$@"
}}

# WAIT FOR THE MOUNT, WHICH THE DAEMON DOES.
#
# The mount is `org.easydiskbake.smbmount`, a LaunchDaemon: a non-root mount_smbfs against Samba
# is refused, and this agent runs as the user. Two jobs, two triggers, and the handover is the
# file this waits for - so whichever of the two starts first, the other catches up.
i=0
while [ ! -f "$SCRIPT" ] && [ "$i" -lt 60 ]; do
    i=$((i + 1))
    sleep 1
done

[ -f "$MARKER" ] && exit 0
if [ ! -f "$SCRIPT" ]; then
    say "[$(date)] $SCRIPT did not appear; is the host share up?"
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


def windows_launcher(host: str, user: str, share: str) -> str:
    """The Windows launcher, reaching the share by UNC path.

    NO DRIVE LETTER AND NO MOUNT POINT, because Windows has neither that survives: a mapped drive
    belongs to the logon session that made it. The credential from `cmdkey` does survive, and the
    redirector consults it for a UNC path in any session - so the launcher addresses
    \\\\host\\share\\bootstrap.ps1 directly.

    THE LOG AND THE MARKER GO ON THE SHARE TOO, beside the script, so that a person looking at the
    host sees the same three files on both families.
    """
    return f"""\
# Run the share's bootstrap.ps1 at logon, once, and leave a log beside it.
#
# INSTALLED BY windows/11/steps/share.yaml. Started by Task Scheduler through
# `powershell.exe -WindowStyle Hidden`, so no console appears.
#
# NO PYTHON: the share is installed before the toolchain on this disk.
#
# NO DRIVE LETTER: a mapped drive belongs to the logon session that created it. The credential
# this relies on is the one `cmdkey` put in the vault, which any session can use.

$Base   = '\\\\{host}\\{share}'
$Script = "$Base\\{SCRIPT['windows']}"
$Log    = "$Base\\{LOG}"
$Marker = "$Base\\{MARKER}"
$Local  = Join-Path $env:USERPROFILE '{LOG}'

function Say($message) {{
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
    # THE SHARE'S LOG FIRST, THE LOCAL ONE SECOND: a logon hook that cannot reach the share has to
    # be able to say so somewhere.
    Add-Content -Path $Log -Value $line -ErrorAction SilentlyContinue
    Add-Content -Path $Local -Value $line -ErrorAction SilentlyContinue
}}

# THE SHARE MAY NOT BE REACHABLE YET. A logon trigger can fire before the network is up, so this
# waits for the file rather than checking once.
$found = $false
for ($i = 0; $i -lt 60; $i++) {{
    if (Test-Path $Script) {{ $found = $true; break }}
    Start-Sleep -Seconds 1
}}

if (-not $found) {{
    # SAID OUT LOUD, because "the task ran and nothing happened" is otherwise indistinguishable
    # from "the task never ran".
    Say "$Script did not appear; is the host share up?"
    exit 0
}}
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


def launcher_body(family: str, host: str = "", user: str = "", share: str = "") -> str:
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
        return windows_launcher(host, user, share)
    raise SystemExit(f"no bootstrap launcher for {family!r}; known: macos, windows")


def launcher_name(family: str) -> str:
    """The launcher's file name: `.sh` on macOS, `.ps1` on Windows."""
    return f"{LAUNCHER}.sh" if family == "macos" else f"{LAUNCHER}.ps1"


def write_launcher(where: str, family: str, host: str = "", user: str = "",
                   share: str = "") -> str:
    """Write the launcher for `family` into `where`, and return its path."""
    os.makedirs(where, exist_ok=True)
    path = os.path.join(where, launcher_name(family))
    with open(path, "w") as fh:
        fh.write(launcher_body(family, host=host, user=user, share=share))
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
