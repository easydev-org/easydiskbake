"""Write a script into a guest's serial console, a line at a time, on a schedule.

    ser = Serial(work, "serial")            # creates serial.in / serial.out
    ser.open()
    ser.say("echo BAKE_BEGIN", wait=3)
    ser.say("sudo nixos-install ... ; echo INSTALL_RC=$?", wait=600)
    ser.close()

WHY THIS EXISTS, AND WHO IT IS FOR

Only NixOS. Its installer is not a wizard that takes a file - it is `nixos-install`,
run at a root shell. So the bake types at that shell, and the way to type into a
guest with no network and no running services is its serial console.

MX 25.3 and Mint are installed through the GUI instead: their installers are wizards
with no working unattended mode, so linux/mx drives them with QMP clicks at measured
coordinates. That is a different tool for a different fact about those distributions,
not an older version of this one.

WHY A FIFO AND NOT QMP

QMP has `human-monitor-command`, and it can send keys - to the KEYBOARD. A serial
console is not the keyboard: keystrokes sent that way reach the guest's input layer,
which a live ISO has routed to a virtual console only while nothing else has it. The
serial chardev is the console itself, and qemu writes it to a pipe.

WHY THE PAIR OF FIFOS

qemu's `-chardev pipe,path=X` opens two files itself:

    X.in    the host writes here; the bytes become the guest's console input
    X.out   the guest's console output; the host reads it

and if either is missing qemu fails at startup with "Could not open ... No such file
or directory". So they are created with mkfifo BEFORE the machine starts, and the
caller opens X.in for writing and keeps it open for the life of the guest.

WHY THE WRITER IS HELD OPEN

A fifo gives EOF to the reader when its last writer closes, and qemu treats that as
the console being gone - the guest then loses its input for the rest of the run. So one
file object stays open from `open()` to `close()`, and each `say()` writes into it. The
shell quoting is the caller's: this adds a carriage return and nothing else.
"""

from __future__ import annotations

import os
import time


def log(message: str) -> None:
    print(f"[serial {time.strftime('%H:%M:%S')}] {message}", flush=True)


def fifo_pair(work: str, name: str = "serial") -> tuple[str, str]:
    """Create <work>/<name>.in and <work>/<name>.out, and return both paths.

    REMOVED AND RECREATED, not reused. A fifo left by a previous run in the same work
    directory would still be a fifo, but anything that wrote into it has gone - and a
    reader opening it would block waiting for a writer that is never coming. Starting
    from two fresh ones is the same cost and cannot inherit that state.
    """
    paths = []
    for suffix in (".in", ".out"):
        path = os.path.join(work, name + suffix)
        if os.path.exists(path):
            os.unlink(path)
        os.mkfifo(path)
        paths.append(path)
    return paths[0], paths[1]


class Serial:
    """A writer for the guest's console, with the timing spelled out at each step.

    THE WAITS ARE THE INTERFACE. There is no way to know from outside whether a command
    has finished: the console is a stream of characters and the shell prompt is a string
    this code would have to parse - which is the same mistake as reading a screenshot for
    a verdict, in text form. So each `say()` takes the time that step needs, measured
    from a run that worked, and the caller passes a duration rather than waiting for a
    pattern.
    """

    def __init__(self, work: str, name: str = "serial"):
        os.makedirs(work, exist_ok=True)
        self.stdin, self.stdout = fifo_pair(work, name)
        self._writer = None

    def open(self, timeout: float = 30.0) -> "Serial":
        """Open the writer side.

        OPENING BLOCKS UNTIL QEMU IS READING. A fifo has no buffer until both ends are
        open, so `open(for writing)` on a host whose qemu has not started yet waits -
        which is correct, but must not last forever if qemu died on its own command line.
        The timeout turns that into a sentence instead of a hung job.
        """
        import threading

        result: dict[str, object] = {}

        def opener() -> None:
            try:
                result["f"] = open(self.stdin, "wb", buffering=0)
            except OSError as exc:  # noqa: BLE001 - reported by the caller
                result["e"] = exc

        thread = threading.Thread(target=opener, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive() or "f" not in result:
            raise RuntimeError(
                f"qemu never opened {self.stdin} for reading within {timeout:.0f}s. "
                f"Either the machine did not start, or its serial is not a chardev on "
                f"this path - check the qemu command line for '-serial chardev:serial0'."
            )
        self._writer = result["f"]
        return self

    def say(self, line: str, wait: float = 3.0) -> None:
        """Write one line and wait.

        A CARRIAGE RETURN, NOT A NEWLINE. The guest's console is a terminal in cooked
        mode; 0x0d is what a terminal sends when Enter is pressed on a serial line, and
        0x0a alone is a line feed that the tty layer may not treat as end-of-line.
        easysandbox-vm's script uses `printf "...\\n"`, and the difference this makes is
        invisible until a command does not run.
        """
        if self._writer is None:
            raise RuntimeError("Serial.open() has not been called")
        log(f"> {line[:110]}")
        self._writer.write(line.encode() + b"\r")
        time.sleep(wait)

    def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except OSError:
                pass
            self._writer = None
