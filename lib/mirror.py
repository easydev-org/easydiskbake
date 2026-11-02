"""Copy an image from one OCI registry to another, layer by layer.

    python3 lib/mirror.py \
        --from ghcr.io/easydev-org/easydiskbake --tag iso-windows11 \
        --to forgejo.develop.fenjin.local/fenjin/easydiskbake

WHY THIS EXISTS

The Forgejo runner sits on the same cluster as the bake, and pulling 7.7 GB from
ghcr.io at 40 KB/s is the whole reason the Forgejo bake stalled. The same bytes from
the cluster's own registry are a local transfer.

So the images are copied once, by a job that can reach both, and the workflows pull
from the local one. The copy is a registry-to-registry stream: each layer is read from
the source and written to the destination as it arrives, never held whole in memory and
never written to disk.

WHY NOT skopeo

skopeo would be the obvious tool and is not in the bake image. Adding it means another
package in an image that is already 1.5 GB, for a job that runs once per image. The
registry API is three requests per layer and needs nothing.

WHAT IS COPIED

Every tag given on the command line, or --all for the ones this repository publishes.

WHAT IS NOT

The old date tags (2026.11.02) and any disk tags: those are outputs, and a run that
needs one of them is a run that can build it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

# The media types a manifest may declare. Both have to be passed as Accept or a
# registry may answer with a manifest list this code does not unwrap.
ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def log(msg: str) -> None:
    print(f"[mirror] {msg}", flush=True)


class MirrorError(RuntimeError):
    pass


class Registry:
    """One registry, with whatever credentials it needs.

    `username`/`password` may be empty, which means anonymous - the case for ghcr.io,
    where this repository's package is public.
    """

    def __init__(self, host: str, *, username: str = "", password: str = "",
                 scheme: str = "https"):
        self.host = host
        self.username = username
        self.password = password
        self.scheme = scheme
        self._token: str | None = None

    def _auth_header(self) -> dict[str, str]:
        if not self.username:
            return {}
        raw = f"{self.username}:{self.password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def _token_for(self, repository: str, scope: str = "pull,push") -> str:
        """A bearer token, or empty string when the registry does not use them.

        ghcr issues tokens; a plain registry answers 404 here and expects Basic auth on
        every request instead. Both are handled by trying the token endpoint once and
        remembering the answer.
        """
        if self._token is not None:
            return self._token

        url = (f"{self.scheme}://{self.host}/token"
               f"?scope=repository:{repository}:{scope}&service={self.host}")
        req = urllib.request.Request(url)
        for key, value in self._auth_header().items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                self._token = json.load(resp).get("token", "")
        except (urllib.error.HTTPError, urllib.error.URLError):
            self._token = ""
        return self._token

    def request(self, path: str, *, method: str = "GET", data=None,
                headers: dict | None = None, timeout: int = 120):
        url = f"{self.scheme}://{self.host}{path}"
        req = urllib.request.Request(url, method=method, data=data)
        token = self._token
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        else:
            for key, value in self._auth_header().items():
                req.add_header(key, value)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        return urllib.request.urlopen(req, timeout=timeout)

    def manifest(self, repository: str, reference: str) -> tuple[dict, bytes, str]:
        self._token_for(repository, "pull")
        with self.request(f"/v2/{repository}/manifests/{reference}",
                          headers={"Accept": ACCEPT}) as resp:
            raw = resp.read()
            return json.loads(raw), raw, resp.headers.get("Content-Type", "")

    def blob(self, repository: str, digest: str):
        self._token_for(repository, "pull")
        return self.request(f"/v2/{repository}/blobs/{digest}", timeout=600)

    def has_blob(self, repository: str, digest: str) -> bool:
        self._token_for(repository, "pull,push")
        try:
            with self.request(f"/v2/{repository}/blobs/{digest}", method="HEAD") as resp:
                return resp.status == 200
        except urllib.error.HTTPError:
            return False

    def start_upload(self, repository: str, digest: str) -> str:
        self._token_for(repository, "pull,push")
        with self.request(f"/v2/{repository}/blobs/uploads/", method="POST") as resp:
            location = resp.headers.get("Location")
        if not location:
            raise MirrorError("the registry did not return an upload location")
        # The location may be absolute or a path.
        if location.startswith("http"):
            location = location.split(self.host, 1)[-1]
        return location

    def upload_blob(self, repository: str, digest: str, stream, size: int) -> None:
        """Stream one layer from `stream` into the registry.

        Content-Length is set from the manifest rather than computed, because the
        source does not always declare one and a chunked PUT is rejected by some
        registries. The bytes are hashed as they pass so a short or corrupt transfer
        is caught here rather than stored under a digest it does not match.
        """
        location = self.start_upload(repository, digest)
        sep = "&" if "?" in location else "?"
        put_path = f"{location}{sep}digest={digest}"

        self._token_for(repository, "pull,push")
        url = f"{self.scheme}://{self.host}{put_path}"

        class Reader:
            def __init__(self, source, expected: int):
                self.source = source
                self.remaining = expected
                self.hasher = hashlib.sha256()

            def read(self, amount: int = -1) -> bytes:
                if self.remaining <= 0:
                    return b""
                want = self.remaining if amount is None or amount < 0 else min(amount, self.remaining)
                chunk = self.source.read(want)
                self.remaining -= len(chunk)
                if chunk:
                    self.hasher.update(chunk)
                return chunk

        body = Reader(stream, size)

        req = urllib.request.Request(url, method="PUT", data=body)
        req.add_header("Content-Length", str(size))
        token = self._token
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        else:
            for key, value in self._auth_header().items():
                req.add_header(key, value)
        req.add_header("Content-Type", "application/octet-stream")
        try:
            with urllib.request.urlopen(req, timeout=1800) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            raise MirrorError(
                f"uploading {digest} failed: HTTP {exc.code}\n"
                f"{(exc.read() or b'')[:400].decode('utf-8', 'replace')}"
            ) from exc

        if body.remaining != 0:
            raise MirrorError(
                f"{digest}: the source ended {body.remaining} bytes early"
            )

    def put_manifest(self, repository: str, reference: str, raw: bytes,
                     content_type: str) -> None:
        self._token_for(repository, "pull,push")
        with self.request(f"/v2/{repository}/manifests/{reference}", method="PUT",
                          data=raw,
                          headers={"Content-Type": content_type}) as resp:
            resp.read()


def copy_tag(source: Registry, source_repo: str, dest: Registry, dest_repo: str,
             tag: str) -> None:
    log(f"=== {source_repo}:{tag} -> {dest_repo}:{tag} ===")
    manifest, raw, content_type = source.manifest(source_repo, tag)

    layers = manifest.get("layers") or []
    config = manifest.get("config") or {}

    # Configuration first, then layers: the manifest that references them is written
    # last, and a registry rejects a manifest whose blobs are missing.
    blobs = [config] if config else []
    blobs += layers

    total = sum(b.get("size", 0) for b in blobs)
    log(f"{len(blobs)} blob(s), {total / 1024 / 1024:.0f} MiB")

    done = 0
    for blob in blobs:
        digest = blob.get("digest")
        size = blob.get("size", 0)
        if not digest:
            continue
        if dest.has_blob(dest_repo, digest):
            log(f"  {digest[7:19]} already present")
            done += size
            continue

        log(f"  {digest[7:19]} {size / 1024 / 1024:.0f} MiB")
        with source.blob(source_repo, digest) as stream:
            dest.upload_blob(dest_repo, digest, stream, size)
        done += size
        log(f"    {done / 1024 / 1024:.0f} / {total / 1024 / 1024:.0f} MiB")

    dest.put_manifest(dest_repo, tag, raw, content_type or
                      "application/vnd.oci.image.manifest.v1+json")
    log(f"{dest_repo}:{tag} copied")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--from", dest="source_host", default="ghcr.io")
    p.add_argument("--from-repo", default="easydev-org/easydiskbake")
    p.add_argument("--from-user", default="")
    p.add_argument("--from-password", default=os.environ.get("GHCR_TOKEN", ""))
    p.add_argument("--to", dest="dest_host", required=True)
    p.add_argument("--to-repo", required=True)
    p.add_argument("--to-user", default=os.environ.get("FORGEJO_USER", "fenjin"))
    p.add_argument("--to-password", default=os.environ.get("FORGEJO_TOKEN", ""))
    p.add_argument("--to-scheme", default="http")
    p.add_argument("--tag", action="append", default=[],
                   help="tag to copy; repeatable")
    p.add_argument("--all", action="store_true",
                   help="copy the tags the workflows read: latest, iso-windows11, "
                        "iso-virtio-win")
    args = p.parse_args(argv)

    tags = list(args.tag)
    if args.all:
        tags += ["latest", "iso-windows11", "iso-virtio-win"]
    if not tags:
        p.error("no tags given; use --tag or --all")

    source = Registry(args.source_host, username=args.from_user,
                      password=args.from_password)
    dest = Registry(args.dest_host, username=args.to_user,
                    password=args.to_password, scheme=args.to_scheme)

    failed = []
    for tag in tags:
        try:
            copy_tag(source, args.from_repo, dest, args.to_repo, tag)
        except (MirrorError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            log(f"FAILED {tag}: {exc}")
            failed.append(tag)

    if failed:
        log(f"failed: {', '.join(failed)}")
        return 1
    log(f"copied {len(tags)} tag(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
