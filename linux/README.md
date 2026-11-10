# linux/

One directory per distribution. Each one supplies only what is genuinely that
distribution's own: how to fetch its image, how to hand it its answers, and how to
prove the disk it produced starts.

```
linux/
  cachyos-gui/    CachyOS + KDE         - Calamares
  endeavour-gui/  EndeavourOS + KDE     - Calamares
  fedora-gui/     Fedora + GNOME        - Anaconda
  kubuntu-gui/    Kubuntu + KDE         - Ubiquity
  manjaro-gui/    Manjaro + KDE         - Calamares
  mint-gui/       Linux Mint + Cinnamon - Ubiquity
  mx-gui/         MX Linux + Xfce       - minstall
  nixos-gui/      NixOS + GNOME         - Calamares (the graphical ISO)
  opensuse-gui/   openSUSE + KDE        - YaST
  popos-gui/      Pop!_OS + COSMIC      - the Pop installer
  ubuntu-gui/     Ubuntu + GNOME        - Ubiquity
  zorin-gui/      Zorin + GNOME         - Ubiquity
```

EVERY DIRECTORY HERE IS A `-gui` BAKE: a live desktop is booted, its graphical installer is
driven, and the disk it wrote is then started and checked. There are no unattended
answer-file bakes left.

## How an installer is answered

| how | which | why |
|---|---|---|
| a GUI driver, by measured coordinates | every distribution above | the installer has a wizard, and clicking it is the only way through |

Nothing is typed at a serial console and nothing reads an answer file. That was the old
shape: a separate `nixos/` wrote `nixos-install` commands to a serial console, and the
preseed / kickstart / autoinstall / autoyast route drove an unattended document. Both are
gone with their workflows, because the point of these images is the desktop a GUI install
produces - and a text-console install cannot produce one.

The GUI-less predecessors of four of the directories above - `mx/`, `nixos/`, `ubuntu/` and
`kubuntu/` - are still on disk. Their workflows are deleted, so nothing runs them; they are
kept as the record of how each installer behaves when it is not clicked, and because
`lib/images.py` and their own docstrings still describe them.

## Every -gui image is proved the same way

```
bake.py     boot the live ISO -> drive the installer's panes -> the disk holds still
launch.py   boot that disk alone, UEFI, and photograph the desktop
ssh.yaml    open the session's own terminal and install sshd, then answer on 2222
```

The third stage is per distribution and is deliberately not one shared file: the terminal is
reached differently on every desktop (GNOME has no ctrl+alt+t, MATE's menu ignores the
mouse), the privilege escalation differs (`sudo -S` on most, `su` rules it out on FreeBSD,
`nixos-rebuild` on NixOS), and the firewall differs (ufw is installed on some images and
drops 22). Each `steps/ssh.yaml` records the frames its numbers were measured from.

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

The coordinates, and nothing else. Every `-gui` directory drives its own installer's panes,
so each one carries its own `steps/install.yaml`:
```
  steps/install.yaml   the panes, in the order that installer draws them
  steps/ssh.yaml       that desktop's terminal, its escalation, its firewall
```

Windows and macOS keep their own shapes and are not part of this file - see `../windows/`
and `../macos/`.

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

1. `artifacts-publish.yml` fetches its installer image once and pushes it to ghcr under a
   tag. Nothing downloads from a mirror at run time - see `lib/iso.py` for why.
2. Create `linux/<distro>/bake.sh`. If the installer takes an answer file, it is
   roughly: fetch ISO, start guest, serve the answer file, wait for the disk, smoke
   test. No OCR, no coordinates.
3. Only if there is no answer file, write a driver and put its measured coordinates
   in it with the frame each one came from.
4. The last line of every bake is the same and is the only one that proves anything:
   boot the disk with no ISO attached and watch it arrive.
