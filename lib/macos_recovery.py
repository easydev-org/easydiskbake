"""Fetch a macOS recovery image from Apple, and publish it.

    python3 lib/macos_recovery.py --release 15 --out recovery.dmg
    python3 lib/macos_recovery.py --release 15 --publish

WHY THIS IS THE HARD PART OF A macOS BAKE

There is no installer ISO. macOS installs from a recovery image that Apple serves
per board identifier, behind a session cookie, with opaque hexadecimal nonce fields in
the request and an expiring token on the CDN URL in the response. Every branch of the
exchange is a failure that cost a run, which is why it is lifted from dockur-macos
(src/install.sh) rather than reinvented.

THE EXCHANGE, IN ORDER

    1. GET https://osrecovery.apple.com/ with a spoofed Host and UA.
       The body is useless; the `session=` cookie in the response headers is not.
    2. POST InstallationPayload/RecoveryImage with that cookie and a text body:

           cid=<16 hex>
           sn=00000000000000000
           bid=<board id>          selects the release
           k=<64 hex>
           fg=<64 hex>
           os=latest

    3. The reply is space-delimited key=value text. Two records matter:
           the oscd...dmg URL        the asset
           the ...expires...dmg one  the token that makes it downloadable
       Both are required: the URL alone returns 403.

WHY THE BOARD ID SELECTS THE RELEASE

Apple's catalog is keyed by model. A board identifier that shipped with - or can
upgrade to - the wanted release gets that release's recovery image. The table below is
dockur-macos's, which is the project that keeps it current by running these bakes.

WHY THE IMAGE IS NOT PINNED BY HASH

Every request produces a different URL and a different token, and Apple reissues the
images. A sha256 pin would expire weekly. What is verified instead is that the reply
parsed, the size is plausible for a recovery image, and the file is a valid DMG - the
three things that catch a bad exchange rather than a bad build.

WHY THE HTTP GOES THROUGH CURL

Not because curl is more capable - it is one subprocess per request, which is a cost -
but because urllib failed on this endpoint with

    CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain

against a certificate chain that openssl reports as Apple's own, both through a proxy
and without one. Python's bundled trust store is the variable, and it differs between
a developer machine, this container and a GitHub runner. curl is in the bake image, is
what dockur-macos uses for the same exchange, and its trust store is the system's.

The cost is real: the reply is text either way, but curl's verbose output is where the
session cookie appears, so the parsing here has to read headers rather than a Cookie
object. That is written out below rather than hidden.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

HOST = "osrecovery.apple.com"


def log(msg: str) -> None:
    print(f"[recovery] {msg}", flush=True)


class RecoveryError(RuntimeError):
    pass


# Apple's catalog is keyed by board identifier: a model that shipped with (or can
# upgrade to) the wanted release gets that release's recovery image.
#
# These are not arbitrary - dockur-macos maintains them by running these bakes, and a
# wrong identifier returns either an error or, worse, a different release.
BOARDS = {
    "26": ("tahoe", "Mac-CFF7D910A743CAAF"),
    "15": ("sequoia", "Mac-937A206F2EE63C01"),
    "14": ("sonoma", "Mac-827FAC58A8FDFA22"),
    "13": ("ventura", "Mac-4B682C642B45593E"),
    "12": ("monterey", "Mac-B809C3757DA9BB8D"),
    "11": ("bigsur", "Mac-2BD1B31983FE1663"),
    "10": ("catalina", "Mac-00BE6ED71E35EB86"),
}

# Names people type. Resolved to a release number so the board table stays the single
# source of the mapping.
ALIASES = {
    "tahoe": "26", "sequoia": "15", "sonoma": "14", "ventura": "13",
    "monterey": "12", "bigsur": "11", "big-sur": "11", "catalina": "10",
}

UA = "InternetRecovery/1.0"
MIN_PLAUSIBLE = 100 * 1024 * 1024   # a recovery image is hundreds of MB


def fetch_published(release: str, work: str) -> str:
    """Put this repository's published recovery image for `release` in `work`.

        import macos_recovery
        recovery = macos_recovery.fetch_published("15", work)

    WHY THIS IS ONE FUNCTION INSTEAD OF THREE COPIES

    Every macOS stage after the bake needs this file, and none of them inherits it: a
    job pulls one disk from ghcr and nothing else, so `work` starts with the disk and no
    recovery image. opencore.build() then fails on the APFS driver, because that driver
    is extracted from BaseSystem inside the recovery image and there is no other source
    for it.

    That was learned three times, in three stages, each fixed separately:

        bake.py       fetches it at the top of the bake                    -> fine
        desktop.py    had assumed it would be there, and run 41773825483
                      died at the first build step                        -> its own copy
        ssh.py        had assumed it too, and run 41979713660 died after
                      setup and desktop had both succeeded                -> this function

    The copies also disagreed about the file name: bake.py writes
    recovery-sequoia.dmg because a work directory can hold more than one release, while
    desktop.py wrote whatever opencore.recovery_image() returned, which is
    recovery.dmg. Both are accepted by opencore.recovery_image() and neither is wrong,
    but a fourth stage writing a third spelling is how a work directory ends up with two
    images and the wrong one being picked.

    THE TAG COMES FROM THE BOARD TABLE above, which is where the release-name mapping
    already lives. A stage that hard-codes "sequoia" works for exactly one release and
    silently names the wrong image for every other one.

    NO FALLBACK TO APPLE. If the tag is not in the package the error says to run
    artifacts-publish.yml, rather than starting an 850 MiB download that hides the
    misconfiguration until it fails somewhere else.
    """
    import os

    # NOQA: iso is imported by sys.path, the way every other module in lib/ does it -
    # see lib/opencore.py, lib/pull_disk.py, lib/publish_iso.py.
    import iso  # noqa: E402

    name = resolve(release)[0]
    tag = f"macos-recovery-{name}"

    # ONE NAME, so the stages agree by construction about which file they used.
    dest = os.path.join(work, f"recovery-{name}.dmg")
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        log(f"  {dest} already present ({os.path.getsize(dest) >> 20} MiB)")
        return dest

    log(f"  pulling {tag}")
    try:
        return iso.fetch_tag(tag, dest)
    except iso.IsoError as exc:
        raise SystemExit(
            f"=== NO RECOVERY IMAGE AND IT COULD NOT BE PULLED ===\n"
            f"  {tag} is not in this repository's ghcr package: {exc}\n"
            f"  The APFS driver is extracted from this image and has no other source,\n"
            f"  so the disk this stage would produce could not boot.\n"
            f"  Run artifacts-publish.yml for {release} first:\n"
            f"      gh workflow run artifacts-publish.yml -f which=macos-recovery-{name}"
        ) from exc


def resolve(release: str) -> tuple[str, str, str]:
    """release -> (name, release number, board id)."""
    key = ALIASES.get(release.lower(), release)
    # "15.1" and "15*" both mean 15: the catalog is per major release.
    major = re.match(r"^(\d+)", key)
    if not major:
        raise RecoveryError(
            f"unknown release {release!r}; known: "
            f"{', '.join(sorted(BOARDS, key=int))} or their names"
        )
    number = major.group(1)
    if number not in BOARDS:
        raise RecoveryError(
            f"no board identifier for release {release!r}; known: "
            f"{', '.join(sorted(BOARDS, key=int))}"
        )
    name, board = BOARDS[number]
    return name, number, board


def _curl(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    """One curl invocation, with the headers Apple expects on this endpoint.

    --disable keeps a user ~/.curlrc out of the request: a stray proxy or user agent
    there would change what Apple sees and the exchange is picky about both.
    """
    base = [
        "curl", "--disable", "--silent", "--show-error",
        "--max-time", str(timeout),
        "--header", f"Host: {HOST}",
        "--header", "Connection: close",
        "--user-agent", UA,
    ]
    return subprocess.run(base + args, capture_output=True, text=True)


def _session() -> str:
    """Step 1: the cookie, read out of curl's verbose header dump.

    The body is not read and not useful. `-D -` writes the response headers to stdout,
    which is cheaper to parse than -v's mixed stream and does not include the request.
    """
    result = _curl(["-D", "-", "-o", os.devnull, f"https://{HOST}/"], timeout=30)
    if result.returncode != 0:
        raise RecoveryError(
            f"could not reach {HOST}: {result.stderr.strip()[:300]}"
        )
    m = re.search(r"(?im)^set-cookie:\s*session=([^;\s]+)", result.stdout)
    if not m:
        raise RecoveryError(
            "no session cookie in the response.\n"
            "Apple changes this endpoint occasionally; check that osrecovery.apple.com\n"
            "still sets `session=` on a plain GET with the InternetRecovery user agent."
        )
    return m.group(1).strip('"')


def _payload(board: str, os_type: str = "latest") -> bytes:
    """Step 2's body. The nonces are opaque; only their shape matters."""
    fields = [
        ("cid", secrets.token_hex(8).upper()),   # 16 hex
        ("sn", "0" * 17),                       # serial: the placeholder Apple accepts
        ("bid", board),
        ("k", secrets.token_hex(32).upper()),   # 64 hex
        ("fg", secrets.token_hex(32).upper()),  # 64 hex
        ("os", os_type),
    ]
    return "\n".join(f"{k}={v}" for k, v in fields).encode()


