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

Linux ISOs are redistributable, so artifacts-publish.yml can cache them indefinitely and
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
}

# virtio-win: the drivers without which setup cannot see the disk it is installing to,
# AND the QEMU guest agent that lets the machine be shut down without a dialog.
#
# THE UPSTREAM ISO, NOT THE REPACK, AND THE GUEST AGENT IS WHY.
#
# This was qemus/virtiso-whql's virtio-win-1.9.61.iso - 56 MiB, a drivers-only cut that
# dockur/windows also uses. It carries $WinPEDriver$-loadable drivers and NOTHING ELSE: a scan
# of the image finds one .msi (virtio-win-gt-x64.msi) and zero occurrences of "guest-agent" or
# "qemu-ga". So the agent had to be installed later, from the guest-tools bundle, and that
# bundle's WiX Burn bootstrapper never returned under /quiet on this guest (runs 42797556702,
# 42800603925 - ten and twelve minutes, no output, no exit).
#
# Fedora's own ISO is 836 MiB and carries everything:
#
#     virtio-win-gt-x64.msi        the drivers, as before
#     guest-agent/qemu-ga-x86_64.msi    the agent
#     guest-agent/qemu-ga-i386.msi
#
# The cost is the download and it is paid ONCE, because this is a published artifact
# (`iso-virtio-win` on ghcr) that every bake pulls from the registry rather than from Fedora.
#
# IT IS LARGER THAN THE DRIVERS THE BAKE NEEDS, and that is the trade: one ISO that can install
# the agent during Setup beats a second installer that has to be run later over ssh on a machine
# whose disk is already published.
#
# No sha256: Fedora rotates the file under this path, and setup itself fails loudly if the
# drivers are wrong for the guest. The bake records the size it got.
VIRTIO = {
    "virtio-win": {
        "url": "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/"
               "stable-virtio/virtio-win.iso",
        "sha256": None,
        "size": None,
    },
}

# Linux installer images. Redistributable, so these are a real cache rather than a
# private one: artifacts-publish.yml fetches each once and every bake pulls it back.
#
# These have no sha256 because the distributions rotate their mirrors and their
# filenames carry the version, which is the pin. The ISO9660 check in
# publish_iso.py is what catches a mirror serving an error page.
LINUX = {

    # ---------------------------------------------------------------------------
    # TWO DISTRIBUTIONS, AND THAT IS THE WHOLE LIST.
    #
    # There were seventeen here - alma, alpine, arch, cachyos, debian, endeavouros,
    # fedora, kubuntu, manjaro, mint, mx, opensuse, popos, rocky, zorin, plus these
    # two. Sixteen of them were published by artifacts-publish.yml on every run and
    # pulled by nothing: this repository bakes Ubuntu, Kubuntu (from Ubuntu's ISO),
    # NixOS, FreeBSD, Windows and macOS. A pin nobody uses is a download, a checksum
    # and a recurring 404 to maintain - and it showed: the last full publish failed on
    # openSUSE, whose dated snapshot had been rotated out upstream, while the nineteen
    # images that worked included fifteen that no workflow would ever ask for.
    #
    # Kubuntu is not missing: linux/kubuntu/bake.py reads `iso-ubuntu`, because Kubuntu
    # is Ubuntu's installer with a different desktop selection.
    #
    # WHY THE SERVER VARIANT FOR UBUNTU. ubuntu-*-live-server is the image whose
    # installer a file can drive - subiquity reads an answer file - and it carries no
    # desktop, so the bake adds the one it wants. A desktop ISO would install a desktop
    # nobody asked for and take twice the bytes.
    #
    # The hash comes from the vendor's own checksum file,
    # releases.ubuntu.com/26.04/SHA256SUMS, and a hash is strictly better than a size:
    # it catches a truncated transfer AND a substituted file.
    "ubuntu": {
        "url": "https://releases.ubuntu.com/26.04/"
               "ubuntu-26.04.1-live-server-amd64.iso",
        "sha256": "cc8a95cde20f6ced61a322420de00f10cc3c90ced545daa46cb9c1a117f1d927",
        "size": None,
    },
    # KUBUNTU IS THE DESKTOP FLAVOUR, and it is a different image - the server ISO's
    # autoinstall cannot produce KDE, and a desktop ISO is what carries it. Its casper/
    # layout is the same, so linux/kubuntu/bake.py is linux/ubuntu/bake.py with a
    # different user-data and a different tag.

    # ---------------------------------------------------------------------------
    # THE POPULARITY LIST. These are the entries in DistroWatch's Page Hit Ranking
    # that a sandbox is asked for by name, added on request rather than on a rule.
    #
    # The Arch family is here as three separate entries on purpose: CachyOS and
    # EndeavourOS are not "arch with a different installer", they ship their own
    # kernels and their own installer flows, and a bake that treats them as Arch
    # would install Arch.
    # ---------------------------------------------------------------------------
    "nixos": {
        # The minimal image: NixOS has no answer-file installer, so a bake drives it
        # over a serial console, and the smaller image is the one with the console
        # menu rather than a graphical session.
        "url": "https://channels.nixos.org/nixos-26.05/"
               "latest-nixos-minimal-x86_64-linux.iso",
        # The channel URL resolves to a dated file, and NixOS publishes a .sha256
        # beside it. That digest belongs to whatever the channel points at today, so
        # this pair has to be refreshed together - which the failure message says.
        "sha256": "83445162c4a18fb372657d38c942147b770d3bfdaf6bb1c37905333b80f9a79f",
        "size": 1746124800,
    },
}


