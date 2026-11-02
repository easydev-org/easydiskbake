"""The three assets macOS needs beyond a disk, fetched by pin and built once.

    python3 lib/opencore.py --out work/macos15

    work/macos15/opencore/EFI/...        the payload OpenCore boots
    work/macos15/boot.img                that payload as a disk the firmware can start
    work/macos15/macos.rom               OVMF code
    work/macos15/macos.vars              OVMF variables, 1920x1080, writable copy
    work/macos15/identity.json           serial, board serial, UUID, MAC

WHY THIS IS NOT COPIED WHOLESALE

dockur/macos builds all of this at container start: it unpacks OpenCore, extracts a
template image, generates a serial with macserial, rewrites config.plist in place, and
lays down a FAT image with mtools. Every step of that is a decision this repository
would otherwise inherit silently, and three of them are worth stating:

    the SMC key            a fixed, well-known literal. Not a secret and not per
                           guest; it is what makes macOS believe it is on Apple
                           hardware. It travels in the qemu command line.
    the identity           HAS to persist. macOS ties an installation to the serial,
                           the board serial and the MAC it was installed with, so a
                           bake that generates a new one each boot produces a disk that
                           only boots on the machine that made it - or stops booting at
                           all when the NVRAM remembers the old one.
    the OVMF vars          per-resolution. The GOP mode is baked into the template, so
                           a disk installed against the 1920x1080 vars does not boot
                           against the 1024x768 ones.

WHAT IS PINNED AND WHAT IS NOT

OpenCore, the template image and the firmware pair have fixed, versioned URLs, so all
four carry a sha256 in lib/images.py and are verified here. Nothing about them changes
per bake, which is the property that makes a pin possible at all - unlike the recovery
image, which Apple reissues on every request and which lib/macos_recovery.py therefore
verifies structurally instead.

WHY THE RESULT IS BUILT EVERY TIME AND CACHED BY HASH

Building it needs `unzip`, `7z` and `mtools` - all present in the bake image - and
takes about two seconds. The alternative is committing a 300 MiB binary to the
repository, which is the thing this project exists to avoid: the disk is the artifact,
and everything else is derived. A cache keyed by the pins is enough to keep a re-run
fast without pretending the assets are inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import time
import uuid
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import images  # noqa: E402
import iso  # noqa: E402

# The SMC key. Every hackintosh since 2009 has used this string; it is in
# OSX-KVM, in dockur/macos and in Apple's own diagnostic tooling. It is not a
# credential - it is the value macOS reads from the SMC device to decide whether
# the firmware is Apple's.
OSK = ("ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc")

# THE DISK GEOMETRY, COPIED FROM dockur/macos RATHER THAN CHOSEN.
#
# These are the five numbers in its buildOpenCoreImage(), and the two that matter are
# not round: 256 MiB and a 4-sector cluster.
#
# WHY THE PAIR MATTERS. FAT32 stops being FAT32 below 65,525 clusters - that is the
# only thing separating it from FAT16, and the specification says so. Cluster count is
# (data sectors / sectors per cluster), so both numbers move it:
#
#     dockur   256 MiB, -c 4   ->  ~130,000 clusters   valid
#     mine     200 MiB, -c 8   ->      50,836 clusters  INVALID
#
# mtools wrote the invalid one anyway and read it back, so mdir and every check on this
# side passed. OVMF's EDK II FAT driver validates the format and refused to mount the
# partition. The console said:
#
#     BdsDxe: failed to load Boot0001 "UEFI Misc Device" from
#             PciRoot(0x0)/Pci(0x4,0x0): Not Found
#
# while the same firmware's mapping table listed that disk with its partition intact:
#
#     BLK1:Alias(s): PciRoot(0x0)/Pci(0x4,0x0)/HD(1,GPT,...,0x800,0x637DF)
#
# GPT read, filesystem not. Five runs went into the qemu arguments before the console
# log was read, and the numbers here were in dockur's source the whole time.
BOOT_IMG_MB = 256
BOOT_IMG_SECTOR = 512
BOOT_IMG_SECTORS = (BOOT_IMG_MB * 1024 * 1024) // BOOT_IMG_SECTOR

# dockur's cluster_size, in sectors. 4 sectors = 2 KiB clusters.
ESP_SECTORS_PER_CLUSTER = 4

# The GPT layout constants, also from dockur: the first usable LBA is 34, and the
# partition ends 34 sectors before the end of the disk, leaving room for the backup.
ESP_FIRST_USABLE_LBA = 34

# THE GUEST'S SCREEN, and it is a contract rather than a preference: the drivers in
# macos/*/install.py and macos/*/drive.py hold positions measured at this size. It also
# has to match the OVMF_VARS template, whose GOP mode is baked in - lib/images.py pins
# OVMF_VARS-1920x1080.fd and nothing else.
GUEST_WIDTH = 1920
GUEST_HEIGHT = 1080
FAT32_MIN_CLUSTERS = 65525

# The boot image is a GPT disk with one partition, and these are the constants of that
# layout. They were parameters of nothing before, which is how the partition table came
# to be missing entirely - see build_boot_image().
SECTOR_SIZE = 512
ESP_START_SECTOR = 2048          # 1 MiB: the conventional, and cluster-aligned, start
GPT_ENTRY_ARRAY_SECTORS = 32     # 128 entries x 128 bytes = 16 KiB
# C12A7328-F81F-11D2-BA4B-00A0C93EC93B, as GPT stores it (first three fields
# little-endian, the rest as written).
ESP_TYPE_GUID = bytes.fromhex("28732ac11ff8d211ba4b00a0c93ec93b")
# The disk's own GUID. Fixed rather than random on purpose: this image is rebuilt on
# every bake and a stable identifier makes two builds comparable byte for byte.
GPT_DISK_GUID = bytes.fromhex("1ACB1E003B8F4B2A86A4D99ED21DCAEB")

# How a serial pair is made: macserial ships inside OpenCore's release archive under
# Utilities/, so the generator travels with the payload it describes rather than being
# a second download that can drift from it.
MACSERIAL = "Utilities/macserial/macserial.linux"

# Where Apple keeps the APFS driver inside a recovery image. The bake extracts it from
# there rather than pinning it - see install_apfs_driver() for why that beats every
# alternative, and lib/images.py for the failure it prevents.
APFS_DRIVER = "macOS Base System/usr/standalone/i386/apfs.efi"


class OpenCoreError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[opencore {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- fetching ----------------------------------------------------------------


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_pinned(name: str, dest: str) -> str:
    """Pull one pinned asset from this repository's package, or reuse it, and verify
    the sha256 either way.

    FROM ghcr, NOT FROM THE VENDOR. The URLs in images.MACOS_ASSETS are real and were
    used once, to derive the pins - but a bake that fetches them at run time is a bake
    whose first step is a transfer from GitHub's release CDN with the job clock
    running, which is the thing publish-iso.yml exists to avoid for every other image
    here. lib/publish_macos_assets.py puts the four files in the package once; this
    pulls them back with the same three registry calls as an ISO.

    THE PIN IS STILL CHECKED. The registry guarantees the bytes it stored, not that
    they are the bytes this code expects: a tag can be overwritten, and
    `macos-asset-opencore` is not a timestamped tag. A mismatch says which pin moved,
    which is the only useful thing to know. It is fatal rather than a warning -
    an OpenCore or an OVMF that silently upgraded would produce a disk that boots
    today and stops booting when the firmware changes its GOP mode.
    """
    pin = images.MACOS_ASSETS.get(name)
    if pin is None:
        raise OpenCoreError(
            f"unknown macOS asset {name!r}; known: "
            f"{', '.join(sorted(images.MACOS_ASSETS))}"
        )

    if os.path.exists(dest):
        got = _sha256(dest)
        if got == pin["sha256"]:
            log(f"{name}: present, sha256 matches")
            return dest
        log(f"{name}: cached file does not match the pin, fetching again")
        os.unlink(dest)

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tag = images.macos_asset_tag(name)
    log(f"{name}: pulling {tag} from ghcr")
    try:
        iso.fetch_tag(tag, dest)
    except iso.IsoError as exc:
        raise OpenCoreError(
            f"{tag} is not in this repository's ghcr package: {exc}\n\n"
            f"Run publish-macos-assets.yml for {tag} first:\n"
            f"    gh workflow run publish-macos-assets.yml -f which={tag}\n"
            f"This bake does not download from the vendor. A second source would be a\n"
            f"second thing to verify, and the failure it hides is exactly the one worth\n"
            f"seeing: an OpenCore archive that changed under a tag."
        ) from exc

    got = _sha256(dest)
    if got != pin["sha256"]:
        os.unlink(dest)
        raise OpenCoreError(
            f"{name}: sha256 {got} but the pin says {pin['sha256']}.\n"
            f"Either {tag} was overwritten or the pin is stale.\n"
            f"Re-derive it from {pin['url']} deliberately - do not paste it in."
        )
    log(f"{name}: {os.path.getsize(dest) >> 20} MiB, sha256 verified")
    return dest


# --- the payload -------------------------------------------------------------


def unpack_opencore(archive: str, work: str) -> str:
    """The EFI tree inside OpenCore's release zip, as a directory.

    The archive holds X64/EFI/ and IA32/EFI/; only X64 is used, and only the EFI tree -
    the documentation sits beside it and would go into the boot image otherwise.

    TWO THINGS THIS TREE DOES NOT HAVE, AND BOTH ARE DELIBERATE:

        Config.plist    OpenCore ships it under Docs/Sample.plist, not in X64/EFI. The
                        config that actually boots macOS comes from the template and is
                        edited by write_config(), so requiring one here would reject a
                        correct archive. (The first version of this function did, and
                        would have failed on every run against the real 1.0.7 release.)
        kexts/drivers   the archive is the bootloader and nothing else. The template
                        supplies the rest; _merge_template() is what combines them.
    """
    out = os.path.join(work, "opencore")
    if os.path.isdir(out):
        return out
    os.makedirs(out, exist_ok=True)
    subprocess.run(
        ["unzip", "-q", archive, "X64/EFI/*", "-d", out],
        check=True,
    )
    efi = os.path.join(out, "X64", "EFI")
    for required in ("BOOT/BOOTx64.efi", "OC/OpenCore.efi"):
        if not os.path.exists(os.path.join(efi, required)):
            raise OpenCoreError(f"{archive} holds no {required}; the archive layout changed")
    return efi


def extract_template_boot(template_iso: str, dest: str) -> str:
    """The EFI tree from the LongQT template image, which is not a plain ISO9660.

    WHY THIS EXISTS AT ALL. OpenCore's own archive is the bootloader and nothing else:
    it has no kexts, no drivers and no config that boots macOS. The template supplies
    the parts specific to this job - Lilu, VirtualSMC, WhateverGreen, the VoodooPS2
    input drivers, the ACPI tables - and upstream's config.plist. (It has no APFS
    driver either; see the note in lib/images.py about where that comes from.)

    WHY 7z AND NOT AN ISO READER. The image carries two name tables. The ISO9660 one
    is 8.3 uppercase with a ";1" version suffix - `CONFIG.PLIST;1`, `SSDT_EC.AML` -
    and the Joliet one has the names the config refers to: `config.plist`,
    `SSDT-EC.aml`, `Lilu.kext`. Extracting through the 9660 table produces a tree
    whose file names match nothing that OpenCore looks for, and the guest then stops
    at a prohibited sign with an empty log. 7z reads the Joliet layer, which is why it
    is in the bake image.

    THE RESULT IS THE `EFI` DIRECTORY, not the `OC` one and not the extraction root:
    the caller merges this tree's contents over OpenCore's, so BOOT/ and OC/ must sit
    at its top level.
    """
    out = os.path.join(dest, "template")
    if os.path.isdir(out):
        return out
    os.makedirs(out, exist_ok=True)
    subprocess.run(["7z", "x", "-y", f"-o{out}", template_iso], check=True,
                   stdout=subprocess.DEVNULL)

    # The archive holds EFI_RELEASE and EFI_DEBUG. Both are complete trees, and the
    # release one is what a bake boots: the debug build is slower and writes a log to
    # the ESP that nothing here reads. It is chosen by name rather than by taking the
    # first match, because os.walk's order is whatever the filesystem returns.
    candidates = []
    for root, _dirs, files in os.walk(out):
        if "OpenCore.efi" not in files:
            continue
        if os.path.basename(root).lower() != "oc":
            continue
        efi = os.path.dirname(root)
        if os.path.basename(efi).lower() != "efi":
            continue
        candidates.append(efi)

    release = [c for c in candidates if "EFI_RELEASE" in c.upper()]
    chosen = release[0] if release else (candidates[0] if candidates else None)
    if chosen is None:
        raise OpenCoreError(
            f"{template_iso} holds no EFI/OC/OpenCore.efi; the template layout "
            f"changed. Looked under {out}."
        )

    for required in ("OC/ACPI", "OC/Kexts", "OC/Drivers", "OC/Resources",
                     "OC/config.plist"):
        if not os.path.exists(os.path.join(chosen, required)):
            raise OpenCoreError(
                f"{template_iso} has no {required} under {chosen}; the guest would "
                f"boot to a prohibited sign with nothing in any log to explain it"
            )
    log(f"template: {chosen}")
    return chosen


def identity(release: str, work: str, model: str = "iMacPro1,1") -> dict:
    """The machine identity, generated once and then read back.

    THE PERSISTENCE IS THE POINT. macOS binds an installation to the serial, the board
    serial and the MAC address. Generating a new one per boot gives a disk that boots on
    the machine that made it and on nothing else; worse, the NVRAM keeps the old
    identity, so the guest can end up disagreeing with itself and stopping at a boot
    loop that looks like a broken disk.

    So this is a file, written once per work directory, and every later stage reads it.
    dockur/macos uses the same shape (writeState/restoreState in src/install.sh) for the
    same reason.
    """
    path = os.path.join(work, "identity.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    serial, board_serial = generate_serial(work, model)
    data = {
        "release": release,
        "model": model,
        "serial": serial,
        "board_serial": board_serial,
        "uuid": str(__import__("uuid").uuid4()).upper(),
        "mac": generate_mac(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    log(f"identity: {data['serial']} / {data['mac']} (written to {path})")
    return data


def generate_serial(work: str, model: str) -> tuple[str, str]:
    """A serial and board serial from macserial, which knows Apple's checksums.

    Hand-rolled serials are the classic mistake: the format looks random but the last
    characters are a checksum over the model and the factory code, and macOS derives
    the board serial from the same data. macserial is the tool the hackintosh projects
    use for exactly this, and it travels inside OpenCore's own archive.
    """
    tool = os.path.join(work, "macserial")
    if not os.path.exists(tool):
        raise OpenCoreError(
            f"macserial is not at {tool}. It ships inside OpenCore's archive under "
            f"{MACSERIAL}; unpack_opencore() puts it beside the payload."
        )
    result = subprocess.run([tool, "--num", "1", "--model", model],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise OpenCoreError(f"macserial failed: {result.stderr.strip()[:300]}")
    line = result.stdout.strip().splitlines()[-1]
    if "|" not in line:
        raise OpenCoreError(f"macserial printed {line!r}, which has no '|' separator")
    serial, board_serial = (part.strip() for part in line.split("|", 1))
    return serial, board_serial


def generate_mac() -> str:
    """A MAC with Apple's own OUI prefix.

    macOS reads the prefix: 00:16:CB is Apple's, and a guest with a random locally
    administered address is a differently-behaved machine. dockur derives it from the
    UUID so it is stable; here the value is kept in identity.json instead, which is the
    same guarantee with one less derivation to get wrong.
    """
    tail = os.urandom(3)
    return "00:16:CB:%02X:%02X:%02X" % (tail[0], tail[1], tail[2])


def write_config(efi: str, template_efi: str, ident: dict, release: str) -> str:
    """Merge the identity into the template's config.plist.

    WHY A MERGE AND NOT A TEMPLATE OF OUR OWN. The template's config carries device
    properties and kext lists that were arrived at by somebody else's runs - the GPU
    and SMC device properties in particular are not guessable. Replacing the file
    would throw those away; only the identity fields and the settings this bake
    depends on are changed:

        SystemSerialNumber, MLB, ROM, SystemUUID   the machine identity
        csr-active-config = 0x7f                   SIP off, which the sandbox image
                                                   needs for its TCC and xcode work
        Boot/ShowPicker = False                    boot straight into the installer
        Boot/Timeout = 0

    WHY plistlib AND NOT xmlstarlet.

    The previous version edited the XML in place with a positional XPath:
    `key[1]/following-sibling::*[1]/key[3]/...`. A plist dict is a flat list of
    key/value pairs, so that indexing is correct only while every dict's key order is
    exactly what it was when the expression was written. Reorder one dict upstream -
    which a release bump does - and the edit lands on a neighbouring key and succeeds,
    reporting success. There is nothing in the output that says which key was written.

    plistlib addresses the file the way OpenCore reads it: by name. `d["PlatformInfo"]
    ["Generic"]["MLB"] = ...` either writes the key it names or raises KeyError. The
    round trip is byte-identical in content (verified against the real 0.7 template:
    load, dump, load again, compare) and the file stays XML, which is what OpenCore
    parses.
    """
    source = _find_file(os.path.join(template_efi, "OC"), "config.plist")
    if source is None:
        raise OpenCoreError(
            f"the template has no config.plist under {template_efi}/OC. Looked for "
            f"config.plist, Config.plist and CONFIG.PLIST; the layout changed."
        )
    target = os.path.join(efi, "OC", "config.plist")

    with open(source, "rb") as fh:
        config = plistlib.load(fh)

    # The identity. ROM is six raw bytes of the MAC as <data>, not a hex string -
    # OpenCore hands those bytes to macOS as the primary network interface's address.
    generic = config["PlatformInfo"]["Generic"]
    generic["SystemSerialNumber"] = ident["serial"]
    generic["MLB"] = ident["board_serial"]
    generic["SystemUUID"] = ident["uuid"]
    generic["ROM"] = bytes.fromhex(ident["mac"].replace(":", ""))

    # SIP off. The value is a 4-byte little-endian bitmask; 0x7f disables every
    # protection, which the sandbox image needs for its TCC and xcode work.
    nvram = config["NVRAM"]["Add"]["7C436110-AB2A-4BBB-A880-FE41995C9F82"]
    nvram["csr-active-config"] = b"\x7f\x00\x00\x00"

    # NO PICKER. A bake boots one thing: the installer. OpenCore's picker with a
    # two-second timeout shows a menu that has to time out before anything happens,
    # and if a key arrives from lib/bootkey.py while it is up it stops there instead.
    # The template ships ShowPicker=true, which is right for a person and wrong here.
    boot = config["Misc"]["Boot"]
    boot["ShowPicker"] = False
    boot["Timeout"] = 0

    # THE SCREEN SIZE IS NOT DECORATION - EVERY COORDINATE IN A DRIVER DEPENDS ON IT.
    #
    # dockur sets UEFI/Output/Resolution to "${WIDTH}x${HEIGHT}@32" in
    # configureOpenCorePlist(), and the template ships it EMPTY. That empty string is
    # why six runs clicked at coordinates that were never on screen: the guest ran at
    # 1280x800 while macos/15/install.py's positions were measured at 1920x1080, and
    # the licence pane's Agree at y=810 was below the bottom of an 800-pixel screen.
    #
    # The frames of run 41443447642 show it directly: 1280x800, a Recovery menu with
    # its rows at y=149, 238, 333, 425 and its button at y=511 - none of which any of
    # the clicks were aimed at.
    #
    # 1920x1080 is also the resolution the OVMF_VARS template was built for
    # (lib/images.py pins OVMF_VARS-1920x1080.fd, and the GOP mode is baked into it),
    # so this is the pair, not one setting.
    # Resolution ONLY - ForceResolution IS LEFT ALONE, which is what dockur does.
    #
    # configureOpenCorePlist() in dockur/macos writes exactly one field here:
    #
    #     resolution="${WIDTH}x${HEIGHT}@32"
    #     xmlstarlet ... -u "$output/key[.='Resolution']/..." -v "$resolution"
    #
    # and the template's ForceResolution=false stays. Setting it true asks OpenCore to
    # impose a mode rather than use one the firmware already has, and there is no
    # reason to take that risk when the pinned OVMF_VARS-1920x1080.fd carries the
    # matching GOP mode already.
    output = config["UEFI"]["Output"]
    output["Resolution"] = f"{GUEST_WIDTH}x{GUEST_HEIGHT}@32"

    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as fh:
        plistlib.dump(config, fh, fmt=plistlib.FMT_XML, sort_keys=False)

    # Read back rather than trusting the write: this file is the whole difference
    # between a guest that boots and one that stops at a prohibited sign.
    with open(target, "rb") as fh:
        check = plistlib.load(fh)
    if check["PlatformInfo"]["Generic"]["SystemSerialNumber"] != ident["serial"]:
        raise OpenCoreError(
            f"{target} does not hold the serial after writing; the identity did not "
            f"land and the guest would boot with another machine's."
        )
    log(f"config written: serial, board serial, UUID, ROM, SIP off, no picker ({release})")
    return target


def _find_file(directory: str, wanted: str) -> str | None:
    """Find `wanted` in `directory`, ignoring case, or None.

    The template image's names come from an ISO9660/Joliet tree and the two layers
    disagree about case; 7z resolves that when it extracts, but the result still
    depends on which layer it preferred. Comparing case-insensitively makes this
    independent of that, which matters because "config.plist" and "Config.plist" are
    the same file to OpenCore and different strings to Python.
    """
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    lower = wanted.lower()
    for entry in entries:
        if entry.lower() == lower:
            return os.path.join(directory, entry)
    return None


# --- the disk the firmware boots ---------------------------------------------


def install_apfs_driver(efi: str, recovery: str, work: str) -> str:
    """Put Apple's own APFS driver into the boot image, and register it.

    WHY THIS IS NOT OPTIONAL, AND WHY IT IS NOT IN THE PINS.

    OpenCore ships no APFS driver and neither does the LongQT template. Its
    config carries EnableJumpstart=true, which is often mistaken for a driver: it
    is not. Jumpstart HOOKS a driver the firmware is expected to already have, and
    OVMF - which is what boots this guest - has none. The symptom is specific and
    silent: an installation completes, the disk is full of a working macOS, and
    OpenCore's picker lists only Reset NVRAM and Toggle SIP because it cannot read
    the AF0A partition the system is on. Nothing in any log says "no APFS driver".

    THE DRIVER IS ALREADY ON DISK, INSIDE THE RECOVERY IMAGE. macOS ships it at
    usr/standalone/i386/apfs.efi in BaseSystem, and that is what the installer itself
    uses. Extracting it from there beats every alternative:

        copying it off the installed disk    impossible before the first boot, and
                                             dockur's fix-opencore-apfs.sh has to do
                                             exactly this on the second run
        taking it from a Linux package       the APFS driver is Apple's, not
                                             apfs-fuse's userspace library
        adding it to the pins                it would be a fifth pinned download for
                                             a file the bake already has

    So the input is the recovery image, which the bake fetches anyway, and the output
    is one 700 KiB file in the EFI tree plus one entry in config.plist.

    The driver is registered with LoadEarly=false: it is a filesystem driver, and
    OpenCore's documentation puts those after the runtime services drivers
    (OpenRuntime) that they may call into.
    """
    source = os.path.join(work, "apfs.efi")
    if not os.path.exists(source):
        if not os.path.isfile(recovery):
            raise OpenCoreError(
                f"no recovery image at {recovery}, and the APFS driver can only come "
                f"from one; the guest would install a macOS it cannot boot"
            )
        log(f"extracting apfs.efi from {os.path.basename(recovery)}")
        # 7z walks the DMG's compressed blocks and its HFS+ contents, which is the only
        # way to reach a file inside a UDIF image without mounting it.
        extract = os.path.join(work, "apfs-extract")
        subprocess.run(
            ["7z", "x", "-y", f"-o{extract}", recovery, APFS_DRIVER],
            check=True, stdout=subprocess.DEVNULL,
        )
        found = None
        for root, _dirs, files in os.walk(extract):
            for name in files:
                if name.lower() == os.path.basename(APFS_DRIVER).lower():
                    found = os.path.join(root, name)
                    break
            if found:
                break
        if found is None:
            raise OpenCoreError(
                f"{recovery} holds no {APFS_DRIVER}; the recovery layout changed.\n"
                f"Without it OpenCore cannot read the installed system and the bake "
                f"produces a disk that only boots with a matching recovery attached."
            )
        shutil.move(found, source)

    # A driver that is not a PE image is not a driver. This is cheap and it is the
    # difference between "the extraction produced a file" and "the extraction produced
    # the file" - an obfuscated or truncated apfs.efi fails at boot with no message.
    with open(source, "rb") as fh:
        head = fh.read(2)
    if head != b"MZ":
        raise OpenCoreError(
            f"{source} is not a PE image (begins {head!r}, expected b'MZ')"
        )
    size = os.path.getsize(source)

    drivers = os.path.join(efi, "OC", "Drivers")
    os.makedirs(drivers, exist_ok=True)
    shutil.copyfile(source, os.path.join(drivers, "apfs.efi"))

    # Register it. The template's list is kept in order and the new entry is appended,
    # because the drivers OpenCore loads first are the ones it needs for the filesystem
    # it is about to read - this one, so it goes last.
    config_path = _find_file(os.path.join(efi, "OC"), "config.plist")
    if config_path is None:
        raise OpenCoreError(f"no config.plist under {efi}/OC to register apfs.efi in")
    with open(config_path, "rb") as fh:
        config = plistlib.load(fh)

    entries = config["UEFI"]["Drivers"]
    if not any(entry.get("Path", "").lower() == "apfs.efi" for entry in entries):
        entries.append({
            "Arguments": "",
            "Comment": "Apple's APFS driver, from the recovery image",
            "Enabled": True,
            "LoadEarly": False,
            "Path": "apfs.efi",
        })
        with open(config_path, "wb") as fh:
            plistlib.dump(config, fh, fmt=plistlib.FMT_XML, sort_keys=False)

    with open(config_path, "rb") as fh:
        check = plistlib.load(fh)
    if not any(e.get("Path", "").lower() == "apfs.efi" and e.get("Enabled")
               for e in check["UEFI"]["Drivers"]):
        raise OpenCoreError(f"apfs.efi did not land in {config_path}")

    log(f"apfs.efi: {size >> 10} KiB, registered in config.plist")
    return source


def recovery_image(work: str) -> str:
    """Where the recovery image for this work directory lives, if it is there.

    The bake fetches it (lib/macos_recovery.py, or a pull of :macos-recovery-<name>)
    and this only has to find it. Two names are accepted because the two routes that
    produce it disagree: the workflow writes recovery-<name>.dmg and a manual run
    writes recovery.dmg. Neither is guessed at - a bake that cannot find its installer
    should stop, not build a boot image for an installer it does not have.
    """
    for name in ("recovery.dmg", "setup.dmg", "BaseSystem.dmg"):
        path = os.path.join(work, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    for name in sorted(os.listdir(work)) if os.path.isdir(work) else []:
        if name.endswith(".dmg"):
            return os.path.join(work, name)
    return os.path.join(work, "recovery.dmg")


def build_boot_image(efi: str, dest: str) -> str:
    """A GPT disk whose single partition is the ESP OpenCore boots from.

    THE PARTITION TABLE IS THE POINT, AND IT WAS MISSING.

    The first version ran `mformat -F` on the whole file, which makes a "superfloppy":
    a FAT filesystem starting at LBA 0 with an MBR signature written over the FAT boot
    sector. mtools reads it happily, so `mdir` listed the tree and the build passed its
    own check. OVMF did not: firmware looks for an EFI SYSTEM PARTITION in a GPT or MBR
    partition table, and a filesystem at LBA 0 is neither. The run died at

        TianoCore ... failed to load Boot0001

    which reads like a broken loader and is actually a disk the firmware cannot
    enumerate. That is the whole class of failure this function now rules out.

    WHAT A BOOTABLE ESP NEEDS, all of it done here:

        LBA 0        a PROTECTIVE MBR: one 0xEE entry covering the disk
        LBA 1        a GPT header with a CRC32
        LBA 2..33    the entry array, 128 x 128 bytes
        LBA 34       first usable sector; the partition may not start below it
        LBA 2048     the ESP itself, FAT32, 1 MiB in
        last LBA-33  the backup entry array
        last LBA     the backup header

    THE ALTERNATIVE WAS sfdisk, which is what dockur/macos uses. It is not in the bake
    image, and adding a partitioning tool to write one fixed table for one fixed image
    is a dependency for something that is forty lines of struct.pack. The values below
    are constants of this layout rather than parameters.
    """
    total_mb = BOOT_IMG_MB
    image_size = total_mb * 1024 * 1024
    total_sectors = image_size // SECTOR_SIZE

    # The ESP, in sectors. 2048 is 1 MiB, which is what makes the FAT filesystem's own
    # geometry land on whole clusters and is the conventional ESP start.
    esp_first = ESP_START_SECTOR
    esp_last = total_sectors - 34
    esp_sectors = esp_last - esp_first + 1

    if os.path.exists(dest):
        os.unlink(dest)
    with open(dest, "wb") as fh:
        fh.truncate(image_size)

    # The FAT filesystem goes INSIDE the partition, at its byte offset. mtools addresses
    # a file offset with the @@ suffix, which writes the same bytes a mount would.
    partition = f"{dest}@@{esp_first * SECTOR_SIZE}"
    geometry = [
        "-F",                       # a full filesystem, not a partition image
        "-i", partition,
        "-c", str(ESP_SECTORS_PER_CLUSTER),
        "-T", str(esp_sectors),
        "-v", "EFI",
    ]
    subprocess.run(["mformat", *geometry], check=True)
    subprocess.run(["mcopy", "-s", "-i", partition, "-m", efi, "::/"], check=True)

    write_gpt(dest, esp_first, esp_last)

    # Verify what the FIRMWARE will see, not what mtools will: the partition table has
    # to be readable and the ESP entry has to be there. This is the check the first
    # version lacked - it asked mtools, which never looks at a partition table.
    check_gpt(dest, esp_first)
    check_esp_filesystem(dest, esp_first)

    listing = subprocess.run(["mdir", "-i", partition, "-/", "::/"],
                             capture_output=True, text=True)
    if listing.returncode != 0 or "OpenCore.efi" not in listing.stdout:
        raise OpenCoreError(
            f"{dest} does not hold OpenCore.efi inside its ESP after mcopy:\n"
            f"{listing.stdout[:600]}"
        )
    log(f"boot.img: {image_size >> 20} MiB, GPT + ESP at LBA {esp_first}, "
        f"EFI tree copied")
    return dest


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _partition_entry(first: int, last: int, *, name: str) -> bytes:
    """One 128-byte GPT entry: type GUID, unique GUID, LBAs, attributes, UTF-16 name."""
    entry = bytearray(128)
    entry[0:16] = ESP_TYPE_GUID          # EFI System Partition
    entry[16:32] = uuid.uuid4().bytes_le  # this partition's own GUID
    struct.pack_into("<Q", entry, 32, first)
    struct.pack_into("<Q", entry, 40, last)
    # attributes, 48..56: zero - no legacy BIOS bootable, no read-only
    for index, char in enumerate(name[:35]):
        struct.pack_into("<H", entry, 56 + index * 2, ord(char))
    return bytes(entry)


def write_gpt(path: str, esp_first: int, esp_last: int) -> None:
    """Write a protective MBR, a primary GPT, and its backup. Idempotent by rewriting.

    The backup copies are not decoration: a firmware that finds the primary header
    damaged falls back to the last sector, and OVMF does look there. Leaving them out
    produces a disk that works until the one run where it does not.
    """
    size = os.path.getsize(path)
    total_sectors = size // SECTOR_SIZE

    # --- protective MBR -----------------------------------------------------------------
    mbr = bytearray(SECTOR_SIZE)
    # Boot code is left zero: the firmware never executes it, and a stub that does
    # nothing is more honest than one that pretends to be a loader.
    mbr[0x1BE:0x1BE + 16] = bytes(
        [0x00, 0x00, 0x02, 0x00, 0xEE, 0xFF, 0xFF, 0xFF]
        + list(struct.pack("<I", 1))
        + list(struct.pack("<I", min(total_sectors - 1, 0xFFFFFFFF)))
    )
    mbr[0x1FE:0x200] = b"\x55\xAA"

    # --- entries ------------------------------------------------------------------------
    entries = bytearray(GPT_ENTRY_ARRAY_SECTORS * SECTOR_SIZE)
    entries[0:128] = _partition_entry(esp_first, esp_last, name="primary")

    entries_crc = _crc32(bytes(entries))

    def header(this_lba: int, other_lba: int, entries_lba: int) -> bytes:
        head = bytearray(SECTOR_SIZE)
        head[0:8] = b"EFI PART"
        struct.pack_into("<I", head, 8, 0x00010000)   # revision 1.0
        struct.pack_into("<I", head, 12, 92)          # header size
        struct.pack_into("<Q", head, 24, this_lba)
        struct.pack_into("<Q", head, 32, other_lba)
        struct.pack_into("<Q", head, 40, 34)           # first usable LBA
        struct.pack_into("<Q", head, 48, total_sectors - 34)
        head[56:72] = GPT_DISK_GUID
        struct.pack_into("<Q", head, 72, entries_lba)
        struct.pack_into("<I", head, 80, 128)          # entry count
        struct.pack_into("<I", head, 84, 128)          # entry size
        struct.pack_into("<I", head, 88, entries_crc)
        # The CRC covers the header's DECLARED SIZE - 92 bytes - not the whole sector.
        # The rest of the sector is padding and is not part of the header. Computing it
        # over all 512 was the bug: the write and the read agreed with each other and
        # disagreed with every other GPT implementation.
        struct.pack_into("<I", head, 16, _crc32(bytes(head[:92])))
        return bytes(head)

    primary_entries_lba = 2
    backup_entries_lba = total_sectors - 1 - GPT_ENTRY_ARRAY_SECTORS
    primary_header_lba = 1
    backup_header_lba = total_sectors - 1

    with open(path, "r+b") as fh:
        fh.seek(0)
        fh.write(mbr)

        fh.seek(primary_header_lba * SECTOR_SIZE)
        fh.write(header(primary_header_lba, backup_header_lba, primary_entries_lba))

        fh.seek(primary_entries_lba * SECTOR_SIZE)
        fh.write(entries)

        fh.seek(backup_entries_lba * SECTOR_SIZE)
        fh.write(entries)

        fh.seek(backup_header_lba * SECTOR_SIZE)
        fh.write(header(backup_header_lba, primary_header_lba, backup_entries_lba))


def check_esp_filesystem(path: str, esp_first: int) -> None:
    """Is the filesystem inside the ESP a VALID FAT32 volume?

    THIS IS THE CHECK THAT FIVE RUNS NEEDED. Every other check on this side read the
    partition table or asked mtools, and mtools accepts a FAT32 volume that its own
    specification disallows. EDK II does not.

    The rule: FAT32 has at least 65,525 clusters. Cluster count comes from the BIOS
    Parameter Block, so this is arithmetic on the boot sector rather than an opinion:

        total_sectors  = BPB_TotSec32 (or BPB_TotSec16 when the former is zero)
        non_data       = reserved + fats * sectors_per_fat
        clusters       = (total_sectors - non_data) / sectors_per_cluster

    A volume below the floor is rejected with the two numbers, because the fix is
    always to change one of them and never to argue.
    """
    with open(path, "rb") as fh:
        fh.seek(esp_first * SECTOR_SIZE)
        boot = fh.read(512)
    if len(boot) < 512:
        raise OpenCoreError(f"{path}: the ESP boot sector is truncated")

    if boot[510:512] != b"\x55\xAA":
        raise OpenCoreError(
            f"{path}: the ESP at LBA {esp_first} has no 0x55AA boot signature"
        )

    bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
    sectors_per_cluster = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    total16 = struct.unpack_from("<H", boot, 19)[0]
    total32 = struct.unpack_from("<I", boot, 32)[0]
    sectors_per_fat = struct.unpack_from("<I", boot, 36)[0]
    label = boot[82:90]

    total = total32 or total16
    if not (bytes_per_sector and sectors_per_cluster and total and sectors_per_fat):
        raise OpenCoreError(
            f"{path}: the ESP boot sector is missing FAT geometry "
            f"(sector={bytes_per_sector} cluster={sectors_per_cluster} "
            f"total={total} fat={sectors_per_fat})"
        )

    if label.rstrip() != b"FAT32":
        raise OpenCoreError(
            f"{path}: the ESP is labelled {label!r}, not FAT32. The whole point of an "
            f"ESP is a FAT32 volume the firmware can mount."
        )

    non_data = reserved + fats * sectors_per_fat
    clusters = (total - non_data) // sectors_per_cluster
    log(f"ESP: {clusters} clusters of {sectors_per_cluster} sectors "
        f"({bytes_per_sector} bytes/sector, label FAT32)")

    if clusters < FAT32_MIN_CLUSTERS:
        raise OpenCoreError(
            f"{path}: the ESP holds {clusters} clusters but FAT32 requires at least "
            f"{FAT32_MIN_CLUSTERS}.\n"
            f"This volume is invalid by its own specification. mtools writes it and "
            f"reads it back, so nothing here notices - OVMF's FAT driver does not, and "
            f"the firmware reports\n"
            f"    failed to load Boot0001 ... : Not Found\n"
            f"while listing the disk's GPT partition correctly.\n"
            f"Fix by enlarging the image (BOOT_IMG_MB) or lowering "
            f"ESP_SECTORS_PER_CLUSTER; both raise the cluster count."
        )


def check_gpt(path: str, esp_first: int) -> None:
    """Read the table back the way the firmware will: MBR, header, entry.

    Deliberately separate from the write. The failure this guards against is a disk
    that mtools likes and the firmware cannot boot, and only reading the table - not
    the filesystem - catches that.
    """
    with open(path, "rb") as fh:
        fh.seek(0x1BE)
        mbr_entry = fh.read(16)
        if mbr_entry[4:5] != b"\xEE":
            raise OpenCoreError(
                f"{path}: the MBR has no protective 0xEE entry (found "
                f"{mbr_entry[4]:#04x}); firmware will treat this as a plain "
                f"partitioned-less disk"
            )

        fh.seek(SECTOR_SIZE)
        head = bytearray(fh.read(SECTOR_SIZE))
        if head[0:8] != b"EFI PART":
            raise OpenCoreError(f"{path}: no GPT header at LBA 1")

        # The header's CRC is computed with its own field zeroed - that is the rule the
        # format states, and forgetting it was a bug here: the check compared a CRC
        # over a header that still contained the CRC, so a correct table failed.
        header_size = struct.unpack_from("<I", head, 12)[0]
        if not 92 <= header_size <= SECTOR_SIZE:
            raise OpenCoreError(
                f"{path}: the GPT header claims a size of {header_size} bytes"
            )
        stored = struct.unpack_from("<I", head, 16)[0]
        body = bytearray(head[:header_size])
        struct.pack_into("<I", body, 16, 0)
        actual = _crc32(bytes(body))
        if actual != stored:
            raise OpenCoreError(
                f"{path}: the GPT header CRC is {stored:#010x} but the header "
                f"computes to {actual:#010x}"
            )

        entries_lba = struct.unpack_from("<Q", head, 72)[0]
        fh.seek(entries_lba * SECTOR_SIZE)
        first_entry = fh.read(128)
        if first_entry[0:16] != ESP_TYPE_GUID:
            raise OpenCoreError(
                f"{path}: the first partition is not an EFI System Partition "
                f"(type {first_entry[0:16].hex()})"
            )
        start = struct.unpack_from("<Q", first_entry, 32)[0]
        if start != esp_first:
            raise OpenCoreError(
                f"{path}: the ESP starts at LBA {start}, expected {esp_first}"
            )
    log(f"boot.img: GPT verified, ESP type GUID at LBA {esp_first}")


def build(release: str, work: str) -> dict:
    """Everything the VM needs, from the pins, into `work`. Idempotent.

    Returns the paths and identity as a dict, so a caller never reconstructs a filename
    - the three paths are the contract between this module and macos/*/bake.py.
    """
    os.makedirs(work, exist_ok=True)

    archive = fetch_pinned("opencore", os.path.join(work, "opencore.zip"))
    template_iso = fetch_pinned("opencore-template",
                                os.path.join(work, "opencore-template.iso"))
    rom = fetch_pinned("ovmf-code", os.path.join(work, "macos.rom"))
    vars_src = fetch_pinned("ovmf-vars-1920x1080",
                            os.path.join(work, "OVMF_VARS-1920x1080.fd"))

    efi = unpack_opencore(archive, work)

    # macserial travels with the archive it describes; extract it once.
    tool = os.path.join(work, "macserial")
    if not os.path.exists(tool):
        subprocess.run(["unzip", "-q", "-j", archive, MACSERIAL, "-d", work],
                       check=True)
        found = os.path.join(work, os.path.basename(MACSERIAL))
        if not os.path.exists(found):
            raise OpenCoreError(
                f"{archive} holds no {MACSERIAL}; OpenCore's release layout changed"
            )
        os.replace(found, tool)
        os.chmod(tool, 0o755)

    ident = identity(release, work)

    # The template supplies kexts, drivers and device properties; its EFI tree is
    # merged into OpenCore's so there is one directory to copy to the boot image.
    template_efi = extract_template_boot(template_iso, work)
    efi = _merge_template(efi, template_efi)
    write_config(efi, template_efi, ident, release)

    # APFS AFTER the config is written, because this registers a driver in it - and
    # BEFORE the boot image is built, because the driver has to be inside that image.
    # The recovery image is an input here as well as the installer; see the function.
    recovery = recovery_image(work)
    install_apfs_driver(efi, recovery, work)

    boot = build_boot_image(efi, os.path.join(work, "boot.img"))

    # The vars file is a WRITABLE COPY per guest, made once and then reused: Windows
    # setup reboots inside one VM and macOS does too, and a fresh copy each boot loses
    # the boot entries the installed system just registered. This is the same rule
    # lib/vm.py states for firmware_vars.
    vars_copy = os.path.join(work, "macos.vars")
    if not os.path.exists(vars_copy):
        shutil.copyfile(vars_src, vars_copy)
        log("macos.vars: writable copy of the 1920x1080 template")

    return {
        "boot": boot,
        "rom": rom,
        "vars": vars_copy,
        "identity": ident,
        "efi": efi,
    }


def _merge_template(efi: str, template_efi: str) -> str:
    """Copy the template's OC subtree over OpenCore's, keeping OpenCore's binaries.

    The two archives disagree about which files are authoritative: OpenCorePkg has the
    newest OpenCore.efi and BOOTx64.efi, while the template has the kexts, the drivers
    and the config that actually boots macOS. OpenCore's own documentation is explicit
    that the bootloader and its config must come from the same release, so the binaries
    are taken from OpenCore and everything else from the template - which is what this
    does by copying the template first and OpenCore's two executables over it.
    """
    keep = {
        os.path.join("BOOT", "BOOTx64.efi"),
        os.path.join("OC", "OpenCore.efi"),
    }
    saved = {}
    for relative in keep:
        path = os.path.join(efi, relative)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                saved[relative] = fh.read()

    for root, _dirs, files in os.walk(template_efi):
        for name in files:
            source = os.path.join(root, name)
            relative = os.path.relpath(source, template_efi)
            target = os.path.join(efi, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, target)

    for relative, payload in saved.items():
        with open(os.path.join(efi, relative), "wb") as fh:
            fh.write(payload)

    # THE CHECK IS ON THE TEMPLATE, NOT ON THE RESULT.
    #
    # It used to look at `efi`, which is the destination - and OpenCore's own archive
    # ships empty OC/Kexts, OC/Drivers and OC/Resources directories. So the test passed
    # whenever the extraction happened at all, and a template that supplied nothing
    # would produce an image with an empty kext list and a guest that stops at a
    # prohibited sign. Non-empty matters as much as present: an empty directory is the
    # same failure with a file listing that looks right.
    for required in ("OC/Kexts", "OC/Drivers", "OC/Resources"):
        source = os.path.join(template_efi, required)
        if not os.path.isdir(source):
            raise OpenCoreError(
                f"the template supplied no {required}; the guest would boot to a "
                f"prohibited sign with nothing in any log to explain it"
            )
        if not os.listdir(source):
            raise OpenCoreError(
                f"the template's {required} is empty; the guest would boot to a "
                f"prohibited sign with nothing in any log to explain it"
            )

    # And the result must be able to boot: one loader the firmware can start and one
    # OpenCore the loader hands over to.
    for required in (("BOOT", "BOOTx64.efi"), ("OC", "OpenCore.efi")):
        path = os.path.join(efi, *required)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise OpenCoreError(
                f"{path} is missing or empty after the merge; the firmware has "
                f"nothing to start"
            )

    log("merged: template kexts/drivers/config + OpenCore's own bootloader")
    return efi


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--release", required=True,
                   help="release the identity is generated for: 14, 15, 26")
    p.add_argument("--out", required=True, help="work directory")
    args = p.parse_args(argv)

    try:
        result = build(args.release, args.out)
    except OpenCoreError as exc:
        log(f"FAILED: {exc}")
        return 1
    for key in ("boot", "rom", "vars"):
        print(f"{key:8} {result[key]}")
    print(f"serial   {result['identity']['serial']}")
    print(f"mac      {result['identity']['mac']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
