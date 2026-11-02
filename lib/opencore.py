"""The three assets macOS needs beyond a disk, fetched by pin and built once.

    python3 lib/opencore.py --out work/macos15

    work/macos15/opencore/EFI/...        the payload OpenCore boots
    work/macos15/boot.img                that payload as a disk the firmware can start
    work/macos15/macos.rom               OVMF code
    work/macos15/macos.vars              OVMF variables, 1920x1080, writable copy
    work/macos15/identity.json           serial, board serial, UUID, MAC

WHY THIS IS NOT COPIED WHOLESALE

dockur/macos builds all of this at container start: it unpacks OpenCore, extracts a
template image, generates a serial with macserial, rewrites config.plist with
xmlstarlet, and lays down a FAT image with mtools. Every step of that is a decision
this repository would otherwise inherit silently, and three of them are worth stating:

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

Building it needs `unzip`, `mtools` and `xmlstarlet` - all present in the bake image -
and takes about two seconds. The alternative is committing a 300 MiB binary to the
repository, which is the thing this project exists to avoid: the disk is the artifact,
and everything else is derived. A cache keyed by the pins is enough to keep a re-run
fast without pretending the assets are inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import images  # noqa: E402

# The SMC key. Every hackintosh since 2009 has used this string; it is in
# OSX-KVM, in dockur/macos and in Apple's own diagnostic tooling. It is not a
# credential - it is the value macOS reads from the SMC device to decide whether
# the firmware is Apple's.
OSK = ("ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc")

# The disk geometry of boot.img, measured from a working dockur build: a 200 MiB
# image with one FAT partition for the 512-byte sector count mtools is told about.
BOOT_IMG_MB = 200
BOOT_IMG_SECTOR = 512
BOOT_IMG_SECTORS = (BOOT_IMG_MB * 1024 * 1024) // BOOT_IMG_SECTOR

# How a serial pair is made: macserial ships inside OpenCore's release archive under
# Utilities/, so the generator travels with the payload it describes rather than being
# a second download that can drift from it.
MACSERIAL = "Utilities/macserial/macserial.linux"


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
    """Download one pinned asset, or reuse it, and verify the sha256 either way.

    A mismatch is fatal and says which pin moved. That direction matters: an OpenCore
    or an OVMF that silently upgraded would produce a disk that boots today and stops
    booting when the firmware changes its GOP mode or its variable layout.
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
    log(f"{name}: fetching {pin['url']}")
    tmp = dest + ".part"
    # urllib rather than curl: this is one plain HTTPS GET of a static file, and
    # lib/iso.py already explains why the *recovery* endpoint needed curl (a
    # self-signed chain). Nothing here does.
    with urllib.request.urlopen(pin["url"], timeout=120) as response, \
            open(tmp, "wb") as fh:
        shutil.copyfileobj(response, fh, length=1 << 20)

    got = _sha256(tmp)
    if got != pin["sha256"]:
        os.unlink(tmp)
        raise OpenCoreError(
            f"{name}: sha256 {got} but the pin says {pin['sha256']}.\n"
            f"Either the URL now serves a different build or the pin is stale.\n"
            f"Re-derive it from {pin['url']} deliberately - do not paste it in."
        )
    os.replace(tmp, dest)
    log(f"{name}: {os.path.getsize(dest) >> 20} MiB, sha256 verified")
    return dest


# --- the payload -------------------------------------------------------------