def _request_image(board: str) -> str:
    """Step 2 and 3: POST and read the reply."""
    session = _session()
    log(f"session {session[:12]}...")

    payload_file = os.path.join("/tmp", f"apple-{os.getpid()}.payload")
    with open(payload_file, "wb") as fh:
        fh.write(_payload(board))
    try:
        result = _curl(
            [
                "--request", "POST",
                "--cookie", f'session="{session}"',
                "--header", "Content-Type: text/plain",
                "--data-binary", f"@{payload_file}",
                f"https://{HOST}/InstallationPayload/RecoveryImage",
            ],
            timeout=90,
        )
    finally:
        try:
            os.unlink(payload_file)
        except OSError:
            pass

    if result.returncode != 0:
        raise RecoveryError(
            f"Apple rejected the recovery request (curl {result.returncode}):\n"
            f"{result.stderr.strip()[:400]}"
        )

    body = result.stdout
    if "oscdn" not in body:
        raise RecoveryError(
            "the reply contained no oscd URL. Apple returns one of these when the\n"
            "board identifier is unknown or the request shape changed:\n"
            + body[:400]
        )
    return body


def parse_urls(body: str) -> tuple[str, str, str]:
    """Pull the asset URL and its token out of the reply.

    THE REPLY'S SHAPE, which is not what it looks like at first:

        AP: 093-10615
        AU: http://oscdn.apple.com/.../BaseSystem.dmg
        AH: F0B5184B...                       the asset's sha256
        AT: expires=1793605576~access=/content/...~md5=19c169...
        CU: http://oscdn.apple.com

    AT is the token, and it is one field containing `~`-separated attributes. The
    first version of this looked for a token that ended in ".dmg" and found none,
    because the `.dmg` lives inside `access=` rather than at the end of the record.

    The URL is fetched with the token as its query string, which is what Apple's own
    installer does - `?` then the AT value verbatim.
    """
    def field(key: str) -> str | None:
        # Values run to end of line. They contain '=' and '/' and '~', so no split on
        # whitespace and no split on '='.
        m = re.search(rf"(?m)^{key}:\s*(\S.*?)\s*$", body)
        return m.group(1) if m else None

    url = field("AU")
    token = field("AT")

    if not url:
        raise RecoveryError(f"no AU (asset URL) in the reply:\n{body[:400]}")
    if not token:
        raise RecoveryError(
            f"found the asset URL but no AT (token); the download would 403.\n"
            f"reply: {body[:400]}"
        )

    # The token's own md5 belongs to the asset, and AH is that same hash in a different
    # field. Disagreeing means the reply was assembled from two different requests.
    md5 = re.search(r"md5=([0-9a-f]{32})", token)
    if md5:
        log(f"asset md5 {md5.group(1)}")

    return url, token, body


