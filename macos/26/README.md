# macos/26 - Tahoe

Empty until `../15/` proves the pipeline. When it is filled, this is what is
different from 15 and it is worth writing down before it is attempted:

## OCR does not work here

`drive-ocr.py` finds a label and clicks it. On 26 the privacy pane's Continue leads
into a pairing handshake a container cannot complete, and the only escape is a
`Not Now` that is replaced by the failure alert in about two seconds - shorter than a
screencap plus a tesseract pass. So a driver that decides by reading is not viable at
any speed.

## The country list does not take clicks

It needs keyboard navigation and a Tab-and-Space commit. Discovered by reading frames,
not from documentation.

## What this means for the port

Both of those are arguments for the marker rather than the driver: if
`/private/var/db/.AppleSetupDone` is written before first boot, neither pane is ever
reached. The per-release directory exists because the *installer* differs between
releases, not because the Setup Assistant walk should be re-derived per release.

If the marker turns out not to be sufficient on 26, that is the finding that would
justify a `drive-26.py`, and the first thing to measure is how long the Not Now button
survives - because that number decides whether any screenshot-then-decide loop can
work at all.
