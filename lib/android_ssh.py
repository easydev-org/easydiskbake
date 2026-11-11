"""Build the Android ssh payload: an openssh that runs INSIDE the guest.

    python3 lib/android_ssh.py --out out/termux-ssh.tar.zst

WHY THIS IS IN lib/ AND NOT IN A STAGE

It is a fact about Android, not about this repository's workflow: bionic is not glibc, so the
server has to come from a publisher that compiles for bionic. That was true on the emulator route
and it is true on BlissOS, so the module is shared rather than copied a second time.

WHY TERMUX AND NOT A STATIC BUILD OF DROPBEAR

Android's C library is bionic, not glibc. A statically linked Debian binary is foreign on that
kernel ABI, and the first attempt at this shipped exactly that - it compiled, `file` called it
"not a dynamic executable", and it would not run. Termux's packages are compiled FOR Android:

    INTERP          /system/bin/linker64          the guest's own dynamic loader
    NEEDED          libandroid-support.so         shipped here
    NEEDED          libcrypto.so.3                shipped here
    NEEDED          libldns.so                    shipped here
    NEEDED          libc.so                       the guest's own

WHAT IS IN THE PAYLOAD

Eight packages from Termux's stable repository, resolved through its own Packages index rather
than pinned by hand - a hand-maintained version string is a pin that goes stale on the next
upstream build and fails with a 404 instead of a sentence:

    openssh             sshd, ssh, ssh-keygen, scp, sftp
    openssl             libcrypto.so.3
    ldns                libldns.so
    libandroid-support  Termux's bionic compatibility layer
    ncurses, libedit    ssh's line editing
    zlib                compression
    resolv-conf         a default /etc/resolv.conf

THE PREFIX IS COMPILED INTO THE BINARY. sshd looks for its config and host keys under
/data/data/com.termux/files/usr, so the payload is assembled as that tree and unpacked there -
not at a path of our choosing.

AND /data IS WHERE IT HAS TO GO, EVEN THOUGH IT IS `nosuid` AND ENCRYPTED.

That combination looks disqualifying and is not, and the difference is worth writing down because
it decides where this whole stage puts things:

    nosuid      forbids a SETUID bit from taking effect. sshd does not need one - it is started AS
                root by adb and then drops privileges itself.
    encrypted   /data is not mounted until vold has the key, which is after init has run its rc
                scripts. So the daemon cannot be in an rc script that runs at boot: it is started
                by this stage, over adb, once the filesystem is there.

That is why the payload is installed into /data rather than into /system: /system is a read-only
squashfs on many Android-x86 builds, and remounting it read-write is a change to the image that
survives only as long as nobody reinstalls.

WHY SYMLINKS ARE MATERIALISED ON THE HOST

`adb push` sends a link as a link, and /data/local/tmp on the guest will not create one:

    adb: error: failed to copy .../lib/libncurses.so.6: remote symlink failed: Permission denied

`tar -h` does NOT help - it dereferences what the archive points AT, not the links it creates, and
869 of them survived it. `cp -rL` walks the extracted tree and writes a COPY of each target in
place of the link, and that is what gets pushed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(REPO, "lib") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "lib"))

INDEX_URL = ("https://packages.termux.dev/apt/termux-main/dists/stable/"
             "main/binary-x86_64/Packages")
POOL = "https://packages.termux.dev/apt/termux-main"

# WHERE THE PAYLOAD GOES, AND IT IS NOT WHERE TERMUX PUTS ITS OWN.
#
# Termux's prefix is `/data/data/com.termux/files/usr`, and it is compiled into sshd as the place
# it looks for its host keys - which is why this module assembles that tree. WHAT IT MUST NOT BE
# IS THE PATH THE PAYLOAD IS INSTALLED AT, and that cost a whole rebuild to find:
#
#     $ ls -ldZ /data/data/com.termux
#     drwx------ 5 u0_a282 u0_a282 u:object_r:app_data_file:s0:c26,c257,c512,c768
#
# That is an APP's private directory. Only uid `u0_a282` - the Termux app - can read it, and
# `adb root` is not that uid in the way that matters:
#
#     $ cp -a /data/data/com.termux/files/usr /data/local/usr
#     cp: bad '/data/data/com.termux/files/usr': Permission denied
#
# on a shell that HAD said `uid=0(root)`. The directory's mode is 700 and its SELinux label is
# `app_data_file`, so init cannot start a daemon there either - which is exactly what the boot
# hook found:
#
#     mkdir: '/data/data/com.termux/files': Permission denied
#     /system/bin/start-sshd.sh[12]: /data/data/com.termux/files/usr/bin/sshd: inaccessible
#
# `/data/local` is `drwxr-x--x root root u:object_r:system_data_file:s0` and a root shell CAN
# create under it - measured. So the payload is installed at PREFIX below and sshd is pointed at
# it with `-f` and `-h`, which is the one thing about the compiled-in path this changes.
PREFIX = "/data/local/sshd"

# WHAT THE PACKAGES ARE FOR, AND HOW THE LIST WAS DERIVED.
#
# THE FIRST VERSION OF THIS LIST WAS DERIVED FROM `bin/sshd` AND WAS INCOMPLETE, twice over,
# because sshd 10.6 splits the per-connection work into `libexec/sshd-session` and THAT binary is
# what links the extra libraries. Two real runs found them, one library each:
#
#     CANNOT LINK EXECUTABLE ".../libexec/sshd-session":
#         library "libtermux-auth.so" not found: needed by main executable
#     CANNOT LINK EXECUTABLE ".../libexec/sshd-session":
#         library "libgssapi_krb5.so.2" not found: needed by main executable
#
# and the failure they produce is a guest whose sshd binds the port, accepts the TCP connection
# and closes it - so the host sees `kex_exchange_identification: Connection closed by remote
# host`, which reads like a config problem in a process nobody is watching.
#
# WHAT sshd LINKED AGAINST, READ FROM ITSELF WITH readelf BEFORE THIS LIST WAS WRITTEN:
#
#     libandroid-support.so  libcrypto.so.3  libldns.so  libc.so
#
# libc.so is the guest's, so the packages below are the three that are not, plus what those three
# need to be usable.
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
    # TERMUX'S OWN AUTHENTICATION LIBRARY, AND THE ONE THAT MADE THE FIRST REAL RUN FAIL.
    #
    # sshd 10.6 split the per-connection work into `libexec/sshd-session`, and THAT binary links
    # a library the main one does not:
    #
    #     CANNOT LINK EXECUTABLE "/data/data/com.termux/files/usr/libexec/sshd-session":
    #     library "libtermux-auth.so" not found: needed by main executable
    #
    # It reads as a broken payload because sshd starts, binds the port, accepts the TCP
    # connection and then closes it - the failure is in a process nobody is watching:
    #
    #     ssh: connect to host 127.0.0.1 port 8022
    #     kex_exchange_identification: Connection closed by remote host
    #
    # which is why this list is resolved from the index rather than guessed at: termux-auth is a
    # dependency of openssh that `readelf` on `bin/sshd` does NOT show, because it is a dependency
    # of the helper that binary spawns.
    "termux-auth",
    # AND krb5, FOR THE SAME REASON AND BY THE SAME MEASUREMENT.
    #
    # `libexec/sshd-session` links two libraries that `bin/sshd` does not, and neither is in
    # openssh's own Depends line - both surfaced the same way, one run at a time:
    #
    #     library "libtermux-auth.so" not found
    #     library "libgssapi_krb5.so.2" not found
    #
    # THE LIST BELOW WAS READ OFF THE HELPER, not guessed a third time:
    #
    #     $ strings libexec/sshd-session | grep '^lib.*\.so' | sort -u
    #     libandroid-support.so  libc.so  libcrypto.so.3
    #     libgssapi_krb5.so.2  libkrb5.so.3  libtermux-auth.so  libz.so.1
    #
    # of which libc.so is the guest's own and the rest are shipped here.
    "krb5",
    # AND THE LIBRARIES krb5 ITSELF LINKS, WHICH IS WHERE THE CHAIN STOPS BEING GUESSABLE.
    #
    #     library "libandroid-glob.so" not found:
    #         needed by .../lib/libgssapi_krb5.so.2
    #
    # FOUR runs, four discovery rounds - `bin/sshd`, then `libexec/sshd-session`, then the
    # libraries that helper pulls in, then THEIR libraries. That is a transitive closure, and
    # writing it out by hand is a list that is wrong the next time Termux rebuilds anything -
    # `krb5`'s own Depends line names `libresolv-wrapper`, and nothing but reading it says so.
    #
    # SO THE PAYLOAD IS CHECKED RATHER THAN TRUSTED, AND THE CHECK IS THE POINT: `build()` walks
    # the closure from `bin/sshd` and refuses a tree with an unreachable library, naming it. Each
    # of the four failures above would have been caught here instead of on a nine-minute boot,
    # and the message says which package to add.
    "libandroid-glob",
    "libresolv-wrapper",
    # AND bash, BECAUSE TERMUX'S sshd HAS THAT PATH COMPILED IN.
    #
    # Measured, after the libraries were resolved and passwords were accepted:
    #
    #     User root not allowed because shell /data/data/com.termux/files/usr/bin/bash
    #     does not exist
    #
    # and `strings bin/sshd | grep bash` shows that path INSIDE THE BINARY - it is not read from
    # /etc/passwd, so writing `/system/bin/sh` there changes nothing. The package is 3 MiB and it
    # is the answer; the alternative is patching a compiled-in string.
    "bash",
    # AND readline, WHICH bash LINKS AND ITS PACKAGE DOES NOT SHIP.
    #
    # The first successful login ended one line later:
    #
    #     CANNOT LINK EXECUTABLE "bash": library "libreadline.so.8" not found
    #
    # `readline` was already installed as krb5's dependency, but as a PACKAGE it is not in bash's
    # Depends line - so a payload built from declared dependencies alone is a payload whose shell
    # does not start. Naming it here makes the closure explicit for the two binaries that matter.
    "readline",
    # AND libiconv, WHICH bash LINKS.
    #
    #     libiconv.so (needed by bin/bash)
    #
    # The fifth library the check found, and the reason it is a CHECK rather than a list: every
    # one of these was discovered by running the thing, and the run that finds it is either a
    # nine-minute boot or this function. `libiconv` provides `libiconv.so`; there is also a
    # `libiconv-static`, which is not this.
    "libiconv",
)

# THE LIBRARIES THE GUEST PROVIDES AND THIS PAYLOAD MUST NOT SHIP. `libc.so` and the bionic
# support it needs come from the system, and shipping a second copy is how a guest ends up with
# two loaders.
GUEST_OWNED = ("libc.so", "libm.so", "libdl.so", "liblog.so", "libstdc++.so",
               "libandroid.so", "libz.so", "linker64", "linker")

# THE PORT TERMUX'S OWN sshd USES, AND THE ONE lib/net.py FORWARDS AS 8022 -> 8022.
SSH_PORT = 8022


def log(msg: str) -> None:
    import time
    print(f"[termux {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(url: str, dest: str) -> None:
    """curl, because the bake images carry it and urllib honours no proxy here."""
    result = subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", dest, url],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed for {url}: {result.stderr[:300]}")


def resolve(index_text: str, wanted: tuple[str, ...]) -> dict[str, str]:
    """{package: Filename} from a Debian Packages index.

    THE INDEX IS THE PIN. Termux rebuilds openssh against a newer openssl every few weeks, and a
    hardcoded filename fails with a 404 the day that happens - while the index names whatever the
    current build is, and its SHA256 is right there beside it.
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
        raise RuntimeError(
            f"not in the Termux index: {', '.join(missing)}\n"
            f"  the index carries {len(index_text.split(chr(10) + chr(10)))} entries; a "
            f"package that disappeared is an upstream change worth seeing")
    return found


