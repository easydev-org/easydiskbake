"""Read a guest's screen and click what it says.

    from ocr import Screen
    s = Screen("/tmp/monitor.sock.qmp", workdir="/tmp")
    words = s.text(s.grab("frame"))
    target = find_button(words, labels, primary=(x, y))
    s.click(*target)

WHY THIS EXISTS, AND WHY IT IS COPIED FROM dockur-macos

Every other bake in this repository is told what to do by a file: preseed,
kickstart, autoinstall, a JSON configuration, a package list. MX's installer has no
unattended mode that survives - --tui starts and then stops at 'Scanning disk
drives', and the GUI is what is left. Driving a GUI means finding a control on the
screen and clicking it, and that is what this does.

dockur-macos drives macOS's Setup Assistant the same way, and its source records the
reasoning that is worth keeping:

  "WHY OCR, AFTER EVERY COORDINATE GUESS FAILED. Each attempt so far picked a target
   from a heuristic and each one was wrong in a different way: a guessed (760,430)
   that was not on the list, an accent band that was Continue's focus ring, another
   that was a 72x76 icon. Reading what is written on the screen is the step that was
   missing."

The pointer half is equally important: HMP's mouse_move carries RELATIVE deltas, and
a guest with usb-tablet is an absolute device - so the deltas go nowhere. Their
comment on that: 64 clicks, pointer still in the top-left corner, 64 byte-identical
frames. QMP's input-send-event takes absolute axes, which qmp.py uses.

WHAT IS DELIBERATELY NOT HERE

dockur's BUTTONS list contains only words that appear on controls and never in body
copy, because a word from prose gets clicked as if it were a button - "later" in
"you can set this up later" cost that project nineteen steps. Any caller of this
module has to keep that rule: labels are controls, not words.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qmp as qmpmod


class Screen:
    """A guest's framebuffer and pointer, through one QMP socket."""

    def __init__(self, qmp_path, workdir=".", width=1280, height=800):
        os.environ.setdefault("SCREEN_W", str(width))
        os.environ.setdefault("SCREEN_H", str(height))
        # qmp.py reads the geometry at import time, so the values have to be in the
        # environment before the module is used.
        qmpmod.SCREEN_W = width
        qmpmod.SCREEN_H = height
        self.q = qmpmod.QMP(qmp_path)
        self.workdir = workdir
        self.width = width
        self.height = height

    def grab(self, stem):
        """Screendump to a PPM and return its path.

        tesseract reads P6 PPM directly, so there is no conversion step to fail.
        dockur's version converted to PNG through sys.executable, which under sudo was
        a different interpreter than the one the script ran under - the conversion
        silently produced nothing and every OCR call then read an empty path.
        """
        ppm = os.path.join(self.workdir, stem + ".ppm")
        reply = self.q.execute("screendump", filename=ppm)
        if "error" in reply:
            print("[ocr] screendump error: %s" % reply["error"], flush=True)
            return None
        import time
        time.sleep(1.5)
        if not os.path.exists(ppm):
            print("[ocr] no file at %s" % ppm, flush=True)
            return None
        return ppm

    def text(self, ppm):
        """OCR the frame into [(text, x_centre, y_centre, width, height)].

        tesseract's tsv output gives one line per word with its bounding box, which
        is what turns "the word Continue" into "click here".

        --psm 3 is the default automatic page segmentation. --psm 11 (sparse text)
        was tried first by dockur and returned nothing at all on a frame that
        demonstrably contains a list of country names - it treats isolated words as
        noise when there is no obvious paragraph.
        """
        if not ppm:
            return []
        proc = subprocess.run(
            ["tesseract", ppm, "stdout", "--psm", "3", "tsv"],
            capture_output=True, text=True,
        )
        out = proc.stdout
        if not out.strip():
            print("[ocr] tesseract produced no output; stderr: %s"
                  % (proc.stderr.strip()[:300] or "(empty)"), flush=True)
            return []
        words = []
        for line in out.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 12:
                continue
            try:
                conf = float(parts[10])
                x, y, w, h = (int(parts[6]), int(parts[7]),
                              int(parts[8]), int(parts[9]))
            except ValueError:
                continue
            text = parts[11].strip()
            if not text or conf < 40:
                continue
            words.append((text, x + w // 2, y + h // 2, w, h))
        return words

    def click(self, x, y):
        qmpmod.click(self.q, x, y)

    def key(self, qcode):
        qmpmod.key(self.q, qcode)

    def type_text(self, text):
        for ch in text:
            qmpmod.key(self.q, ch)
            import time
            time.sleep(0.1)

    def close(self):
        self.q.close()


def find_button(words, labels, primary, height=800):
    """Pick a control, preferring the lower-right corner where forward controls live.

    Three passes, in order of how much they can be trusted:

      1. a label from `labels` below 60% of the height - above that is body copy,
         which is where dockur clicked the word "System" and "See" as if they were
         list rows;
      2. anything in the button row at the primary control's x, whatever OCR made of
         the text - that project measured its forward button at (1306,810) on every
         pane while OCR read the label as "Continue", "Conique" and "Contlnue";
      3. a run of consecutive words on one line that forms a label, because OCR
         splits "Not Now" into two words and joins "Back Not Now" into one line.

    Returns ((x, y), matched_text) or (None, None).
    """
    best = None
    for text, x, y, w, h in words:
        low = text.lower().strip(" .,:;")
        if not any(low == b or low.startswith(b) for b in labels):
            continue
        if y < height * 0.60:
            continue
        score = (x, y)
        if best is None or score > best[0]:
            best = (score, (x, y), text)
    if best is not None:
        return (best[1], best[2])

    for text, x, y, w, h in words:
        # 30px, not 60: at 60 the separator glyph between two adjacent buttons also
        # matched, and clicking it did nothing.
        if y > height * 0.72 and abs(x - primary[0]) <= 30:
            return ((x, y), text)

    band = [w for w in words if w[2] > height * 0.45]
    band.sort(key=lambda w: (w[2], w[1]))

    def same_line(a, b):
        return abs(a[2] - b[2]) <= 14

    for start in range(len(band)):
        for length in (3, 2, 1):
            chunk = band[start:start + length]
            if len(chunk) != length:
                continue
            if any(not same_line(chunk[0], w) for w in chunk[1:]):
                continue
            phrase = " ".join(w[0] for w in chunk).strip(" .,:;").lower()
            if not any(phrase == b or phrase.startswith(b) for b in labels):
                continue
            xs = [w[1] for w in chunk]
            ys = [w[2] for w in chunk]
            return ((sum(xs) // len(xs), sum(ys) // len(ys)),
                    " ".join(w[0] for w in chunk))
    return (None, None)
