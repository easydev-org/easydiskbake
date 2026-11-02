"""Compress a finished disk, with the options this qemu-img actually has.

    from compress import compress
    compress("disk.qcow2", "out.qcow2")

WHY THIS IS NOT ONE SUBPROCESS CALL AT EACH CALL SITE

Every bake ended with the same line:

    qemu-img convert -O qcow2 -c -o compression_type=zstd disk.qcow2 out.qcow2

and on 2026-11-02 that line failed every Windows bake at the last step, after a
successful 20-minute installation:

    qemu-img: Invalid parameter 'compression_type'

The image is built FROM qemux/qemu:7.50 but its qemu-img is 2.12.0
(v2.12.0-19097-g85fa07f04ef) - a 2018 build. `compression_type` was added in qemu 5.1,
so the option has never existed in the tool the bake runs. The qemu BINARY is 11.1;
only qemu-img is the old one, which is why nothing before the last step noticed.

WHY THE VERSION IS PROBED AND NOT PARSED

A version number is a claim; the option list is the fact. qemu-img can be a vendor fork
with a distribution suffix (this one is), a backport can carry an option without the
version, and a future release can rename it. So this asks the tool:

    create a 1 MiB qcow2
    qemu-img convert -O qcow2 -c -o help <that file> <temp out>
    read the option names it prints

That is a real conversion of one megabyte, which costs milliseconds, and it answers the
only question that matters: does `-o compression_type=zstd` work here.

WHAT HAPPENS WHEN IT DOES NOT

The output is compressed with qcow2's default (zlib) instead. That is a worse ratio -
zstd is typically 10-20% smaller and several times faster to decompress - but a
publishable disk is the point and the format is the same either way. The caller is told
which one it got, because a disk that is 15% larger than expected should have a reason
in the log rather than look like a difference in the guest.

THE FALLBACK IS ALSO CHECKED. If neither form works the conversion fails and raises,
rather than publishing an uncompressed qcow2 under a name that promises a small one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile


def log(msg: str) -> None:
    print(f"[compress] {msg}", flush=True)


def qemu_img() -> str:
    return os.environ.get("QEMU_IMG", "qemu-img")


def version() -> str:
    try:
        result = subprocess.run([qemu_img(), "--version"], capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(unknown: {exc})"
    return (result.stdout or result.stderr).splitlines()[0].strip() if result.stdout \
        else f"(unknown: exit {result.returncode})"


def supports_zstd() -> bool:
    """Can this qemu-img write a zstd-compressed qcow2?

    Probed by asking qemu-img for its own option list, against a throwaway image. The
    version string is not consulted: see the module docstring for why.
    """
    tmp = tempfile.mkdtemp(prefix="qemu-img-probe-")
    source = os.path.join(tmp, "probe.qcow2")
    target = os.path.join(tmp, "out.qcow2")
    try:
        created = subprocess.run(
            [qemu_img(), "create", "-f", "qcow2", source, "1M"],
            capture_output=True, text=True, timeout=60,
        )
        if created.returncode != 0:
            log(f"could not create a probe image: {created.stderr.strip()[:200]}")
            return False

        # `-o help` prints the format's options and exits without converting. It is
        # supported by 2.12 as well (this is how that version was identified), so it is
        # a fair question to ask of any version.
        result = subprocess.run(
            [qemu_img(), "convert", "-O", "qcow2", "-c", "-o", "help", source, target],
            capture_output=True, text=True, timeout=120,
        )
        text = f"{result.stdout}\n{result.stderr}"
        return "compression_type" in text
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"probe failed: {exc}")
        return False
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def compress(source: str, dest: str) -> str:
    """Convert `source` into a compressed qcow2 at `dest`. Returns the path.

    Never writes `dest` unless the conversion succeeded: qemu-img writes in place, so a
    failure part-way would leave a truncated file with the final name, and the publish
    step would push it. The temporary name is what makes that impossible.
    """
    if not os.path.isfile(source):
        raise FileNotFoundError(f"no disk to compress at {source}")

    stamp = version()
    zstd = supports_zstd()
    if zstd:
        options = ["-o", "compression_type=zstd"]
        log(f"compressing with zstd ({stamp})")
    else:
        options = []
        # Not a silent downgrade: the size difference is real and whoever reads the log
        # should know why the artifact is bigger than the one from a newer qemu-img.
        log(f"compressing with qcow2's default zlib: {stamp} has no "
            f"compression_type option (added in qemu 5.1)")

    tmp = dest + ".part"
    if os.path.exists(tmp):
        os.unlink(tmp)

    args = [qemu_img(), "convert", "-O", "qcow2", "-c", *options, source, tmp]
    log(" ".join(args))
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise RuntimeError(
            f"qemu-img convert failed ({result.returncode}):\n"
            f"{result.stdout[-800:]}\n{result.stderr[-800:]}"
        )

    os.replace(tmp, dest)
    log(f"{dest}: {os.path.getsize(dest) >> 20} MiB "
        f"({os.path.getsize(dest) * 100 // max(os.path.getsize(source), 1)}% of "
        f"{os.path.getsize(source) >> 20} MiB)")
    return dest


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit("usage: compress.py <source.qcow2> [dest.qcow2]")
    source = argv[0]
    dest = argv[1] if len(argv) > 1 else source.replace(".qcow2", "-compressed.qcow2")
    compress(source, dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
