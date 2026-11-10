# linux/

One directory per distribution. Each one supplies only what is genuinely that
distribution's own: how to fetch its image, how to hand it its answers, and how to
prove the disk it produced starts.

```
linux/
  cachyos/    CachyOS + KDE         - Calamares
  endeavour/  EndeavourOS + KDE     - Calamares
  fedora/     Fedora + GNOME        - Anaconda
  kubuntu/    Kubuntu + KDE         - Ubiquity
  manjaro/    Manjaro + KDE         - Calamares
  mint/       Linux Mint + Cinnamon - Ubiquity
  mx/         MX Linux + Xfce       - minstall
  nixos/      NixOS + GNOME         - Calamares (the graphical ISO)
  opensuse/   openSUSE + KDE        - YaST
  popos/      Pop!_OS + COSMIC      - the Pop installer
  ubuntu/     Ubuntu + GNOME        - Ubiquity
  zorin/      Zorin + GNOME         - Ubiquity
```

EVERY DIRECTORY HERE IS A GUI BAKE: a live desktop is booted, its graphical installer is
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

THE GUI-LESS PREDECESSORS ARE GONE, AND THE NAMES TOOK THEIR PLACE. There used to be a
`mx/`, `nixos/`, `ubuntu/` and `kubuntu/` that installed by answer file or serial console, and
a parallel set of `-gui` directories beside them. The old ones were deleted with their
workflows, and the `-gui` directories were renamed to the names the old ones had - so
`linux/ubuntu/` is now the GUI bake, and there is no second directory to confuse it with.

## Every image is proved the same way

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

## What a distribution directory provides

Two files at most:

```
  install.py           the bake driver, run as python3 linux/<distro>/install.py
  steps/install.yaml   the panes it drives, in the order that installer draws them
  steps/ssh.yaml       that desktop's terminal, its escalation, its firewall
```

The driver is the small part. Everything shared lives in `lib/`:

| the caller supplies | where it comes from |
|---|---|
| fetch the ISO | `lib/fetch_iso.py`, `lib/images.py` -> the pinned tag |
| start a guest with vnc, ssh, two QMP sockets | `lib/vm.py` -> `Guest(...).start()` |
| drive the screen | `lib/qmp.py`, `lib/ocr.py`, `lib/keys.py` |
| walk a pane sequence | `lib/steps.py` -> `run_file(...)` |
| photograph the run | `lib/snapshots.py`, `lib/live.py` |
| prove the disk boots | `lib/launch_linux.py` |
| prove ssh answers | `lib/ssh_step.py` |
| encode the recording | `lib/vm.py` -> `record_many(out, *dirs)` |

## The one thing that is not shared per-distro

The coordinates, and nothing else. Every directory drives its own installer's panes, so the
YAML is where a distribution's knowledge lives - and each step records the frame its number
was measured from.

Windows, macOS, Android and BSD keep their own shapes and are not part of this file - see
`../windows/`, `../macos/`, `../android/` and `../bsd/`.

## Adding a distribution

1. `artifacts-publish.yml` fetches its installer image once and pushes it to ghcr under a
   tag. Nothing downloads from a mirror at run time - see `lib/fetch_iso.py` for why.
2. Write `linux/<distro>/steps/install.yaml`: one step per pane, each with the coordinate
   it was measured at.
3. Write `linux/<distro>/install.py`: boot the live ISO, call `steps.run_file`, then let
   `disk_settle` decide the disk is written.
4. Add the four jobs - `bake`, `launch`, `ssh`, plus `prepare` - from an existing workflow
   such as `zorin.yml`; they are the same shape for every distribution, and only the tag
   and the ISO differ.
5. Write `linux/<distro>/steps/ssh.yaml` by booting the finished disk and walking its
   desktop to a terminal yourself first. The path differs on every one.