def _fetch(url: str, token: str, part: str) -> subprocess.CompletedProcess:
    """Download with aria2c. There is no other path.

    WHY aria2c AND NOT curl

    An 847 MiB transfer from oscd.apple.com gets cut short with one connection. Measured
    here, curl reported

        curl: (18) end of response with 887566699 bytes missing
        ... 20971520 bytes

    twenty megabytes in, repeatedly, on a connection that a HEAD says is healthy. That
    is not a token problem and not a rate limit - it is one long-lived connection being
    reset, which is what a segmented downloader exists to survive.

    aria2c splits the file, resumes each segment and reconnects per segment. It is also
    what dockur-macos uses (it cleans up `$dest.aria2`, aria2's control file), so this
    is the same approach that project settled on for the same CDN.

    THIS USED TO FALL BACK TO CURL. The fallback was removed: the case it covered -
    aria2c absent - is a broken image, and the fallback turned that into a download that
    looks like it is working and then stops at twenty megabytes with a message about a
    missing token. `aria2` is in the Containerfile; a host running this script needs it.

    The Cookie header and Host are the load-bearing part: the token is a cookie rather
    than a query parameter, and the CDN checks the Host.
    """
    if not shutil.which("aria2c"):
        raise RecoveryError(
            "aria2c is not installed, and this download has no other path.\n"
            "It is in the bake image (see Containerfile); a host running this needs\n"
            "`apt-get install aria2`. A single-connection download is cut short by this\n"
            "CDN around twenty megabytes, which is why curl is not an alternative."
        )

    args = [
        "aria2c",
        "--max-connection-per-server=8",
        "--split=8",
        "--min-split-size=8M",
        "--max-tries=10",
        "--retry-wait=5",
        "--timeout=120",
        "--connect-timeout=30",
        "--continue=true",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--file-allocation=none",
        "--summary-interval=30",
        "--console-log-level=warn",
        "--user-agent", UA,
        "--header", f"Cookie: AssetToken={token}",
        "--header", "Connection: close",
        "--dir", os.path.dirname(part) or ".",
        "--out", os.path.basename(part),
        url,
    ]
    return subprocess.run(args, capture_output=True, text=True)


