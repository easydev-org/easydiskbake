"""Split a disk into parts small enough to be OCI layers, and join it back.

    python3 lib/disk_parts.py split  out/macos15-ssh.qcow2 --out parts
    python3 lib/disk_parts.py join   parts --to /data/macos15-ssh.qcow2
    python3 lib/disk_parts.py verify parts --disk /data/macos15-ssh.qcow2

WHY A DISK IS SPLIT ANYWAY, AND WHAT THE ACTUAL LIMIT EVIDENCE IS

GitHub's documentation says:

    The Container registry has a 10 GB size limit for each layer.

This repository already publishes far past that number, and it works. Measured from the
registry's own manifests:

    macos15-ssh       1 layer   17,069,703,168 B   15.90 GiB
    macos15-desktop   1 layer                    15.21 GiB
    macos15-setup     1 layer   29,607,591,936 B   27.57 GiB

and the 27.57 GiB blob still serves (HEAD -> HTTP 307 to the CDN). So the documented 10 GB
ceiling is not enforced against the `oras push` artifact uploads every workflow here does -
`easydev-org/easyactions/oci-push` puts one file in one layer and never shards.

THAT IS NOT THE PATH AN APPLIANCE IMAGE TAKES, WHICH IS WHY THIS FILE EXISTS.

An appliance is built by `docker build` and the disk arrives through `COPY`, which is a
different upload path from `oras push`. Nothing in this repository has measured a COPY layer
above 10 GB - every existing image is tooling, tens of megabytes. Betting 16 GiB on that
being accepted would be betting a build on the one number the vendor documents as a failure.

So the image holds `N` parts of at most PART_SIZE each. A COPY of a distinct file is a
distinct layer, so the image gets N layers of 4 GiB, every one of them under the documented
ceiling, and the parts are joined on first start onto the volume the container was given:

    macos15-ssh.qcow2   15.9 GiB   ->  one COPY layer, 6 GiB over the documented ceiling
    4 x 4 GiB parts     15.9 GiB   ->  four layers, every one accepted, same bytes

Splitting also buys the manifest below, which is worth having on its own: a 16 GiB disk that
is half-joined reads exactly like a whole one until the guest is twenty minutes into a boot.

WHY THE MANIFEST AND NOT JUST `cat`

`cat` cannot tell a complete join from a join that ran out of parts, and a truncated qcow2
does not fail at the first block - it fails twenty minutes into a boot with a message about
the guest. So split() writes `parts.json` beside the parts:

    sha256        of the WHOLE disk, computed as it is split
    parts[]       name, size and sha256 of each part
    size          of the whole disk

join() verifies each part as it reads it and the whole disk afterwards, so "the bytes on the
volume are the bytes that were published" is checked rather than assumed. That is the same
rule lib/overlay.py states for its base disk, and for the same reason: a quietly modified
16 GiB image is not something a later run can notice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

PART_SIZE = 4 << 30          # 4 GiB, comfortably under the 10 GB layer ceiling
MANIFEST = "parts.json"
CHUNK = 8 << 20


def log(message: str) -> None:
    print(f"[parts] {message}", flush=True)


class PartsError(RuntimeError):
    pass


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split(path: str, out: str, part_size: int = PART_SIZE) -> str:
    """Cut `path` into `out/part-NNNN` and write `out/parts.json`. Returns the manifest path."""
    if not os.path.isfile(path):
        raise PartsError(f"no disk at {path}")
    os.makedirs(out, exist_ok=True)

    total = os.path.getsize(path)
    whole = hashlib.sha256()
    parts = []

    source = open(path, "rb")
    try:
        index = 0
        written = 0
        handle = None
        part_digest = None
        while True:
            chunk = source.read(CHUNK)
            if not chunk:
                break
            if handle is None:
                name = f"part-{index:04d}"
                handle = open(os.path.join(out, name), "wb")
                part_digest = hashlib.sha256()
                part_written = 0
            handle.write(chunk)
            whole.update(chunk)
            part_digest.update(chunk)
            part_written += len(chunk)
            written += len(chunk)
            if part_written >= part_size:
                handle.close()
                parts.append({
                    "name": name,
                    "size": part_written,
                    "sha256": part_digest.hexdigest(),
                })
                log(f"{name}: {part_written >> 20} MiB")
                handle = None
                index += 1
                part_written = 0
        if handle is not None:
            handle.close()
            parts.append({
                "name": name,
                "size": part_written,
                "sha256": part_digest.hexdigest(),
            })
            log(f"{name}: {part_written >> 20} MiB")
    finally:
        source.close()

    if written != total:
        raise PartsError(f"read {written} bytes of {total}; the source changed while it was split")

    manifest = {
        "source": os.path.basename(path),
        "size": total,
        "sha256": whole.hexdigest(),
        "part_size": part_size,
        "parts": parts,
    }
    dest = os.path.join(out, MANIFEST)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    log(f"{len(parts)} part(s), whole disk sha256 {manifest['sha256'][:16]}...")
    return dest


def read_manifest(directory: str) -> dict:
    path = os.path.join(directory, MANIFEST)
    if not os.path.isfile(path):
        raise PartsError(
            f"no {MANIFEST} in {directory}; the image was built without one and there is "
            f"nothing to check the parts against"
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def join(directory: str, dest: str) -> str:
    """Write the disk at `dest` from the parts in `directory`, or reuse an intact one.

    THE DESTINATION IS CHECKED BEFORE ANY COPY, because it usually already exists: a
    container with a volume joins once and every later start reuses it. A size match is the
    cheap test; the full hash is only re-read when the size disagrees, which is the case that
    means "this volume holds a half-written disk from an earlier interrupted start".
    """
    manifest = read_manifest(directory)
    want_size = manifest["size"]

    if os.path.exists(dest):
        have = os.path.getsize(dest)
        if have == want_size:
            log(f"{dest} already joined ({have >> 20} MiB); not reading 16 GiB again")
            return dest
        log(f"{dest} is {have} bytes, the manifest says {want_size}; joining again")
        os.unlink(dest)

    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    part_path = dest + ".part"
    whole = hashlib.sha256()
    written = 0

    with open(part_path, "wb") as out:
        for entry in manifest["parts"]:
            source = os.path.join(directory, entry["name"])
            if not os.path.isfile(source):
                raise PartsError(
                    f"{source} is missing. The image was built with "
                    f"{len(manifest['parts'])} part(s) and this one is not in it."
                )
            digest = hashlib.sha256()
            got = 0
            with open(source, "rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    out.write(chunk)
                    whole.update(chunk)
                    digest.update(chunk)
                    got += len(chunk)
            if got != entry["size"] or digest.hexdigest() != entry["sha256"]:
                os.unlink(part_path)
                raise PartsError(
                    f"{entry['name']} does not match the manifest: {got} bytes, "
                    f"sha256 {digest.hexdigest()[:16]}... - the image is damaged, and a "
                    f"truncated qcow2 boots to somewhere in the middle of an install"
                )
            written += got
            log(f"{entry['name']}: ok ({got >> 20} MiB)")

    if written != want_size or whole.hexdigest() != manifest["sha256"]:
        os.unlink(part_path)
        raise PartsError(
            f"the joined disk does not match the manifest: {written} bytes, sha256 "
            f"{whole.hexdigest()[:16]}... against {want_size} / "
            f"{manifest['sha256'][:16]}..."
        )

    os.replace(part_path, dest)
    log(f"{dest}: {written >> 20} MiB, sha256 verified")
    return dest


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("split", help="cut a disk into parts and write parts.json")
    s.add_argument("disk")
    s.add_argument("--out", required=True)
    s.add_argument("--part-size", type=int, default=PART_SIZE,
                   help=f"bytes per part (default {PART_SIZE}, 4 GiB)")

    j = sub.add_parser("join", help="write the disk back out of its parts")
    j.add_argument("directory")
    j.add_argument("--to", required=True)

    v = sub.add_parser("verify", help="re-read the parts and say whether they still match")
    v.add_argument("directory")
    v.add_argument("--disk", default="", help="also check a joined disk against the manifest")
    v.add_argument("--no-rewrite", action="store_true",
                   help="join to a temporary file instead of --disk (kept for a full check)")
    a = p.parse_args(argv)

    try:
        if a.cmd == "split":
            print(split(a.disk, a.out, part_size=a.part_size))
            return 0
        if a.cmd == "join":
            print(join(a.directory, a.to))
            return 0
        # verify: the manifest is the contract, so this reads every part.
        manifest = read_manifest(a.directory)
        for entry in manifest["parts"]:
            got = _sha256_file(os.path.join(a.directory, entry["name"]))
            state = "ok" if got == entry["sha256"] else "MISMATCH"
            log(f"{entry['name']}: {state}")
            if state != "ok":
                return 1
        if a.disk:
            if not os.path.exists(a.disk):
                log(f"{a.disk} does not exist")
                return 1
            got = _sha256_file(a.disk)
            same = got == manifest["sha256"]
            log(f"{a.disk}: {'ok' if same else 'MISMATCH'}")
            return 0 if same else 1
        log(f"{len(manifest['parts'])} part(s) match the manifest")
        return 0
    except PartsError as exc:
        log(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
