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


MACOS_LAUNCHER = f"""\
#!/bin/sh
# Run ~/shared/bootstrap.sh at login, once, and leave a log beside it.
#
# INSTALLED BY macos/15/steps/share.yaml. Editing this file on the disk is fine; the step only
# writes it when it is missing.
#
# NO PYTHON, AND THAT IS THE POINT: this runs on a disk that may not have the developer
# toolchain yet, because the share is installed before it.
set -u

SHARED="$HOME/shared"
SCRIPT="$SHARED/{SCRIPT['macos']}"
LOG="$SHARED/{LOG}"
MARKER="$SHARED/{MARKER}"

say() {{
    echo "$@" >> "$LOG" 2>/dev/null || echo "$@"
}}

# THE SHARE MAY NOT BE MOUNTED YET. LaunchAgents at login can start before a network mount is
# in place, so this waits for the directory rather than checking once.
i=0
while [ ! -d "$SHARED" ]; do
    i=$((i + 1))
    [ "$i" -gt 60 ] && exit 0
    sleep 1
done

[ -f "$MARKER" ] && exit 0
[ -f "$SCRIPT" ] || exit 0

say "[$(date)] running $SCRIPT"
if /bin/sh "$SCRIPT" >> "$LOG" 2>&1; then
    # THE MARKER IS WRITTEN ONLY ON SUCCESS, so a failing bootstrap tries again next login.
    date > "$MARKER"
    say "[$(date)] ok"
else
    say "[$(date)] FAILED; it will run again at the next login"
fi
"""


def windows_ps1(launcher: str) -> str:
    """The `schtasks` command that registers the logon task.

    POWERSHELL 5.1, WHICH WINDOWS 11 HAS. The launcher is a `.ps1` run through
    `powershell.exe -WindowStyle Hidden`, so no console flashes on the desktop.
    """
    return f"""\
# Register the logon task that runs ~/shared/bootstrap.ps1 once.
#
# INSTALLED BY windows/11/steps/share.yaml.
#
# `-WindowStyle Hidden` AND NOT `pythonw.exe`: there is no Python on this disk when this stage
# runs, because the share is installed before the toolchain. PowerShell 5.1 is part of Windows.
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


WINDOWS_LAUNCHER = f"""\
# Run ~/shared/bootstrap.ps1 at logon, once, and leave a log beside it.
#
# INSTALLED BY windows/11/steps/share.yaml. Started by Task Scheduler through
# `powershell.exe -WindowStyle Hidden`, so no console appears.
#
# NO PYTHON: the share is installed before the toolchain on this disk.

$Shared = Join-Path $env:USERPROFILE 'shared'
$Script = Join-Path $Shared '{SCRIPT['windows']}'
$Log    = Join-Path $Shared '{LOG}'
$Marker = Join-Path $Shared '{MARKER}'

function Say($message) {{
    Add-Content -Path $Log -Value $message -ErrorAction SilentlyContinue
}}

# THE SHARE MAY NOT BE MAPPED YET. A logon trigger can fire before the network drive is back, so
# this waits for the file rather than checking once.
$found = $false
for ($i = 0; $i -lt 60; $i++) {{
    if (Test-Path $Script) {{ $found = $true; break }}
    Start-Sleep -Seconds 1
}}

if (-not $found) {{ exit 0 }}
if (Test-Path $Marker) {{ exit 0 }}

Say "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] running $Script"
try {{
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Script *>&1 |
        ForEach-Object {{ Say $_ }}
    $code = $LASTEXITCODE
}} catch {{
    Say "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] could not run it: $_"
    exit 0
}}

if ($code -eq 0) {{
    # THE MARKER IS WRITTEN ONLY ON SUCCESS, so a failing bootstrap tries again at the next logon.
    Set-Content -Path $Marker -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    Say "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ok"
}} else {{
    Say "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] FAILED (exit $code); it will run again at the next logon"
}}
"""


def launcher_body(family: str) -> str:
    """The launcher text for one family."""
    if family == "macos":
        return MACOS_LAUNCHER
    if family == "windows":
        return WINDOWS_LAUNCHER
    raise SystemExit(f"no bootstrap launcher for {family!r}; known: macos, windows")


def launcher_name(family: str) -> str:
    """The launcher's file name: `.sh` on macOS, `.ps1` on Windows."""
    return f"{LAUNCHER}.sh" if family == "macos" else f"{LAUNCHER}.ps1"


def write_launcher(where: str, family: str) -> str:
    """Write the launcher for `family` into `where`, and return its path."""
    os.makedirs(where, exist_ok=True)
    path = os.path.join(where, launcher_name(family))
    with open(path, "w") as fh:
        fh.write(launcher_body(family))
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
