#!/usr/bin/env bash
# Repack a local release candidate; publish/sign only after this transformation.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${1:?Usage: rechunk-image.sh SOURCE_IMAGE OUTPUT_IMAGE}"
OUTPUT="${2:?Supply a separate local output image tag}"
if [[ "$SOURCE" == "$OUTPUT" ]]; then
  echo "Use a separate output tag to retain the original image for comparison" >&2
  exit 2
fi
for command in podman skopeo; do
  command -v "$command" >/dev/null || { echo "Missing dependency: $command" >&2; exit 2; }
done
# Mounting images with rootless overlay storage requires its user namespace.
if (( EUID != 0 )); then
  exec podman unshare bash "$ROOT_DIR/scripts/rechunk-image.sh" "$SOURCE" "$OUTPUT"
fi
# Chunkah v0.7.1, pinned to the image used in the local bandwidth trial.
CHUNKAH=quay.io/coreos/chunkah:v0.7.1@sha256:a2c6ef1cbf47f0c2360fd3c8e580ada5c809f17b2ffc4b0bff9fa5083b980769
SOURCE_ID="$(podman image inspect --format '{{.Id}}' "$SOURCE")"
if [[ "$(podman image inspect --format '{{index .Config.Labels "containers.bootc"}}' "$SOURCE_ID")" != 1 ]]; then
  echo "Source must be a bootc payload" >&2
  exit 2
fi
WORK="$(mktemp -d)"
SOURCE_ROOT=
cleanup() {
  if [[ -n "$SOURCE_ROOT" ]]; then
    podman image unmount "$SOURCE_ID" >/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
CHUNKAH_CONFIG_STR="$(podman image inspect "$SOURCE_ID")"
export CHUNKAH_CONFIG_STR
# A mounted rootfs also works with the older Podman shipped on release runners;
# --mount=type=image requires newer Podman versions.
SOURCE_ROOT="$(podman image mount "$SOURCE_ID")"
# Convert the inherited OSTree image to plain bootc, following Chunkah's
# documented bootc recipe. Retain the rest of the OCI config and annotations.
# A fixed epoch keeps non-RPM build timestamps from defeating layer reuse.
podman run --rm --security-opt label=disable \
  -v "$SOURCE_ROOT:/chunkah:ro" \
  -v "$WORK:/out" -e CHUNKAH_CONFIG_STR "$CHUNKAH" build \
  --max-layers 128 --prune /sysroot/ \
  --label ostree.commit- --label ostree.final-diffid- \
  --source-date-epoch 0 --compressed --output oci:/out/image
skopeo copy "oci:$WORK/image" "containers-storage:$OUTPUT"
bash "$ROOT_DIR/scripts/validate-image.sh" "$OUTPUT"
printf 'Validated rechunked payload: %s\n' "$OUTPUT"
