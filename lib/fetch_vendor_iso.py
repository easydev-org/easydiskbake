"""Fetch a vendor ISO into the workspace, in parallel, resumably, and verify its sha256.

WHY A SEPARATE FETCHER FROM lib/iso.py

lib/iso.py pulls from this repository's own ghcr package, which is what a bake consumes. An
ISO that has not been published yet - the Ubuntu desktop image this experiment needs - is not
there, and `oci-pull` has no way to reach a vendor's release directory.

WHY PARALLEL AND WHY RESUMABLE

The same reason the macOS disk needed it: measured from a pod in this cluster, direct egress
to releases.ubuntu.com runs at tens of KB/s and through the cluster's proxy at tens of MB/s.
A 6 GiB image is 40 hours at the first rate and four minutes at the second. The offset of each
segment is kept in a sidecar file, so a killed pod continues instead of starting over.

THE CHECKSUM IS NOT OPTIONAL. A vendor ISO is the root of everything a bake produces; a
truncated one installs a broken system and the failure surfaces hours later as "the guest did
not reach a desktop". SHA256SUMS is fetched from the same directory, so a mirror serving an
old image says so here rather than at boot.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

# A USER-AGENT IS REQUIRED BY SOME MIRRORS, AND python-urllib IS NOT ONE.
#
# Measured from a pod in this cluster against the SAME URL, one second apart:
#
#     curl -I                    HTTP/2 200   content-length: 3188850688
#     urllib HEAD                HTTP 403 Forbidden
#     urllib GET Range 0-0       HTTP 403 Forbidden
#
# The only difference is the User-Agent. CachyOS's mirror sits behind Cloudflare, which
# refuses the default python-urllib string - and it refuses it for HEAD and GET alike, so no
# amount of falling back between methods helps. A browser UA is what curl sends.
UA = os.environ.get("ISO_USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) bake-fetch/1.0")

# READ LAZILY, SO `--help` AND `--url` WORK WITHOUT THE ENVIRONMENT.
#
# These were os.environ[...] at import time, which made the module unusable from a shell:
# `--help` died with KeyError('ISO_URL') before argparse ran. The workflow sets all three, so
# nothing changes for it; a person gets the flags.
URL = os.environ.get("ISO_URL", "")
SHA = os.environ.get("ISO_SHA256", "").lower()
DEST = os.environ.get("DEST", "")
SEGMENTS = int(os.environ.get("SEGMENTS", "8"))
CHUNK = int(os.environ.get("CHUNK", str(4 << 20)))
RETRIES = int(os.environ.get("RETRIES", "8"))

_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[iso {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _size() -> int:
    """The file's length, by HEAD and falling back to a one-byte ranged GET.

    WHY HEAD IS NOT ENOUGH. Cloudflare - which fronts CachyOS's mirror - answers a HEAD with
    HTTP 403 while the same URL as a GET returns 200 and a content-length. Measured:

        curl -I   https://mirror.cachyos.org/.../cachyos-desktop-linux-260809.iso  -> 200
        urllib HEAD on the same URL                                                -> 403

    So a fetcher that sizes the file with HEAD alone reports "Forbidden" for an image it could
    have downloaded. The fallback asks for a single byte, which every CDN allows, and reads
    the total out of Content-Range.

    THIS IS ALSO WHY THE FIRST BATCH'S CACHYOS LEFT NO FILE: the failure was in the size probe,
    before the destination was ever created, which is exactly the shape that was observed.
    """
    try:
        req = urllib.request.Request(URL, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            length = r.headers.get("Content-Length")
            # A 200 WITH NO CONTENT-LENGTH IS NOT A SIZE, AND CACHYOS'S MIRROR DOES IT.
            #
            #     File "lib/fetch_vendor_iso.py", line 85, in _size
            #       return int(r.headers["Content-Length"])
            #     TypeError: int() argument must be a string ... not 'NoneType'
            #
            # which killed cachyos twice before it downloaded a byte. The HEAD answers
            # 200 and simply omits the header - a chunked or dynamically-fronted origin is
            # entitled to - so the branch below falls through to the one-byte probe, which
            # reads the total out of Content-Range and does not depend on this header at
            # all.
            if length is not None:
                return int(length)
            log("  HEAD gave no Content-Length; sizing with a one-byte range instead")
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 405, 501):
            raise
        log(f"  HEAD refused ({exc.code}); sizing with a one-byte range instead")

    req = urllib.request.Request(URL, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        # Content-Range: bytes 0-0/3188850688
        content_range = r.headers.get("Content-Range", "")
        if "/" in content_range:
            return int(content_range.rsplit("/", 1)[1])
        length = r.headers.get("Content-Length")
        if length is None:
            raise SystemExit(
                "=== THE SERVER WILL NOT SAY HOW LARGE THE IMAGE IS ===\n"
                f"  {URL}\n"
                "  neither HEAD nor a one-byte range returned a size, so there is nothing\n"
                "  to verify the download against.")
        return int(length)


def _save(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _fetch(fd: int, index: int, start: int, end: int, state_path: str, state: dict) -> None:
    key = str(index)
    want = end - start
    have = min(int(state.get(key, 0)), want)
    if have == want:
        return
    pos = start + have
    if have:
        log(f"  segment {index}: resuming at +{have} of {want}")

    attempt = 0
    while pos < end:
        attempt += 1
        req = urllib.request.Request(
            URL, headers={"User-Agent": UA, "Range": f"bytes={pos}-{end - 1}"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                if resp.status != 206:
                    raise IOError(f"expected 206 for a range, got {resp.status}")
                while pos < end:
                    chunk = resp.read(min(CHUNK, end - pos))
                    if not chunk:
                        break
                    os.pwrite(fd, chunk, pos)
                    pos += len(chunk)
                    with _lock:
                        state[key] = pos - start
                if pos < end:
                    raise IOError(f"stream ended at +{pos - start} of {want}")
        except Exception as exc:                      # noqa: BLE001 - retry anything
            with _lock:
                state[key] = pos - start
                _save(state_path, state)
            if attempt >= RETRIES:
                raise SystemExit(f"segment {index} failed at +{pos - start}/{want}: {exc}")
            wait = min(30, 2 ** min(attempt, 5))
            log(f"  segment {index}: attempt {attempt} at +{pos - start}/{want} ({exc}); "
                f"retrying in {wait}s")
            time.sleep(wait)
        else:
            with _lock:
                state[key] = pos - start
                _save(state_path, state)


def main(argv: list[str] | None = None) -> int:
    """Fetch one ISO. Arguments are optional overrides for the environment variables.

    BOTH FORMS EXIST BECAUSE THE CALLERS DIFFER. A workflow step has no argv to pass and sets
    ISO_URL/ISO_SHA256/DEST as environment; a person at a shell would rather type them. The
    environment is read first so an existing caller keeps working unchanged.
    """
    global SEGMENTS, URL, SHA, DEST
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default=URL)
    p.add_argument("--sha256", default=SHA, dest="sha")
    p.add_argument("--dest", default=DEST)
    p.add_argument("--segments", type=int, default=SEGMENTS)
    a = p.parse_args(argv)
    URL, SHA, DEST, SEGMENTS = a.url, a.sha.lower(), a.dest, a.segments
    if not (URL and SHA and DEST):
        print("need --url, --sha256 and --dest (or ISO_URL, ISO_SHA256, DEST)", file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(DEST) or ".", exist_ok=True)
    state_path = DEST + ".segments.json"

    # A COMPLETE FILE IS NOT RE-DOWNLOADED. The sha256 is what says it is complete, so this
    # is the one place a cached file is trusted - and only after the hash matches.
    if os.path.exists(DEST) and os.path.getsize(DEST) > 0:
        log(f"{DEST} exists; verifying against {SHA[:16]}...")
        if _digest(DEST, _algorithm(SHA)) == SHA:
            log("already complete and verified")
            return 0
        log("cached file does not match the pin; fetching again")
        os.unlink(DEST)

    total = _size()
    log(f"{URL}  {total / 2**30:.2f} GiB  -> {DEST}")

    state: dict[str, int] = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path))
            log(f"state file: {sum(int(v) for v in state.values()) / 2**30:.2f} GiB fetched")
        except (OSError, ValueError):
            state = {}

    per = total // SEGMENTS
    bounds = [(i, i * per, total if i == SEGMENTS - 1 else (i + 1) * per)
              for i in range(SEGMENTS)]

    with open(DEST, "a+b") as fh:
        fh.truncate(total)
    fd = os.open(DEST, os.O_RDWR)
    try:
        log(f"segmenting into {SEGMENTS} parts of {per >> 20} MiB")
        started = time.time()
        with cf.ThreadPoolExecutor(max_workers=SEGMENTS) as pool:
            futures = [pool.submit(_fetch, fd, i, s, e, state_path, state)
                       for i, s, e in bounds]
            while any(not f.done() for f in futures):
                time.sleep(20)
                with _lock:
                    got = sum(int(v) for v in state.values())
                el = max(time.time() - started, 1e-9)
                log(f"  {got / 2**30:.2f} / {total / 2**30:.2f} GiB "
                    f"({got * 100 // max(total, 1)}%)  {got / 2**20 / el:.1f} MiB/s")
            for f in futures:
                f.result()
    finally:
        os.close(fd)

    log("verifying sha256")
    actual = _digest(DEST, _algorithm(SHA))
    if actual != SHA:
        raise SystemExit(f"checksum mismatch:\n  expected {SHA}\n  actual   {actual}")
    log(f"checksum ok: {actual}")
    os.unlink(state_path)
    log("done")
    return 0


def _algorithm(pin: str) -> str:
    """Which hash a pin of this length is: 128 hex chars is sha512, 64 is sha256."""
    return {128: "sha512", 64: "sha256", 32: "md5"}.get(len(pin.strip()), "sha256")


def _digest(path: str, algorithm: str = "sha256") -> str:
    """Hash a file with the named algorithm. The name is the pin, not a guess.

    WHY THE LENGTH DECIDES. Vendors publish sha256, sha512 and occasionally md5, and the
    checksum file says which by the length of the hex it contains - 64 characters is sha256,
    128 is sha512. EndeavourOS publishes only sha512sum, so a fetcher that assumed sha256
    would reject every EndeavourOS ISO as corrupt.

    The algorithm is passed in rather than detected from the file because the caller already
    has the pin: taking the length of THAT string is unambiguous, while sniffing the file is a
    second decision that can disagree with the first.
    """
    h = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