def extract(deb: str, into: str) -> None:
    """Unpack a .deb's data member. `dpkg-deb -x` if present, ar+tar otherwise.

    THE BAKE IMAGES DO NOT ALWAYS CARRY dpkg-deb, and this is not the place to find out. A .deb
    is an `ar` archive whose third member is the payload tar - `ar x` and `tar` are in every image
    that can run this at all.
    """
    if shutil.which("dpkg-deb"):
        subprocess.run(["dpkg-deb", "-x", deb, into], check=True)
        return
    work = deb + ".ar"
    os.makedirs(work, exist_ok=True)
    subprocess.run(["ar", "x", deb], cwd=work, check=True)
    data = [n for n in os.listdir(work) if n.startswith("data.tar")]
    if not data:
        raise RuntimeError(f"{deb} has no data.tar member")
    member = data[0]
    args = ["tar", "-xf", member, "-C", into]
    if member.endswith(".zst"):
        args = ["tar", "--zstd", "-xf", member, "-C", into]
    subprocess.run(args, cwd=work, check=True)
    shutil.rmtree(work, ignore_errors=True)


def needed_libraries(elf: str) -> list[str]:
    """The DT_NEEDED names of an ELF, read out of the binary without readelf.

    THE IMAGE HAS NO readelf AND NO objdump - measured - and installing binutils into every bake
    to answer one question is a worse trade than reading the section by hand. DT_NEEDED is a
    sequence of plain NUL-terminated strings in `.dynstr`, referenced by a table of 8-byte
    entries in `.dynamic`; the strings themselves are the `lib*.so*` names, and that is all this
    needs. It over-reports (a string that looks like a library but is not one), which is safe:
    the caller only uses it to ask "is this file in the payload or owned by the guest".
    """
    import re
    try:
        with open(elf, "rb") as fh:
            blob = fh.read()
    except OSError:
        return []
    if not blob.startswith(b"\x7fELF"):
        return []
    return sorted({m.decode("ascii") for m in
                   re.findall(rb"lib[A-Za-z0-9_+.\-]+\.so(?:\.[0-9]+)*", blob)})


