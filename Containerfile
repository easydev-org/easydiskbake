# The image every bake runs in.
#
# Base is qemux/qemu:7.50 because it already carries qemu 11.1 with KVM support,
# 7z, xorriso, python3, jq, curl, mkfifo, timeout and setsid - measured, see
# README.md. Everything added here is a tool a stated requirement names and the
# base does not have.

FROM qemux/qemu:7.50

# What each of these is for, and why the base does not already have it:
#
#   ffmpeg            video of the whole run, read from qemu's own VNC server.
#                     Nothing else here can produce a video, and a recording is the
#                     one artefact that shows an installer doing something no log
#                     line mentions.
#   tesseract-ocr     OCR, for installers with no unattended format at all.
#   tesseract-ocr-eng ...and the language pack: without it tesseract runs, exits
#                     zero and reads nothing, which looks exactly like a blank screen.
#   e2fsprogs         debugfs: read a file out of a finished ext4 disk.
#   genisoimage       rebuild an ISO with an answer file at its root, the way
#                     dockur/windows does for Autounattend.xml. Kept for the legacy
#                     media path; xorriso (already present) handles modern ones.
#   wimtools          wimlib-imagex: read a Windows ISO's install.wim metadata
#                     without Windows. This is what verifies that the image index in
#                     Autounattend.xml resolves to the edition it claims to - the
#                     check a guest-side script otherwise has to do after the fact.
#   swtpm             a software TPM 2.0 for Windows 11, which refuses to install
#                     without one. tpm2-tools is not needed: qemu speaks the
#                     chardev protocol directly.
#   ovmf              the UEFI firmware. Windows 11 will not install on SeaBIOS, and
#                     a guest with no UEFI boots to "no bootable device" - which looks
#                     like a broken ISO rather than a missing package.
#   aria2             segmented downloader for the macOS recovery image. An 847 MiB
#                     transfer from Apple's CDN gets reset around 20 MiB with one
#                     connection; aria2c splits and resumes it. dockur-macos relies on
#                     the same tool (it cleans up aria2's control file).
#   p7zip-full        `7z`, for the macOS OpenCore template. That file is an ISO9660
#                     image with a Joliet tree inside it; the 9660 name table gives
#                     every file an 8.3 uppercase name with a ";1" version suffix, and
#                     config.plist refers to "Lilu.kext" and "SSDT-EC.aml". 7z reads
#                     the Joliet layer and writes the names the config expects, which
#                     is why lib/opencore.py uses it rather than parsing the ISO itself.
#   mtools            mformat/mcopy/mdir: build the 200 MiB FAT image OpenCore boots
#                     from, without loop-mounting it. A container job has no privileges
#                     to mount, and this writes the same filesystem without them.
#   samba             smbd, for qemu's `-netdev user,smb=<dir>`. That option makes qemu
#                     fork its own smbd with a generated config, which is how a guest
#                     gets a shared folder without the host opening a port. qemu checks
#                     for the binary at startup and refuses the netdev if it is absent -
#                     the check is CONFIG_SMBD_COMMAND, normally /usr/sbin/smbd.
#   nodejs, git       for actions and for the workflow itself. `actions/checkout@v4`
#                     is a Node program the runner executes inside this container, and
#                     the step that follows it runs `git` to report what it cloned:
#
#                         exec: "node": executable file not found in $PATH
#                         git: command not found
#
#                     Both failed before any of this repository's code ran. The image
#                     was written for the bake, which needs neither - GitHub's runner VM
#                     supplies them, and a container job does not get that for free.
#
# (Comments cannot sit inside a line-continued RUN, so they sit above it.)
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        tesseract-ocr-eng \
        e2fsprogs \
        genisoimage \
        xorriso \
        # makefs is what rebuilds a FreeBSD mfsroot after /etc/installerconfig is added
        # to it. Without it the FreeBSD bake cannot put its answer file on the medium,
        # and there is no other transport - see bsd/freebsd/bake.py.
        makefs \
        wimtools \
        swtpm \
        ovmf \
        aria2 \
        p7zip-full \
        mtools \
        samba \
        nodejs \
        git \
        ; \
    rm -rf /var/lib/apt/lists/*

# A build-time self-check. A base image that loses one of these is caught here rather
# than twenty minutes into a bake, and every entry is a tool the drivers call directly.
RUN set -eux; \
    qemu-system-x86_64 --version | head -1; \
    qemu-img --version | head -1; \
    qemu-io --version 2>&1 | head -1; \
    7z 2>/dev/null | head -2 | tail -1; \
    xorriso -version 2>&1 | head -2 | tail -1; \
    command -v makefs; \
    python3 --version; \
    ffmpeg -version 2>&1 | head -1; \
    tesseract --version 2>&1 | head -1; \
    tesseract --list-langs 2>&1 | head -5; \
    wimlib-imagex --version | head -1; \
    swtpm --version; \
    aria2c --version | head -1; \
    smbd --version | head -1; \
    node --version; \
    git --version; \
    7z 2>/dev/null | head -2 | tail -1; \
    mformat --version 2>&1 | head -1; \
    ls -l /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_VARS_4M.fd; \
    command -v debugfs genisoimage mkfifo timeout setsid jq curl

# ---------------------------------------------------------------------------
# The Android emulator and adb.
#
# WHY THEY ARE HERE AND cmdline-tools IS NOT
#
# A bake pulls a finished AVD from ghcr and starts the emulator on it. It never calls
# avdmanager, so it never needs the JDK or cmdline-tools - those live in the separate
# android-builder image (android/15/Containerfile.builder), which one workflow uses
# once and discards.
#
# What it does need is `/opt/android-sdk/emulator` and `/opt/android-sdk/platform-tools`,
# because that is where the emulator looks for its system images and where adb comes
# from. Both are fetched with the SDK's own package manager here rather than by
# copying binaries, so the licences are accepted in the image and no workflow has to
# answer a prompt.
ARG CMDLINE_TOOLS=commandlinetools-linux-16111833_latest.zip

RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        unzip \
        openjdk-21-jre-headless \
        libx11-6 libx11-xcb1 libxcb1 libxau6 libxdmcp6 libbsd0 \
        libpulse0 libasound2t64 libnss3 libnspr4 \
        libgl1 libglx-mesa0 libegl1 libgles2 \
        libgtk-3-0t64 libxcb-render0 libxcb-shape0 libxcb-xfixes0 libxcb-randr0 \
        ; \
    rm -rf /var/lib/apt/lists/*

ENV ANDROID_SDK_ROOT=/opt/android-sdk \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_AVD_HOME=/opt/android-avd

RUN set -eux; \
    mkdir -p "$ANDROID_SDK_ROOT/cmdline-tools" "$ANDROID_AVD_HOME"; \
    curl -fsSL -o /tmp/cmdtools.zip \
        "https://dl.google.com/android/repository/${CMDLINE_TOOLS}"; \
    unzip -q /tmp/cmdtools.zip -d "$ANDROID_SDK_ROOT/cmdline-tools"; \
    rm -f /tmp/cmdtools.zip; \
    mv "$ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools" \
       "$ANDROID_SDK_ROOT/cmdline-tools/latest"; \
    yes | "$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" --licenses >/dev/null 2>&1 || true; \
    "$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" \
        "platform-tools" "emulator" \
        "system-images;android-35;default;x86_64" >/dev/null; \
    rm -rf "$ANDROID_SDK_ROOT/cmdline-tools" /root/.android/cache

# THE SYSTEM IMAGE IS PART OF THE IMAGE, NOT OF THE AVD, AND RUN 41605230808 IS WHY.
#
# A published AVD holds config.ini, userdata, cache and an initrd - not the system
# partition. That comes from $ANDROID_SDK_ROOT/system-images/<api>/<tag>/<abi>/, which
# the AVD's config.ini names as `image.sysdir.1`. The first version of this RUN block
# installed only platform-tools and emulator, so every Android bake started an emulator
# that exited at once:
#
#     WARNING | /opt/android-sdk/system-images/android-35/default/x86_64/ is not a
#               valid directory
#     FATAL   | Broken AVD system path. Check your ANDROID_SDK_ROOT value
#
# The comment above this block said the emulator looks for its system images here, and
# then nothing put them there.
#
# THE VERSION IS PINNED TO WHAT android/15/android_pin.py BUILDS THE AVD ON, so the two
# cannot drift: a system image of a different API level is not interchangeable, and the
# mismatch would show up as an emulator that starts and never boots.

ENV PATH="$ANDROID_SDK_ROOT/platform-tools:$ANDROID_SDK_ROOT/emulator:$PATH"

RUN set -eux; \
    adb version | head -1; \
    emulator -version 2>&1 | head -1

# GitHub Actions runs a container job as `docker run <image> <entrypoint>`; this base's
# entrypoint starts a VM, which is not what a job wants.
ENTRYPOINT []
CMD ["/bin/bash"]
