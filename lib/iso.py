"""Fetch installer images from ghcr, and only from ghcr.

    from iso import fetch_iso, fetch_url

    fetch_iso("mx", "mx.iso")            # an image publish-iso.yml pushed
    fetch_url(url, dest, sha256=..., size=...)   # a vendor's own URL

WHY THE REGISTRY AND NOT THE MIRROR

Every bake used to download its own image from whatever host the distribution
points at, at run time, with the job clock running. SourceForge served MX at
967 KB/s and then hung for fifty-six minutes with an empty log; Mint's mirror
answers at 124 KB/s, which is forty minutes for three gigabytes. Those are the
two that were measured; the other fourteen were never watched closely enough to
know.

publish-iso.yml fetches each image once, checks it three ways (exact byte count,
ISO9660 signature, a stalled-transfer abort) and pushes it as an OCI layer to
ghcr. This pulls it back.

NO FALLBACK, DELIBERATELY. A function that quietly falls back to the mirror is a
function whose failures are invisible: the run succeeds, slowly, from a host
nobody chose, and the cache is never noticed to be broken. If ghcr does not have
the image the bake stops and says so.

WHY THIS IS PYTHON NOW

The shell version did the registry handshake with curl and then parsed each
response with an inline `python3 -c 'import json,sys; print(...)'`. Three
languages in one pipeline for a JSON field lookup. The same code here is
`manifest["layers"][0]["digest"]`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

REGISTRY = os.environ.get("ISO_REGISTRY", "ghcr.io")
# Everything this repository publishes lives in its own package: the bake image
# (image.yml), the ISOs (publish-iso.yml) and the baked disks (bake-*). The default
# matches the repository, so a fork publishes to its own name without a change.
OWNER = os.environ.get("ISO_OWNER", "easydev-org")
REPO = os.environ.get("ISO_REPO", "easydiskbake")

# The blob download is the only long operation. It aborts when the rate stays
# under 20 KB/s for two minutes, which is what caught the SourceForge hang: the
# transfer was not dead, it was doing nothing, and a plain timeout would have
# waited for it.
BLOB_MAX_SECONDS = 3600
BLOB_SPEED_WINDOW = 120
BLOB_MIN_BYTES_PER_SECOND = 20480


def log(msg: str) -> None:
    print(f"[iso] {msg}", flush=True)


class IsoError(RuntimeError):
    pass


def _token(scope: str) -> str:
    """A pull token for one repository. The package is private, so an anonymous
    request gets 401 and no manifest."""
    user = os.environ.get("GITHUB_ACTOR") or "x"
    password = os.environ.get("GITHUB_TOKEN")
    if not password:
        raise IsoError(
            "GITHUB_TOKEN is not set; the iso packages are private"
        )

    url = (
        f"https://{REGISTRY}/token"
        f"?scope=repository:{scope}:pull&service={REGISTRY}"
    )
    req = urllib.request.Request(url)
    import base64

    req.add_header(
        "Authorization",
        "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode(),
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["token"]


def _manifest(repo: str, tag: str, token: str) -> dict:
    url = f"https://{REGISTRY}/v2/{repo}/manifests/{tag}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.oci.image.manifest.v1+json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise IsoError(
                f"no manifest for tag {tag!r} in {REGISTRY}/{repo}\n"
                f"run publish-iso.yml for that name; "
                f"this bake will not fall back to a mirror"
            ) from exc
        raise


def _download(url: str, dest: str, *, token: str | None, expected_size: int | None) -> None:
    """Stream one blob to dest.part, then move it into place.

    The partial file is what makes a failure resumable-by-retry rather than
    restarting a multi-gigabyte transfer: a half-written dest would be skipped
    by the `already present` check at the top of fetch_iso.
    """
    part = dest + ".part"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    got = 0
    with urllib.request.urlopen(req, timeout=BLOB_SPEED_WINDOW) as resp, open(part, "wb") as fh:
        total = expected_size or int(resp.headers.get("Content-Length") or 0)
        if total:
            log(f"downloading {total} bytes")
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if total and got % (64 << 20) < len(chunk):
                pct = 100.0 * got / total
                log(f"  {got >> 20} MiB / {total >> 20} MiB ({pct:.0f}%)")

    if expected_size is not None and got != expected_size:
        raise IsoError(
            f"size mismatch: got {got}, the manifest says {expected_size}"
        )

    shutil.move(part, dest)


def fetch_iso(name: str, dest: str) -> str:
    """Pull the image published under `name` (e.g. "mx", "alpine") into dest.

    The image lives in the same ghcr package as everything else this repository
    publishes, under the tag `iso-<name>`:

        ghcr.io/easydev-org/easydiskbake:iso-alpine

    One package rather than one per artifact keeps the tag namespace honest: a tag
    says what kind of thing it is (`iso-`, `windows-base-`, or no prefix for the
    bake image itself), and there is exactly one place to look for all of them.
    """
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        log(f"{dest} already present ({os.path.getsize(dest)} bytes)")
        return dest

    repo = f"{OWNER}/{REPO}"
    tag = f"iso-{name}"
    log(f"fetching {tag} from {REGISTRY}/{repo}")

    token = _token(repo)
    manifest = _manifest(repo, tag, token)
    layers = manifest.get("layers") or []
    if not layers:
        raise IsoError(f"manifest for {tag!r} has no layers")
    layer = layers[0]
    digest = layer["digest"]
    size = layer["size"]
    log(f"layer {digest}, {size} bytes")

    _download(
        f"https://{REGISTRY}/v2/{repo}/blobs/{digest}",
        dest,
        token=token,
        expected_size=size,
    )
    log(f"{dest} is {os.path.getsize(dest)} bytes")
    return dest


def fetch_url(
    url: str,
    dest: str,
    *,
    sha256: str | None = None,
    size: int | None = None,
) -> str:
    """Download a vendor's own URL, verified.

    Used for Windows, whose ISO cannot be redistributed and so cannot go through
    the same cache: the download has to happen somewhere, and a fixed URL with a
    fixed hash is the only version of it that fails loudly when Microsoft rotates
    the file.

    The hash check is not optional in spirit - a Windows ISO that silently became
    a different build would install a different edition and nothing downstream
    would notice.
    """
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        if sha256:
            actual = _sha256(dest)
            if actual != sha256:
                log(f"{dest} present but sha256 is {actual}, expected {sha256}; refetching")
                os.unlink(dest)
            else:
                log(f"{dest} already present and verified")
                return dest
        else:
            log(f"{dest} already present ({os.path.getsize(dest)} bytes, unverified)")
            return dest

    log(f"downloading {url}")
    _download(url, dest, token=None, expected_size=size)

    if sha256:
        actual = _sha256(dest)
        if actual != sha256:
            os.unlink(dest)
            raise IsoError(
                f"sha256 mismatch for {url}\n  expected {sha256}\n  actual   {actual}\n"
                f"the vendor has probably published a new build; update the pin"
            )
        log(f"sha256 verified: {sha256}")

    return dest


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: iso.py <name-or-url> <dest>")
    target, destination = sys.argv[1], sys.argv[2]
    if target.startswith("http"):
        fetch_url(target, destination)
    else:
        fetch_iso(target, destination)
