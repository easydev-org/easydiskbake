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

## macOS: 15 is the supported release, 26 is not

**macOS 15 (Sequoia) is the only macOS release this project bakes end to end** - setup,
desktop, ssh and dev all pass, and the published disk boots on its own to a login
screen that accepts `docker` / `admin`.

**macOS 26 (Tahoe) is known to be broken and is kept as a two-stage experiment**
(`setup` → `desktop`, see `.github/workflows/macos26.yml`):

| what | result |
|---|---|
| the installer and the wizard's panes | work; the disk is installed and the wizard can be driven |
| the wizard finishing | **never**. Its "Update Mac Automatically" pane greys Continue and Back and spins forever; `system_reset` is the only exit and it leaves the wizard state unwritten |
| `system_powerdown` | **ignored**; measured at 60 s and 150 s with no response |
| seeing a 26 desktop | **only by restarting inside the setup run** - the wizard has to be driven forward in the same boot |
| booting the baked disk later | **hangs**, or lands back on the wizard. A macOS 26 disk baked by CI does not survive on its own |

So `macos26.yml` stops at the desktop and has no ssh or dev stage: those would publish a
disk that cannot boot. `/var/db/.AppleSetupDone` and every `DidSee*` key being true does
not help - the wizard still reappears after a clean shutdown. Use `macos15.yml` for
anything that needs a macOS disk.

### Running one: the macOS appliance image

`image-build.yml` with `only: appliance` builds `ghcr.io/easydev-org/easydiskbake:macos15-appliance`,
a container that **is** a macOS 15 machine - the baked `macos15-ssh` disk, the recovery
image and the OpenCore assets are all inside it, so its first start never touches the
network:

```sh
docker run --rm -it \
    --device /dev/kvm --device /dev/net/tun --cap-add NET_ADMIN \
    -p 6080:6080 -p 2222:2222 -p 5900:5900 \
    -v macos15-data:/data \
    ghcr.io/easydev-org/easydiskbake:macos15-appliance
```

```
http://localhost:6080/vnc.html      the guest's screen
ssh -p 2222 docker@localhost        password: admin
```

The volume holds the joined base disk, a qcow2 overlay backing onto it, and the OpenCore
work tree; qemu boots the overlay, so the base stays byte-identical to what the image
shipped. Delete the volume and the next start is a factory boot.

Network is the same single mode every bake here uses - a tap on a bridge with dnsmasq,
MASQUERADE and DNAT (`lib/net.py`), never qemu's `-netdev user`. KVM is required; there is
no software-emulation fallback, for the reason `lib/vm.py` gives. qemu is PID 1.

It is not built by `only: both`: it is 16 GiB, takes about a quarter of an hour, and only
someone who wants to run macOS pulls it.

## Layout

```
lib/        the shared half: qmp, keys, ocr, panefp, snapshots, iso, vm, smoke
linux/      one directory per distribution; mx/ is the worked example
macos/      14/, 15/, 26/ - one per release, because the pane sequence differs;
            15 is the only one with a complete pipeline, see above
windows/    11/ - one directory for every Windows, because one XML covers them all
android/    placeholder: an emulator image is not an installer, see its README
bsd/        placeholder: same shape as linux, three assumptions in lib/ differ
```

A platform directory supplies the installer step and the "did it arrive" pattern. It
does not supply the VM, the recording, the disk checks or the compression - those are
`lib/`, and they are the same for every platform.

See `linux/README.md`, `macos/README.md`, `windows/README.md`, `android/README.md` and
`bsd/README.md` for what each one has to provide and what is still missing in it.

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