def missing_libraries(root: str) -> dict[str, list[str]]:
    """{missing library: [the files that need it]} for one assembled payload tree.

    THIS IS THE CHECK THAT REPLACES GUESSING, and it exists because guessing failed three times
    in a row - each time with the same symptom, which is the expensive part:

        sshd binds port 8022, accepts the TCP connection, and closes it.
        The host reports: kex_exchange_identification: Connection closed by remote host.

    The guest's own error is one line in a child process nobody is watching:

        CANNOT LINK EXECUTABLE ".../libexec/sshd-session":
            library "libgssapi_krb5.so.2" not found: needed by main executable

    So the tree is inspected before it is pushed: every shipped ELF's NEEDED names are collected,
    and any that is neither in the payload nor owned by the guest is reported by name. A tree with
    an unresolved library is a payload that will fail the same way, and it fails here instead -
    before a nine-minute boot.
    """
    shipped: set[str] = set()
    for directory, _, names in os.walk(root):
        for name in names:
            shipped.add(name)
    # THE CLOSURE FROM sshd, WALKED, BECAUSE A LIBRARY NEEDS LIBRARIES TOO.
    #
    # The first version of this check looked only at `bin/sshd` and `libexec/sshd-session`, and it
    # passed a payload that still failed - the missing library was one `libgssapi_krb5.so.2`
    # needed, not one sshd named:
    #
    #     library "libresolv_wrapper.so" not found:
    #         needed by /data/data/com.termux/files/usr/lib/libgssapi_krb5.so.2
    #
    # So the reachable set is walked: start at the two sshd binaries, follow every library that
    # IS shipped, and only judge what cannot be reached at all. `ktutil` and `kadmin` are
    # therefore ignored (nothing sshd loads names them) while `libgssapi_krb5.so.2`'s own needs
    # are fatal, which is the distinction that matters.
    reachable: set[str] = set()
    # THE LOGIN SHELL IS ON THE PATH TOO. Termux's sshd has `bin/bash` compiled in as the shell
    # it demands, and a missing library there fails AFTER authentication:
    #
    #     CANNOT LINK EXECUTABLE "bash": library "libreadline.so.8" not found
    #
    # which looks like a broken login rather than a missing package. Three roots, not two.
    queue = [os.path.join(root, "bin", "sshd"),
             os.path.join(root, "libexec", "sshd-session"),
             os.path.join(root, "bin", "bash")]
    while queue:
        path = queue.pop()
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        for library in needed_libraries(path):
            if library in reachable:
                continue
            reachable.add(library)
            # A SHIPPED LIBRARY IS FOLLOWED; the guest's own are leaves. The search covers
            # `lib/` and the flat `libexec/` tree, because Termux puts helper binaries in both.
            for candidate in (os.path.join(root, "lib", library),
                              os.path.join(root, "libexec", library)):
                if os.path.isfile(candidate):
                    queue.append(candidate)
                    break

    gaps: dict[str, list[str]] = {}
    for directory, _, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            for library in needed_libraries(path):
                if library in shipped:
                    continue
                if any(library == own or library.startswith(own + ".")
                       for own in GUEST_OWNED):
                    continue
                rel = os.path.relpath(path, root)
                # ONLY THE FILES sshd ACTUALLY LOADS ARE FATAL.
                #
                # The Termux packages ship administration tools this stage never runs - ktutil,
                # kadmin, pwlogin, and krb5's db2 KDB plugin - and they legitimately need
                # libraries that only a kadmin user would have. Counting those made the first
                # version of this check refuse a payload that WORKED:
                #
                #     libdb-18.1.so        (needed by lib/krb5/plugins/kdb/db2.so)
                #     libreadline.so.8     (needed by bin/ktutil, bin/kadmin, bin/kadmin.local)
                #     libresolv_wrapper.so (needed by lib/libcom_err.so, ...)
                #     libtermux-exec.so    (needed by bin/pwlogin)
                #
                # What matters is the closure from `bin/sshd` - which loads `libexec/sshd-session`,
                # and that is the binary whose missing libraries are the ones that produced
                # "Connection closed by remote host" three times. So the check is scoped to the
                # files on that path, and a gap elsewhere is a note rather than a refusal.
                # FATAL ONLY IF sshd CAN REACH IT. See the walk above.
                if library in reachable:
                    gaps.setdefault(library, []).append(rel)
    return gaps


