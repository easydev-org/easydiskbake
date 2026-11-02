# The image that builds an Android AVD, and nothing else.
#
# WHY A SECOND IMAGE
#
# Creating an AVD needs cmdline-tools (which brings a JDK), avdmanager and the system
# image - about 1.5 GB. The bake image does not need any of it: by the time a bake
# runs, the AVD already exists and was published to ghcr. Shipping the SDK into the
# runtime image would be 1.5 GB carried by every Windows and Linux bake for a tool
# they never call.
#
# So this image exists for one workflow, publish-android.yml, and is discarded after.
#
# WHAT IT CONTAINS, AND WHY IT BOOTS A GUEST AFTER ALL
#
# The first version of this image carried:
#
#     WHAT IT DOES NOT CONTAIN
#     No emulator, no qemu, no KVM. Building an AVD does not boot anything.
#
# Both halves of that are wrong, and each cost a publish run:
#
#   avdmanager create avd needs the `emulator` SDK package.
#       Error: "emulator" package must be installed!
#
#   avdmanager does not write disk images. system.img, userdata.img and cache.img are
#   created when the emulator first starts the AVD. Packing right after create produced
#   a 1,642-byte archive of one config file, and that is what reached ghcr.
#
# So this image installs the emulator package AND boots the AVD once, which means it
# needs the emulator's own dependencies and /dev/kvm. The publish workflow passes the
# device through.
#
# What it still does not need is the bake image's qemu: the emulator is Google's own
# qemu fork and ships its own binaries.
#
# TWO SOURCES, BOTH PINNED BY URL
#
#   cmdline-tools   versioned by Google, from dl.google.com
#   system image    fetched by lib/publish_android.py and verified against the sha1
#                   in lib/images.py - the pin lives there, not here, because the bake
#                   reads the same table.

FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

# unzip for the system image, zstd for packing the AVD, tar is in the base.
# openjdk is for avdmanager, which is a Java tool.
#
# The lib* group is the emulator's runtime. It is the same list the bake image installs
# for the same binary, taken from there rather than guessed: a missing shared library
# shows up as a linker error naming a .so, several steps away from the package that
# should have provided it.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
        file \
        p7zip-full \
        zip \
        xz-utils \
        zstd \
        tar \
        python3 \
        openjdk-21-jre-headless \
        libx11-6 libx11-xcb1 libxcb1 libxau6 libxdmcp6 libbsd0 \
        libpulse0 libasound2t64 libnss3 libnspr4 \
        libgl1 libglx-mesa0 libegl1 libgles2 \
        libgtk-3-0t64 libxcb-render0 libxcb-shape0 libxcb-xfixes0 libxcb-randr0 \
        ; \
    rm -rf /var/lib/apt/lists/*

# cmdline-tools: the only piece of the SDK this image needs. The version is Google's
# release number; a newer one is a one-line change, and nothing else depends on it.
ARG CMDLINE_TOOLS=commandlinetools-linux-16111833_latest.zip

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
    # Accepting the licence here means no workflow has to pipe `yes` into it. The
    # SDK licence is accepted by using the SDK; this is the same acceptance the
    # build would otherwise prompt for.
    yes | "$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" --licenses >/dev/null 2>&1 || true; \
    # avdmanager validates the SDK before it writes an AVD and names this package when
    # it is missing. --no_https is not needed; dl.google.com is https.
    "$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" "emulator" >/dev/null

ENV PATH="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$PATH"

# A build-time self-check. avdmanager is the one tool the workflow cannot do without,
# and it fails with a Java stack trace when the JRE is missing rather than saying so.
RUN set -eux; \
    python3 --version; \
    unzip -v | head -1; \
    zstd --version; \
    java -version 2>&1 | head -1; \
    sdkmanager --version; \
    avdmanager --help 2>&1 | head -3; \
    test -d "$ANDROID_SDK_ROOT/emulator"; \
    test -x "$ANDROID_SDK_ROOT/emulator/emulator"

ENTRYPOINT []
CMD ["/bin/bash"]
