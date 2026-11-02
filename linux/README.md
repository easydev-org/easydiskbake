# linux/

One directory per distribution. Each one supplies only what is genuinely that
distribution's own: how to fetch its image, how to hand it its answers, and how to
prove the disk it produced starts.

```
linux/
  mx/          MX Linux 25.3 (Xfce) - GUI driven, no working unattended mode
  debian/      (not yet) preseed
  ubuntu/      (not yet) autoinstall (subiquity)
  fedora/      (not yet) kickstart
  arch/        (not yet) pacstrap
  ...
```

## What a distribution directory has to provide

A `bake.sh` that is callable as `bash linux/<distro>/bake.sh` with `OUT` pointing at
the qcow2 to produce, plus whatever drivers that distribution's installer needs. It
sources `lib/` and nothing else from the repository - the shared half is the same
for every Linux.

| the caller supplies | where it comes from |
|---|---|
| fetch the ISO | `lib/iso.py` -> `fetch_iso(name, dest)` |
| start a guest with vnc, ssh, two QMP sockets | `lib/vm.py` -> `Guest(...).start()` |
| drive the screen | `lib/qmp.py`, `lib/ocr.py`, `lib/keys.py` |
| photograph the run | `lib/snapshots.py` |
| prove the disk boots | `lib/smoke.py` |
| encode the recording | `lib/vm.py` -> `record_many(out, *dirs)` |

## The one thing that is not shared per-distro

Whether the installer takes an answer file at all. That decision is not a
configuration value - it is the reason a distribution needs its own driver:

| installer | how it is answered | driver needed |
|---|---|---|
| preseed, kickstart, autoinstall, autoyast | a file on a second ISO or on the kernel command line | none |
| Windows (see `../windows/`) | `Autounattend.xml` in a rebuilt ISO | none |
| macOS (see `../macos/`) | nothing exists; Setup Assistant is driven | OCR then coordinates |
| MX | the GUI, because every unattended mode stops | `mx/drive-mx.py` |

So a distribution gets a driver only when it has no unattended format, and the
driver is the smallest part of the directory.

## mx/ - the worked example

Five files, and the comments record what each one cost:

| file | what it does | why it is not in lib/ |
|---|---|---|
| `bake.sh` | the sequence: ISO -> boot -> drive -> fix bootloader -> boot again | the second boot is MX's, because minstall installs no bootloader |
| `drive-mx.py` | walks the GUI by measured coordinates, checks each step with one OCR read of a title strip | the coordinates are this installer's |
| `fix_grub.py` | boots the live ISO again and runs `grub-install` in a chroot | only needed because minstall leaves a zeroed MBR |
| `learn.py` | offline: learns pane fingerprints from a real run's frames | per-installer by definition |
| `verify.py` | checks a learned table against frames it was not built from | the same |

`minstall.conf` is kept even though it does not work: it documents the three
unattended forms that were measured and rejected, which is the reason the driver
exists at all.

## Adding a distribution

1. `publish-iso.yml` fetches its installer image once and pushes it to ghcr under a
   tag. Nothing downloads from a mirror at run time - see `lib/iso.py` for why.
2. Create `linux/<distro>/bake.sh`. If the installer takes an answer file, it is
   roughly: fetch ISO, start guest, serve the answer file, wait for the disk, smoke
   test. No OCR, no coordinates.
3. Only if there is no answer file, write a driver and put its measured coordinates
   in it with the frame each one came from.
4. The last line of every bake is the same and is the only one that proves anything:
   boot the disk with no ISO attached and watch it arrive.
