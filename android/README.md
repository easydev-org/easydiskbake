# android/

```
android/
  blissos16/    BlissOS 16.9.7 (Android 13, x86_64) - Android-x86, installed onto a GPT disk
```

## Two stages, four files, no program

```
blissos16/
  fetch_iso.py             download the pinned ISO, verify its sha256, unpack the three members
  steps/install.yaml       make the disk, boot the installer, answer ten panes, place Grub2,
                           read the ESP back, publish
  steps/launch.yaml        boot the installed disk with no -kernel and no cdrom, wait for
                           Android's own UI, photograph it
  steps/bliss_esp.py       the two `python:` extensions: edit the ESP's FAT, read it back
```

`./easyvm android/blissos16/steps/install.yaml` and `.../launch.yaml` are the whole stage. There
is no `install.py` and no `launch.py` - what used to be in them is either a step (`disk:` with
`disk_gb:`, `boot:` with `os: android`, `compress:`) or a `python:` extension for the one act that
is not an action on a screen: editing a FAT filesystem at a byte offset inside a qcow2.

## Why 16.9.7, and why it is Android 13 rather than 15

It is the newest publicly published BlissOS image. The project's own pages say "Public images are
paused" with no ETA; the BlissOS17 (Android 14) and Voyager/18 (Android 15) directories on
SourceForge exist and contain no ISO; `BlissOS/stable_releases` v17.8.3 pins `android-14.0.0_r53`;
and Android 15 exists only as source in `platform_manifest`'s `voyager-qpr2-x86` tree, which has to
be **built** - 350 GB and hours, not a download.

## Why this is a root and not a `linux/` distribution

Android-x86 is *almost* a PC, and the parts where it is not are the whole of this directory:

| stage | Windows / macOS / Linux | Android-x86 |
|---|---|---|
| fetch | an ISO from ghcr | an ISO from SourceForge, pinned by sha256 (`lib/images.py`) |
| install | an installer writes a disk | **the same** - `boot: os: android` with an ISO, then ten panes |
| boot an installed disk | firmware, ESP, bootloader | **the same**, and this is the point of the launch stage |
| drive | QMP: `lib/qmp.py`, `lib/ocr.py` | **the same** |
| record | `lib/snapshots.py` over QMP | **the same** |
| prove | ssh, RDP, a greeter | `desktop: android` - a frame, read by `lib/desktop.py` |
| publish | `lib/compress.py` | **the same** |

The differences are all facts the file states rather than a second pipeline:

    -vga std FOR THE INSTALLER, virtio-vga FOR THE INSTALLED SYSTEM
        The installer's TUI draws on a plain framebuffer. The installed system runs Android's
        HWC with `HWC=drm_minigbm`, which needs a DRM/KMS node; `-vga std` gives a framebuffer
        and no DRM device, and the screen then stops on initrd's last line for as long as you
        wait - measured at 240 s, 600 s and 600 s again before the cause was found.

    NO console=ttyS0 ON THE INSTALLER
        With it the TUI moves to the serial line and the keys the walkthrough sends go to a
        console nobody is drawing on. Measured, four runs.

    THE BOOTLOADER HAS TO BE AT /EFI/BOOT
        `install_grub_efi` writes /EFI/BLISSOS and registers its entry with `efibootmgr`, which
        lands in the VARS image and not on the disk. A disk started with any other NVRAM falls
        into the firmware's own shell. `lib/esp.py` copies the loaders to the removable-media
        path and replaces the config's `search.fs_uuid` - a UUID the installer recorded BEFORE
        it formatted the partition, so it names a filesystem that does not exist.

All three live in `lib/vm_session.py`'s android branch and in `steps/install.yaml`, beside the
numbers they were measured from.

## Why there is no emulator route any more

There was: `android/15` booted Google's AOSP system image through the SDK's `emulator` binary,
with two workflows (`android.yml`, `android-qemu.yml`), two images (`android-builder`,
`android-qemu`) and five lib modules. It is gone, and the four facts that made it expensive are
why BlissOS replaced it rather than joined it:

    kernel-ranchu is not a bzImage     measured: 497 bytes of 0xff, HdrS at 514, no 0xAA55
    -system publishes no GPT uevents   measured: first-stage init waits in /sys forever, so the
                                       disks had to be split into three and kept paired
    /data is encrypted, on nosuid      measured: dm-42 ext4 (rw,seclabel,nosuid,...)
    su accepts root and shell only     measured: an app uid gets "not allowed"

BlissOS has none of them - a real bzImage, an initrd that assembles the root itself, and a plain
ext4 for /data - and it needs what a PC needs, which is a partition table, an ESP and a
bootloader. That is a disk this repository installs and then boots, which is the one claim the
emulator route could never make: it never had a disk of its own to start.
