"""Decide whether a Linux guest's Wayland session is actually running, over ssh.

    from session import wait_for_wayland, check_wayland_session

    ok = wait_for_wayland(port=2222, process="gnome-shell", seconds=900, note=log)

WHY THIS EXISTS AND WHY IT IS NOT A PIXEL TEST

linux/nixos/launch.py began with a screenshot test: a frame with colour in it counted as
"the desktop drew". Two of six desktops broke it while working perfectly.

    labwc    mean  0.0  chroma 0.0     the whole screen stayed black
    miracle  mean 50.7  chroma 8.0     a flat backdrop, no colour to measure

Both had booted - `nixos login: docker (automatic login)` was in their consoles - but
labwc runs greetd with WLR_RENDERER=pixman and miracle-wm is a Mir compositor, and neither
necessarily paints anything on `-device VGA`. A screen test cannot tell "this compositor
does not draw on the emulated adapter" from "this compositor failed to start", and the
second is the only question worth asking.

So the verdict is the session, read from inside the guest:

    1. loginctl reports an ACTIVE session whose Type is wayland
    2. that session's Class is not `greeter`
    3. the compositor process is running and serves a WAYLAND_DISPLAY socket

WHY CLASS IS CHECKED. greetd, sddm and gdm each register a Wayland session of their own
before any user logs in, so a type test alone passes a disk sitting at a login prompt.
logind reports Class=greeter for the display manager's session and Class=user for the one
with a desktop in it.

WHY AN X11 SESSION IS A FAILURE. labwc, hyprland and miracle-wm are Wayland-only
compositors; gnome, kde and cosmic all run Wayland on these configurations. An `x11` type
means something fell back, and passing it would pass a system that is not the one that was
built.

WHY THE TYPE IS ASKED FOR RATHER THAN PARSED. `loginctl list-sessions` reports SESSION,
UID, USER, SEAT and TTY - not the type. `show-session <id> -p Type --value` is the only
reliable source, so this asks once per id and reads only field one of the list. A version
that adds a column cannot shift it.

HOW IT GETS IN. lib/vm.py forwards a host port to the guest's 22 on every machine it
builds. configuration-minimal.nix (NixOS) and both Ubuntu answer files enable sshd with
password authentication for the account docker/admin, so nothing here needs a key.
sshpass supplies the password because there is no terminal to type it at.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

try:  # `import session` with lib/ on the path, or `from lib import session`
    import net
except ModuleNotFoundError:  # pragma: no cover - depends on how the caller added lib/
    from lib import net

# THE ACCOUNT, WHICH IS THE SAME ONE EVERY IMAGE IN THIS REPOSITORY USES.
#
# THE ENVIRONMENT OVERRIDES ARE FOR THE WALKTHROUGH DRIVER, NOT FOR A BAKE.
#
# A bake always means the guest on the bridge, and a default that moved would be a bake
# connecting somewhere else. But a `ssh:` step in a walkthrough is also the right shape for
# testing the driver itself against a plain sshd on the same host, and a hard-coded
# 172.30.0.1 makes that impossible without patching this file. So the defaults are unchanged
# and reading them is the only thing that changed.
SSH_USER = os.environ.get("SSH_USER") or "vmuser"
SSH_PASSWORD = os.environ.get("SSH_PASSWORD") or "admin"

# THE FORWARDED PORT EXISTS ON THE BRIDGE ADDRESS AND NOWHERE ELSE. Connecting to
# 127.0.0.1:2222 inside the container reaches the DNAT rule too, but DNAT does not rewrite
# the source, so the guest replies to its own loopback and the connect hangs. lib/net.py has
# the measurement; this is the one place the address is read from.
SSH_HOST = os.environ.get("SSH_HOST") or net.FORWARD_HOST


def log(message: str) -> None:
    print(f"[session {time.strftime('%H:%M:%S')}] {message}", flush=True)


def ssh(port: int, command: str, timeout: int = 30, host: str | None = None,
        user: str | None = None, password: str | None = None) -> tuple[int, str]:
    """Run one command in the guest. Returns (exit status, combined output).

    `host` IS HOW THE "ANOTHER CONTAINER" PROOF IS MADE. Everything in this repository
    connects to net.FORWARD_HOST, the bridge address, which only exists inside the container
    that owns the bridge. A sibling container cannot dial it. Passing one of this container's
    own addresses is the same ssh, over the same DNAT chain, arriving on PREROUTING instead
    of OUTPUT - which is exactly the path an outside client takes.

    `user` DEFAULTS TO SSH_USER, AND A WALKTHROUGH MAY NAME ITS OWN. Pop!_OS' installer
    derives the login name from the full name, so its disk carries `dockeruser` while every
    other image here carries the name it was given. That is one distribution's fact, so it
    travels with that distribution's file: the walk's `ssh_user` var reaches the `ssh:` steps
    through this parameter. See linux/popos/steps/ssh.yaml.

    NO KEY, NO TERMINAL. The password is the one every image here uses, and sshpass is
    what supplies it - there is no tty to prompt at.

    StrictHostKeyChecking=off and UserKnownHostsFile=/dev/null because this is a fresh
    guest on a forwarded port every run: a known-hosts file would either grow without
    bound or refuse the connection, and neither is a fact about the disk.
    """
    if not shutil.which("sshpass"):
        return 127, "sshpass is not installed"
    argv = [
        "sshpass", "-p", password or SSH_PASSWORD,
        "ssh", "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=10",
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        f"{user or SSH_USER}@{host or SSH_HOST}",
        command,
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"the command did not finish in {timeout}s"
    except OSError as exc:
        return 126, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def scp(local: str, remote: str, *, port: int = 2222, timeout: int = 300,
        user: str | None = None, host: str | None = None,
        password: str | None = None) -> tuple[int, str]:
    """Copy one local file to the guest. Returns (exit status, combined output).

    WHY THIS IS IN session.py AND NOT IN A STAGE. windows/11/devtools.py had its own `scp_to`
    because a 190-line PowerShell script cannot be typed at a keyboard and had to be a file.
    That is not a Windows fact - it is a fact about any stage whose guest work is a script,
    and a walkthrough that can only say `ssh:` cannot express it. So the client lives here,
    beside ssh(), with the same host/password/port rules.

    `user` and `host` are the same pair ssh() takes and for the same reasons: the account on
    the disk may not be the default one, and the "another container" proof needs an address
    that is not the bridge's.
    """
    if not shutil.which("sshpass"):
        return 127, "sshpass is not installed"
    argv = [
        "sshpass", "-p", password or SSH_PASSWORD,
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=10",
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        "-P", str(port),
        local, f"{user or SSH_USER}@{host or SSH_HOST}:{remote}",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"the copy did not finish in {timeout}s"
    except OSError as exc:
        return 126, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def wait_for_ssh(port: int, seconds: int, note=log, settle: int = 10,
                 probe: str = "true", user: str | None = None,
                 password: str | None = None) -> bool:
    """Wait until sshd answers and the account can log in.

    A BANNER IS NOT ENOUGH - this runs a command, because a socket that accepts a
    connection and then refuses authentication is a different failure and has been one
    here before. The probe is the smallest command that proves a shell ran.

    THE PROBE IS A PARAMETER BECAUSE `true` IS NOT A WINDOWS COMMAND. This module is used
    by Linux, FreeBSD and Windows guests, and the default is the POSIX shell's builtin; a
    Windows guest runs cmd.exe, where it is

        'true' is not recognized as an internal or external command,

    which is what run 41844808248 reported after a Windows sshd that was answering
    perfectly - the banner had already been read, `install ssh` had already verified
    `(Get-Service sshd).Status` was Running, and this wait then timed out for fifteen
    minutes on a command the shell did not have. Callers with a Windows guest pass
    probe="echo ok".

    A MISSING CLIENT FAILS IMMEDIATELY. ssh() returns 127 and the string "sshpass is not
    installed" when the binary is absent, and waiting minutes for a program that will
    never appear turns a one-line configuration mistake into a timeout that reads like a
    network problem. Run 41660216876 lost its whole 600-second budget that way.
    """
    if not shutil.which("sshpass"):
        note("=== sshpass IS NOT INSTALLED IN THIS CONTAINER ===")
        note("  the verdict is read over ssh, so the client has to be here before the")
        note("  wait begins. Install it in the workflow's prepare step.")
        return False

    deadline = time.time() + seconds
    started = time.time()
    out = ""
    status = None
    # A HEARTBEAT, BECAUSE 900 SILENT SECONDS ARE INDISTINGUISHABLE FROM A HANG.
    #
    # Run 42874167311's clean waited its whole budget and printed one line at the end. Whether
    # the guest was booting slowly, had booted with no network, or had died at HANDOFF TO XNU
    # was unknowable from the log - and those want opposite fixes. One line a minute costs
    # nothing and says which.
    beat = 60.0
    next_beat = started + beat
    while time.time() < deadline:
        status, out = ssh(port, probe, timeout=20, user=user, password=password)
        if status == 0:
            note(f"ssh answers after {time.time() - started:.0f}s")
            return True
        if time.time() >= next_beat:
            next_beat = time.time() + beat
            last = (out or "").strip().splitlines()
            note(f"  still waiting after {time.time() - started:.0f}s of {seconds}s "
                 f"(last status {status}: {last[-1][:120] if last else 'no output'})")
        time.sleep(settle)
    note(f"=== SSH DID NOT ANSWER IN {seconds}s ===")
    note(f"  the probe was: {probe}")
    # THE STATUS, NOT ONLY THE OUTPUT. A failure with an EMPTY output says nothing at all,
    # and that is exactly what run 42090649098 reported:
    #
    #     === SSH DID NOT ANSWER IN 180s ===
    #     the probe was: true
    #     last output:
    #
    # "ssh argued about credentials" and "ssh said nothing" print the same thing when only
    # the text is reported, and they are different failures. 255 is ssh's own code for a
    # failed connection or credential; 124 is this function's timeout; 127 is a missing client.
    note(f"  last status: {status}")
    note(f"  last output: {out[:300]}")
    explain(port, probe, note, user=user, password=password)
    return False


def explain(port: int, probe: str, note=log, user: str | None = None,
            password: str | None = None) -> None:
    """One last attempt with ssh's own account of why, printed in full.

    THIS IS NOT A SECOND CHECK, IT IS THE EXPLANATION FOR THE FIRST ONE. Everything the run
    has gathered so far is ambiguous by construction:

        the guest's own `ssh -o BatchMode=yes` says Permission denied
            - but BatchMode cannot answer a password prompt, so it proves only that sshd
              OFFERS password authentication

        `sshd -T` says passwordauthentication yes, usepam yes, no AllowUsers/DenyUsers
            - so the config should admit the account

        the host's probe fails with an empty output
            - which is neither an acceptance nor a readable refusal

    `-vvv` prints each method ssh offered, which one it tried, and what the server replied to
    each. It is the only output in this run that can separate "wrong password" from "PAM
    refused the account" from "the password never arrived", and it costs one connection.

    `user` IS THE ACCOUNT THE CALLER MEANT, because the retry has to name the same one the
    probe did - explaining a failure against a different account explains nothing.
    """
    if not shutil.which("sshpass"):
        return
    argv = [
        "timeout", "30",
        "sshpass", "-p", password or SSH_PASSWORD,
        "ssh", "-vvv", "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        f"{user or SSH_USER}@{SSH_HOST}",
        probe,
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        note(f"  the verbose retry failed to run: {exc}")
        return
    # THE LAST LINES, BECAUSE THE DECISION IS AT THE END. The banner and the cipher list are
    # the first twenty lines and say nothing about credentials.
    tail = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    note(f"  verbose retry exited {result.returncode}; last lines:")
    for line in tail[-12:]:
        note(f"    {line[:200]}")


def check_wayland_session(port: int, process: str, note=log,
                          allow_x11: bool = False) -> bool:
    """The verdict: an active, non-greeter Wayland session with `process` in it.

    `process` is the compositor for this desktop, matched with pgrep -f so a session
    started through a wrapper script is still found. Which binary it is belongs to the
    caller, because only the caller knows which desktop was built.
    """
    # --value is what makes this a bare `wayland` rather than `Type=wayland`. Active
    # matters: a session that is registered but inactive is a greeter that logged out.
    probe = (
        "for s in $(loginctl list-sessions --no-legend --no-pager 2>/dev/null | awk '{print $1}'); do "
        "  printf '%s|%s|%s|%s\\n' \"$s\" "
        "    \"$(loginctl show-session \"$s\" -p Type --value 2>/dev/null)\" "
        "    \"$(loginctl show-session \"$s\" -p Class --value 2>/dev/null)\" "
        "    \"$(loginctl show-session \"$s\" -p Active --value 2>/dev/null)\"; "
        "done"
    )
    status, sessions = ssh(port, probe)
    if status != 0:
        note(f"  loginctl could not be read: {sessions[:200]}")
        return False

    note(f"  sessions (id|type|class|active): {sessions.splitlines() or '(none)'}")

    wayland = []
    x11 = []
    for line in sessions.splitlines():
        fields = line.split("|")
        if len(fields) < 4:
            continue
        session_id, session_type, session_class, active = fields[:4]
        if active.strip().lower() not in ("yes", "1", "true"):
            continue
        klass = session_class.strip().lower()
        # THE GREETER'S OWN SESSION IS NOT A DESKTOP. See the module docstring.
        if klass not in ("user", "manager"):
            note(f"  ignoring session {session_id}: class {klass!r} is a greeter")
            continue
        kind = session_type.strip().lower()
        if kind == "wayland":
            wayland.append(session_id.strip())
        elif kind == "x11":
            x11.append(session_id.strip())

    if x11 and not wayland and not allow_x11:
        note(f"=== AN X11 SESSION INSTEAD OF WAYLAND: {x11} ===")
        note("  this disk is configured for Wayland; an x11 session means it fell back")
        return False

    # FREEBSD HAS NO logind AT ALL, SO THERE IS NO SESSION LIST TO READ.
    #
    # `loginctl list-sessions` returns nothing on this disk - not because no desktop is
    # running but because FreeBSD does not ship systemd-logind. The X server and lightdm
    # are ordinary processes there, and run 41763773907's screenshot shows the XFCE desktop
    # fully drawn while every check above said "no session":
    #
    #     sessions (id|type|class|active): (none)
    #     === NO ACTIVE USER WAYLAND SESSION ===
    #
    # So when `allow_x11` is set, the session list is not consulted at all: the question is
    # whether the desktop's own process is running, and whether an X server is up beside
    # it. That is the whole of what a BSD desktop can be asked.
    if allow_x11 and not wayland and not x11:
        note("no logind session is listed (this system has none); checking processes")
        return _check_x11_process(port, process, note)

    # FREBSD'S XFCE IS AN X SESSION AND THAT IS THE CONFIGURATION.
    #
    # every Linux desktop in this repository runs Wayland, and an x11 session there means
    # something fell back. FreeBSD is the exception by design: XFCE on Xorg is what its
    # ports tree recommends for a VM, and the disk was built to run it. `allow_x11` says so
    # once, in the caller, rather than the check carrying a list of distributions.
    if x11 and not wayland:
        note(f"an X11 session is running ({x11}) - allowed for this disk")
        return _check_x11_process(port, process, note)

    if not wayland:
        note("=== NO ACTIVE USER WAYLAND SESSION ===")
        note("  the system booted and answers on ssh, so this is the desktop itself")
        status, logtext = ssh(
            port,
            "journalctl -b --no-pager -n 40 "
            "-u greetd -u sddm -u gdm -u display-manager 2>/dev/null | tail -30 || true",
        )
        for line in logtext.splitlines()[-20:]:
            note(f"  journal: {line}")
        return False

    status, procs = ssh(port, f"pgrep -af '{process}' 2>/dev/null | head -5 || true")
    if not procs.strip():
        note(f"=== NO '{process}' PROCESS, THOUGH A WAYLAND SESSION EXISTS ===")
        note("  the greeter registered a session whose compositor is not running")
        status, logtext = ssh(
            port,
            "journalctl -b --no-pager -n 60 "
            "-u greetd -u sddm -u gdm -u display-manager 2>/dev/null | tail -30 || true",
        )
        for line in logtext.splitlines()[-20:]:
            note(f"  journal: {line}")
        return False

    note(f"wayland session(s) {wayland} running {process}")
    for line in procs.splitlines()[:3]:
        note(f"  process: {line.strip()}")

    # THE WAYLAND SOCKET ON DISK, READ WITH sudo.
    #
    # WAYLAND_DISPLAY is what a CLIENT sets to find a compositor. The compositor is the
    # SERVER: it creates the socket and does not need the variable, so asking its
    # environment for one is asking a question that has nothing to do with whether the
    # session works. Run 41646780105 failed all six desktops on exactly that -
    # kwin_wayland running with `--socket wayland-1`, logind reporting
    # `1|wayland|user|yes`, and the check still saying "not serving a Wayland socket"
    # because the server had no WAYLAND_DISPLAY of its own.
    #
    # miracle failed differently and for the same reason: /proc/625/environ was
    # "Permission denied" because the compositor runs as sddm's uid, not docker's. A
    # check that reads another user's /proc needs sudo, and one that reads a socket file
    # in /run/user/1000 needs it too.
    #
    # So: sudo, and the socket file itself. `wayland-1` is the abstract name of
    # /run/user/1000/wayland-1, so an existing file with that name is the server's own
    # proof that it bound a socket. 1000 is docker's uid in every image here
    # (configuration-minimal.nix creates it as the first normal user).
    # THE SOCKET, FOUND THREE WAYS, BECAUSE sudo IS NOT GUARANTEED.
    #
    # `sudo ls` is the direct route and it runs as docker, which subiquity puts in the
    # sudo group - where sudo asks for a password. Over ssh with no tty there is nowhere
    # to type one, so the command fails silently and this reported "NO WAYLAND SOCKET" on
    # a system whose kwin_wayland had been started with --socket wayland-0 (run
    # 41675028701).
    #
    # So the list is built from what docker can already reach: its own runtime directory
    # (no sudo needed for your own /run/user/<uid>), the compositor's command line, and
    # only then sudo as a fallback.
    probe_socket = (
        "echo XDG:${XDG_RUNTIME_DIR:-unset}; "
        "ls -l /run/user/$(id -u)/wayland-* 2>/dev/null | head -5; "
        "echo ---; "
        "ls -l /tmp/.X11-unix 2>/dev/null | head -2; "
        "echo ---; "
        "for p in $(pgrep -f '" + process + "' | head -1); do "
        "tr '\\0' '\\n' < /proc/$p/cmdline 2>/dev/null "
        "| grep -oE 'wayland-[0-9]+' | head -2; done; "
        "echo ---; "
        "sudo -n ls -l /run/user/$(id -u)/wayland-* 2>/dev/null | head -5"
    )
    status, sockets = ssh(port, probe_socket)
    listed = sockets.strip()
    note(f"  wayland sockets: {listed.replace(chr(10), ' | ') if listed else '(none)'}")
    if "wayland-" not in listed:
        note("=== NO WAYLAND SOCKET FOR THE RUNNING COMPOSITOR ===")
        note("  the compositor process exists but no socket names it; the probe above")
        note("  shows the runtime dir, the X11 sockets, and its own command line")
        status, listing = ssh(port, "ls -la /run/user/$(id -u)/ 2>/dev/null | head -15")
        for line in listing.splitlines()[:12]:
            note(f"  run: {line.strip()}")
        return False

    return True


def _check_x11_process(port: int, process: str, note) -> bool:
    """The X11 half of the check: is the desktop's process running in that session?

    Same question as the Wayland path - a session registered but whose desktop never started
    is the failure this stage exists to catch - and the same answer: pgrep for the process
    the caller names. FreeBSD's entry is `xfce4-session`.
    """
    status, procs = ssh(port, f"pgrep -af '{process}' 2>/dev/null | head -5 || true")
    if not procs.strip():
        note(f"=== NO '{process}' PROCESS, THOUGH AN X11 SESSION EXISTS ===")
        note("  the display manager started a session whose desktop is not running")
        status, logtext = ssh(
            port,
            "tail -30 /var/log/lightdm/lightdm.log 2>/dev/null || "
            "tail -30 /var/log/Xorg.0.log 2>/dev/null || true",
        )
        for line in logtext.splitlines()[-15:]:
            note(f"  log: {line}")
        return False
    note(f"the X11 session is running {process}")
    for line in procs.splitlines()[:3]:
        note(f"  process: {line.strip()}")

    # AND AN X SERVER BESIDE IT. A desktop process with no display would be started and
    # useless; the pair is what makes this a session rather than a stray daemon.
    status, xserver = ssh(port, "pgrep -af 'Xorg|X :' 2>/dev/null | head -3 || true")
    if xserver.strip():
        note(f"  X server: {xserver.splitlines()[0].strip()[:80]}")
    else:
        note("  no X server process found; the desktop may be running without a display")
    return True


def wait_for_wayland(port: int, process: str, seconds: int, note=log,
                     ssh_seconds: int | None = None, settle: int = 20,
                     allow_x11: bool = False) -> bool:
    """Wait for ssh, then for the Wayland session. True only when both happen.

    SSH IS A PRECONDITION, NOT A SEPARATE TEST. If it never answers the session cannot be
    read, and the useful sentence is about ssh rather than about logind.
    """
    if not wait_for_ssh(port, ssh_seconds or min(seconds, 600), note):
        return False

    deadline = time.time() + seconds
    started = time.time()
    while time.time() < deadline:
        if check_wayland_session(port, process, note, allow_x11=allow_x11):
            note(f"the Wayland session is up after {time.time() - started:.0f}s")
            return True
        note(f"  {time.time() - started:.0f}s: no session yet")
        time.sleep(settle)
    note(f"=== NO WAYLAND SESSION IN {seconds}s ===")
    return False
