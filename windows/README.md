# windows/

```
windows/
  11/    Windows 11 (also covers 10, Server, and the legacy 9x/NT5 family)
```

One directory, not one per version, because Windows is the one platform where the
installer interface is genuinely declarative. `Autounattend.xml` covers Windows 2000
through Server 2025 in the same format, and `dockur/windows` already walks all of them
with one mechanism.

## Why this platform needs no driver

The whole reason `linux/mx/` has five Python files is that minstall has no working
unattended mode. Windows has the opposite: a complete unattended format consumed by
setup itself, in three passes.

| pass | what is set there |
|---|---|
| `windowsPE` | language, disk configuration, image index |
| `specialize` | computer name, timezone, RDP and its firewall rule |
| `oobeSystem` | the account, autologon, OOBE skips |

`answer/Autounattend.xml` is the entire guest-side contract. Nothing else runs inside
the guest - no `SetupComplete.cmd`, no `$OEM$` tree.

## How the answer file reaches setup

**A second CD, not a remastered install ISO.**

Microsoft's documented answer-file search order puts removable read-only media at the
root of the drive as item 5, and requires only that the file be named
`Autounattend.xml`:

> | 5 | Removable read-only media in order of drive letter, at the root of the drive. |
> The name of the answer file must be Autounattend.xml, and must be located at the
> root of the drive. |

So the bake attaches two CDs: the untouched Windows ISO as the boot device, and a
few-hundred-kilobyte ISO whose root holds the answer file. `lib/winiso.py` builds the
second one.

The alternative - dockur's approach - is to put the answer file inside the install
ISO. Its own source shows what that costs, because it does not actually rewrite the
install ISO either: `install.sh` keeps the original (`useOriginalImage`) and puts the
automation on a separate `setup.img`. The comment there says it plainly:

> Bootable ISOs can be reused unchanged with the generated setup image.

An earlier version of this bake tried the full remaster anyway, with
`xorriso -indev ... -outdev ... -map <one file>`, and produced a **448 KB** ISO: a
boot record and the answer file, with none of the installer's files. qemu started it,
found nothing to boot, and the bake sat at the arrival check until it was cancelled.
That is the failure the second-CD design cannot have.

## Why there is no script in the guest

Two things used to justify a staged script, and both are cheaper to check from the
host - earlier, and without the guest's cooperation.

| question | asking the guest | asking the host |
|---|---|---|
| which edition was installed? | read the caption after setup | `lib/wim.py` reads `install.wim`'s index metadata **before boot** |
| did setup finish? | `BAKE_END` marker on COM1 | port 3389 opens only after OOBE completes |

The first is strictly better on the host: an index off by one installs "Windows 11
Pro N" instead of "Windows 11 Pro", which is invisible on screen and only readable
from the registry. Reading the WIM catches it before a VM exists at all.

The second is also better on the host, and it closes the failure a script cannot
catch: if one pass of the answer file is malformed, setup **does not fail** - it
silently continues interactively, and `SetupComplete.cmd` never runs. A screenshot
then shows a desktop either way. Port 3389 does not open until OOBE has finished, so
it distinguishes the two.

`lib/smoke.py`'s docstring records the same lesson for a different check: reading the
disk directly beats inferring from a log.

## What has to come from the platform, not from lib/

| requirement | why | where it lands |
|---|---|---|
| **UEFI (OVMF) + pflash vars** | Windows 11 refuses to install on legacy BIOS | `lib/vm.py` `Shape.machine` / `firmware_code` / `firmware_vars` |
| **TPM 2.0** (`swtpm`) | Windows 11 hard requirement | `Shape.tpm_socket`, started by `bake.py` because its state must outlive a boot |
| **a disk setup can see** | setup has no virtio driver until the guest tools are in | `virtio-win` as a second CD |
| **an ISO that carries the answer file** | setup reads `\Autounattend.xml` from the boot medium only | `lib/winiso.py` |

The first three are a machine, not a driver: `lib/vm.py` takes them as parameters so
Windows and macOS share one implementation.

## The pipeline

```
ISO (pinned URL + sha256)
  -> verify the WIM index names the expected edition        lib/wim.py
  -> rebuild the ISO with Autounattend.xml at its root      lib/winiso.py
  -> qemu: UEFI + pflash vars + TPM 2.0                     lib/vm.py
  -> setup runs unattended, reboots itself twice
  -> port 3389 opens  ==  the guest is a running Windows    bake.py::wait_for_arrival
  -> boot the disk again with no ISO attached               lib/smoke.py
  -> compress
```

There is no second boot to fix a bootloader (setup writes its own) and no GUI driver.

## Where the existing fork stands

`dockur-windows` is a **byte-for-byte unmodified fork** of `dockur/windows`: same 12
files in `src/`, same `pushed_at`, same commit. It was never adapted - it is the
reference for what the unattended path looks like, not a source of changes to carry
over.

What was taken from it:

- the pinned Microsoft URL + sha256 per version (`src/define.sh::getMido`)
- the answer-file structure (three passes, OOBE skips, `SkipAutoActivation`)
- the arrival signal (TCP 3389, `install.yml::check_rdp_port`)

What was deliberately not taken:

- the 1592-line workflow and its PowerShell callback validation
- `$OEM$` staging of guest scripts
- the samba share, the Z: drive, the OEM wallpaper

## What is still missing

| | why it is not here |
|---|---|
| **ssh tag** | It must be derived from an already-published base, the way `dockur-macos` does its stage 3. Doing it in the install path means one broken step invalidates a finished disk. Not implemented. |
| **legacy Windows (7 and older)** | They need `winnt.sif` instead of XML, a different El Torito layout, and no UEFI. The directory is `11/` for a reason. |
| **a serial console in the guest** | Windows has none by default. Enabling it would make the arrival check cheaper still, but the port check works and needs nothing in the guest. |

## Directory contents

```
windows/11/
  bake.py            fetch -> verify -> rebuild -> boot -> wait -> smoke -> compress
  answer/
    Autounattend.xml the whole guest-side contract
```
