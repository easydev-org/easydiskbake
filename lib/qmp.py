#!/usr/bin/env python3
"""Minimal QMP client for driving a QEMU guest with absolute pointer events.

    qmp.py <qmp.sock> <command> [args...]

Commands:
    status                     print the guest's run state
    move <x> <y>               move the pointer to absolute display coordinates
    click <x> <y>              move and left-click
    key <qcode>                press and release one key
    shot <path.ppm>            framebuffer dump (same file format as HMP)
    size                       print the display geometry QEMU reports

WHY QMP AND NOT HMP. The HMP driver in this repo failed completely: 64 clicks
left the pointer in the top-left corner and all 64 frames were byte-identical.
HMP's `mouse_move` carries relative deltas, while the guest has a usb-tablet,
which is an absolute device - so the deltas went nowhere useful. QMP's
input-send-event takes `abs` axis events plus explicit button state, which is
what an absolute pointer actually needs:

    {"execute":"input-send-event","arguments":{"events":[
        {"type":"abs","data":{"axis":"x","value":X}},
        {"type":"abs","data":{"axis":"y","value":Y}},
        {"type":"btn","data":{"down":true,"button":"left"}},
        {"type":"btn","data":{"down":false,"button":"left"}}]}}

The axis value is in the range QEMU reports for the console; query it with
query-input? No - it is reported per-event-range by `query-display-options` only
for some backends, so this uses the documented 0..32767 range that QEMU accepts
for absolute events and scales the requested pixel coordinates into it.
"""
import json
import os
import socket
import sys
import time

# QEMU accepts absolute pointer events on a 0..32767 scale regardless of the
# framebuffer size, so pixel coordinates are converted here.
ABS_MAX = 32767


def log(message: str) -> None:
    print(f"[qmp] {message}", flush=True)

SCREEN_W = int(os.environ.get("SCREEN_W", "1920"))
SCREEN_H = int(os.environ.get("SCREEN_H", "1080"))


