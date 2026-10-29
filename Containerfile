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
#                     dockur/windows does for Autounattend.xml.
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
    python3 --version; \
    ffmpeg -version 2>&1 | head -1; \
    tesseract --version 2>&1 | head -1; \
    tesseract --list-langs 2>&1 | head -5; \
    command -v debugfs genisoimage mkfifo timeout setsid jq curl

# GitHub Actions runs a container job as `docker run <image> <entrypoint>`; this base's
# entrypoint starts a VM, which is not what a job wants.
ENTRYPOINT []
CMD ["/bin/bash"]
