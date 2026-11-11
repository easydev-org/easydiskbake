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
#   sshpass           the password on the command line, for every stage that reads a
#                     guest over ssh. IT IS HERE BECAUSE IT WAS IN THIRTEEN WORKFLOW
#                     STEPS AND MISSING FROM FOUR. Each stage that needed it ran
#
#                         if ! command -v sshpass; then apt-get install -y -qq sshpass; fi
#
#                     which is a copy of one fact in thirteen places, and the copies
#                     disagreed: ubuntu, kubuntu, freebsd and nixos installed it in bake
#                     and launch but not in dev, so run 41985172898's dev job moved a
#                     12 GiB disk into place, started qemu, and then reported
#
#                         === sshpass IS NOT INSTALLED IN THIS CONTAINER ===
#                         === THE GUEST NEVER ANSWERED ===
#
#                     two seconds after the machine booted. The guest was fine; the
#                     client that was supposed to talk to it was not there. One source
#                     for the fact is the fix, and the image is it.
#                     `--no-install-recommends` as everywhere else in this RUN.
#   openssh-client    sshpass IS ONLY A WRAPPER - it runs `ssh`, and with no ssh on PATH
#                     every connection fails as
#
#                         SSHPASS: Failed to run command: No such file or directory
#
#                     which reads like a missing sshpass rather than a missing ssh. Runs
#                     41992862680 and 41992878416 both died on it after their installers
#                     had finished (735s and 825s of work), at the step that boots the
#                     disk to prove it works.
#
#                     IT WAS INVISIBLE UNTIL sshpass MOVED HERE. The thirteen workflow
#                     steps this replaced ran `apt-get install -y -qq sshpass` WITHOUT
#                     --no-install-recommends, so apt pulled openssh-client in as a
#                     Recommend and every stage had an ssh it never named. Putting the
#                     package in the image kept the package and dropped the side effect.
#
# (Comments cannot sit inside a line-continued RUN, so they sit above it.)
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ffmpeg \
        openssh-client \
        sshpass \
        # python3-pil is what lets lib/assistant.py crop a region of a screendump before
        # handing it to tesseract. The capacity in Disk Utility's detail pane is about ten
        # pixels tall at 1920x1080 and unreadable full-frame; run 41691829177 lost a bake to
        # "Pillow is not installed, so a crop cannot be taken" and then correctly refused to
        # erase any disk, because it could not tell the 64 GB target from a 268 MB one.
        python3-pil \
        # python3-yaml, for lib/cleancheck.py. That check reads .github/workflows/*.yml to
        # compare each job against the table of which systems have ssh, and every workflow in
        # this repository is YAML - parsing it needs a parser. Without it the check fails at
        # import, which is what selftest run 42026452129 reported:
        #
        #     File "/__w/.../lib/cleancheck.py", line 50, in <module>
        #     import yaml
        #     ModuleNotFoundError: No module named 'yaml'
        #
        # lib/entrypoints.py said "ok" for the same file, because it only runs `--help` and
        # that returns from argparse before the import is reached.
        python3-yaml \
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
        # zstd is what decompresses Redox OS's images (`.img.zst`). It is NOT for qcow2:
        # lib/compress.py writes compression_type=zstd through qemu-img, which needs no
        # binary at all. This one is for a vendor that publishes raw images compressed.
        zstd \
        mtools \
        samba \
        nodejs \
        git \
        ; \
    rm -rf /var/lib/apt/lists/*

# A build-time self-check. A base image that loses one of these is caught here rather
# than twenty minutes into a bake, and every entry is a tool the drivers call directly.
#
# `ssh` IS CHECKED SEPARATELY FROM `sshpass` and prints its version. Checking the wrapper
# and not the program it runs is how an image passed this self-check while every ssh in
# it failed with "SSHPASS: Failed to run command: No such file or directory".
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
    command -v debugfs genisoimage mkfifo timeout setsid jq curl sshpass; \
    ssh -V; \
    python3 -c "import yaml; print('yaml', yaml.__version__)"

# NO ANDROID SDK IN THIS IMAGE, AND THAT IS NOW A FACT RATHER THAN A PATH FIX.
#
# It used to carry cmdline-tools, the emulator and adb, because one route booted Google's AOSP
# system image through the emulator. android/blissos16 replaced it: BlissOS is Android-x86, a
# stock qemu boots it, and it installs onto a GPT disk with an ESP that UEFI starts - so nothing
# in this image needs to know what an AVD is.
#
# The reason to say so here is what the SDK cost when it WAS present: an `ENV PATH` that put the
# emulator's directory ahead of /usr/bin put the emulator's own qemu-img (2.12, a 2018 build with
# no zstd) in front of the real one for EVERY
# platform's bake, and this repository compressed a disk with /usr/bin/qemu-img 11.1 and then
# failed to open its own output:
#
#     qemu-img: Invalid parameter 'compression_type'
#     qemu-img: Could not open '...': Unsupported qcow2 feature(s): compression type
#
# Removing the SDK removes the trap. If an Android tool ever needs to come back, it comes back by
# ABSOLUTE PATH and never by prepending to PATH.

