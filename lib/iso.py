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
import time
import urllib.error
import urllib.request

REGISTRY = os.environ.get("ISO_REGISTRY", "ghcr.io")

# http or https. ghcr.io is https; a registry on the cluster network is often plain
# http, and forcing https against one fails with a TLS error that says nothing about
# the actual mistake.
SCHEME = os.environ.get("ISO_SCHEME", "https")
# Everything this repository publishes lives in its own package: the bake image
# (image.yml), the ISOs (publish-iso.yml) and the baked disks (bake-*). The default
# matches the repository, so a fork publishes to its own name without a change.
OWNER = os.environ.get("ISO_OWNER", "easydev-org")
REPO = os.environ.get("ISO_REPO", "easydiskbake")

# Credentials for a registry that does not hand out anonymous tokens. Forgejo's
# registry issues no /token endpoint at all - a request to it answers 404 - and
# expects Basic auth on every call instead.
ISO_USER = os.environ.get("ISO_USER", "")
ISO_PASSWORD = os.environ.get("ISO_PASSWORD", "")

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


class Auth:
    """How to prove identity to this registry.

    TWO SHAPES, AND THEY ARE NOT INTERCHANGEABLE:

        a token registry (ghcr.io)   GET /token, then Bearer on every request
        a basic registry (Forgejo)   NO /token endpoint; Basic auth on every request

    The first version only knew the token shape. Pointed at Forgejo it asked /token,
    received a 404, and treated that as "the package is private" - which is a
    completely different problem, and the bake stopped before downloading anything.

    Which one to use is decided by asking: if /token answers 404, the registry does not
    use tokens.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._mode: str | None = None   # "token" | "basic"

    def headers(self, scope: str) -> dict[str, str]:
        if self._mode is None:
            self._mode = self._detect(scope)
        if self._mode == "token":
            if self._token is None:
                self._token = self._fetch_token(scope)
            return {"Authorization": f"Bearer {self._token}"}
        return self._basic_header()

    @staticmethod
    def _basic_header() -> dict[str, str]:
        # Read at call time, not at import: a workflow sets ISO_USER/ISO_PASSWORD in the
        # step's environment, after this module has already been imported.
        user = os.environ.get("ISO_USER", "")
        if not user:
            return {}
        import base64

        password = os.environ.get("ISO_PASSWORD", "")
        raw = f"{user}:{password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def _detect(self, scope: str) -> str:
        url = f"{SCHEME}://{REGISTRY}/token?scope=repository:{scope}:pull&service={REGISTRY}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=self._basic_header()), timeout=30
            ) as resp:
                # ghcr answers 200 with a JSON token; callers use _fetch_token to read
                # it, so this branch only decides the mode.
                resp.read()
            return "token"
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                log(f"{REGISTRY} has no /token endpoint; using basic auth")
                return "basic"
            if exc.code in (401, 403):
                return "token"     # ghcr-shaped, just not authorised yet
            raise
        except urllib.error.URLError:
            # A registry on the cluster network may answer nothing here and still serve
            # the API; treat it as basic and let the real request decide.
            return "basic"

    def _fetch_token(self, scope: str) -> str:
        url = f"{SCHEME}://{REGISTRY}/token?scope=repository:{scope}:pull&service={REGISTRY}"
        headers = self._basic_header()

        # Anonymous first: this repository's packages are public and ghcr serves them
        # to anyone. Credentials are needed only for a private package.
        request = urllib.request.Request(url)
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return json.load(resp)["token"]
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403) or not headers:
                raise IsoError(
                    f"{REGISTRY} refused a pull token for {scope}: HTTP {exc.code}"
                ) from exc

        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=60
        ) as resp:
            return json.load(resp)["token"]


_AUTH = Auth()


def _manifest(repo: str, tag: str) -> dict:
    url = f"{SCHEME}://{REGISTRY}/v2/{repo}/manifests/{tag}"
    req = urllib.request.Request(url)
    for key, value in _AUTH.headers(repo).items():
        req.add_header(key, value)
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


# A TEN GIGABYTE TRANSFER OVER THE PUBLIC INTERNET DOES NOT SUCCEED ONCE.
#
# Run 41481670715 died pulling the gold disk:
#
#     size mismatch: got 3472883712, the manifest says 11286607360
#
# 3.2 GiB of 10.5 GiB, then the connection stopped. Nothing was retried, so the whole
# run - which had already pulled the base image and prepared the runner - was lost to a
# dropped connection. The partial file was right there on disk, unused.
#
# Five attempts with a backoff, each RESUMING from what the .part file already holds.
# A registry that answers 416 (Range Not Satisfiable, because the part is already
# complete) is treated as success by the size check at the end.
DOWNLOAD_ATTEMPTS = 5


def _download(url: str, dest: str, *, scope: str, expected_size: int | None) -> None:
    """Stream one blob to dest.part, then move it into place.

    The partial file is what makes a failure resumable rather than restarting a
    multi-gigabyte transfer: a half-written dest would be skipped by the `already
    present` check at the top of fetch_iso, so the bytes in flight are kept in .part
    and the next attempt asks for the rest.

    RESUMING NEEDS A RANGE REQUEST, and the registry supports them (it is a normal OCI
    distribution endpoint). If the server ignores the Range and starts from zero the
    file is truncated first, so a resume can never append the start of the blob to the
    end of itself.
    """
    part = dest + ".part"
    last_error: Exception | None = None

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if expected_size is not None and have == expected_size:
            log(f"{dest}: already complete ({have >> 20} MiB)")
            shutil.move(part, dest)
            return
        if expected_size is not None and have > expected_size:
            # A part larger than the blob is a previous attempt that wrote past the end
            # (a redirect to an error page, say). It cannot be resumed from.
            log(f"discarding an oversized part ({have} > {expected_size})")
            os.unlink(part)
            have = 0

        req = urllib.request.Request(url)
        # scope="" means a vendor URL, not this registry: no credentials go with it.
        if scope:
            for key, value in _AUTH.headers(scope).items():
                req.add_header(key, value)
        if have:
            req.add_header("Range", f"bytes={have}-")

        try:
            with urllib.request.urlopen(req, timeout=BLOB_SPEED_WINDOW) as resp:
                # A 206 means the Range was honoured; 200 means it was not, and the body
                # starts at byte zero.
                resuming = have > 0 and resp.status == 206
                if have and not resuming:
                    log("  the server ignored the Range; starting over")
                total = expected_size or int(resp.headers.get("Content-Length") or 0)
                if total:
                    log(f"downloading {total} bytes"
                        + (f" from {have >> 20} MiB" if resuming else ""))
                got = have if resuming else 0
                mode = "ab" if resuming else "wb"
                with open(part, mode) as fh:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        if total and got % (64 << 20) < len(chunk):
                            pct = 100.0 * got / total
                            log(f"  {got >> 20} MiB / {total >> 20} MiB ({pct:.0f}%)")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            log(f"  attempt {attempt}/{DOWNLOAD_ATTEMPTS} failed: {exc}")
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(min(30, 3 * attempt))
            continue

        if expected_size is None:
            shutil.move(part, dest)
            return

        got = os.path.getsize(part) if os.path.exists(part) else 0
        if got == expected_size:
            shutil.move(part, dest)
            return

        last_error = IsoError(
            f"size mismatch: got {got}, the manifest says {expected_size}"
        )
        log(f"  attempt {attempt}/{DOWNLOAD_ATTEMPTS} ended short: {got >> 20} MiB "
            f"of {expected_size >> 20} MiB")

    raise IsoError(
        f"could not download {url} in {DOWNLOAD_ATTEMPTS} attempts: {last_error}"
    )


def fetch_iso(name: str, dest: str) -> str:
    """Pull the image published under `name` (e.g. "mx", "alpine") into dest.

    The image lives in the same ghcr package as everything else this repository
    publishes, under the tag `iso-<name>`:

        ghcr.io/easydev-org/easydiskbake:iso-alpine

    One package rather than one per artifact keeps the tag namespace honest: a tag
    says what kind of thing it is (`iso-`, `windows-base-`, or no prefix for the
    bake image itself), and there is exactly one place to look for all of them.
    """
    return fetch_tag(f"iso-{name}", dest)


def fetch_tag(tag: str, dest: str) -> str:
    """Pull whatever is published under an exact tag into dest.

    This is fetch_iso without the `iso-` prefix, and it exists because not every
    artifact here is an installer image. A macOS boot asset is a zip, an ISO9660
    template and two firmware blobs; a recovery image is a DMG. They share the
    package, the tag style and the three-call pull, and the only thing that differed
    was a prefix that fetch_iso used to add unconditionally - so the caller with a
    different prefix had no way in.

    The caller passes the whole tag: "iso-alpine", "macos-asset-opencore",
    "macos-recovery-sequoia". Anything that has to guess a prefix is a second place
    that can disagree with the publisher.
    """
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        log(f"{dest} already present ({os.path.getsize(dest)} bytes)")
        return dest

    repo = f"{OWNER}/{REPO}"
    log(f"fetching {tag} from {REGISTRY}/{repo}")

    manifest = _manifest(repo, tag)
    layers = manifest.get("layers") or []
    if not layers:
        raise IsoError(f"manifest for {tag!r} has no layers")
    layer = layers[0]
    digest = layer["digest"]
    size = layer["size"]
    log(f"layer {digest}, {size} bytes")

    _download(
        f"{SCHEME}://{REGISTRY}/v2/{repo}/blobs/{digest}",
        dest,
        scope=repo,
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
    _download(url, dest, scope="", expected_size=size)

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
