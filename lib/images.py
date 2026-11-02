"""The installer images this repository bakes from, and where they come from.

One table, used by two things that must not disagree:

    lib/publish_iso.py       downloads and verifies them, then the workflow pushes
                             each to ghcr as :iso-<tag>
    windows/11/bake.py       pulls :iso-<tag> back instead of downloading

WHY THE TABLE IS SHARED RATHER THAN DUPLICATED

A bake that downloads its own image from the vendor and a publish workflow that
downloads "the same" image are two places that can drift. When they drift, the bake
verifies one file and installs another, and the symptom is a Windows build number
that does not match the pin - which nothing else would notice.

WHY WINDOWS IS DIFFERENT FROM THE LINUX DISTRIBUTIONS

Linux ISOs are redistributable, so publish-iso.yml can cache them indefinitely and
lib/iso.py refuses to fall back to a mirror on principle. A Windows ISO cannot be
redistributed at all: putting it in this repository's package is a private cache, not
a publication, and Microsoft rotates the URL and the file with every release.

That rotation is why every Windows entry carries an exact sha256 AND size. When
Microsoft publishes a new build:

    publish_iso.py     fails with "the vendor has probably published a new build"
    bake.py            fails at the same check, on the cached copy

Neither silently installs something different, which is the only failure mode that
matters here - an ISO one build newer installs a different edition at the same index,
and nothing downstream reads the edition except lib/wim.py.

WHERE THE PINS CAME FROM

dockur/windows src/define.sh::getMido, which is the project that keeps these current.
The URLs are Microsoft's own static download hosts, not a mirror, and the hashes are
the ones that repository verifies against.
"""

from __future__ import annotations


def tag(name: str) -> str:
    """The ghcr tag an image is published under, in this repository's package.

    The prefix says what kind of artifact it is, because the tag namespace is shared:

        iso-<name>          an installer image (Windows, Linux)
        sysimg-<name>       an Android system image
        macos-recovery-<n>  a macOS recovery image
        <nothing>           the bake image itself, at :latest

    A caller can pass a full tag with a colon, which is how the non-iso kinds are
    published without every name needing to be classified here.
    """
    if ":" in name:
        return name
    return f"iso-{name}"


# Windows install media. `index` is the /IMAGE/INDEX the answer file selects, and it
# is verified against the WIM's own metadata before the guest boots - see lib/wim.py
# for why an off-by-one there is invisible everywhere else.
WINDOWS = {
    "windows11": {
        "url": "https://software-static.download.prss.microsoft.com/dbazure/"
               "888969d5-f34g-4e03-ac9d-1f9786c66749/"
               "26200.6584.250915-1905.25h2_ge_release_svc_refresh_CLIENT_CONSUMER_x64FRE_en-us.iso",
        "sha256": "d141f6030fed50f75e2b03e1eb2e53646c4b21e5386047cb860af5223f102a32",
        "size": 7736125440,
        "index": 6,
        "edition": "Windows 11 Pro",
        # "Pro" is a substring of "Pro N". Without this the check passes on an index
        # that installs the edition people actually complain about.
        "forbid": " N",
        "wim_index": 6,
    },
    "windows11-enterprise-eval": {
        "url": "https://software-static.download.prss.microsoft.com/dbazure/"
               "888969d5-f34g-4e03-ac9d-1f9786c66749/"
               "26200.6584.250915-1905.25h2_ge_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso",
        "sha256": "a61adeab895ef5a4db436e0a7011c92a2ff17bb0357f58b13bbc4062e535e7b9",
        "size": 7092807680,
        "index": 1,
        "edition": "Windows 11 Enterprise",
        "forbid": None,
        "wim_index": 1,
    },
}

# virtio-win: the drivers without which setup cannot see the disk it is installing to.
# The failure without this CD is a pane with an empty disk list, which the answer
# file's WillShowUI=OnError would show as a dialog nobody is watching.
#
# Not from Microsoft: qemus/virtiso-whql republishes the upstream virtio-win ISO under
# a version tag, which is what dockur/windows consumes for the same purpose.
# No sha256: the tag pins the release and setup itself fails loudly if the drivers are
# wrong for the guest. The size is recorded so a truncated download is caught.
VIRTIO = {
    "virtio-win": {
        "url": "https://github.com/qemus/virtiso-whql/releases/download/v1.9.61-0/"
               "virtio-win-1.9.61.iso",
        "sha256": None,
        "size": None,
    },
}

