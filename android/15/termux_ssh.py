"""Build the Termux ssh payload: an openssh that runs inside the Android guest.

    python3 android/15/termux_ssh.py --out out/termux-ssh.tar.zst

WHY TERMUX AND NOT A STATIC BUILD OF DROPBEAR

Android's C library is bionic, not glibc. A statically linked Debian binary is foreign on
that kernel ABI, and the first attempt at this shipped exactly that - it compiled, it was
`not a dynamic executable`, and it would not run. Termux's packages are compiled FOR Android:

    INTERP          /system/bin/linker64          the guest's own dynamic loader
    NEEDED          libandroid-support.so         shipped here
    NEEDED          libcrypto.so.3                shipped here
    NEEDED          libldns.so                    shipped here
    NEEDED          libc.so                       the guest's own

WHAT IS IN THE PAYLOAD

Eight packages from Termux's stable repository, resolved through its own Packages index
rather than pinned by hand - a hand-maintained version string is a pin that goes stale on
the next upstream build and fails with a 404 instead of a sentence:

    openssh             sshd, ssh, ssh-keygen, scp, sftp
    openssl             libcrypto.so.3
    ldns                libldns.so
    libandroid-support  Termux's bionic compatibility layer
    ncurses, libedit    ssh's line editing
    zlib                compression
    resolv-conf         a default /etc/resolv.conf

THE PREFIX IS COMPILED INTO THE BINARY. sshd looks for its config and host keys under
/data/data/com.termux/files/usr, so the payload is assembled as that tree and unpacked
there - not at a path of our choosing.

WHAT THIS DOES NOT DO

It does not install anything. `run_qemu.py --ssh-payload` pushes this tree into a RUNNING
guest: /data/data belongs to vold, which creates, labels and encrypts it on first boot, and
pre-creating it in the image produces a guest that restarts forever (see mkdisks.py's
history of that).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile

INDEX_URL = ("https://packages.termux.dev/apt/termux-main/dists/stable/"
             "main/binary-x86_64/Packages")
POOL = "https://packages.termux.dev/apt/termux-main"

# WHAT sshd LINKED AGAINST, READ FROM ITSELF WITH readelf BEFORE THIS LIST WAS WRITTEN:
#
#     libandroid-support.so  libcrypto.so.3  libldns.so  libc.so
#
# libc.so is the guest's, so the packages below are the three that are not, plus what those
# three need to be usable.
PACKAGES = (
    "openssh",
    "openssl",
    "ldns",
    "libandroid-support",
    "ncurses",
    "libedit",
    "zlib",
    "resolv-conf",
    "ca-certificates",
)


def log(msg: str) -> None:
    print(f"[termux {__import__('time').strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(url: str, dest: str) -> None:
    """curl, because the bake images carry it and urllib honours no proxy here.

    THE SAME REASON lib/iso.py sets a User-Agent: some mirrors answer 403 to a bare Python
    request and 200 to curl. This one is a real package host and is fine either way, but one
    code path for downloads is one place to look when a download breaks.
    """
    result = subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", dest, url],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"=== curl failed for {url}: {result.stderr[:300]} ===")


def resolve(index_text: str, wanted: tuple[str, ...]) -> dict[str, str]:
    """{package: Filename} from a Debian Packages index.

    THE INDEX IS THE PIN. Termux rebuilds openssh against a newer openssl every few weeks,
    and a hardcoded filename fails with a 404 the day that happens - while the index names
    whatever the current build is, and its SHA256 is right there beside it.
    """
    found: dict[str, str] = {}
    for entry in index_text.split("\n\n"):
        name = re.search(r"^Package: (.+)$", entry, re.M)
        if not name:
            continue
        package = name.group(1).strip()
        if package not in wanted:
            continue
        filename = re.search(r"^Filename: (.+)$", entry, re.M)
        if filename:
            found[package] = filename.group(1).strip()
    missing = [p for p in wanted if p not in found]
    if missing:
        raise SystemExit(
            f"=== not in the Termux index: {', '.join(missing)} ===\n"
            f"  the index carries {len(index_text.split(chr(10)+chr(10)))} entries; a "
            f"package that disappeared is an upstream change worth seeing")
    return found


def extract(deb: str, into: str) -> None:
    """Unpack a .deb's data member. `dpkg-deb -x` if present, ar+tar otherwise.

    THE BAKE IMAGES DO NOT ALWAYS CARRY dpkg-deb, and this is not the place to find out.
    A .deb is an `ar` archive whose third member is the payload tar - `ar x` and `tar` are
    in every image that can run this at all.
    """
    if shutil.which("dpkg-deb"):
        subprocess.run(["dpkg-deb", "-x", deb, into], check=True)
        return
    work = deb + ".ar"
    os.makedirs(work, exist_ok=True)
    subprocess.run(["ar", "x", deb], cwd=work, check=True)
    data = [n for n in os.listdir(work) if n.startswith("data.tar")]
    if not data:
        raise SystemExit(f"=== {deb} has no data.tar member ===")
    member = data[0]
    zstd = member.endswith(".zst")
    args = ["tar", "-xf", member, "-C", into]
    if zstd:
        args = ["tar", "--zstd", "-xf", member, "-C", into]
    subprocess.run(args, cwd=work, check=True)
    shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/termux-ssh.tar.zst")
    p.add_argument("--keep", action="store_true", help="keep the unpacked tree")
    a = p.parse_args(argv)

    work = os.path.dirname(os.path.abspath(a.out)) or "."
    os.makedirs(work, exist_ok=True)
    debs = os.path.join(work, "termux-debs")
    tree = os.path.join(work, "termux-tree")
    shutil.rmtree(debs, ignore_errors=True)
    shutil.rmtree(tree, ignore_errors=True)
    os.makedirs(debs, exist_ok=True)
    os.makedirs(tree, exist_ok=True)

    index = os.path.join(work, "termux-Packages")
    if not os.path.isfile(index):
        log(f"fetching the index ({len(PACKAGES)} packages wanted)")
        fetch(INDEX_URL, index)
    index_text = open(index, encoding="utf-8", errors="replace").read()
    files = resolve(index_text, PACKAGES)

    prefix = "data/data/com.termux/files/usr"
    for package, path in sorted(files.items()):
        deb = os.path.join(debs, os.path.basename(path).replace("%3a", ":"))
        log(f"{package:20} {os.path.basename(path)}")
        fetch(f"{POOL}/{path}", deb)
        extract(deb, tree)

    # THE PAYLOAD IS THE PREFIX, NOT THE WHOLE .deb LAYOUT. sshd looks under
    # /data/data/com.termux/files/usr, and run_qemu.py pushes this tree into exactly that.
    payload_root = os.path.join(tree, prefix)
    if not os.path.isdir(os.path.join(payload_root, "bin")):
        raise SystemExit(f"=== the packages did not produce {prefix}/bin ===")

    for required in ("bin/sshd", "bin/ssh-keygen", "lib/libcrypto.so.3",
                     "lib/libldns.so", "lib/libandroid-support.so"):
        if not os.path.exists(os.path.join(payload_root, required)):
            raise SystemExit(f"=== {required} is missing from the payload ===")

    # SYMLINKS ARE MATERIALISED HERE, NOT IN THE GUEST. /data/local/tmp refuses to create
    # them (`remote symlink failed: Permission denied`) and `tar -h` does not help - it
    # dereferences what the archive points at, not the links it writes. `cp -rL` replaces
    # each link with a copy of its target, and that is what gets pushed.
    flat = os.path.join(work, "termux-flat")
    shutil.rmtree(flat, ignore_errors=True)
    os.makedirs(flat, exist_ok=True)
    subprocess.run(["cp", "-rL", payload_root + "/.", flat + "/"],
                   capture_output=True, text=True)
    links = sum(1 for root, _, fs in os.walk(flat)
                for f in fs if os.path.islink(os.path.join(root, f)))
    if links:
        raise SystemExit(f"=== {links} symlinks survived cp -rL; adb push will refuse them ===")

    files_count = sum(len(fs) for _, _, fs in os.walk(flat))
    with tarfile.open(a.out, "w:zst") as tf:
        for name in sorted(os.listdir(flat)):
            tf.add(os.path.join(flat, name), arcname=name)
    size = os.path.getsize(a.out) >> 20
    log(f"{files_count} files, {size} MiB -> {a.out}")

    if not a.keep:
        shutil.rmtree(flat, ignore_errors=True)
        shutil.rmtree(tree, ignore_errors=True)
        shutil.rmtree(debs, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
