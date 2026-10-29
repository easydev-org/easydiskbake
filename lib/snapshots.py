"""Photograph a guest's screen on a timer, for as long as it runs.

    snapshots.py <qmp.socket> <outdir> [interval] [limit]

Writes outdir/frame-0000.ppm, frame-0001.ppm ... one every `interval` seconds
(default 30), until the QMP socket goes away or `limit` frames exist.

WHY THIS EXISTS

Every failure in the MX bake so far was diagnosed from a log line, and every log line
was a summary of something that had already happened. What was missing each time was
the screen: whether a dialog was waiting, whether a pane had changed, whether the
installer was still drawing. Those questions have answers in a picture and nowhere
else - a curses TUI and a GRUB prompt both look like silence in a serial log.

Printing the *number* of words OCR read was what finally showed that the installer's
button row is invisible to tesseract. A frame every thirty seconds is the same idea
with less guesswork: after the run, the whole sequence can be read in order.

The frame number is in the filename so the sequence sorts without a manifest, and the
timestamp is written beside it, because "which step was this" is the first question
anyone asks of a picture.
"""

import json
import os
import socket
import sys
import time


class QMP:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(20)
        self.sock.connect(path)
        self.buf = b""
        self._read()
        self.execute("qmp_capabilities")

    def _read(self):
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
        while True:
            msg = self._read()
            if "return" in msg or "error" in msg:
                return msg


def main():
    qmp_path, outdir = sys.argv[1], sys.argv[2]
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else 400

    os.makedirs(outdir, exist_ok=True)
    index_path = os.path.join(outdir, "frames.txt")

    # The socket may not exist yet: qemu creates it a moment after it starts.
    q = None
    for _ in range(60):
        try:
            if os.path.exists(qmp_path):
                q = QMP(qmp_path)
                break
        except (OSError, RuntimeError):
            pass
        time.sleep(2)
    if q is None:
        print("[snap] no QMP socket at %s - no frames" % qmp_path, flush=True)
        return 1

    print("[snap] connected; one frame every %gs into %s" % (interval, outdir), flush=True)

    n = 0
    with open(index_path, "a") as index:
        while n < limit:
            name = "frame-%04d.ppm" % n
            path = os.path.join(outdir, name)
            try:
                reply = q.execute("screendump", filename=path)
            except (OSError, RuntimeError):
                # qemu stopped: that is the end of the run, not an error.
                print("[snap] QMP closed after %d frames" % n, flush=True)
                break
            if "error" in reply:
                print("[snap] screendump %d failed: %s" % (n, reply["error"]), flush=True)
            else:
                stamp = time.strftime("%H:%M:%S")
                index.write("%s %s %s\n" % (name, stamp, os.path.getsize(path)
                                            if os.path.exists(path) else 0))
                index.flush()
                print("[snap] %s at %s" % (name, stamp), flush=True)
            n += 1
            time.sleep(interval)

    print("[snap] %d frames written" % n, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