def build(work: str) -> str:
    """Assemble the payload as a DIRECTORY of real files. Returns its path."""
    debs = os.path.join(work, "termux-debs")
    tree = os.path.join(work, "termux-tree")
    flat = os.path.join(work, "termux-flat")
    for directory in (debs, tree, flat):
        shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(debs, exist_ok=True)
    os.makedirs(tree, exist_ok=True)
    os.makedirs(flat, exist_ok=True)

    index = os.path.join(work, "termux-Packages")
    if not os.path.isfile(index):
        log(f"fetching the index ({len(PACKAGES)} packages wanted)")
        fetch(INDEX_URL, index)
    index_text = open(index, encoding="utf-8", errors="replace").read()
    files = resolve(index_text, PACKAGES)

    for package, path in sorted(files.items()):
        deb = os.path.join(debs, os.path.basename(path).replace("%3a", ":"))
        log(f"{package:20} {os.path.basename(path)}")
        fetch(f"{POOL}/{path}", deb)
        extract(deb, tree)

    # THE PACKAGES LAND IN TERMUX'S OWN LAYOUT, WHICH IS NOT WHERE THEY ARE INSTALLED. See PREFIX:
    # the tree is what sshd's binaries expect to be sitting in, and `flat` is a copy of its `usr`
    # directory - the same files under a path root can write to.
    termux_prefix = "data/data/com.termux/files/usr"
    payload_root = os.path.join(tree, termux_prefix)
    if not os.path.isdir(os.path.join(payload_root, "bin")):
        raise RuntimeError(f"the packages did not produce {termux_prefix}/bin")

    for required in ("bin/sshd", "bin/ssh-keygen", "lib/libcrypto.so.3",
                     "lib/libldns.so", "lib/libandroid-support.so"):
        if not os.path.exists(os.path.join(payload_root, required)):
            raise RuntimeError(f"{required} is missing from the payload")

    # SYMLINKS ARE MATERIALISED HERE, NOT IN THE GUEST. See the module docstring.
    subprocess.run(["cp", "-rL", payload_root + "/.", flat + "/"],
                   capture_output=True, text=True)
    links = sum(1 for root, _, fs in os.walk(flat)
                for f in fs if os.path.islink(os.path.join(root, f)))
    if links:
        raise RuntimeError(f"{links} symlinks survived cp -rL; adb push will refuse them")

    files_count = sum(len(fs) for _, _, fs in os.walk(flat))
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(flat) for f in fs)
    log(f"{files_count} files, {size >> 20} MiB")

    # AND THE TREE IS CHECKED FOR UNRESOLVED LIBRARIES BEFORE IT IS HANDED TO ANYBODY.
    gaps = missing_libraries(flat)
    if gaps:
        detail = "\n".join(f"  {lib} (needed by {', '.join(users[:3])})"
                            for lib, users in sorted(gaps.items()))
        raise RuntimeError(
            "the payload links libraries it does not ship, and sshd would bind its port and "
            f"then close every connection:\n{detail}\n"
            "  add the Termux packages that provide them to PACKAGES - the index names them, "
            "and `awk '/^Package: /{p=$2} /<lib>/{print p}' Packages` finds the one you need")

    for directory in (debs, tree):
        shutil.rmtree(directory, ignore_errors=True)
    return flat


