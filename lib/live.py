"""Keep the recording and the last frame current, so a cancelled run still has them.

    live = live_artifacts(shots, "out/install.mp4", "out/last-frame.png")
    live.start()          # before qemu, or right after
    ...
    live.stop()           # optional; it is a daemon thread and ends with the process

THE PROBLEM THIS SOLVES, AND IT IS NOT A SMALL ONE

Every workflow here uploads `out/install.mp4` and `out/last-frame.png` on `always()`, on
the reasoning that a cancelled run should still show what the guest was doing. That was
untrue in the one case it mattered: the encode happens at the END of a bake, so a run
cancelled at minute forty uploaded neither. Run 41588015377 was cancelled after sixteen
minutes of a macOS install and its artifact was 4 KiB - a console log and a list of frame
filenames nobody could open.

The frames themselves were on the runner the whole time and were thrown away.

WHAT THIS DOES

A thread that wakes every few seconds and, when there are new frames, rewrites both
artifacts from whatever exists at that moment:

    the mp4          every frame so far, re-encoded
    last-frame.png   the newest frame, converted

Re-encoding the whole sequence repeatedly sounds wasteful and is not, for the sizes here:
thirty seconds of 1280x800 PPM encodes in about a second with -preset ultrafast, and a
bake runs for twenty to ninety minutes. The alternative - an encoder that appends - needs
a fixed frame count and a container that tolerates it, and neither survives the run being
killed halfway through writing.

WHY IT IS A THREAD AND NOT A SUBPROCESS

It has to die with the run. A daemon thread does, without anything having to notice a
SIGTERM. A subprocess would need the same signal handling the bake is already failing to
do, which is the problem in the first place.

WHY THE ARTIFACTS ARE REPLACED, NOT WRITTEN IN PLACE

ffmpeg writes a container with a footer; a reader that opens it mid-write gets a truncated
file even when the frames were all there. So each update encodes to a temporary name and
renames over the target, which is atomic on the same filesystem - the artifact a consumer
downloads is always a complete one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time


def log(message: str) -> None:
    print(f"[live {time.strftime('%H:%M:%S')}] {message}", flush=True)


class LiveArtifacts:
    """A background encoder over a growing frame directory.

    EVERY FAILURE HERE IS LOGGED AND IGNORED. This is evidence, not the result: a bake
    that installed a system and could not encode a video is a successful bake with a
    worse artifact, and turning that into a failure would trade one problem for a bigger
    one.
    """

    def __init__(self, frames_dirs: list[str], video: str, last_frame: str,
                 every: float = 20.0, fps: int = 1):
        self.frames_dirs = [d for d in frames_dirs if d]
        self.video = video
        self.last_frame = last_frame
        self.every = every
        self.fps = fps
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # The number of frames the current video was made from, so an unchanged frame
        # count does not re-encode the same thing.
        self._encoded = -1

    def start(self) -> "LiveArtifacts":
        if not shutil.which("ffmpeg"):
            log("no ffmpeg, so no live recording; the frames are still on disk")
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self, final: bool = True) -> None:
        """Stop the loop, and do one last encode so the artifacts match the last frame.

        THE FINAL PASS IS THE POINT OF CALLING THIS AT ALL. The loop's last encode may
        be up to `every` seconds behind the last frame, and the difference is exactly the
        interesting part of a run that failed near its end.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None
        if final:
            self._encode(force=True)

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            try:
                self._encode()
            except Exception as exc:  # noqa: BLE001 - evidence is not worth a failure
                log(f"  could not update the artifacts: {exc}")

    def count(self) -> int:
        total = 0
        for directory in self.frames_dirs:
            try:
                total += len([f for f in os.listdir(directory)
                              if f.startswith("frame-") and f.endswith(".ppm")])
            except OSError:
                pass
        return total

    def _encode(self, force: bool = False) -> None:
        total = self.count()
        if total == 0:
            return
        if not force and total <= self._encoded:
            return

        self._write_last_frame()
        if self._write_video():
            self._encoded = total

    def _write_last_frame(self) -> None:
        """The newest frame as a PNG, replaced atomically."""
        newest = None
        newest_mtime = -1.0
        for directory in self.frames_dirs:
            try:
                names = [n for n in os.listdir(directory)
                         if n.startswith("frame-") and n.endswith(".ppm")]
            except OSError:
                continue
            for name in names:
                path = os.path.join(directory, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest_mtime = mtime
                    newest = path

        if newest is None:
            return

        # A frame that is still being written cannot be read; skip this round rather than
        # writing a truncated PNG over a good one.
        import lastframe

        tmp = self.last_frame + ".part"
        try:
            if not lastframe.convert(newest, tmp):
                if os.path.exists(tmp):
                    os.unlink(tmp)
                return
            os.replace(tmp, self.last_frame)
        except Exception as exc:  # noqa: BLE001
            log(f"  last frame: {exc}")
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _write_video(self) -> bool:
        """Every frame so far as an mp4, replaced atomically. True when it was written."""
        stage = tempfile.mkdtemp(prefix="live-mp4-")
        try:
            n = 0
            for directory in self.frames_dirs:
                try:
                    names = sorted(
                        f for f in os.listdir(directory)
                        if f.startswith("frame-") and f.endswith(".ppm")
                    )
                except OSError:
                    continue
                for name in names:
                    src = os.path.join(directory, name)
                    dst = os.path.join(stage, f"f-{n:05d}.ppm")
                    try:
                        os.link(src, dst)
                    except OSError:
                        try:
                            shutil.copyfile(src, dst)
                        except OSError:
                            # A frame mid-write: leave it out of this pass rather than
                            # encoding half a picture.
                            continue
                    n += 1

            if n == 0:
                return False

            tmp = self.video + ".part"
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-framerate", str(self.fps), "-start_number", "0",
                    "-i", os.path.join(stage, "f-%05d.ppm"),
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    tmp,
                ],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                log(f"  encode failed: {(result.stderr or '').strip()[:200]}")
                if os.path.exists(tmp):
                    os.unlink(tmp)
                return False
            os.replace(tmp, self.video)
            return True
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def start(frames_dirs: list[str], video: str, last_frame: str,
          every: float = 20.0, fps: int = 1) -> LiveArtifacts:
    """Convenience: build and start in one call."""
    return LiveArtifacts(frames_dirs, video, last_frame, every=every, fps=fps).start()