def download(url: str, token: str, dest: str) -> str:
    """Step 4: fetch the asset.

    THE TOKEN IS A COOKIE, NOT A QUERY PARAMETER.

    This is the mistake that cost several attempts: `AT:` looks like a signed query
    string (`expires=...~access=...~md5=...`), so appending it to the URL looks right.
    dockur-macos does not - it sends

        -H "Cookie: AssetToken=${downloadSession}"

    with the URL unmodified, and its header comment is the whole explanation:
    "The recovery response supplies the CDN URL and its expiring token as separate
    records; both are required for the authenticated download."

    A URL with the token glued on gets a 200 with an error body, or a partial transfer,
    because the CDN parses the query string and finds attributes it did not ask for.

    The Host header is set explicitly: the CDN is picky about it and curl would derive
    it from the URL, which is `oscdn.apple.com` here but not always.
    """
    part = dest + ".part"
    if os.path.exists(part):
        os.unlink(part)

    log(f"downloading {url.rsplit('/', 1)[-1]} from oscd.apple.com")

    started = time.time()
    result = _fetch(url, token, part)

    elapsed = max(1.0, time.time() - started)

    if result.returncode != 0:
        size = os.path.getsize(part) if os.path.exists(part) else 0
        raise RecoveryError(
            f"the CDN download failed ({result.returncode}), {size} bytes:\n"
            f"{result.stderr.strip()[:400]}"
        )
    elapsed = max(1.0, time.time() - started)

    if result.returncode != 0:
        size = os.path.getsize(part) if os.path.exists(part) else 0
        raise RecoveryError(
            f"the CDN download failed (curl {result.returncode}), {size} bytes:\n"
            f"{result.stderr.strip()[:400]}\n"
            f"A 403 here almost always means the token did not match the URL."
        )

    got = os.path.getsize(part)
    log(f"{got} bytes in {elapsed:.0f}s ({got / elapsed / 1024 / 1024:.1f} MiB/s)")

    if got < MIN_PLAUSIBLE:
        # A body this small after a 200 means the transfer never started. Report what
        # is actually in the file rather than blaming the board identifier: the 502-body
        # case this used to misdiagnose was curl aborting on a speed limit.
        try:
            head = open(part, "rb").read(200)
        except OSError:
            head = b""
        os.unlink(part)
        raise RecoveryError(
            f"only {got} bytes, which is not an image.\n"
            f"curl exited {result.returncode}. First bytes: {head[:120]!r}\n"
            f"If this is an HTTP error body, the token did not match the URL; if it is\n"
            f"gzip data, the transfer was cut short."
        )

    os.replace(part, dest)
    return dest