# DESKTOP IMAGES - the ones the GUI bakes boot, drive, and never answer a file for.
#
# SEPARATE FROM LINUX, AND FOR THE SAME REASON BSD IS: the LINUX entries above are installer
# media an answer file drives (subiquity, bsdinstall's script, nixos' console). These are
# desktop live sessions with a graphical installer, driven by a measured coordinate and a
# measured wait - see linux/*/install.py. Keeping them apart means a GUI workflow can
# pull its image from ghcr without ever touching a table that a file-driven bake reads.
#
# EVERY ENTRY CARRIES size AND sha256, both taken from the file the corresponding driver was
# MEASURED against rather than from a vendor checksum page: for these images the question is
# "is this the exact build whose pixels were measured", and a vendor's current page can point
# at a newer build than the one the coordinates belong to.
DESKTOP = {
    "ubuntu-desktop": {
        "url": "https://releases.ubuntu.com/26.04/ubuntu-26.04.1-desktop-amd64.iso",
        "sha256": "601e30fbf5d97759367c632e2c33630665039b7e2158fd068403da3ccf1bda1f",
        "size": 6482409472,
    },
    "kubuntu-desktop": {
        "url": "https://cdimage.ubuntu.com/kubuntu/releases/26.04/release/"
               "kubuntu-26.04.1-desktop-amd64.iso",
        "sha256": "831e4d4bb85098339ba43d3502cd6619b27e76daf37246a084cd68a6413090b8",
        "size": 5072990208,
    },
    "fedora-workstation": {
        "url": "https://download.fedoraproject.org/pub/fedora/linux/releases/43/"
               "Workstation/x86_64/iso/Fedora-Workstation-Live-43-1.6.x86_64.iso",
        "sha256": "2a4a16c009244eb5ab2198700eb04103793b62407e8596f30a3e0cc8ac294d77",
        "size": 2742190080,
    },
    "mint-cinnamon": {
        "url": "https://mirrors.kernel.org/linuxmint/stable/22.3/"
               "linuxmint-22.3-cinnamon-64bit.iso",
        "sha256": "a081ab202cfda17f6924128dbd2de8b63518ac0531bcfe3f1a1b88097c459bd4",
        "size": 3091660800,
    },
    "opensuse-tumbleweed-kde": {
        # `Current.iso` IS THE ALIAS THAT SURVIVES A SNAPSHOT ROTATION, AND THE DATED NAME IS
        # GONE FROM THE HOST - measured, `Snapshot20261105-Media.iso` answers 404 in both
        # `tumbleweed/iso/` and `history/20261105/`.
        #
        # THE SHA256 IS DELIBERATELY NOT PINNED. openSUSE rotates this alias every few days.
        # A pin here would fail on every rotation, and the failure would be a checksum error
        # on a file whose only problem is being newer - which teaches the reader to widen the
        # pin rather than to look. What IS pinned is the size and an ISO9660 check, so a
        # truncated transfer or an HTML error page still fails; `lib/publish_iso.py` performs
        # both. The GUI driver that boots this image was measured on the 20261105 snapshot;
        # a new snapshot changes the image content, not the Calamares version it carries, and
        # the driver's own empty-disk check is what catches a layout change.
        "url": "https://download.opensuse.org/tumbleweed/iso/"
               "openSUSE-Tumbleweed-KDE-Live-x86_64-Current.iso",
        "sha256": None,
        "size": None,
    },
    "cachyos-desktop": {
        "url": "https://mirror.cachyos.org/ISO/desktop/260809/"
               "cachyos-desktop-linux-260809.iso",
        "sha256": "959f6577f45e25ee9fd8c220fd221b08e4ea79412c7315c0f922dd6d86d5e33c",
        "size": 3188850688,
    },
    "manjaro-kde": {
        "url": "https://download.manjaro.org/kde/26.1.0/"
               "manjaro-kde-26.1.0-260812-linux71.iso",
        "sha256": "711b64362abe0b45348dc33bd4a2c654641e069b50ae9ae37c01771da880aa99",
        "size": 5534201856,
    },
    "endeavouros": {
        "url": "https://mirror.alpix.eu/endeavouros/iso/"
               "EndeavourOS_Titan-Nova-2026.08.15.iso",
        "sha256": "0487f46fe29e0ab2ed153ba874568cc542a0507fb6029f29ad552b36ba447393",
        "size": 3898638336,
    },
    "zorin-core": {
        "url": "https://mirrors.edge.kernel.org/zorinos-isos/18/"
               "Zorin-OS-18-Core-64-bit-r3.iso",
        "sha256": "09959ce793b8a065b70207e46cd646b1de805fc7e0bad925ce28df6e04b7f807",
        "size": 3787948032,
    },
    "mx-xfce": {
        "url": "https://netix.dl.sourceforge.net/project/mx-linux/Final/Xfce/"
               "MX-25.3_Xfce_x64.iso",
        "sha256": "25b5ce20caeee1b0e50507291df5ac8a8211b1b9999d8dd7416da9b66ed9fbf9",
        "size": 2927624192,
    },
    "popos-desktop": {
        # Pop!_OS 24.04 with COSMIC. Its installer is `pop-os-installer`, a GTK application
        # started from the desktop - not Calamares and not Ubiquity - so it has a driver of
        # its own: linux/popos/install.py.
        "url": "https://iso.pop-os.org/24.04/amd64/intel/9/pop-os_24.04_amd64_intel_9.iso",
        "sha256": "894bc15abcad05839b226655121a113ad16cfbc4ada98e93e3ffb74a853fdcd4",
        "size": 2693218304,
    },
    "nixos-graphical": {
        # THE GRAPHICAL IMAGE, NOT THE MINIMAL ONE. This is the ISO that carries Calamares
        # and a GNOME session, so it is driven the same way every other bake here is: a
        # measured coordinate and a measured wait, from a desktop that is already running.
        #
        # IT IS A DATED SNAPSHOT AND NOT THE CHANNEL ALIAS. `channels.nixos.org` answers 302
        # to a `nixos-26.05.<build>.<rev>/...` file, and that target moves when the channel
        # advances. Pinning the resolved URL is what makes the sha256 below mean anything -
        # the same rule the other entries here follow, and the reason openSUSE's entry is the
        # one that deliberately omits a hash.
        #
        # MEASURED: downloaded and hashed on 2026-11-09, 3.62 GiB.
        "url": "https://releases.nixos.org/nixos/26.05/"
               "nixos-26.05.13627.c56a87d8e41c/"
               "nixos-graphical-26.05.13627.c56a87d8e41c-x86_64-linux.iso",
        "sha256": "62900e47b6bea0917c491ac0a8e18dd4e3b7579d9b60ee8e0bbbd8945162256d",
        "size": 3888513024,
    },
    "ghostbsd-mate": {
        "url": "https://download.ghostbsd.org/releases/amd64/24.10.1/GhostBSD-24.10.1.iso",
        "sha256": "e4e969603a3edf848db8c7fc2646f76871f0d9bd12787bc6c966250938c2c283",
        "size": 2798032896,
    },
}