# Linux installer images. Redistributable, so these are a real cache rather than a
# private one: publish-iso.yml fetches each once and every bake pulls it back.
#
# These have no sha256 because the distributions rotate their mirrors and their
# filenames carry the version, which is the pin. The ISO9660 check in
# publish_iso.py is what catches a mirror serving an error page.
LINUX = {
    "mx": {
        # The file is MX-25.3_Xfce_x64.iso, not MX-25.3_x64.iso: the edition is part
        # of the name. The earlier value returned 404 and would have failed the first
        # --all run.
        #
        # No sha256 and no size: SourceForge redirects to a per-request CDN URL with
        # an expiring token, so the bytes are the same but nothing about the response
        # is stable. The ISO9660 check in publish_iso.py is what catches a bad one.
        "url": "https://sourceforge.net/projects/mx-linux/files/Final/Xfce/"
               "MX-25.3_Xfce_x64.iso/download",
        "sha256": None,
        "size": None,
    },
    "alpine": {
        "url": "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/"
               "alpine-virt-3.21.3-x86_64.iso",
        # Measured with a HEAD; the filename carries the version, which is the pin.
        "sha256": None,
        "size": 66060288,
    },
}


# macOS recovery images are NOT in this table, because they have no fixed URL.
#
# Apple issues one per request: a board identifier selects the release, a session
# cookie authorises the exchange, and the reply carries a CDN URL plus an expiring
# token that is sent as a Cookie header. There is nothing to pin. They are fetched by
# lib/macos_recovery.py, which drives that exchange, and published as
# :macos-recovery-<name>.
MACOS_RELEASES = ["14", "15", "26"]


# The macOS boot assets. Four files, no recovery image, and the distinction matters:
#
#     the recovery image   Apple issues one per request. No fixed URL, no pin, and
#                          lib/macos_recovery.py drives the exchange. Published as
#                          :macos-recovery-<name> by its own workflow.
#
#     these four           fixed, versioned URLs. They are the files that make the
#                          firmware and OpenCore able to boot ANY macOS; the recovery
#                          image is what they boot. Pinned by sha256 like the Windows
#                          ISO, and for the same reason: a silent upgrade here is a
#                          disk that boots today and stops booting when OpenCore
#                          changes its plist schema or OVMF changes its GOP mode.
#
# WHY THEY ARE FOUR AND NOT ONE
#
# OpenCore's own release archive is the bootloader and nothing else: BOOTx64.efi and
# OpenCore.efi, with no kexts, no drivers and no config. A Mac has no such problem
# because the firmware is Apple's; here the missing half is Lilu, VirtualSMC,
# WhateverGreen, the VoodooPS2 input drivers and the device properties that were
# arrived at by somebody else's runs. Those live in LongQT-sea's template ISO, which
# is a bootable image rather than an archive - lib/opencore.py extracts it with 7z,
# which walks both its ISO9660 filesystem and the Apple partition map inside it.
#
# OVMF is the third thing and it is TWO files: the code, which is immutable, and the
# variable store, which is not. The variables template carries the GOP resolution
# baked into it, so a disk installed against the 1920x1080 template does not boot
# against the 1024x768 one. They come from kholia/OSX-KVM at a commit, not from the
# distribution's `ovmf` package, because that package's OVMF_VARS is a generic one
# whose default mode is whatever the build chose - and this bake's coordinates are
# measured at 1920x1080.
#
# WHAT IS DELIBERATELY NOT HERE
#
# apfs.efi. OpenCore's archive has no APFS driver and neither does the template, so a
# freshly installed disk is not readable by the firmware that installed it. macOS drops
# one into the installed system's EFI partition while installing - so the fix is to
# copy it out of the finished disk and into boot.img, which is a step of the bake and
# not an input to it. dockur-macos/scripts/fix-opencore-apfs.sh documents the same
# finding: "EnableJumpstart does not supply a driver, it hooks one that the firmware is
# expected to already have. OVMF has none."
MACOS_ASSETS = {
    # The bootloader. Also carries Utilities/macserial/macserial.linux, which
    # generates the serial and board serial - a hand-rolled one fails Apple's
    # checksum and macOS stops booting when the NVRAM remembers it.
    "opencore": {
        "url": "https://github.com/acidanthera/OpenCorePkg/releases/download/1.0.7/"
               "OpenCore-1.0.7-RELEASE.zip",
        "sha256": "2ffab6ebf58c7aefb0bcb3a1a385d207746823d6dd87d44bd666e1286939943e",
        "size": 10437696,
        "kind": "zip",
    },
    # The other half: kexts, drivers, resources and the config.plist the guest boots.
    "opencore-template": {
        "url": "https://github.com/LongQT-sea/OpenCore-ISO/releases/download/v0.7/"
               "LongQT-OpenCore-v0.7.iso",
        "sha256": "287328995d4198f1b05166f087d85bf7ef66bedafe150d17ad112ac8de60051d",
        "size": 15884288,
        "kind": "iso9660",
    },
    # UEFI. OVMF_CODE.fd is read-only pflash; OVMF_VARS is the guest's NVRAM and is
    # copied per work directory before anything writes to it.
    "ovmf-code": {
        "url": "https://raw.githubusercontent.com/kholia/OSX-KVM/"
               "326053dd61f49375d5dfb28ee715d38b04b5cd8e/OVMF_CODE.fd",
        "sha256": "52a625db5a45329bf8005232db33e94781787d92ae8d088f2f73332dd4ded3a9",
        "size": 1966080,
        "kind": "firmware",
    },
    "ovmf-vars-1920x1080": {
        "url": "https://raw.githubusercontent.com/kholia/OSX-KVM/"
               "326053dd61f49375d5dfb28ee715d38b04b5cd8e/OVMF_VARS-1920x1080.fd",
        "sha256": "6ed987af3a3c155be71665f510eae3e007eda9b8b94afd59d45e91c4a11565cc",
        "size": 131072,
        "kind": "firmware",
    },
}