class QMP:
    """A QMP connection.

    QEMU SERVES ONE CLIENT AT A TIME on a socket. A second connection is accepted by the
    listening socket and then queued - so connect() succeeds and the GREETING never arrives:

        File "lib/qmp.py", line 52, in __init__
            self.greeting = self._read_message()
        TimeoutError: timed out

    which is run 41886018594, after the desktop had drawn.

    THIS CLASS USED TO ARGUE THE WAIT WAS UNAVOIDABLE AND LENGTHEN THE TIMEOUT TO 120 SECONDS.
    That was the wrong answer to the right observation. The constraint is real, but it is a
    constraint on the SOCKET, not on each caller - so two callers that want the same socket are
    the same client, and this class hands them the same object (see `_open` below). A second
    connection is now impossible rather than unlikely, and every "remember to pass q= down"
    at a call site stops being load-bearing.

    WHAT PROMPTED IT: four separate stages fixed the same bug at four call sites.
    `dismiss_post_login_windows` opened its own connection and then called
    `dismiss_keyboard_assistant` without passing it, so the inner call opened a second one and
    blocked on the greeting for its whole 240-second timeout with no output. The fix at that
    call site was one argument; the fix here is that there is no such argument to forget.
    """

    # --- ONE CONNECTION PER SOCKET, FOR THE WHOLE PROCESS -------------------------------
    #
    # THE CONSTRAINT IS QEMU'S, SO IT IS ENFORCED HERE ONCE RATHER THAN AT TWENTY-FOUR CALL
    # SITES. `-qmp unix:...,server=on,wait=off` serves ONE client per socket: a second connect()
    # succeeds at the TCP/unix level and then never gets a greeting, because the first client is
    # still being served. Every "pass q=... down" in this repository was a call site remembering
    # that, and each one that forgot cost a stage:
    #
    #     dismiss_post_login_windows opened its own and called dismiss_keyboard_assistant
    #     without passing it; the inner call opened a second and blocked on the greeting
    #     forever, inside a 240-second timeout, producing no output at all.
    #
    # The docstring below used to argue the wait was unavoidable and lengthened the timeout to
    # 120 seconds. It is avoidable: two callers that want the same socket are the same client,
    # and handing them one object makes "a second connection" impossible rather than unlikely.
    #
    # REFERENCE COUNTED, because `with`-less code all over this repository does
    #
    #     q = QMP(path); ...; q.close()
    #
    # and closing on the first `close()` would break the caller that is still using it. The
    # socket closes when the last holder does. `_forget()` drops the entry so a LATER process
    # or a rebooted qemu gets a new connection rather than a dead object.
    _open: "dict[str, QMP]" = {}

    def __new__(cls, path, *, greeting_timeout: int = 120):
        existing = cls._open.get(path)
        if existing is not None and existing.alive():
            existing._holders += 1
            log(f"reusing the QMP connection to {path} (holder {existing._holders})")
            return existing
        self = super().__new__(cls)
        self._holders = 1
        return self

    def __init__(self, path, *, greeting_timeout: int = 120):
        # __new__ MAY HAVE RETURNED A LIVE OBJECT, and re-running __init__ on it would send a
        # second `qmp_capabilities` and reset the greeting - so the work happens once.
        if getattr(self, "_connected", False):
            self.path = path
            return
        self.path = path
        self._connected = False
        self._closed = False
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(greeting_timeout)
        try:
            self.sock.connect(path)
        except OSError:
            self._holders = 0
            raise
        self.buf = b""
        self.greeting = self._read_message()
        # Once the greeting is in, a request should be answered promptly again.
        self.sock.settimeout(30)
        self.execute("qmp_capabilities")
        self._connected = True
        type(self)._open[path] = self

    def alive(self) -> bool:
        """Is this connection still usable? A dead one must not be handed to a new caller."""
        if getattr(self, "_closed", True) or self.sock is None:
            return False
        try:
            return self.sock.fileno() >= 0
        except (OSError, ValueError):
            return False

    def _forget(self) -> None:
        if type(self)._open.get(getattr(self, "path", "")) is self:
            type(self)._open.pop(self.path, None)

    def _read_message(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("QMP closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def execute(self, command, **arguments):
        payload = {"execute": command}
        if arguments:
            payload["arguments"] = arguments
        self.sock.sendall((json.dumps(payload) + "\n").encode())
        # Skip asynchronous events; return the first reply carrying 'return'.
        while True:
            msg = self._read_message()
            if "return" in msg or "error" in msg:
                return msg

    def close(self):
        """Release one hold. The socket closes when the LAST holder lets go.

        A caller that closed the shared connection while another step was still reading would
        turn a working walkthrough into "OSError: Bad file descriptor" - which this repository
        has already spent a run on (see macos/15/ssh.py's note about closing a caller's socket
        taking the screen reader down with it).
        """
        self._holders = getattr(self, "_holders", 1) - 1
        if self._holders > 0:
            return
        self._closed = True
        self._forget()
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass


def to_abs(pixel, extent):
    value = int(round(pixel * ABS_MAX / float(extent)))
    return max(0, min(ABS_MAX, value))


def _send(q, what, events):
    """Send one input event list and RAISE IF QEMU REFUSED IT.

    EVERY CALLER HERE USED TO DROP THE REPLY. qemu answers

        {"error": {"class": "GenericError", "desc": "..."}}

    for a device that is not attached, an axis it does not know, or a fd that has gone
    away - and a dropped reply turns that into a keystroke that silently did not happen,
    followed by a log line saying it did. Run 41871160859 clicked the dock for ninety
    seconds and the dock region of every frame was byte identical: the pointer never moved
    and nothing said so.
    """
    reply = q.execute("input-send-event", events=events)
    if isinstance(reply, dict) and "error" in reply:
        raise RuntimeError(
            f"input-send-event ({what}) was refused by qemu: {reply['error']}. "
            f"The input did not reach the guest; a caller that logs it as though it did "
            f"is testing the screen saver."
        )


def move(q, x, y):
    return q.execute("input-send-event", events=[
        {"type": "abs", "data": {"axis": "x", "value": to_abs(x, SCREEN_W)}},
        {"type": "abs", "data": {"axis": "y", "value": to_abs(y, SCREEN_H)}},
    ])


def click(q, x, y):
    """Move, then press and release, and REFUSE TO PRETEND IT WORKED.

    `execute()` returns the reply, and every caller here used to drop it - so a qemu that
    answered

        {"error": {"class": "GenericError", "desc": "..."}}

    to `input-send-event` produced no click, no exception, and a caller that logged
    "click (115, 1051)" as though it had happened. Run 41871160859 is what that looks like
    from the outside: the dock region of the frame was BYTE IDENTICAL before and after the
    click, twenty times over ninety seconds, because the pointer never moved.

    A device that is not there, an axis QEMU does not know, a `usb-tablet` that did not
    attach - all of those answer an error, and all of them were invisible.
    """
    # THE POSITION AND THE PRESS GO IN THE SAME EVENT LIST.
    #
    # Three separate requests - move, then press, then release - leave the guest free to
    # handle the button before it has applied the coordinates, and Orbital did: run
    # 41882033727 clicked (115, 1051) six times across three attempts and the dock only
    # flickered (dark fraction 0.40 -> 0.41 -> 0.40). The frames show the dock redrawing
    # around the cursor and no launch, which is what a click delivered at the OLD pointer
    # position looks like - the pointer had not moved yet when the button arrived.
    #
    # QMP does not reorder events inside one input-send-event, so putting the axes and the
    # press together makes "at (x, y), pressed" a single fact. The release follows in its
    # own request after the same 0.15s, because a widget that polls needs to see the button
    # down for at least one poll - sent back to back, the guest reads up/up and opens
    # nothing.
    # A POINTER THAT HAS NEVER MOVED MAY NOT BE AT (0, 0). A usb-tablet resumes with
    # whatever position it last held, and the guest's first absolute report can be ignored -
    # so the coordinate stream starts with a corner and a move to the target, in one request,
    # then the same target again. The duplicate is not redundant: the first move establishes
    # the position, the second is the one the guest acts on.
    _send(q, f"move to {x}, {y}", [
        {"type": "abs", "data": {"axis": "x", "value": 0}},
        {"type": "abs", "data": {"axis": "y", "value": 0}},
        {"type": "abs", "data": {"axis": "x", "value": to_abs(x, SCREEN_W)}},
        {"type": "abs", "data": {"axis": "y", "value": to_abs(y, SCREEN_H)}},
    ])
    time.sleep(0.2)
    _send(q, f"move and press at ({x}, {y})", [
        {"type": "abs", "data": {"axis": "x", "value": to_abs(x, SCREEN_W)}},
        {"type": "abs", "data": {"axis": "y", "value": to_abs(y, SCREEN_H)}},
        {"type": "btn", "data": {"down": True, "button": "left"}},
    ])
    time.sleep(0.15)
    _send(q, f"release at ({x}, {y})", [
        {"type": "btn", "data": {"down": False, "button": "left"}}])


def point_relative(q, x, y):
    """Put a PS/2 pointer at (x, y) without pressing anything."""
    # A CORNER FIRST, so the position is known rather than assumed: a large negative delta
    # saturates every guest's accumulator at (0, 0), and from there the target is exact.
    _send(q, "home the pointer", [
        {"type": "rel", "data": {"axis": "x", "value": -4000}},
        {"type": "rel", "data": {"axis": "y", "value": -4000}},
    ])
    time.sleep(0.2)
    _send(q, f"move to ({x}, {y})", [
        {"type": "rel", "data": {"axis": "x", "value": x}},
        {"type": "rel", "data": {"axis": "y", "value": y}},
    ])
    time.sleep(0.2)
    q._cursor = (x, y)


def click_relative(q, x, y):
    """Move a PS/2 mouse from where it is to (x, y), then click.

    A PS/2 mouse reports RELATIVE motion - it has no idea where the screen is - so a
    coordinate cannot be sent to it. This walks it: the guest tells us nothing, so the motion
    is the difference from the last position this function placed it at, which is tracked on
    the `q` object.

    WHY THIS EXISTS: Redox's usbhid does not bind `usb-tablet`, so its cursor never moves on
    an absolute event - measured on run 41894870375, where the arrow is still at (0, 0) in
    the frame after six clicks. Its ps2d driver does handle the PS/2 mouse, which is the
    device this drives.
    """
    point_relative(q, x, y)
    _send(q, f"press at ({x}, {y})", [
        {"type": "btn", "data": {"down": True, "button": "left"}}])
    time.sleep(0.15)
    _send(q, f"release at ({x}, {y})", [
        {"type": "btn", "data": {"down": False, "button": "left"}}])


def key(q, qcode):
    """Press and release one key, in a single QMP event list.

    DOWN AND UP MUST ARRIVE TOGETHER.

    This used to be two execute() calls with a sleep between them. That works, and it is
    what dockur-macos had until it found the failure mode: when another thread shares the
    QMP socket - the frame recorder here, the capture thread there - the second call can
    be interleaved with the other thread's traffic, and the guest sees a key that went
    down and never came up. The visible symptom is a modifier stuck on, or a menu that
    opens every time the pointer moves.

    One events list keeps the pair inside one request. dockur-macos fixed the same thing
    in the same way: "sending down and up as one events list keeps the pair inside a
    single lock acquisition".
    """
    _send(q, f"key {qcode}", [
        {"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": qcode}}},
        {"type": "key", "data": {"down": False, "key": {"type": "qcode", "data": qcode}}},
    ])