def start_command() -> str:
    """The guest-side command that starts sshd, with the environment it needs.

    `LD_LIBRARY_PATH` BECAUSE THE BINARIES ARE bionic AND THEIR LIBRARIES ARE NOT IN THE DEFAULT
    SEARCH PATH. Termux's own wrapper sets this, and without it sshd fails before the first line
    with a linker error that names libcrypto.

    `var/empty` IS sshd's PRIVILEGE SEPARATION DIRECTORY, AND IT HAS TO EXIST EMPTY. Without it:

        Missing privilege separation directory:
            /data/data/com.termux/files/usr/var/empty

    `cp -rL` does not create empty directories, so the install has to.
    """
    return (
        f"export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib; "
        f"mkdir -p /data/data/com.termux/files/usr/etc/ssh; "
        f"mkdir -p /data/data/com.termux/files/usr/var/empty; "
        f"chmod 755 /data/data/com.termux/files/usr/var/empty; "
        f"mkdir -p /data/data/com.termux/files/usr/var/run; "
        f"/data/data/com.termux/files/usr/bin/sshd -p {SSH_PORT} "
        f"-h /data/data/com.termux/files/usr/etc/ssh/ssh_host_rsa_key "
        f"-h /data/data/com.termux/files/usr/etc/ssh/ssh_host_ed25519_key"
    )


