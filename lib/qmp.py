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

SCREEN_W = int(os.environ.get("SCREEN_W", "1920"))
SCREEN_H = int(os.environ.get("SCREEN_H", "1080"))


class QMP:
    """A QMP connection.

    QEMU SERVES ONE CLIENT AT A TIME on a socket, and this repository opens two kinds: the
    frame recorder (lib/snapshots.py, which holds its connection for the whole bake) and the
    driver that types and clicks. A second connection is accepted by the listening socket and
    then queued - so the connect() succeeds and the GREETING never arrives:

        File "lib/qmp.py", line 52, in __init__
            self.greeting = self._read_message()
        TimeoutError: timed out

    which is run 41886018594, after the desktop had drawn.

    There is no version of this that avoids the wait - the socket is single-client by design
    - so the wait is explicit and long enough for a frame write, rather than a 30-second
    timeout that reads as "qemu is dead". A caller that has one connection and keeps it (see
    redox/bake.py's Console.live()) never pays this.
    """

    def __init__(self, path, *, greeting_timeout: int = 120):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(greeting_timeout)
        self.sock.connect(path)
        self.buf = b""
        self.greeting = self._read_message()
        # Once the greeting is in, a request should be answered promptly again.
        self.sock.settimeout(30)
        self.execute("qmp_capabilities")

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
        try:
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
    _send(q, f"move and press at ({x}, {y})", [
        {"type": "abs", "data": {"axis": "x", "value": to_abs(x, SCREEN_W)}},
        {"type": "abs", "data": {"axis": "y", "value": to_abs(y, SCREEN_H)}},
        {"type": "btn", "data": {"down": True, "button": "left"}},
    ])
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


def key_combo(q, *qcodes):
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
