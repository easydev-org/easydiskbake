# android/

```
android/
  15/    Android 15 (API 35), AOSP - the `default` flavour, no GMS
```

## Why this is a root and not a `linux/` distribution

Android does not have an installer that writes a disk. Google ships a system image
whose contents *are* the disks - `system.img`, `vendor.img`, `userdata.img` - plus a
kernel and ramdisk that boot them. There is no partition table to create, no installer
to drive, and nothing to watch for half an hour.

So the stages line up differently, and it is worth being exact about which ones are
shared:

| stage | Windows / macOS / Linux | Android |
|---|---|---|
| fetch | an ISO from ghcr | a **system image** from ghcr, published by `android-15_aosp-publish.yml` |
| install | an installer writes a disk | **none** - the image already is the disk |
| boot | `lib/vm.py` builds a qemu argv | `lib/emulator.py` builds an emulator argv |
| drive | QMP: `lib/qmp.py`, `lib/ocr.py` | **the same** - the emulator speaks QMP |
| record | `lib/snapshots.py` over QMP | **the same** |
| prove | serial console, RDP, a greeter | `adb getprop sys.boot_completed` |
| publish | `lib/publish_disk.py` | **the same** |

Five of seven stages are the same code. The two that differ are the launcher and the
arrival check, and both differ for a reason rather than by preference.

## Why the emulator and not `qemu-system-x86_64`

The system image is built for Google's `ranchu` machine and its `goldfish` device
family - framebuffer, pipe, audio, battery. Upstream qemu does not implement them.
Booting a ranchu image on a `q35` machine stops in the kernel: no display device, no
`goldfish_pipe`, and `/system` never mounts.

The emulator *is* qemu - Google's fork, with those devices. So the alternative to the
emulator is not "plain qemu", it is "build a qemu with goldfish support", which is the
same work Google already did.

What makes this acceptable is that the emulator speaks QMP. `lib/qmp.py`,
`lib/ocr.py` and `lib/snapshots.py` drive it exactly as they drive a qemu guest, so the
driver and recording code is shared even though the launcher is not.

## What is published, and why it is an AVD rather than a qcow2

```
ghcr.io/easydev-org/easydiskbake:sysimg-android-15-aosp    the AVD input  (android-15_aosp-publish.yml)
ghcr.io/easydev-org/easydiskbake:android-15-aosp           the baked AVD  (android-15_aosp-bake.yml)
```

The runtime consumes `emulator -avd sandbox`, so the artifact is an AVD directory
tarred with zstd (`--sparse`, because the 4 GiB data partition is mostly holes). A
qcow2 of an ext4 system image is not something the emulator can boot.

Two artifacts rather than one because creating an AVD needs `cmdline-tools` and a JDK.
Doing that once in `android-15_aosp-publish.yml` means the bake image carries neither - it
carries only the emulator and adb, and pulls a finished AVD.

## The system image, and what it is pinned to

| | |
|---|---|
| package | `system-images;android-35;default;x86_64` |
| API | 35 (Android 15) |
| flavour | `default` - AOSP, no Google services |
| url | `dl.google.com/android/repository/sys-img/android/x86_64-35_r02.zip` |
| sha1 | `2d857d170c0d1b827149565da34b3383e5306f7f` (Google's own, from `sys-img2-3.xml`) |

The `gms` flavour (`google_apis`) is a different package and is not published here.
Adding it is one entry in `lib/images.py`, not a change to any workflow.

## The sandbox shape

The AVD is configured as a 1024x768 landscape desktop with a hardware mouse, matching
the Windows, macOS and Linux sandboxes so a viewer sees the same shape whichever
platform they are driving. Those entries are written explicitly in `lib/avd.py`
because the `pixel_6` device profile is a phone in portrait, and the emulator reads
some of them from the config in preference to the command line.

## What is not here yet

| | why |
|---|---|
| **GMS / Play** | a different system image; one entry in `lib/images.py` when it is wanted |
| **input** | the QMP path supports it, but nothing drives the guest yet - a sandbox is view-and-adb |
| **a persistent /data** | `-wipe-data` is unconditional; a bake must start cold and a snapshot would hide a broken image |