def keygen_command() -> str:
    """Generate the host keys with the SAME prefix's ssh-keygen, once.

    ONCE, IN THE GUEST, AND KEPT ON THE DISK. A guest that regenerates its host keys on every boot
    gives every caller "REMOTE HOST IDENTIFICATION HAS CHANGED", which trains people to ignore it.
    """
    p = "/data/data/com.termux/files/usr"
    return (
        f"export LD_LIBRARY_PATH={p}/lib; "
        f"mkdir -p {p}/etc/ssh; "
        f"[ -f {p}/etc/ssh/ssh_host_rsa_key ] || "
        f"{p}/bin/ssh-keygen -t rsa -b 2048 -f {p}/etc/ssh/ssh_host_rsa_key -N '' -q; "
        f"[ -f {p}/etc/ssh/ssh_host_ed25519_key ] || "
        f"{p}/bin/ssh-keygen -t ed25519 -f {p}/etc/ssh/ssh_host_ed25519_key -N '' -q; "
        f"echo keys-ok"
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="out/termux-ssh.tar.zst")
    p.add_argument("--keep", action="store_true", help="keep the unpacked tree")
    a = p.parse_args(argv)

    work = os.path.dirname(os.path.abspath(a.out)) or "."
    os.makedirs(work, exist_ok=True)
    flat = build(work)

    # TAR, THEN zstd AS A SEPARATE PROCESS. `tarfile.open(..., "w:zst")` needs Python 3.14; this
    # repository runs 3.13, where it raises `CompressionError: unknown compression type 'zst'`
    # after every package has already been downloaded - the worst possible place to find out.
    plain = a.out + ".tar"
    with tarfile.open(plain, "w") as tf:
        for name in sorted(os.listdir(flat)):
            tf.add(os.path.join(flat, name), arcname=name)
    packed = subprocess.run(["zstd", "-19", "-f", plain, "-o", a.out],
                            capture_output=True, text=True)
    os.unlink(plain)
    if packed.returncode != 0:
        raise RuntimeError(f"zstd failed: {packed.stderr[:300]}")
    log(f"{os.path.getsize(a.out) >> 20} MiB -> {a.out}")

    if not a.keep:
        shutil.rmtree(flat, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
