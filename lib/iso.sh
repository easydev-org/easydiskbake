# Fetch a distribution's install image from ghcr, and only from ghcr.
#
# WHY THE REGISTRY AND NOT THE MIRROR
#
# Every bake used to download its own image from whatever host the distribution
# points at, at run time, with the job clock running. SourceForge served MX at
# 967 KB/s and then hung for fifty-six minutes with an empty log; Mint's mirror
# answers at 124 KB/s, which is forty minutes for three gigabytes. Those are the two
# that were measured; the other fourteen were never watched closely enough to know.
#
# publish-isos.yml fetches each image once, checks it three ways (exact byte count,
# ISO9660 signature, a stalled-transfer abort) and pushes it as an OCI layer to
# ghcr. This pulls it back.
#
# NO FALLBACK, DELIBERATELY. A function that quietly falls back to the mirror is a
# function whose failures are invisible: the run succeeds, slowly, from a host nobody
# chose, and the cache is never noticed to be broken. If ghcr does not have the image
# the bake stops and says so.
#
# WHY RAW REGISTRY CALLS AND NOT oras
#
# The runner image is qemux/qemu with tesseract added, and nothing else. oras would be
# another download at run time to solve a download problem - and the whole point is
# that the image carries what it needs. The pull is three requests: token, manifest,
# blob.

ISO_REGISTRY="${ISO_REGISTRY:-ghcr.io}"
ISO_OWNER="${ISO_OWNER:-easydev-org}"

# bake_fetch_iso <name> <dest>
#
# <name> is the tag publish-isos.yml pushed under, e.g. "ubuntu" or "pop-os".
bake_fetch_iso() {
  local name="$1" dest="$2"
  local repo="$ISO_REGISTRY/$ISO_OWNER/iso"
  local token_file manifest blob_digest blob_size got

  [ -n "${GITHUB_TOKEN:-}" ] || {
    echo "bake_fetch_iso: GITHUB_TOKEN is not set; the iso packages are private" >&2
    return 1
  }

  if [ -s "$dest" ]; then
    echo "[iso] $dest already present ($(stat -c%s "$dest") bytes)"
    return 0
  fi

  echo "[iso] fetching $name from $repo"

  # 1. A pull token for this repository. The package is private, so an anonymous
  #    request gets 401 and no manifest.
  token_file=$(mktemp)
  if ! curl -fsS --max-time 60 \
       "https://$ISO_REGISTRY/token?scope=repository:$ISO_OWNER/iso:pull&service=$ISO_REGISTRY" \
       -u "x:$GITHUB_TOKEN" -o "$token_file"; then
    echo "[iso] could not get a registry token" >&2
    rm -f "$token_file"
    return 1
  fi
  local token
  token=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$token_file")
  rm -f "$token_file"

  # 2. The manifest, which names the layer.
  manifest=$(mktemp)
  if ! curl -fsS --max-time 60 -H "Authorization: Bearer $token" \
       -H "Accept: application/vnd.oci.image.manifest.v1+json" \
       "https://$ISO_REGISTRY/v2/$ISO_OWNER/iso/manifests/$name" -o "$manifest"; then
    echo "[iso] no manifest for tag '$name' in $repo" >&2
    echo "[iso] run publish-isos.yml for that name; this bake will not fall back to a mirror" >&2
    rm -f "$manifest"
    return 1
  fi
  blob_digest=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["layers"][0]["digest"])' "$manifest")
  blob_size=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["layers"][0]["size"])' "$manifest")
  rm -f "$manifest"
  echo "[iso] layer $blob_digest, $blob_size bytes"

  # 3. The blob.
  if ! curl -fSL --retry 4 --retry-delay 5 --retry-all-errors \
       --max-time 3600 --speed-time 120 --speed-limit 20480 \
       -H "Authorization: Bearer $token" \
       "https://$ISO_REGISTRY/v2/$ISO_OWNER/iso/blobs/$blob_digest" -o "$dest.part"; then
    echo "[iso] blob download failed; partial $(stat -c%s "$dest.part" 2>/dev/null || echo 0) bytes" >&2
    return 1
  fi

  got=$(stat -c%s "$dest.part")
  if [ "$got" != "$blob_size" ]; then
    echo "[iso] size mismatch: got $got, the manifest says $blob_size" >&2
    return 1
  fi
  mv "$dest.part" "$dest"
  echo "[iso] $dest is $got bytes"
}
