"""Pull one pinned installer image from this repository's ghcr package.

    python3 lib/fetch_iso.py zorin-core "out/iso/Zorin-OS-18-Core-64-bit-r3.iso"

WHY THIS IS A THIN WRAPPER OVER lib/iso.py

Nothing about pulling is new - `lib/iso.fetch_iso(name, dest)` already does the registry
handshake, the blob download and the digest check, and `lib/images.DESKTOP` already holds
the pins. What was missing is a COMMAND, so a workflow step can say one line instead of
carrying a Python snippet that has to import three modules in the right order.

WHAT IT CHECKS AFTER THE PULL

The size and the sha256 from `lib/images.py` are checked again here, on the file as it sits
on the runner. The registry has its own digest, but a digest says the layer arrived intact -
it does not say the layer is the build whose pixels were measured. Those are two different
claims, and the drivers in linux/* depend on the second one.

NO FALLBACK TO THE VENDOR. If the tag is not published, this stops and says which tag and
which command publishes it; a silent mirror download is a run that succeeds slowly from a
host nobody chose, which is exactly the behaviour lib/iso.py was written to end.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import images   # noqa: E402
import iso      # noqa: E402


def log(msg: str) -> None:
    print(f"[iso] {msg}", flush=True)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("name", help="the pin's name in lib/images.py, e.g. zorin-core")
    p.add_argument("dest", help="where to write the image")
    args = p.parse_args(argv)

    pin = images.DESKTOP.get(args.name)
    if pin is None:
        known = ", ".join(sorted(images.DESKTOP))
        raise SystemExit(
            f"=== NO SUCH DESKTOP IMAGE: {args.name} ===\n  known: {known}")

    os.makedirs(os.path.dirname(os.path.abspath(args.dest)) or ".", exist_ok=True)

    if os.path.exists(args.dest) and pin.get("size") and os.path.getsize(args.dest) == pin["size"]:
        log(f"{args.dest} is already present at the pinned size")
    else:
        log(f"pulling iso-{args.name} from ghcr -> {args.dest}")
        try:
            iso.fetch_iso(args.name, args.dest)
        except Exception as exc:                   # noqa: BLE001 - reported, then fatal
            raise SystemExit(
                f"=== COULD NOT PULL iso-{args.name} ===\n"
                f"  {exc}\n"
                f"  Publish it first:  gh workflow run artifacts-publish.yml "
                f"-f group=iso -f which=iso-{args.name}") from exc

    size = os.path.getsize(args.dest)
    log(f"  bytes: {size}")
    if pin.get("size") and size != pin["size"]:
        raise SystemExit(
            f"=== SIZE MISMATCH for {args.name} ===\n"
            f"  got {size}, the pin says {pin['size']}")

    if pin.get("sha256"):
        actual = sha256_of(args.dest)
        if actual != pin["sha256"]:
            raise SystemExit(
                f"=== SHA256 MISMATCH for {args.name} ===\n"
                f"  expected {pin['sha256']}\n"
                f"  actual   {actual}")
        log("  sha256 ok")

    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
