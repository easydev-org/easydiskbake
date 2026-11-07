# bsd/

Two BSDs, and they are installed in completely different ways.

| directory | installer | driven by |
|---|---|---|
| `freebsd/` | `bsdinstall` | an installerconfig script on the media, no screen read |
| `ghostbsd/` | `gbi` (GTK) | measured coordinates, like the Linux GUI installers |

## GhostBSD, and the loader variable it needs

`bsd/ghostbsd/install.py` drives gbi the same way `linux/ubuntu-gui` drives Ubuntu's
installer: a fixed sequence of positions, with OCR only writing to the log. It needs one
thing the Linux guests never did, and the reason is worth knowing before anyone "fixes"
`lib/qmp.py` over it.

QEMU's `usb-tablet` is an absolute-positioning HID device. On GhostBSD it does nothing:

    ugen0.2: <QEMU Tablet Adomax Technology Co., Ltd> at usbus0
    uhid0:   <QEMU QEMU USB Tablet, class 0/0, rev 2.00/2.00, addr 1> on usbus0

The tablet *is* attached - to `uhid(4)`. It reports HID subclass 0 / protocol 0, the
non-boot protocol; `ums(4)` accepts only subclass 1 / protocol 2 and declines it, so `uhid`
takes the raw reports and never writes a position into the input layer. The pointer stays
at the top-left corner no matter what the host sends.

`hw.usb.usbhid.enable=1` fixes it: `usbhid` + `hms` then claim the tablet at enumeration and
absolute coordinates work. **It has to be set in the loader.** Setting it from a root shell
succeeds and changes nothing, because the sysctl is read at enumeration and the device is
already open by `uhid`; `usbconfig -d ugen0.2 reset` does not rebind it either. The loader
runs before enumeration, and - verified - QMP key events reach it.

Relative motion is not an acceptable substitute. `qmp.click_relative()` walks a PS/2 mouse
out of a corner and does move the cursor, but X applies its own acceleration curve to
relative deltas, so the error accumulates and a 30-pixel icon is missed.

## Why it is a separate root

BSD is closer to Linux than Android is - it has an installer and produces a disk - but
three of the shared assumptions in `lib/` break:

| assumption | Linux | BSD |
|---|---|---|
| serial console | `console=ttyS0` on the kernel line | set in the loader (`/boot/loader.conf` after install, or the boot menu before) |
| reading the disk afterwards | `debugfs` reads ext4 | needs `ufs` tools; `debugfs` is the wrong binary |
| proving arrival | `login:`, `lightdm`, a greeter | `login:` too, but the console is `ttyu0`/`ttyv0`, and there is no systemd to look for |

`lib/smoke.py` checks for an ext superblock at 1 MiB unless told otherwise. That
call is already marked non-fatal for a non-ext root, so a BSD disk passes the MBR check
and skips the filesystem one - which is honest, but it means the disk-level half of the
smoke test does nothing for BSD. The boot half works unchanged.

## Which BSD, and which installer

These do not share an installer:

| system | installer | unattended? |
|---|---|---|
| FreeBSD | `bsdinstall` | scripted via `bsdinstall script` - what `freebsd/` does |
| GhostBSD | `gbi` (GTK) | no answer file; driven by position - what `ghostbsd/` does |

FreeBSD's scripted install is the closest to the Linux answer-file model, so that is the
one to reach for when a BSD has to install with no screen at all. GhostBSD's `gbi` has no
scripted mode on the interactive path, which is why it is a coordinate driver instead.

There is no graphical FreeBSD installer to drive: `bsdinstall` draws `dialog(1)` menus on a
text console, and `bsd/freebsd/installerconfig` already calls the same path as the wizard
with the prompts skipped.

## What a bake would produce

A qcow2 of an installed BSD, proven by a serial login prompt. The Linux `bake.sh`
sequence (fetch -> boot -> install -> smoke -> record -> compress) applies with the
installer step and the arrival pattern swapped, which is the same shape as every other
platform here.
