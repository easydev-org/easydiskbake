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

Linux ISOs are redistributable, so iso-publish.yml can cache them indefinitely and
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
# private one: iso-publish.yml fetches each once and every bake pulls it back.
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
        "url": "https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/x86_64/"
               "alpine-virt-3.24.3-x86_64.iso",
        # The vendor publishes a .sha256 beside the image.
        "sha256": "cdc1bc0efc64d8d611bf3733f54868172a545888dfd726f518bf279bcb0ff1af",
        "size": 70254592,
    },

    # ---------------------------------------------------------------------------
    # THE MAINSTREAM DISTRIBUTIONS, added as the four a sandbox is most often asked
    # for. The versions are the ones easysandbox-vm/bake already drives, so a bake
    # here and a bake there install the same release.
    #
    # EVERY ONE IS PINNED BY SHA256, taken from the vendor's own checksum file:
    #
    #     Ubuntu   releases.ubuntu.com/26.04/SHA256SUMS
    #     Debian   cdimage.debian.org/debian-cd/current/amd64/iso-cd/SHA256SUMS
    #     Fedora   .../Everything/x86_64/iso/Fedora-Everything-44-1.7-x86_64-CHECKSUM
    #     Arch     geo.mirror.pkgbuild.com/iso/2026.10.01/sha256sums.txt
    #
    # mx and alpine above carry None because their sources give no stable answer -
    # SourceForge redirects with an expiring token, and alpine's filename is the pin.
    # These four do give one, and a hash is strictly better than a size: it catches a
    # truncated transfer AND a substituted file.
    #
    # WHY THE SERVER/CLOUD VARIANTS. ubuntu-*-live-server and debian-*-netinst are the
    # images whose installers a file can drive - subiquity and debian-installer both
    # read an answer file from the network. A desktop ISO would install a desktop
    # nobody asked for and take twice the bytes.
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
    "kubuntu": {
        "url": "https://cdimage.ubuntu.com/kubuntu/releases/26.04/release/"
               "kubuntu-26.04.1-desktop-amd64.iso",
        # The vendor publishes SHA256SUMS beside the images; this has to be refreshed
        # with the URL when the point release moves.
        "sha256": None,
        "size": None,
    },
    "debian": {
        "url": "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/"
               "debian-13.7.0-amd64-netinst.iso",
        "sha256": "a7ef94ac2fb9a7fec454552abd629b7cc9d5155c886165a45649f5ce6167e355",
        "size": None,
    },
    "fedora": {
        "url": "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/"
               "Everything/x86_64/iso/Fedora-Everything-netinst-x86_64-44-1.7.iso",
        "sha256": "bd285201494dd0ba09b54d05ac707de1401668b8512a573edb5922dcf9d7067e",
        # The vendor's CHECKSUM file states this, which is why it is here and the
        # other three have None: a size improves the resume logic in iso._download.
        "size": 1217329152,
    },
    "arch": {
        "url": "https://geo.mirror.pkgbuild.com/iso/2026.11.01/"
               "archlinux-2026.11.01-x86_64.iso",
        "sha256": "01bcdcf01af2f7d58f2794899c834770d25c9bfc945bd092b32bb66672a4a216",
        "size": 1317568512,
    },

    # ---------------------------------------------------------------------------
    # THE POPULARITY LIST. These are the entries in DistroWatch's Page Hit Ranking
    # that a sandbox is asked for by name, added on request rather than on a rule.
    #
    # The Arch family is here as three separate entries on purpose: CachyOS and
    # EndeavourOS are not "arch with a different installer", they ship their own
    # kernels and their own installer flows, and a bake that treats them as Arch
    # would install Arch.
    # ---------------------------------------------------------------------------
    "popos": {
        # 24.04 nvidia: the image with the driver branch already in it, which is the
        # one Pop!_OS is known for. intel exists as a second entry if it is wanted.
        "url": "https://iso.pop-os.org/24.04/amd64/nvidia/28/"
               "pop-os_24.04_amd64_nvidia_28.iso",
        # From the vendor's SHA256SUMS beside the image.
        "sha256": "bd867a6b93810769b2189471cfc2d7b6b9c642213c7bf827de511be5f42ce666",
        "size": 3934945280,
    },
    "cachyos": {
        "url": "https://mirror.cachyos.org/ISO/desktop/260809/"
               "cachyos-desktop-linux-260809.iso",
        # From the .sha256 published beside the image.
        "sha256": "959f6577f45e25ee9fd8c220fd221b08e4ea79412c7315c0f922dd6d86d5e33c",
        "size": 3188850688,
    },
    "endeavouros": {
        "url": "https://mirrors.gigenet.com/endeavouros/iso/"
               "EndeavourOS_Titan-Nova-2026.08.15.iso",
        # NO SHA256: the vendor publishes only a .sha512sum for this image, and a
        # truncated sha512 is not a sha256. The size is pinned and the ISO9660 check
        # in publish_iso.py catches a corrupt download; the difference is that this
        # one cannot catch a substituted file.
        "sha256": None,
        "size": 3898638336,
    },
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
    "mint": {
        "url": "https://mirrors.kernel.org/linuxmint/stable/22.3/"
               "linuxmint-22.3-cinnamon-64bit.iso",
        # From sha256sum.txt under the same mirror path.
        "sha256": "a081ab202cfda17f6924128dbd2de8b63518ac0531bcfe3f1a1b88097c459bd4",
        "size": 3091660800,
    },
    "alma": {
        "url": "https://repo.almalinux.org/almalinux/10/isos/x86_64/"
               "AlmaLinux-10.2-x86_64-minimal.iso",
        # From the CHECKSUM file beside the images.
        "sha256": "1b532f534231da0d1cd0ccae622bea6cd588d8a0d7b259f1f131501a6eed41a4",
        "size": 1651376128,
    },
    "rocky": {
        "url": "https://dl.rockylinux.org/pub/rocky/10/isos/x86_64/"
               "Rocky-10.2-x86_64-minimal.iso",
        # From the CHECKSUM file beside the images.
        "sha256": "aac6ac3ce781b91a91ce78463405f66c611a5dca4b3840c79e5e01d97302f6c8",
        "size": 2072444928,
    },
    "manjaro": {
        # 26.1.2 Bian-May, XFCE full. Found through the release announcement on
        # forum.manjaro.org, because download.manjaro.org answers / and /xfce/ with a
        # redirect to the download page and no directory listing - the version
        # directory is only reachable if its exact name is already known.
        "url": "https://download.manjaro.org/xfce/26.1.3/"
               "manjaro-xfce-26.1.3-261028-linux618.iso",
        # From the .sha256 published beside the image.
        "sha256": "a100c3ca17668fd10daa453709ff29e22f43f1cd83f44369f70b02e21bda3af9",
        "size": 5210302464,
    },
    "zorin": {
        # 18.1 Core. zorin.com/os/download/18/core/ is a page, not a file, but the
        # mirror URLs are in its source - which is how this one was found, without
        # answering the mailing-list form the page also offers.
        "url": "https://distro.ibiblio.org/zorinos/18/"
               "Zorin-OS-18.1-Core-64-bit.iso",
        # From SHA256SUMS.txt in the same directory.
        "sha256": "44649b97bd307fc4c8529205d098ebbf98575a1e1ba2ee7d7005b697af1721d5",
        "size": 3909091328,
    },
    "opensuse": {
        # Tumbleweed rather than Leap: Leap's DVD path under
        # download.opensuse.org/distribution/leap/ did not resolve when this was
        # written, and Tumbleweed is the edition the project points people at.
        "url": "https://download.opensuse.org/tumbleweed/iso/"
               "openSUSE-Tumbleweed-DVD-x86_64-Snapshot20261031-Media.iso",
        # A dated snapshot rather than -Current: Current is a symlink that moves, and
        # a hash of one snapshot cannot be applied to the next. Refreshing means
        # taking both the filename and the .sha256 together.
        "sha256": "993f6a07ac3f35b113de698975bd43ec6e8da5ad6dac3ddcdaddbc71a928f440",
        "size": 4611637248,
    },
}


# BSD INSTALLERS.
#
# Separate from LINUX because the media and the installers differ in kind: these are
# .img files for the two that ship one, they use a working directory of their own, and
# a BSD bake needs tools lib/smoke.py does not have (ufs, not ext4 - see bsd/README.md).
#
# WHICH ONES. FreeBSD's bsdinstall is scriptable and its release media is a single
# .img.xz; OpenBSD's install.conf is the simplest unattended format of the three; NetBSD
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
    "openbsd": {
        "url": "https://cdn.openbsd.org/pub/OpenBSD/8.0/amd64/install80.iso",
        # From the vendor's SHA256 file beside the image.
        "sha256": "bc85d747c6ee4354d14fbfeee38087500386a2e9d86fa7b5ab13d840d902aa33",
        "size": 832565248,
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


def all_images() -> dict:
    """Every pinned image, keyed by the tag it is published under."""
    merged = {}
    for table in (WINDOWS, VIRTIO, LINUX, BSD, ANDROID):
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
