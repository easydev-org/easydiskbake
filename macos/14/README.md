# macos/14 - Sonoma

Empty until `../15/` proves the pipeline.

## What is expected to differ from 15

Nothing, in the mechanism. 14 and 15 are both walkable by OCR (`drive-ocr.py` works on
both), which is the property that makes them the safe pair to generalise from: if the
pipeline works on 15 it should work here with a different recovery image and the same
setup steps.

## The one thing to check first

`drive-ocr.py` reads a `Continue` at (1306, 810) and says every pane uses that
lower-right placement. That was measured on one release. Before assuming it holds on
14, confirm it on a captured frame - a 31-pixel offset like the one MX's account pane
had is enough to make every click a miss, and it is invisible in a log.
