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
    """The ghcr tag an image is published under, in this repository's package."""
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


def all_images() -> dict:
    """Every pinned image, keyed by the tag it is published under."""
    merged = {}
    for table in (WINDOWS, VIRTIO, LINUX):
        for name, pin in table.items():
            merged[tag(name)] = pin
    return merged


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