def unpack_opencore(archive: str, work: str) -> str:
    """The EFI tree inside OpenCore's release zip, as a directory.

    The archive holds X64/EFI/ and IA32/EFI/; only X64 is used, and only the EFI tree -
    the sample configs and the documentation sit beside it and would go into the boot
    image otherwise.
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
    for required in ("BOOT/BOOTx64.efi", "OC/OpenCore.efi", "OC/Config.plist"):
        if not os.path.exists(os.path.join(efi, required)):
            raise OpenCoreError(f"{archive} holds no {required}; the archive layout changed")
    return efi


def extract_template_boot(template_iso: str, dest: str) -> str:
    """The EFI tree from the LongQT template image, which is not a plain ISO9660.

    WHY THIS EXISTS AT ALL. OpenCore's own archive is the bootloader and nothing else:
    it has no kexts, no drivers and no config that boots macOS. The template supplies
    the parts specific to this job - Lilu, VirtualSMC, WhateverGreen, the APFS driver,
    the SMC and GPU device properties, the boot-args - and upstream's config.plist.

    The image is read with 7z, which walks both the ISO9660 filesystem and the Apple
    partition map inside it, and which is in the bake image for the Windows ISO work.
    """
    out = os.path.join(dest, "template")
    if os.path.isdir(out):
        return out
    os.makedirs(out, exist_ok=True)
    subprocess.run(["7z", "x", "-y", f"-o{out}", template_iso], check=True,
                   stdout=subprocess.DEVNULL)
    # The tree arrives under a directory whose name comes from the volume label, so it
    # is found rather than assumed.
    for root, dirs, _files in os.walk(out):
        if "OpenCore.efi" in _files:
            return root
        if os.path.basename(root).lower() == "oc" and "EFI" in os.path.basename(
                os.path.dirname(root)):
            return os.path.dirname(os.path.dirname(root))
    raise OpenCoreError(
        f"{template_iso} holds no OpenCore.efi; the template layout changed. "
        f"Looked under {out}."
    )


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
    would throw those away; only the identity fields and the two settings this bake
    depends on are changed:

        SystemSerialNumber, MLB, ROM, SystemUUID   the machine identity
        csr-active-config = 0x7f                   SIP off, which the sandbox image
                                                   needs for its TCC and xcode work
        boot-args                                 keeps the serial console visible

    The edit is done with xmlstarlet rather than a plist library: plists are XML here,
    xmlstarlet is in the bake image, and a text rewrite of a binary plist is the kind
    of thing that appears to work until the file is 200 bytes shorter than it should be.
    """
    source = os.path.join(template_efi, "OC", "Config.plist")
    target = os.path.join(efi, "OC", "Config.plist")
    if not os.path.exists(source):
        raise OpenCoreError(f"the template has no config at {source}")

    # ROM is six raw bytes of the MAC, as <data>, not as a string of hex digits.
    rom_hex = ident["mac"].replace(":", "")
    rom_b64 = __import__("base64").b64encode(bytes.fromhex(rom_hex)).decode()

    edits = [
        # (path in the plist, value, is_data)
        ("PlatformInfo/Generic/SystemSerialNumber", ident["serial"], False),
        ("PlatformInfo/Generic/MLB", ident["board_serial"], False),
        ("PlatformInfo/Generic/SystemUUID", ident["uuid"], False),
        ("PlatformInfo/Generic/ROM", rom_b64, True),
        ("NVRAM/Add/7C436110-AB2A-4BBB-A880-FE41995C9F82/csr-active-config",
         "fwAAAE8=", True),   # 0x7f000000 little-endian: SIP off
    ]

    shutil.copyfile(source, target)
    for path, value, is_data in edits:
        result = subprocess.run(
            ["xmlstarlet", "ed", "-L",
             "-u", f"/plist/dict/{_plist_path(path)}",
             "-v", value, target],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise OpenCoreError(
                f"xmlstarlet could not set {path} in {target}:\n"
                f"{result.stderr.strip()[:400]}"
            )
    log(f"config written: serial, board serial, UUID, ROM, SIP off ({release})")
    return target


def _plist_path(path: str) -> str:
    """`A/B/C` -> the plist key chain xmlstarlet wants.

    A plist dict is a flat list of <key>/<value> pairs, so the nth key is the (2n+1)th
    child. The translation is mechanical and worth doing in one place rather than
    spelling out key[1]/key[3]/key[5] at five call sites.
    """
    parts = path.split("/")
    steps = []
    for index in range(len(parts)):
        steps.append(f"key[{index * 2 + 1}]")
        steps.append(f"following-sibling::*[{1}]")
    # The last "sibling" step lands on the value, so the chain is the keys interleaved
    # with the hops between them.
    return "/".join(steps[:-1])


# --- the disk the firmware boots ---------------------------------------------


def build_boot_image(efi: str, dest: str) -> str:
    """A raw FAT image with the EFI tree at its root, which is what OpenCore boots from.

    mtools rather than a loop mount: mounting needs privileges a container job does not
    have, and mformat/mcopy write the same filesystem without them. dockur/macos does
    exactly this and records the one non-obvious part - the cluster and sector counts
    have to be consistent with each other or mcopy reports a full disk on a 200 MiB
    image that is empty.
    """
    if os.path.exists(dest):
        os.unlink(dest)
    with open(dest, "wb") as fh:
        fh.truncate(BOOT_IMG_MB * 1024 * 1024)

    geometry = [
        "-F",                       # a full disk, not a partition image
        "-i", dest,
        "-c", "8",                  # 8 sectors per cluster: small enough for many files
        "-T", str(BOOT_IMG_SECTORS),
        "-v", "EFI",
    ]
    subprocess.run(["mformat", *geometry], check=True)

    subprocess.run(["mcopy", "-s", "-i", dest, "-m", efi, "::/"], check=True)
    # The template's drivers and kexts sit beside OpenCore.efi, so the whole EFI tree
    # above was copied; nothing is filtered, because a missing kext is a guest that
    # stops at a prohibited sign with no explanation in any log.
    listing = subprocess.run(["mdir", "-i", dest, "-/", "::/"], capture_output=True,
                             text=True)
    if listing.returncode != 0 or "OpenCore.efi" not in listing.stdout:
        raise OpenCoreError(
            f"{dest} does not hold OpenCore.efi after mcopy:\n{listing.stdout[:600]}"
        )
    log(f"boot.img: {os.path.getsize(dest) >> 20} MiB, EFI tree copied")
    return dest


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

    for required in ("OC/Kexts", "OC/Drivers", "OC/Resources"):
        if not os.path.isdir(os.path.join(efi, required)):
            raise OpenCoreError(
                f"the template supplied no {required}; the guest would boot to a "
                f"prohibited sign with nothing in any log to explain it"
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
