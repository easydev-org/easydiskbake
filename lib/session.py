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

import shutil
import subprocess
import time

# THE ACCOUNT, WHICH IS THE SAME ONE EVERY IMAGE IN THIS REPOSITORY USES.
SSH_USER = "docker"
SSH_PASSWORD = "admin"


def log(message: str) -> None:
    print(f"[session {time.strftime('%H:%M:%S')}] {message}", flush=True)


def ssh(port: int, command: str, timeout: int = 30) -> tuple[int, str]:
    """Run one command in the guest. Returns (exit status, combined output).

    NO KEY, NO TERMINAL. The password is the one every image here uses, and sshpass is
    what supplies it - there is no tty to prompt at.

    StrictHostKeyChecking=off and UserKnownHostsFile=/dev/null because this is a fresh
    guest on a forwarded port every run: a known-hosts file would either grow without
    bound or refuse the connection, and neither is a fact about the disk.
    """
    if not shutil.which("sshpass"):
        return 127, "sshpass is not installed"
    argv = [
        "sshpass", "-p", SSH_PASSWORD,
        "ssh", "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=10",
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        f"{SSH_USER}@127.0.0.1",
        command,
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"the command did not finish in {timeout}s"
    except OSError as exc:
        return 126, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def wait_for_ssh(port: int, seconds: int, note=log, settle: int = 10) -> bool:
    """Wait until sshd answers and the account can log in.

    A BANNER IS NOT ENOUGH - this runs a command, because a socket that accepts a
    connection and then refuses authentication is a different failure and has been one
    here before. `true` is the smallest command that proves a shell ran.
    """
    deadline = time.time() + seconds
    started = time.time()
    out = ""
    while time.time() < deadline:
        status, out = ssh(port, "true", timeout=20)
        if status == 0:
            note(f"ssh answers after {time.time() - started:.0f}s")
            return True
        time.sleep(settle)
    note(f"=== SSH DID NOT ANSWER IN {seconds}s ===")
    note(f"  last output: {out[:300]}")
    return False


def check_wayland_session(port: int, process: str, note=log) -> bool:
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

    if x11 and not wayland:
        note(f"=== AN X11 SESSION INSTEAD OF WAYLAND: {x11} ===")
        note("  every disk here is configured for Wayland; an x11 session means it fell back")
        return False

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

    # WAYLAND_DISPLAY IN THE COMPOSITOR'S OWN ENVIRONMENT is the proof that a Wayland
    # socket exists - not merely that a process with the right name is in the table.
    status, envout = ssh(
        port,
        "for p in $(pgrep -f '" + process + "' | head -1); do "
        "tr '\\0' '\\n' < /proc/$p/environ 2>/dev/null "
        "| grep -E '^(WAYLAND_DISPLAY|XDG_SESSION_TYPE)=' ; done",
    )
    for line in envout.splitlines():
        note(f"  env: {line.strip()}")
    if "XDG_SESSION_TYPE=x11" in envout:
        note("=== THE COMPOSITOR'S OWN ENVIRONMENT SAYS X11 ===")
        return False
    if "WAYLAND_DISPLAY=" not in envout:
        note("=== THE COMPOSITOR HAS NO WAYLAND_DISPLAY ===")
        note("  a process by that name is running but is not serving a Wayland socket")
        return False

    return True


def wait_for_wayland(port: int, process: str, seconds: int, note=log,
                     ssh_seconds: int | None = None, settle: int = 20) -> bool:
    """Wait for ssh, then for the Wayland session. True only when both happen.

    SSH IS A PRECONDITION, NOT A SEPARATE TEST. If it never answers the session cannot be
    read, and the useful sentence is about ssh rather than about logind.
    """
    if not wait_for_ssh(port, ssh_seconds or min(seconds, 600), note):
        return False

    deadline = time.time() + seconds
    started = time.time()
    while time.time() < deadline:
        if check_wayland_session(port, process, note):
            note(f"the Wayland session is up after {time.time() - started:.0f}s")
            return True
        note(f"  {time.time() - started:.0f}s: no session yet")
        time.sleep(settle)
    note(f"=== NO WAYLAND SESSION IN {seconds}s ===")
    return False