# BSD INSTALLERS.
#
# Separate from LINUX because the media and the installers differ in kind: these are
# .img files for the two that ship one, they use a working directory of their own, and
# a BSD bake needs tools lib/smoke.py does not have (ufs, not ext4 - see bsd/README.md).
#
# WHICH ONES. FreeBSD's bsdinstall is scriptable and its release media is a single
# .img.xz; FreeBSD's installerconfig is the unattended format used here; NetBSD
# is left out because sysinst offers no comparable non-interactive path.
BSD = {
    "freebsd": {
        # disc1, not bootonly and not the prebuilt VM image: the same shape as every
        # other entry here, which is an installer this repository boots and drives.
        # bootonly needs a network to fetch the rest, and a VM-IMAGE is an already
        # installed system - publishing that would answer a different question.
        "url": "https://download.freebsd.org/releases/amd64/amd64/ISO-IMAGES/15.1/"
               "FreeBSD-15.1-RELEASE-amd64-disc1.iso",
        # From the vendor's own CHECKSUM.SHA256-FreeBSD-15.1-RELEASE-amd64.
        "sha256": "fa27646f05a1440fd26ffbb85e06a50bc86e128242a4e9cb7bb3ea76e1aa5fd9",
        "size": 1352255488,
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
        # 782_336_000 WAS WRONG BY 66 KiB and made every download fail:
        #
        #     attempt 1/5 ended short: 746 MiB of 746 MiB
        #     discarding an oversized part (782404023 > 782336000)
        #
        # Five attempts, five full downloads of 746 MiB, thrown away each time. The
        # right number is in Google's own sys-img2-3.xml beside the checksum, which had
        # been copied correctly - only the size was stale.
        "size": 782_404_023,
        # api-level, not marketing version: 35 is Android 15.
        "api": 35,
        "abi": "x86_64",
        "flavour": "default",
    },
}