def verify_dmg(path: str) -> None:
    """A UDIF image ends with a 512-byte trailer that STARTS with "koly".

    The magic is the FIRST FOUR BYTES OF THE TRAILER, not of the file and not of its
    last four bytes. Measured on the real asset:

        first 64 bytes:   78 5e ...           zlib - normal, UDIF chunks are compressed
        last 512 (first): "koly\\x00\\x00\\x00\\x04"
        last 512 (last):  \\x00\\x00\\x00\\x00...

    So a file that begins with zlib data is a perfectly good DMG, and a check on the
    last four bytes of the file rejects every one of them. That mistake cost a run:
    three valid 800 MB downloads were thrown away.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        if size < 512:
            raise RecoveryError(f"{path} is {size} bytes; too small to be a DMG")
        fh.seek(size - 512)
        trailer = fh.read(512)

    if trailer[:4] != b"koly":
        raise RecoveryError(
            f"{path} does not look like a UDIF image: the 512-byte trailer at the end\n"
            f"begins {trailer[:4]!r}, not b'koly'. size={size}"
        )
    log(f"{size} bytes, DMG trailer present")


def fetch(release: str, dest: str) -> str:
    name, number, board = resolve(release)
    log(f"release {number} ({name}), board {board}")

    if os.path.exists(dest) and os.path.getsize(dest) >= MIN_PLAUSIBLE:
        log(f"{dest} already present")
        try:
            verify_dmg(dest)
            return dest
        except RecoveryError:
            log("the cached file is not a DMG; fetching again")
            os.unlink(dest)

    body = _request_image(board)
    url, token, _ = parse_urls(body)
    download(url, token, dest)
    verify_dmg(dest)
    log(f"ok: {dest}")
    return dest


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--release", required=True,
                   help="release number or name: 15, sequoia, 14, sonoma, ...")
    p.add_argument("--out", default=None, help="destination .dmg")
    p.add_argument("--publish", action="store_true",
                   help="push to ghcr as :macos-recovery-<release>")
    p.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = p.parse_args(argv)

    name, number, _ = resolve(args.release)
    dest = args.out or f"recovery-{name}.dmg"

    try:
        fetch(args.release, dest)
    except RecoveryError as exc:
        log(f"FAILED: {exc}")
        return 1

    if args.publish:
        if not args.repository:
            log("FAILED: --repository (or $GITHUB_REPOSITORY) is required to publish")
            return 1
        # Imported here so a plain fetch does not need oras installed.
        import ci
        import publish_disk

        try:
            ci.install_oras()
            # oras reads the same credential store docker does, so a push without this
            # is an anonymous push - which ghcr answers with "requested access to the
            # resource is denied" rather than "not logged in", because by then it is
            # talking about a specific manifest.
            ci.login("ghcr.io")
            publish_disk.push(
                dest, f"macos-recovery-{name}", ci.lowercase_repository(),
                kind="macos-recovery",
                media_type="application/vnd.easydev.dmg",
            )
        except ci.Failed as exc:
            log(f"FAILED: {exc}")
            return 1

    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main(sys.argv[1:]))