# Android system images. Not an installer and not an ISO: Google ships a zip whose
# contents are the disks themselves (system.img, vendor.img, userdata.img) plus the
# kernel and ramdisk the emulator boots them with.
#
# WHY THIS IS IN THE TABLE AT ALL
#
# The system image is the only input an Android bake has. Fetching it from
# dl.google.com at bake time would be the same mistake the Windows ISO had: an
# unpinned dependency fetched on the critical path, where an upstream change surfaces
# as a guest that does not boot rather than as a version that changed.
#
# The sha1 is Google's own, from sys-img2-3.xml. It is a 32-bit hash and weaker than
# the sha256 used for Windows; it is what the publisher provides, and it still catches
# a truncated or replaced download, which is what the check is for.
ANDROID = {
    "sysimg:android-15-aosp": {
        "url": "https://dl.google.com/android/repository/sys-img/android/"
               "x86_64-35_r02.zip",
        "sha1": "2d857d170c0d1b827149565da34b3383e5306f7f",
        "sha256": None,
        "size": 782_336_000,
        # api-level, not marketing version: 35 is Android 15.
        "api": 35,
        "abi": "x86_64",
        "flavour": "default",
    },
}


def all_images() -> dict:
    """Every pinned image, keyed by the tag it is published under."""
    merged = {}
    for table in (WINDOWS, VIRTIO, LINUX, ANDROID):
        for name, pin in table.items():
            merged[tag(name)] = pin
    return merged


def all_macos_assets() -> dict:
    """Every pinned macOS boot asset, keyed by the tag it is published under."""
    return {macos_asset_tag(name): pin for name, pin in MACOS_ASSETS.items()}


def macos_asset_tag(name: str) -> str:
    """The ghcr tag a macOS boot asset is published under.

    `macos-asset-<name>`, alongside `macos-recovery-<name>` and `iso-<name>`. One
    package, one namespace, and the prefix says which of the three kinds it is - so a
    reader who sees `macos-asset-ovmf-code` in a package listing knows both what it is
    and that it is not a recovery image.
    """
    return f"macos-asset-{name}"


def pin_for_bake(image: str) -> dict:
    """The Windows pin a bake of `image` should use."""
    if image not in WINDOWS:
        raise KeyError(
            f"unknown image {image!r}; known: {', '.join(sorted(WINDOWS))}"
        )
    return WINDOWS[image]


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "tag":
        print(tag(sys.argv[2]))
    else:
        for name, pin in all_images().items():
            size = pin.get("size")
            print(f"{name:32} {size if size else 'size unpinned':>16}  {pin['url'][:60]}")