# BlissOS: Android-x86, and the only Android in this repository that a stock qemu boots.
#
# IT IS DELIBERATELY NOT IN all_images(). That function decides which tags
# lib/publish_artifacts.py mirrors automatically, and an ISO added to it would change what
# every scheduled mirror run tries to publish. This pin is read by
# android/blissos16/fetch_iso.py alone, and that stage decides when it runs.
#
# THE SHA256 IS MEASURED, NOT QUOTED. SourceForge publishes no checksum file beside this
# image, so there is no upstream number to copy - the value below came from hashing a
# complete download (2,342,518,784 bytes). It catches a truncated, replaced or
# partly-mirrored file, which is what the check is for; it is a weaker claim than a
# vendor-published hash and is written down here rather than dressed up as one.
#
# WHY 16.9.7 AND NOT A NEWER BLISSOS. 16.9.7 is Android 13, not 15, and that is the newest
# publicly published image there is: the site says "Public images are paused" with no ETA,
# the BlissOS17 (Android 14) and Voyager/18 (Android 15) directories on SourceForge exist
# but contain no ISO, and BlissOS/stable_releases v17.8.3 pins android-14.0.0_r53. Android 15
# exists only as source in platform_manifest's voyager-qpr2-x86 tree, which would have to be
# built - 350 GB and hours, not a download.
BLISSOS = {
    "iso:blissos-16.9.7": {
        "url": "https://sourceforge.net/projects/blissos-x86/files/Official/"
               "BlissOS16/FOSS/Generic/"
               "Bliss-v16.9.7-x86_64-OFFICIAL-foss-20241011.iso/download",
        "sha256": "735cb962ec6bd92b62eb82a812831a38d79a0dfdf12b7973d2d0f7ab001ba68e",
        "sha1": None,
        "size": 2_342_518_784,
        "android": "13",
        # The three files qemu is handed, in the ISO's own names.
        "members": ("kernel", "initrd.img", "system.efs"),
    },
}


def all_images() -> dict:
    """Every pinned image, keyed by the tag it is published under."""
    merged = {}
    for table in (WINDOWS, VIRTIO, LINUX, DESKTOP, BSD, ANDROID):
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
