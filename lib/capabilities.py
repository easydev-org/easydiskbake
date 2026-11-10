"""What a guest can actually do, checked before a step relies on it.

    import capabilities
    capabilities.require("qga", guest=guest)          # raises Broken if there is no agent
    capabilities.require("ssh", port=2222)            # raises Broken if nothing answers

WHY THIS EXISTS: A FALLBACK CHAIN IS A SILENT LIE

`vm_session.stop()` used to try the agent, then ssh, then the power button, then kill - each one
falling through to the next without saying so. The result was a stage that reported a clean
shutdown and had actually killed the machine, and the log could not be read to tell which:

    macOS has no working ACPI power button. So "3. the power button" was never going to work,
    the fallback still ran for 300 seconds, and only the kill at the end was real.

THE SAME SHAPE ONE LEVEL UP: a step that says `ssh:` on a guest whose sshd never came up waits out
its timeout and reports a connection error. That is not a fact about ssh - it is a fact about the
guest, and it was known before the step started.

SO A STEP DECLARES WHAT IT NEEDS AND A MISSING CAPABILITY STOPS THE RUN

    - name: shut down through the agent
      vm_shutdown: guest_agent
      requires: [qga]

    - name: shut down over ssh
      vm_shutdown: ssh
      requires: [ssh]

`requires:` is checked BEFORE the step does anything, so the failure names the missing capability
rather than the timeout it caused. That is a breaker, not a fallback: the run stops, and the
message says what the plan assumed.

WHAT THIS IS NOT. It is not a retry and not a preference order. A file that wants "the agent if it
is there, ssh otherwise" says so with two steps and an `unless:`, which is visible; a chain inside
one function is not.
"""

from __future__ import annotations

import os
import socket
import sys


class Broken(RuntimeError):
    """A capability the step declared is not present. The run stops."""


def _log(message: str) -> None:
    print(f"[capabilities] {message}", flush=True)


# --- the probes ---------------------------------------------------------------------------
#
# EACH RETURNS (ok, why). `why` is what the failure message says, and it is written for a reader
# looking at a log rather than at this file: "no qga socket at <path>" beats "False".


def probe_ssh(port: int = 2222, *, user: str = "vmuser", password: str = "admin",
              timeout: int = 5) -> tuple[bool, str]:
    """Does a shell answer on this port, over the same channel the `ssh:` step uses?"""
    try:
        import session
    except ImportError as exc:
        return False, f"no session module ({exc})"
    status, output = session.ssh(port, "echo ok", timeout=timeout,
                                 user=user, password=password)
    if status == 0:
        return True, ""
    return False, f"ssh on port {port} answered {status}: {(output or '').strip()[:120]}"


def probe_qga(socket_path: str, *, timeout: float = 10.0) -> tuple[bool, str]:
    """Does a guest agent answer on this socket?

    A SOCKET THAT EXISTS IS NOT AN AGENT. qemu creates the chardev when the machine starts and
    the agent inside the guest arrives later - or never, if it was not installed. `qga.Agent`
    waits for the file and then pings, which is the only test that distinguishes the two.
    """
    if not socket_path:
        return False, "this machine was started with no qga device (Shape.qga_socket is empty)"
    if not os.path.exists(socket_path):
        return False, f"no qga socket at {socket_path}; the machine has no agent channel"
    try:
        import qga
    except ImportError as exc:
        return False, f"no qga module ({exc})"
    try:
        agent = qga.Agent(socket_path, connect_timeout=2.0, read_timeout=timeout)
    except qga.AgentError as exc:
        return False, f"the agent socket exists but nothing answers: {exc}"
    try:
        if agent.wait(timeout=timeout):
            return True, ""
        return False, (f"a socket at {socket_path} and no agent behind it; the guest either has "
                       f"no qga installed or it is not running")
    finally:
        agent.close()


def probe_network(host: str, port: int = 445, *, timeout: float = 3.0) -> tuple[bool, str]:
    """Is anything accepting on this host and port?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, f"nothing accepting on {host}:{port} ({exc})"


def probe_share(host: str = "", port: int = 445) -> tuple[bool, str]:
    """Is the host serving the share the guest is about to mount?"""
    try:
        import hostshare
        if hostshare.listening(host, port):
            return True, ""
        return False, f"nothing serving on {host or 'the bridge'}:{port}"
    except ImportError as exc:
        return False, f"no hostshare module ({exc})"


# --- the registry -------------------------------------------------------------------------
#
# A CAPABILITY IS A NAME AND A PROBE THAT NEEDS THE CONTEXT. The step supplies the context, so
# the probes above stay functions of their own arguments rather than globals.


def check(name: str, *, guest=None, port: int = 2222, user: str = "vmuser",
          password: str = "admin", host: str = "", share_port: int = 445,
          log=_log) -> tuple[bool, str]:
    """Is `name` available? Returns (ok, why)."""
    if name == "ssh":
        return probe_ssh(port, user=user, password=password)
    if name == "qga":
        socket_path = getattr(getattr(guest, "shape", None), "qga_socket", "")
        return probe_qga(socket_path)
    if name == "share":
        return probe_share(host, share_port)
    if name == "network":
        ok, why = probe_network(host or "127.0.0.1", share_port)
        return ok, why
    if name == "overlay":
        return (True, "") if guest is not None else (False, "no machine is running")
    return False, (f"unknown capability {name!r}; this file knows ssh, qga, share, network, "
                   f"overlay")


def require(names, *, guest=None, port: int = 2222, user: str = "vmuser",
            password: str = "admin", host: str = "", share_port: int = 445) -> None:
    """Raise Broken unless every capability in `names` is available.

    ALL OF THEM ARE CHECKED before the first failure is raised, so one run reports every missing
    capability rather than one per attempt. A stage that needs two things and has neither should
    say so once.
    """
    if isinstance(names, str):
        names = [names]
    missing = []
    for name in names:
        ok, why = check(name, guest=guest, port=port, user=user, password=password,
                        host=host, share_port=share_port)
        if not ok:
            missing.append(f"{name}: {why}")
        else:
            _log(f"  {name}: available")
    if missing:
        raise Broken(
            "a capability this step declares is missing, so the step cannot run:\n  "
            + "\n  ".join(missing)
            + "\n  (the step says `requires:`; nothing here fell back to something else)")
