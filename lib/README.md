# lib/

The parts that are the same for every distribution.

| file | what | why it is not per-distribution |
|---|---|---|
| `qmp.py` | QMP client: absolute pointer, key events, screendump | every guest is driven the same way |
| `keys.py` | characters to QKeyCodes | QMP has no "send a string" |
| `ocr.py` | screendump -> tesseract -> click a control | needed by any installer with no unattended mode |
| `snapshots.py` | one frame every N seconds, for the whole run | watching a run is not installer-specific |
| `iso.sh` | fetch an ISO from ghcr | every bake starts with an image |
| `mkpasswd.py` | SHA-512 crypt, for installers that take a hash | the format is fixed and published |

Nothing here knows what distribution it is working on. That is the point: the
differences between installers live in their own configuration files, and the
differences in *how they are reached* live in the per-distribution driver.

## Where this came from

These files were written and debugged against nine distributions in
`easydev-org/easysandbox-vm`. What follows is what each one cost, because a comment
that says why a line exists is cheaper than rediscovering it.

**`qmp.py`** - HMP's `mouse_move` carries relative deltas and a `usb-tablet` guest
expects absolute ones. An HMP run clicked 64 times and left the pointer in the
top-left corner; every frame it captured was byte-identical. QMP's `input-send-event`
takes `abs` axes and explicit button state, which is what an absolute device needs.

**`keys.py`** - QMP takes a QKeyCode, not a character, so `"docker"` becomes six key
presses and `"A"` becomes `shift-a`. A character with no mapping is *reported*, not
dropped: a silently missing letter produces an account with the wrong name, and that
failure surfaces much later as "the password does not work".

**`ocr.py`** - three passes for finding a control, in order of how much each can be
trusted: a label from a list of control-words below 60% of the height; then anything in
the button row at the primary control's x, whatever OCR made of the text; then a run of
consecutive words forming a label, because OCR splits "Not Now" into two words and joins
"Back Not Now" onto one line.

The list of control-words matters: a word that also appears in body copy gets clicked as
if it were a button. "later" in "you can set this up later" cost one project nineteen
steps.

**`snapshots.py`** - a curses pane, a GRUB `grub>` prompt and a black screen are
indistinguishable in a serial log and obvious in a picture. This is what finally showed
that an installer's button row is invisible to tesseract: the diagnostic printed every
word OCR found with its position, and the lowest one was 64 pixels above the buttons.

**`iso.sh`** - no fallback to a mirror, deliberately. A function that quietly falls back
is a function whose failures are invisible: the run succeeds, slowly, from a host nobody
chose, and nobody notices the cache is broken.
