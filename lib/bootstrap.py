"""A script that runs at login on both disks, from one file.

WHAT IT IS

    ~/shared/bootstrap.py     the file a person puts on the host

If it exists when the machine logs in, it is run once, and its output goes to
`~/shared/bootstrap.log` so the person who wrote it can see what happened without a console.

WHY PYTHON AND NOT A SHELL SCRIPT

Because there are two guests and only one language they both have. A `#!/bin/sh` script does
not run on Windows and a `.ps1` does not run on macOS without a decision about which shell;
Python is on both disks by the end of the dev stage, so the same file is the same file:

    macOS     /opt/homebrew/bin/python3   (Homebrew; see macos/15/steps/dev.yaml)
    Windows   python.exe                  (winget; see windows/11/steps/dev.yaml)

THAT DEPENDENCY IS THE WHOLE COST OF THIS DESIGN, so it is checked rather than assumed: the
launcher looks for an interpreter and writes to the log if it cannot find one, instead of
failing in a way nobody sees.

WHY IT RUNS AT LOGIN AND NOT AT BOOT

Because the share is an SMB mount made by the user's session. At boot there is no session, no
mount, and no `~/shared` to read the script from - so a boot trigger would find nothing and
look broken.

WHY "ONCE"

A marker file (`~/shared/bootstrap.done`) is written after a successful run. A failing script
leaves no marker, so the next login tries again - which is what a person debugging a bootstrap
wants. Deleting the marker re-runs it.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap

# The name the person uses, and the names this writes.
SCRIPT = "bootstrap.py"
LOG = "bootstrap.log"
MARKER = "bootstrap.done"

# WHERE EACH FAMILY PUTS THE LAUNCHER AND WHAT RUNS IT.
#
# macOS  a LaunchAgent: runs in the user's session, which is where the SMB mount is.
#        `RunAtLoad` makes it fire when the agent is loaded, which is at login.
#
# Windows a Task Scheduler task with a logon trigger. `pythonw.exe` keeps a console from
#        flashing on the desktop, and `/rl LIMITED` avoids an elevation prompt for a task that
#        has no business needing one.
MACOS_LABEL = "org.easydiskbake.bootstrap"
MACOS_PLIST = f"~/Library/LaunchAgents/{MACOS_LABEL}.plist"

WINDOWS_TASK = "easydiskbake-bootstrap"


def macos_plist(launcher: str) -> str:
    """The LaunchAgent that runs the launcher at login.

    `ProgramArguments` names `/bin/sh` and the launcher rather than the launcher alone: the
    launcher is a shell script and macOS does not execute a file just because it has a shebang
    when it is handed to launchd. StandardOutPath is where the launcher's own echo of the log
    goes if the launcher dies before writing it.
    """
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
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
    """)


MACOS_LAUNCHER = f"""\
#!/bin/sh
# Run ~/shared/{SCRIPT} at login, once, and leave a log beside it.
#
# INSTALLED BY macos/15/steps/dev.yaml. Editing this file on the disk is fine; the step only
# writes it when it is missing.
#
# THE SHARE MAY NOT BE MOUNTED YET. LaunchAgents at login can start before a network mount is
# in place, so this waits for the directory to have the script in it rather than checking once.
set -u

SHARED="$HOME/shared"
SCRIPT="$SHARED/{SCRIPT}"
LOG="$SHARED/{LOG}"
MARKER="$SHARED/{MARKER}"

# THE LOG LIVES IN THE SHARE, AND THE SHARE MAY NOT EXIST. Nothing here may fail loudly for
# that reason: a bootstrap is an addition to the disk, not a condition of it working.
say() {{
    echo "$@" >> "$LOG" 2>/dev/null || echo "$@"
}}

# WAIT FOR THE MOUNT, UP TO A MINUTE. The alternative is a race that reads as "my bootstrap
# did not run" on a machine where it simply had not mounted yet.
i=0
while [ ! -d "$SHARED" ]; do
    i=$((i + 1))
    [ "$i" -gt 60 ] && exit 0
    sleep 1
done

[ -f "$MARKER" ] && exit 0
[ -f "$SCRIPT" ] || exit 0

# THE INTERPRETER. Homebrew on both architectures, then the system one - which cannot install
# packages but can run a script that imports nothing.
PYTHON=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$candidate" ]; then PYTHON="$candidate"; break; fi
done
if [ -z "$PYTHON" ]; then
    say "[$(date)] no python3 found; {SCRIPT} was not run"
    exit 0
fi

say "[$(date)] running $SCRIPT with $PYTHON"
if "$PYTHON" "$SCRIPT" >> "$LOG" 2>&1; then
    # THE MARKER IS WRITTEN ONLY ON SUCCESS. A failing bootstrap should try again next login -
    # that is what a person debugging one wants - and deleting the marker re-runs a good one.
    date > "$MARKER"
    say "[$(date)] ok"
else
    say "[$(date)] FAILED (exit $?); it will run again at the next login"
fi
"""


