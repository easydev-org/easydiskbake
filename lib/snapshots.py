"""Photograph a guest's screen on a timer, for as long as it runs.

    snapshots.py <qmp.socket> <outdir> [interval] [limit]

Writes outdir/frame-0000.ppm, frame-0001.ppm ... one every `interval` seconds
(default 30), until the QMP socket goes away.

THE `limit` ARGUMENT IS OPTIONAL AND IS NORMALLY OMITTED. The socket going away is when qemu
exited, which is the only end of a recording that means anything; a frame count on top of it
can cut a run short. One caller still passes one and means it: lib/selftest.py takes 6, because
it is checking that the recorder runs rather than recording anything.

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
import subprocess
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

    # NO FRAME LIMIT BY DEFAULT, AND THAT IS A CORRECTION.
    #
    # There was a required `limit` argument, and every caller passed one - 2000 for most
    # bakes, chosen against a 5s interval so that it covered about three hours. When the
    # interval became 1s the same number covered 33 minutes, and a macOS setup that takes 66
    # would have had its second half silently dropped: the recording would end mid-install
    # and nothing in the log would say the recorder had stopped rather than the guest.
    #
    # The cap was never protecting anything. The loop already ends when QMP closes, which is
    # when qemu exits, which is the only end that means anything. A count on top of that can
    # only ever cut a run short - so the default is unlimited, and a caller that wants a cap
    # still passes one.
    limit = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].strip() else None

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
        while limit is None or n < limit:
            # THE FRAME IS BUILT UNDER A HIDDEN NAME AND RENAMED WHEN IT IS FINISHED.
            #
            # Every reader in this repository picks a frame by listing this directory for
            # names that start with `frame-` and end in .ppm or .jpg. While a screendump is
            # being written and converted, the final name must therefore hold NOTHING - or
            # it holds a PPM that is about to be unlinked, and the reader loses it.
            #
            # That race cost three separate fixes (lib/lastframe.newest_frame,
            # lib/live._write_last_frame, and a retry inside convert) and each one only
            # narrowed the window. The window is the bug: a name that appears before the file
            # is complete is a lie to every process that reads the directory.
            #
            # THE PREFIX IS `.tmp-`, NOT `frame-`. A reader filters on `frame-`, so a
            # half-written file is invisible to it - it is not "a frame that might be
            # readable", it is not a frame at all yet. `.tmp-` also sorts out of the way in
            # any listing and cannot collide with `frame-NNNN`.
            tmp_ppm = os.path.join(outdir, ".tmp-frame-%04d.ppm" % n)
            tmp_jpg = os.path.join(outdir, ".tmp-frame-%04d.jpg" % n)
            name = "frame-%04d.jpg" % n
            path = os.path.join(outdir, name)
            try:
                reply = q.execute("screendump", filename=tmp_ppm)
            except (OSError, RuntimeError):
                # qemu stopped: that is the end of the run, not an error.
                print("[snap] QMP closed after %d frames" % n, flush=True)
                break
            if "error" in reply:
                print("[snap] screendump %d failed: %s" % (n, reply["error"]), flush=True)
                # A FAILED SCREENDUMPS LEAVES NOTHING BEHIND. The temporary name is not
                # visible to a reader, but it is still a file on a runner with a few GB to
                # spare, and a persistent failure would accumulate them.
                for leftover in (tmp_ppm, tmp_jpg):
                    if os.path.exists(leftover):
                        try:
                            os.unlink(leftover)
                        except OSError:
                            pass
            else:
                # THE PPM IS CONVERTED TO JPEG AND DELETED, IMMEDIATELY.
                #
                # A 1280x800 PPM is 3.07 MB and this writes one a second for the length of a
                # bake. macos26 runs for over an hour:
                #
                #     3960 frames x 3.07 MB = 11.3 GiB
                #
                # which filled the runner mid-install and made ffmpeg fail with "No space left
                # on device" while the guest kept writing. The recording that came out of the
                # same run was 1.8 MB - the frames, not the video, were the problem.
                #
                # JPEG at q=4 is 27 KB for the same picture: 0.22 GiB for the whole run.
                converted = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                     "-i", tmp_ppm, "-q:v", "4", tmp_jpg],
                    capture_output=True, text=True,
                )
                if converted.returncode == 0 and os.path.exists(tmp_jpg):
                    # THE RENAME IS THE PUBLICATION. Before it, no reader can see this frame
                    # at all; after it, the name holds a complete JPEG and nothing will
                    # unlink it. os.replace is atomic within a filesystem, so a listing
                    # either does not see the name or sees the finished file.
                    os.replace(tmp_jpg, path)
                    if os.path.exists(tmp_ppm):
                        os.unlink(tmp_ppm)
                    size = os.path.getsize(path)
                else:
                    # A CONVERSION THAT FAILED PUBLISHES THE PPM INSTEAD. It is the frame,
                    # and losing it to save space would be trading the evidence for the room
                    # to store it - so the finished PPM takes the published name and readers
                    # get a real picture, in the format the recorder could produce.
                    print("[snap] could not convert %s: %s"
                          % (name, (converted.stderr or "").strip()[:120]), flush=True)
                    png_name = "frame-%04d.ppm" % n
                    path = os.path.join(outdir, png_name)
                    name = png_name
                    os.replace(tmp_ppm, path)
                    size = os.path.getsize(path) if os.path.exists(path) else 0
                    if os.path.exists(tmp_jpg):
                        os.unlink(tmp_jpg)

                stamp = time.strftime("%H:%M:%S")
                index.write("%s %s %s\n" % (name, stamp, size))
                index.flush()
                print("[snap] %s at %s" % (name, stamp), flush=True)
            n += 1
            time.sleep(interval)

    print("[snap] %d frames written" % n, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
