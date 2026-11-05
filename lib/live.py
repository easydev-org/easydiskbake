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
import sys
import tempfile
import threading
import time


def log(message: str) -> None:
    print(f"[live {time.strftime('%H:%M:%S')}] {message}", flush=True)


def part_path(target: str) -> str:
    """A temporary name ffmpeg will still recognise the format of.

    THE SUFFIX GOES BEFORE THE EXTENSION. `install.mp4.part` has no extension as far as
    ffmpeg is concerned, and it refuses to guess:

        Unable to choose an output format for '.../install.mp4.part';
        use a standard extension for the filename or specify the format manually

    Every muxer in this module is chosen by extension - mp4 for the video, png for the
    frame - so appending ".part" to the whole name breaks both. Run 41599424550 printed
    that error every twenty seconds for the whole NixOS install, and the fix in the
    temporary naming, not in the muxer list.
    """
    stem, dot, suffix = target.rpartition(".")
    if not dot:
        return target + ".part"
    return f"{stem}.part.{suffix}"


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
        # Set when an encode failed because the filesystem filled up. The loop stops trying;
        # the caller's stop() still writes the last frame from the frames on disk.
        self._full = False
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
                              if f.startswith("frame-") and f.endswith((".ppm", ".jpg"))])
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
        """The newest frame as a PNG, replaced atomically.

        A PPM WITH A JPEG BESIDE IT IS NOT A CANDIDATE, and that rule is the whole of this
        change. lib/snapshots.py writes frame-N.ppm, converts it to frame-N.jpg and deletes
        the PPM, once a second, for the whole run. The PPM is the NEWEST file at the moment
        this looks - it was just written - so picking by mtime chooses it, and by the time
        ffmpeg opens it the conversion has deleted it:

            [lastframe] ffmpeg failed: Error opening input: No such file or directory
            Error opening input file .../frames/shots/frame-1103.ppm

        which run 42009463444 printed every few minutes for an hour. The reflection was
        already fixed in lib/lastframe.newest_frame (skip a PPM that has a JPEG beside it)
        and lib/lastframe.newest_frame_stable (re-check that the file is still there), but
        THIS function does its own selection and never called either - so the fix covered the
        stage's final frame and not the live one updated every twenty seconds.

        The extension comparison is done after the listing, not during, because the two
        names have to be seen together: `present` is a set from one listdir, so a JPEG that
        appears between two listdir calls cannot make this choose a PPM that has no JPEG.
        """
        newest = None
        newest_mtime = -1.0
        for directory in self.frames_dirs:
            try:
                names = [n for n in os.listdir(directory)
                         if n.startswith("frame-") and n.endswith((".ppm", ".jpg"))]
            except OSError:
                continue
            present = set(names)
            for name in names:
                path = os.path.join(directory, name)
                if name.endswith(".ppm") and (name[:-4] + ".jpg") in present:
                    # The conversion finished and the unlink has not happened yet. The JPEG
                    # is the same picture and is not about to disappear.
                    continue
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
        #
        # THE MODULE IS FOUND RELATIVE TO THIS FILE, NOT TO sys.path. Every caller here
        # adds the repository root to sys.path, and lastframe lives in lib/ - so
        # `import lastframe` resolved only in the bakes that also inserted lib/ itself.
        # The BSD bakes do not, and run 41599358426 printed
        #
        #     could not update the artifacts: No module named 'lastframe'
        #
        # every twenty seconds for the whole install. The recording is the artifact that
        # explains a failure, and this was silently losing half of it.
        lib_dir = os.path.dirname(os.path.abspath(__file__))
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        import lastframe

        # THE SELECTION ABOVE NARROWS THE WINDOW; convert() CLOSES IT.
        #
        # Skipping a PPM that has a JPEG beside it cannot cover the case where the listing
        # sees a lone PPM and the conversion runs before ffmpeg opens it. lib/lastframe.convert
        # now handles exactly that - it falls back to the sibling JPEG with the same stem and
        # retries - so this does not repeat the logic. One place decides what a missing frame
        # means, and that place is the function that reads it.
        tmp = part_path(self.last_frame)
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
                    # `frame-` FILES, WHATEVER SNAPSHOTS.PY LEFT THEM AS.
                    #
                    # lib/snapshots.py converts each screendump to JPEG and deletes the PPM -
                    # a PPM is 3 MB and an hour of them is 11 GiB. The encoder does not care
                    # which format it reads, so this looks for the name rather than the
                    # extension and trusts ffmpeg with what it finds.
                    names = sorted(
                        f for f in os.listdir(directory)
                        if f.startswith("frame-") and f.endswith((".ppm", ".jpg"))
                    )
                except OSError:
                    continue
                for name in names:
                    src = os.path.join(directory, name)
                    # THE EXTENSION IS KEPT: ffmpeg identifies an image by its content, but a
                    # staged file named .ppm that holds JPEG bytes is the kind of thing that
                    # works until a tool trusts the name.
                    dst = os.path.join(stage, f"f-{n:05d}{os.path.splitext(name)[1]}")
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

            # EVERY STAGED FRAME IS A JPEG, WHATEVER IT ARRIVED AS.
            #
            # The encoder needs one extension to build a sequence from, and `frame-*.ppm`
            # stopped being the only one when lib/snapshots.py began converting each
            # screendump to JPEG: during a transition there really is a mixed set.
            #
            # `-pattern_type glob -i 'f-*'` DOES NOT WORK FOR THAT, and this is worth
            # recording because it looks like it should. ffmpeg's image2 demuxer rejects a
            # glob with no extension at the end:
            #
            #     [in#0 @ ...] Error opening input: No such file or directory
            #     Error opening input file /tmp/live-mp4-xxxx/f-*.
            #
            # measured on ffmpeg 7.1.5, for `f-*`, `f-*.[jp]*` and `f-*.?` alike, while
            # `f-*.jpg` and `f-*.ppm` both work. macos26's bake logged that error every
            # twenty seconds for an hour and produced a recording artifact containing one
            # PNG and no video.
            #
            # So the staging pass normalises instead: anything that is not already a JPEG is
            # converted here, into the same `f-%05d.jpg` sequence the encoder is then given.
            # It costs one ffmpeg call per non-JPEG frame, which is only the frames written
            # before snapshots.py started converting - normally none.
            sequence = os.path.join(stage, "seq-%05d.jpg")
            k = 0
            for name in sorted(os.listdir(stage)):
                src = os.path.join(stage, name)
                if not name.startswith("f-"):
                    continue
                dst = os.path.join(stage, f"seq-{k:05d}.jpg")
                if name.endswith(".jpg"):
                    os.replace(src, dst)
                else:
                    r = subprocess.run(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                         "-i", src, "-q:v", "4", dst],
                        capture_output=True, text=True,
                    )
                    if r.returncode != 0 or not os.path.exists(dst):
                        # A FRAME THAT WILL NOT CONVERT IS DROPPED, NOT FIXED UP. It is one
                        # of hundreds and the run continues; guessing at its format would
                        # put a wrong picture in the recording.
                        log(f"  could not convert {name}: "
                            f"{(r.stderr or '').strip()[:120]}")
                        continue
                    try:
                        os.unlink(src)
                    except OSError:
                        pass
                k += 1
            if k == 0:
                return False

            tmp = part_path(self.video)
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-framerate", str(self.fps),
                    "-i", sequence,
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    tmp,
                ],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or "").strip()
                log(f"  encode failed: {detail[:200]}")
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

                # A FULL DISK STOPS THE RECORDING RATHER THAN THE RUN.
                #
                # macos26 filled its runner mid-install and this retried on every pass, each
                # attempt writing a partial file into a filesystem that had no room for it -
                # so the failure was repeated every twenty seconds for the rest of the bake
                # and the guest's own writes were competing with it.
                #
                # The frames on disk are still there and `keep the last frame` uploads them, so
                # stopping the ENCODER loses the video and nothing else. Stopping it loudly is
                # better than a log full of identical errors.
                if "No space left on device" in detail:
                    log("  === THE RUNNER IS FULL; RECORDING STOPS HERE ===")
                    log("  the frames are still on disk and the last one is uploaded")
                    self._full = True
                return False
            os.replace(tmp, self.video)
            return True
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def start(frames_dirs: list[str], video: str, last_frame: str,
          every: float = 20.0, fps: int = 1) -> LiveArtifacts:
    """Convenience: build and start in one call."""
    return LiveArtifacts(frames_dirs, video, last_frame, every=every, fps=fps).start()