def windows_ps1(launcher: str) -> str:
    """The `schtasks` command that registers the logon task.

    WRITTEN AS A FILE SO IT CAN BE READ AND RUN BY HAND. `schtasks /Create` has enough quoting
    that a version inline in a YAML string is a version nobody can check; here the two paths
    are the only variables and they are at the top.
    """
    return textwrap.dedent(f"""\
        # Register the logon task that runs ~/shared/{SCRIPT} once.
        #
        # INSTALLED BY windows/11/steps/dev.yaml.
        #
        # pythonw.exe, NOT python.exe. The console variant flashes a black window on the
        # desktop every time it runs, and a bootstrap that interrupts whatever the person is
        # doing is a bootstrap they will delete.
        #
        # /rl LIMITED AND NO ELEVATION. A task registered with highest privileges prompts for
        # consent at creation, which nothing here can answer; and a bootstrap that needs admin
        # should say so in its own script rather than in its trigger.
        $ErrorActionPreference = 'Stop'

        $pythonw = $null
        foreach ($candidate in @(
            "$env:LOCALAPPDATA\\Programs\\Python\\Python313\\pythonw.exe",
            "$env:LOCALAPPDATA\\Programs\\Python\\Python312\\pythonw.exe",
            "$env:LOCALAPPDATA\\Programs\\Python\\Python311\\pythonw.exe",
            "C:\\Python313\\pythonw.exe",
            "C:\\Python312\\pythonw.exe"
        )) {{
            if (Test-Path $candidate) {{ $pythonw = $candidate; break }}
        }}
        if (-not $pythonw) {{
            $found = (Get-Command pythonw.exe -ErrorAction SilentlyContinue)
            if ($found) {{ $pythonw = $found.Source }}
        }}
        if (-not $pythonw) {{
            Write-Output "NO_PYTHONW: bootstrap will not be registered"
            exit 1
        }}
        Write-Output "pythonw: $pythonw"

        $launcher = "{launcher}"
        # schtasks wants one string for the action, and both paths have spaces on Windows.
        $action = "`"$pythonw`" `"$launcher`""

        schtasks /Create /F /TN "{WINDOWS_TASK}" `
            /TR $action `
            /SC ONLOGON `
            /RL LIMITED | Out-String | Write-Output

        if ($LASTEXITCODE -ne 0) {{
            Write-Output "SCHTASKS_FAILED: $LASTEXITCODE"
            exit 1
        }}
        Write-Output "BOOTSTRAP_TASK_REGISTERED"
    """)


WINDOWS_LAUNCHER = f"""\
# Run ~/shared/{SCRIPT} at logon, once, and leave a log beside it.
#
# INSTALLED BY windows/11/steps/dev.yaml. Run by pythonw.exe through Task Scheduler.
#
# THIS FILE IS PYTHON, NOT A .ps1. The task's action is pythonw.exe with this file as its
# argument, because that is the only way to get no console window - a .ps1 through powershell
# -WindowStyle Hidden still flashes one before the flag takes effect.

import os
import subprocess
import sys
import time

SHARED = os.path.join(os.path.expanduser("~"), "shared")
SCRIPT = os.path.join(SHARED, "{SCRIPT}")
LOG = os.path.join(SHARED, "{LOG}")
MARKER = os.path.join(SHARED, "{MARKER}")


def say(message):
    with open(LOG, "a") as fh:
        fh.write(message + "\\n")


def main():
    # THE SHARE MAY NOT BE MAPPED YET. A logon trigger can fire before the network drive is
    # back, so this waits for the file rather than checking once - a minute, then gives up
    # quietly. A bootstrap is an addition to the disk, not a condition of it working.
    for _ in range(60):
        if os.path.isfile(SCRIPT):
            break
        time.sleep(1)

    if os.path.isfile(MARKER) or not os.path.isfile(SCRIPT):
        return 0

    say("[%s] running %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), SCRIPT))
    try:
        result = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True,
                                timeout=3600)
    except (OSError, subprocess.SubprocessError) as exc:
        say("[%s] could not run it: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), exc))
        return 0

    say(result.stdout or "")
    say(result.stderr or "")
    if result.returncode == 0:
        # THE MARKER IS WRITTEN ONLY ON SUCCESS, so a failing bootstrap tries again at the
        # next logon. Deleting the marker re-runs a good one.
        with open(MARKER, "w") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        say("[%s] ok" % time.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        say("[%s] FAILED (exit %d); it will run again at the next logon"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), result.returncode))
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def launcher_body(family: str) -> str:
    """The launcher text for one family - the single place the two bodies live."""
    if family == "macos":
        return MACOS_LAUNCHER
    if family == "windows":
        return WINDOWS_LAUNCHER
    raise SystemExit(f"no bootstrap launcher for {family!r}; known: macos, windows")


LAUNCHER = "bootstrap-launcher.py"


def write_launcher(where: str, family: str) -> str:
    """Write the launcher for `family` into `where`, and return its path.

    THE NAME IS THE SAME ON BOTH DISKS: `bootstrap-launcher.py`. Both files are Python, so the
    extension is honest on both; what differs is how each is started:

        macOS     `/bin/sh <file>` (see macos_plist) - launchd runs a program, not a file
                  association, and the shebang is not consulted
        Windows   `pythonw.exe <file>` (see windows_ps1) - pythonw is what suppresses the
                  console, and .py is what that interpreter is asked to run

    A person looking for "the bootstrap launcher" therefore finds one name on either disk,
    and the bootstrap itself is `~/shared/bootstrap.py` on both.
    """
    os.makedirs(where, exist_ok=True)
    path = os.path.join(where, LAUNCHER)
    with open(path, "w") as fh:
        fh.write(launcher_body(family))
    return path


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--write", metavar="DIR", help="write the launcher into DIR (needs --family)")
    p.add_argument("--family", choices=("macos", "windows"),
                   help="which launcher --write should write")
    p.add_argument("--macos-plist", metavar="LAUNCHER", help="print the LaunchAgent")
    p.add_argument("--windows-task", metavar="LAUNCHER", help="print the schtasks script")
    args = p.parse_args(argv)

    if args.write:
        # --write NEEDS --family, because the launcher has ONE NAME on both disks and two
        # different bodies - so two files cannot share one path.
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
