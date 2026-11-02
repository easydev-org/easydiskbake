# android/

Placeholder. Nothing baked here yet.

## Why it is a separate root and not a `linux/` distribution

Android is not installed by a distribution installer that writes a disk; it is an
emulator image. The stages that `lib/` provides do not line up:

| stage | Linux | Android |
|---|---|---|
| fetch | an ISO from ghcr | a system image (AVD) from Google's SDK repository |
| install | an installer writes a disk | the image already *is* the disk |
| boot | SeaBIOS/UEFI -> bootloader -> kernel | the emulator starts the image |
| prove | serial console says `login:` or a greeter appears | `adb wait-for-device` + `getprop sys.boot_completed` |

So the shared half here is the recording and the smoke assertion, not the vm or the
installer. `easysandbox-android` in the sandbox repo already has this shape - it builds
a container image around the official emulator rather than baking a qcow2.

## What a bake would produce

Not a qcow2. An AVD system image plus whatever sandbox pieces are injected into it,
and the "did it boot" answer comes from `adb`, not from a serial console.

If this is wired up, `lib/vm.py` is the wrong entry point and the emulator's own
`-no-window` plus a screenshot path replaces it. The recording half (`snapshots.py`
takes a QMP socket) would need a different frame source.
