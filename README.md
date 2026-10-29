# easydiskbake

Bake a bootable qcow2 from any installer ISO, over QMP, with VNC and a recording of
every frame.

This is the generalised form of what three projects each solved in their own way:

| project | how it installs | why |
|---|---|---|
| **dockur/windows** | `Autounattend.xml` inside a rebuilt ISO | Windows has a complete unattended format |
| **dockur/macos** | OCR and the pointer, 1357 lines | macOS has none at all |
| **easysandbox-vm/bake** | preseed, kickstart, autoinstall, JSON, package lists | the Linux installers have one natively |

All three end the same way: a disk that boots. The difference is only ever the
installer's own configuration interface. So the shared parts belong in one place -
booting an ISO, watching it, recording it, reaching it over VNC and SSH, and deciding
whether the disk that came out is bootable - and each distribution keeps only what is
genuinely its own.

## The pipeline

```
ISO --> qemu (VNC + SSH + QMP) --> live system --> installer --> disk --> smoke
         |                          |               |             |
         |                          |               |             +- boot it, screenshot it
         |                          |               +- declarative config, or OCR if there is none
         |                          +- reached over VNC for a human, SSH for the bake
         +- QMP: screendump every frame, input, and a recording of the whole run
```

## What the image has to carry, and why

The base is `qemux/qemu:7.50`. Measured, not assumed: `qemu-system-x86_64 11.1.0`,
`qemu-img 11.1.0`, `7z 25.01`, `xorriso 1.5.6`, and `curl wget python3 jq mkfifo timeout
setsid unshare` are all present.

Everything below is a tool a requirement names.

### 1. QMP automation - already present

No package. `qemu-system-x86_64` speaks QMP, and the drivers will be Python speaking
JSON to a unix socket.

Deliberately QMP and not HMP: HMP's `mouse_move` carries relative deltas while a
`usb-tablet` guest expects absolute ones, so an HMP-driven run clicks 64 times and
leaves the pointer in the top-left corner. QMP's `input-send-event` takes `abs` axes and
explicit `down`/`up` button state, which is what an absolute pointer needs.

### 2. VNC and SSH, reachable from outside - already present

No package. `-vnc :0` is qemu. SSH from the host is
`-netdev user,id=n0,hostfwd=tcp::2222-:22`. Publishing those ports is the container
runtime's job, not the image's.

### 3. Recording the run as video - **ffmpeg**

The one requirement nothing else covers. qemu does not write video.

| approach | how | cost |
|---|---|---|
| **ffmpeg reads the VNC stream** | `ffmpeg -f vnc -i localhost:5900 out.mp4` | ~60 MB of packages |
| **capture the framebuffer on a timer** | `screendump` over QMP, then encode | no dependency for capture, but the output is frames |

The second is what `easysandbox-vm/bake` does today (`lib/snapshots.py`, every 30
seconds), and it is what finally explained several silent failures: a curses pane, a
GRUB `grub>` prompt and a black screen are identical in a serial log and obvious in a
picture.

For a video the first is required, and `ffmpeg` is the only thing that does it. It also
serves the frame path afterwards - ffmpeg can pull frames out of the recording - so one
dependency covers both.

### 4. OCR, for installers with no unattended mode - **tesseract-ocr, tesseract-ocr-eng**

Measured the hard way. A probe of MX's live desktop came back as four 537 KB PNGs - the
framebuffer worked - and reported `tesseract is not installed`. Without it the only
option is guessing coordinates, and every guess was wrong differently: `(1120,745)` was
not a button at all, and `(971,693)` was correct on one pane and 31 pixels below the
buttons on another.

`tesseract-ocr-eng` matters as much as the binary: without a language pack tesseract
runs, exits zero, and reads nothing, which looks exactly like a blank screen.

### 5. Reading a file out of a finished disk - **e2fsprogs**

The smoke test has to answer questions only the disk can: is there boot code in the MBR,
is there a root filesystem, what is in `/etc/shadow`.

The first two are 512 bytes and a superblock, and `qemu-io` reads them with no package
at all. For a path inside the filesystem, `debugfs` - from `e2fsprogs`, which the base
image already carries - reads ext4 directly.

### 6. Building a modified ISO - `genisoimage`, and `xorriso` which is present

`genisoimage` is what dockur/windows uses to drop `Autounattend.xml` at the ISO root and
rewrite the El Torito boot record. `xorriso 1.5.6` is already present and does the same
job. `7z` already extracts from an ISO without mounting it, which matters because
mounting needs privileges a container may not have.

### 7. Everything else the run needs - already present

`python3` for the drivers, `jq` for JSON, `mkfifo` for the serial console, `timeout` and
`setsid` to bound a run, `curl` for the configuration server and for ghcr. All present.

## The list

```dockerfile
FROM qemux/qemu:7.50

RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr tesseract-ocr-eng \
        e2fsprogs \
        genisoimage; \
    rm -rf /var/lib/apt/lists/*
```

| package | requirement | in the base? |
|---|---|---|
| `ffmpeg` | record the VNC stream to video | no |
| `tesseract-ocr` + `-eng` | OCR for installers with no unattended mode | no |
| `e2fsprogs` | read a file out of the finished disk (`debugfs`) | check, often present |
| `genisoimage` | rebuild an ISO with an answer file inside | no (`xorriso` is) |
| qemu, 7z, xorriso, python3, jq, curl, mkfifo, timeout | everything else | **yes** |

## Log streaming, from the first second

Every workflow wraps its work in `log-stream` with an explicit `interval`, and the
reason is worth recording because it cost this project five rounds:

* GitHub serves a running job's Actions log from an internal store, and the public
  endpoints answer 404 or 403 until the job finishes;
* a check run's `output.text` has no such restriction, and `log-stream` keeps that field
  current - but only with a short `interval`, and only if the output goes to **stdout**;
* that field is capped at 65535 characters, with 31 characters of timestamp per line, so
  a curses TUI fills it in seconds and the useful lines are pushed out.

What works, measured: short lines with content, on stdout, every ten seconds.
`[snap] frame-0007.ppm at 08:54:03` is worth more than a megabyte of redrawn screen.
