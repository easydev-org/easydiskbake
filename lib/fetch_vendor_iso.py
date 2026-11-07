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
    req = urllib.request.Request(URL, method="HEAD")
    with urllib.request.urlopen(req, timeout=120) as r:
        return int(r.headers["Content-Length"])


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
        req = urllib.request.Request(URL, headers={"Range": f"bytes={pos}-{end - 1}"})
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
        if _sha256(DEST) == SHA:
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
    actual = _sha256(DEST)
    if actual != SHA:
        raise SystemExit(f"checksum mismatch:\n  expected {SHA}\n  actual   {actual}")
    log(f"checksum ok: {actual}")
    os.unlink(state_path)
    log("done")
    return 0


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