def key_combo(q, *qcodes, hold: float = 0.0):
    """Press several keys together, e.g. key_combo(q, "meta_l", "spc") for Cmd-Space.

    QMP delivers held state, so a chord is the modifiers down, the key down and up, then
    the modifiers up in reverse order. Sending them as separate presses produces Cmd,
    release, Space, release, which macOS reads as two unrelated keystrokes rather than a
    shortcut.

    ALL OF IT GOES IN ONE EVENT LIST. The previous version executed 2N separate
    requests, one per key press and one per release, with sleeps between them - so a
    shared socket could interleave anything at all into the middle of a chord, and a
    failed request part-way through left the modifiers held down.
    """
    events = [
        {"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": qcode}}}
        for qcode in qcodes
    ]
    events += [
        {"type": "key", "data": {"down": False, "key": {"type": "qcode", "data": qcode}}}
        for qcode in reversed(qcodes)
    ]
    # A HOLD LONG ENOUGH FOR THE GUEST TO SEE, WHEN THE GUEST IS NOT A HID STACK.
    #
    # One event list is right for macOS and Windows: their input stacks deliver a modifier
    # and a key from the same message as a chord. Redox is not that. Its fbcond reads /dev
    # events and the launcher is handed super-key presses through a
    # `code >= 0x1000_0000` marker - a state the kernel sets while meta is DOWN - so a
    # modifier that goes down and up inside the same input message never produces a super
    # event at all.
    #
    # Measured on run 41920674838: Super+T sent this way opened nothing, and its launcher
    # never logged the keystroke.
    #
    # IT IS OPT-IN (`hold=<seconds>`), AND THAT IS DELIBERATE. macOS and Windows are driven
    # through this same function with the single-list form and both are verified working -
    # four macOS shortcuts and a Windows Win+R among them. Changing what they receive to fix
    # a Redox quirk would be trading a known-good path for a guess.
    #
    # So the chord is sent as two requests with the modifier held between them:
    #
    #     press meta + t     (one list, so both arrive together)
    #     150ms              long enough for the event to be read with meta down
    #     release t + meta   (one list, in reverse)
    #
    # The release order is still reversed, so a guest that does see it as one chord is
    # unaffected.
    if hold and len(qcodes) > 1:
        presses = [{"type": "key",
                    "data": {"down": True, "key": {"type": "qcode", "data": qcode}}}
                   for qcode in qcodes]
        _send(q, "chord down " + "+".join(qcodes), presses)
        time.sleep(hold)
        releases = [{"type": "key",
                     "data": {"down": False, "key": {"type": "qcode", "data": qcode}}}
                    for qcode in reversed(qcodes)]
        _send(q, "chord up " + "+".join(qcodes), releases)
        return
    _send(q, "chord " + "+".join(qcodes), events)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    sock_path, cmd = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]

    q = QMP(sock_path)
    try:
        if cmd == "status":
            print(json.dumps(q.execute("query-status")))
        elif cmd == "size":
            print(json.dumps(q.execute("query-display-options")))
        elif cmd == "move":
            move(q, int(args[0]), int(args[1]))
            print("moved to %s,%s" % (args[0], args[1]))
        elif cmd == "click":
            click(q, int(args[0]), int(args[1]))
            print("clicked %s,%s" % (args[0], args[1]))
        elif cmd == "key":
            key(q, args[0])
            print("key " + args[0])
        elif cmd == "shot":
            print(json.dumps(q.execute("screendump", filename=args[0])))
        else:
            sys.exit("unknown command: " + cmd)
    finally:
        q.close()


if __name__ == "__main__":
    # Guarded so this file can also be imported as a module by
    # setup-assistant-drive.py. Without the guard, importing it ran main(),
    # which tried to open a socket from argv that the importer did not supply -
    # the driver died at the import line and the run produced no frames at all.
    main()
