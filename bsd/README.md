# bsd/

Placeholder. Nothing baked here yet.

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

## Which BSD, if any

Worth deciding before writing anything, because they do not share an installer:

| system | installer | unattended? |
|---|---|---|
| FreeBSD | `bsdinstall` | scripted via `bsdinstall script` |
| OpenBSD | its own | `install.conf` (autoinstall, since 5.x) |
| NetBSD | `sysinst` | limited |

FreeBSD's scripted install is the closest to the Linux answer-file model and would be
the natural first one. OpenBSD's `install.conf` is arguably simpler still.

## What a bake would produce

A qcow2 of an installed BSD, proven by a serial login prompt. The Linux `bake.sh`
sequence (fetch -> boot -> install -> smoke -> record -> compress) applies with the
installer step and the arrival pattern swapped, which is the same shape as every other
platform here.
