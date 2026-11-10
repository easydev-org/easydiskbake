"""Talk to the qemu guest agent: a JSON stream over a unix socket.

    import qga
    a = qga.Agent("/work/qga.sock")
    a.wait(timeout=120)          # the agent answers after it starts
    a.shutdown("powerdown")      # a REAL shutdown request, executed by the guest
    a.close()

WHY THIS EXISTS AND `system_powerdown` DOES NOT

`system_powerdown` IS AN ACPI EVENT. What a guest does with it is the guest's business, and the
two families here disagree:

    Windows   stops its services, flushes its filesystems, powers off. Works.
    macOS     puts up the "Restart / Sleep / Cancel / Shut Down" dialog and waits for a click.
              On a headless guest nobody clicks, so the request becomes a timeout and a kill -
              which is what this repository spent runs 42762244256 and 42778795463 discovering.

The guest agent is the other route: the request goes to a process INSIDE the guest that calls the
system's own shutdown. It is still the guest's shutdown path - it is not a power cut - which is
what `shutdown -h now` over ssh also gives. The difference is that it needs no ssh, no password,
and no network: it works on a guest whose sshd has died, and it works before sshd has started.

WHAT THE PROTOCOL IS

One JSON object per line, {"execute": "..."} to the agent and {"return": ...} back. The channel is
a virtio-serial port (Windows, Linux) or an ISA serial port (macOS, where the third-party agent
moved to ISA because Apple's own virtio agent is gated on Apple's Virtualization.framework).

    guest-ping        is anything there?
    guest-sync        flush a stale reply out of the pipe before asking something real
    guest-shutdown    mode: powerdown (default), reboot, halt

THE FIRST READ IS ALWAYS A SYNC, AND THAT IS NOT PEDANTRY. The channel is a byte stream with no
framing, and an agent that was restarted mid-reply leaves a partial line in it. `guest-sync` takes
a token and echoes it back, so anything received before the echo was debris.
"""

from __future__ import annotations

import json
import os
import socket
import time

# THE AGENT IS NOT THERE UNTIL IT IS. On Windows the QEMU-GA service starts a few seconds after
# login; on macOS the LaunchDaemon does. A socket that exists means qemu made the chardev, not
# that anything is listening on the other end.
DEFAULT_TIMEOUT = 60


class AgentError(RuntimeError):
    pass


class Agent:
    """One connection to one guest's agent socket."""

    def __init__(self, path: str, *, connect_timeout: float = 30.0,
                 read_timeout: float = 60.0) -> None:
        self.path = path
        deadline = time.time() + connect_timeout
        while not os.path.exists(path):
            if time.time() > deadline:
                raise AgentError(
                    f"the agent socket {path} did not appear within {connect_timeout}s; "
                    f"the guest is either not running or was started without a qga device")
            time.sleep(0.5)
        # A FILE THAT EXISTS IS NOT A LISTENER. qemu creates the socket when the machine starts;
        # the agent inside the guest arrives later, so a connect can succeed and the first read
        # can block forever. `read_timeout` is what turns that into an error.
        time.sleep(0.5)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(read_timeout)
        try:
            self.sock.connect(path)
        except OSError as exc:
            raise AgentError(f"could not connect to {path}: {exc}") from exc
        self.buf = b""

    # --- the wire ---------------------------------------------------------------------

    def _read_message(self) -> dict:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AgentError("the agent closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        line = line.strip()
        if not line:
            return self._read_message()
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            # A PARTIAL LINE FROM A HALF-FINISHED REPLY IS EXPECTED BEFORE A SYNC, and it is not
            # an error worth raising: the caller's sync is what clears it.
            return {"return": None, "_unparsed": line[:120].decode("utf-8", "replace")}

    def _execute(self, command: str, **arguments) -> dict:
        payload = {"execute": command}
        if arguments:
            payload["arguments"] = arguments
        self.sock.sendall((json.dumps(payload) + "\n").encode())
        # SKIP ASYNCHRONOUS EVENTS. The agent emits {"event": ...} for things the host did not ask
        # about (a suspend, a filesystem freeze); the reply is the message with return or error.
        while True:
            msg = self._read_message()
            if "return" in msg or "error" in msg:
                return msg

    # --- the calls --------------------------------------------------------------------

    def sync(self, token: int = -1) -> bool:
        """Clear whatever is left in the pipe. True if the agent echoed the token."""
        token = token if token >= 0 else int(time.time()) & 0x7FFFFFFF
        try:
            reply = self._execute("guest-sync", id=token)
        except (AgentError, OSError):
            return False
        return reply.get("return") == token

    def ping(self) -> bool:
        try:
            return "return" in self._execute("guest-ping")
        except (AgentError, OSError):
            return False

    def wait(self, timeout: float = DEFAULT_TIMEOUT) -> bool:
        """Yes when the agent answers a ping. The guest may take seconds to start it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.sync() and self.ping():
                return True
            time.sleep(1)
            # THE SOCKET CAN BE RESET BY THE GUEST BOOTING DURING THIS LOOP, so a dead connection
            # is retried rather than fatal - the caller is waiting for a process that has not
            # started yet.
            if not self._socket_alive():
                self._reconnect()
        return False

    def _socket_alive(self) -> bool:
        try:
            self.sock.send(b"")
            return True
        except OSError:
            return False

    def _reconnect(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(60.0)
            self.sock.connect(self.path)
            self.buf = b""
        except OSError:
            pass

    def shutdown(self, mode: str = "powerdown") -> None:
        """Ask the guest to shut down (`powerdown`), restart (`reboot`) or halt.

        THE REPLY MAY NEVER ARRIVE, AND THAT IS SUCCESS. The guest powers off while the agent is
        still writing the answer, so the socket dies mid-call. Treating that as an error would
        report every successful shutdown as a failure - so a closed connection here is not raised.
        """
        try:
            self._execute("guest-shutdown", mode=mode)
        except (AgentError, OSError):
            pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Agent":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
