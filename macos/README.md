# macos/

```
macos/
  14/    Sonoma
  15/    Sequoia
  26/    Tahoe
```

One directory per release. The reason they are separate at all is the one recorded in
`dockur-macos/scripts/drive-coords.py`:

> None of that variation is in the pane's GEOMETRY. The wizard is a fixed 1920x1080
> window that draws its controls in the same places every boot; only the labels and
> the stack of panes differ between releases.

So the release number is not a configuration value for a shared driver - it selects a
different pane sequence. 14 and 15 can still be walked by OCR. 26 cannot, and the
reason is worth keeping:

> the privacy pane's Continue leads into a pairing handshake that a container can never
> complete, and the only way past it is a Not Now that lives for about two seconds
> before the failure alert replaces it - far too short for a screenshot, a tesseract
> pass and a decision

## What has to come from the platform, not from lib/

Three things `lib/vm.py` did not do before the firmware parameters were added, because MX needs none of them:

| requirement | why | where it lands |
|---|---|---|
| **UEFI (OVMF)** | macOS will not boot from SeaBIOS; OpenCore is an EFI payload | `lib/vm.py` `Shape` takes them as parameters |
| **a per-release OVMF_VARS** | the GOP resolution is baked into the vars template (`OVMF_VARS-1920x1080.fd`), and a disk baked at one resolution does not boot at another | `macos/<release>/firmware/` |
| **OpenCore** | the bootloader macOS is installed by; carries the machine identity, the SMC key and the NVRAM | `macos/<release>/opencore/` |

The SMC key and machine identity are generated, not stored: `dockur-macos` pipes an
obfuscated literal through `tr` at boot rather than keeping the key in the file.

## The pipeline, which is the same shape as linux/

```
recovery image -> qemu (UEFI + OpenCore) -> macOS Recovery -> startosinstall
                -> disk -> Setup Assistant driven -> desktop -> golden
```

Two steps are genuinely different from a Linux bake and both are already solved in
`dockur-macos`:

1. **The installer is not an ISO.** It is a recovery image fetched from
   `osrecovery.apple.com` with a board identifier and a hex nonce. The response hands
   back a CDN URL with an expiring token. `src/install.sh` records each failure mode
   (incomplete download, wrong expected size, session cookie needed before the real
   request).
2. **There is no answer file at all.** Two mechanisms exist and only the second is
   reliable - this is the finding worth carrying over:

   | mechanism | verdict |
   |---|---|
   | drive Setup Assistant by OCR | works on 14 and 15; on 26 the window of opportunity is ~2s |
   | drive Setup Assistant by measured coordinates | what `drive-coords.py` does |
   | write `/private/var/db/.AppleSetupDone` | what `repair-setup-assistant.yml` ended on - "the marker Setup Assistant reads is unambiguous" |

   Prefer the marker. It needs no driver, no coordinates and no OCR, and it survives a
   release bump, which is exactly what a per-release directory was going to be for.

## The 18 GiB problem, which is not a bake problem but decides the output

`easysandbox-macos/README.md` measured every store and none of them fit:

| store | limit | 18 GiB fits? |
|---|---|---|
| Git LFS | 2 GiB/file (Free/Pro) | no |
| release asset | < 2 GiB | no |
| OCI layer | 10 GiB **and** a 10-minute upload timeout | no |

So the disk is sharded into <4 GiB parts, one single-layer image each, and joined by
`fetch-golden.sh` with a whole-disk sha256 for the join. A bake that ends by writing a
local qcow2 has not finished; the last step is `publish-golden`, and it has to be
planned for from the start rather than bolted on.

That also decides the disk format: build sparse, `qemu-img convert -c
-o compression_type=zstd` at the end, and keep the parts under the layer limit.

## Directory contents (planned)

```
macos/15/
  bake.sh          fetch recovery -> boot -> startosinstall -> marker -> shutdown
  firmware/        OVMF_CODE.fd, OVMF_VARS-1920x1080.fd
  opencore/        config.plist template, Drivers/, Kexts/
  verify.sh        boot the disk with no recovery attached, screenshot the desktop
```

`bake.sh` should be `linux/mx/bake.sh` with two substitutions - the installer step and
the "did it arrive" pattern - because every other stage (vm, snapshots, smoke, record,
compress) is already shared.
