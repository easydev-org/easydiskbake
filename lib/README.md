# lib/

The parts that are the same for every distribution and every platform.

| file | what | why it is not per-distribution |
|---|---|---|
| `qmp.py` | QMP client: absolute pointer, key events, screendump | every guest is driven the same way |
| `keys.py` | characters to QKeyCodes | QMP has no "send a string" |
| `ocr.py` | screendump -> tesseract -> click a control | needed by any installer with no unattended mode |
| `panefp.py` | a pane fingerprint: identify a screen without OCR | any installer can be learned this way |
| `snapshots.py` | one frame every N seconds, for the whole run | watching a run is not installer-specific |
| `iso.py` | fetch an ISO from ghcr, or a pinned vendor URL | every bake starts with an image |
| `vm.py` | the qemu command line, as a pure function, plus a `Guest` | every guest is a qemu with the same four channels |
| `smoke.py` | is the disk bootable; boot it and watch | proving a disk works is the same for all of them |
| `winiso.py` | put an answer file at an ISO's root, keeping El Torito | Windows-only in practice, but it is an ISO operation |
| `wim.py` | read a Windows WIM's edition table | verifies the image index before boot |
| `mkpasswd.py` | SHA-512 crypt, for installers that take a hash | the format is fixed and published |
| `hexdump.py` | parse a `qemu-io read -v` dump | used by `smoke.py` |

Nothing here knows what distribution it is working on. That is the point: the
differences between installers live in their own configuration files, and the
differences in *how they are reached* live in the per-distribution driver.

## Everything here is a module, not a sourced script

The shell versions could not be tested. `bake_vm_start()` built an argv and forked
qemu in the same function, so the only way to find out what command line it produced
was to run a VM - and on a machine without `/dev/kvm` that was impossible.

`vm.build_args()` is now pure: no process, no filesystem writes, no environment reads
beyond an explicit `Shape`. That is what makes `tests/` possible, and the tests are
the reason the UEFI parameters could be added without being able to boot a Windows
guest on the machine they were written on.

Two functions in was-shell files were rewritten rather than ported, because the shell
version's failure mode was the reason the file existed:

- `smoke.py` read a disk by shelling out to `qemu-io`, capturing a hexdump to a temp
  file, shelling out to `hexdump.py` to parse it, and grepping the result for
  `"55aa"`. Four processes for a two-byte comparison, and the offset was wrong for
  three runs.
- `winiso.py` built the path `$OEM$/$$/Setup/Scripts/` with two escaped dollars inside
  a double-quoted string inside a function. In Python it is a literal.

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

**`iso.py`** - no fallback to a mirror, deliberately. A function that quietly falls back
is a function whose failures are invisible: the run succeeds, slowly, from a host nobody
chose, and nobody notices the cache is broken.

**`vm.py`** - the firmware parameters (`machine`, `firmware_code`, `firmware_vars`,
`tpm_socket`) are not Windows- or macOS-specific enough to live in those directories.
A guest that needs UEFI needs the same three things done the same way, and the defaults
are exactly what MX used before this existed, so nothing that worked changed.

**`smoke.py`** - the ext superblock check is opt-in per caller (`root_fs="gpt"` for
Windows and macOS) because those disks have no ext magic at 1 MiB and the check only
ever produced false alarms there.

**`wim.py`** - exists because of a specific mistake that is otherwise invisible: an
answer file's image index off by one installs "Windows 11 Pro N" instead of "Windows 11
Pro", and the two are identical on screen. Reading the WIM catches it before a guest
exists.
