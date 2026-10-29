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
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect(path)
        self.buf = b""
        self.greeting = self._read_message()
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


def move(q, x, y):
    q.execute("input-send-event", events=[
        {"type": "abs", "data": {"axis": "x", "value": to_abs(x, SCREEN_W)}},
        {"type": "abs", "data": {"axis": "y", "value": to_abs(y, SCREEN_H)}},
    ])


def click(q, x, y):
    move(q, x, y)
    time.sleep(0.3)
    q.execute("input-send-event", events=[
        {"type": "btn", "data": {"down": True, "button": "left"}}])
    time.sleep(0.15)
    q.execute("input-send-event", events=[
        {"type": "btn", "data": {"down": False, "button": "left"}}])


def key(q, qcode):
    q.execute("input-send-event", events=[
        {"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": qcode}}}])
    time.sleep(0.08)
    q.execute("input-send-event", events=[
        {"type": "key", "data": {"down": False, "key": {"type": "qcode", "data": qcode}}}])


def key_combo(q, *qcodes):
    """Press several keys together, e.g. key_combo(q, "meta_l", "spc") for Cmd-Space.

    QMP delivers held state, so a chord is the modifiers down, the key down and up,
    then the modifiers up in reverse order. Sending them as separate presses - which
    is what key() does - produces Cmd, release, Space, release, which macOS reads as
    two unrelated keystrokes rather than a shortcut.
    """
    for qcode in qcodes:
        q.execute("input-send-event", events=[
            {"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": qcode}}}])
        time.sleep(0.05)
    time.sleep(0.1)
    for qcode in reversed(qcodes):
        q.execute("input-send-event", events=[
            {"type": "key", "data": {"down": False, "key": {"type": "qcode", "data": qcode}}}])
        time.sleep(0.05)


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
