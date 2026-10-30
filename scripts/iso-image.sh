#!/usr/bin/env bash
# for creating the installer ISO
# requires root
set -euo pipefail
SECONDS=0
trap 'echo "> Total ISO build: ${SECONDS}s (exit $?)"' EXIT

if [ "$EUID" -ne 0 ]; then
  echo "!! This script must be run as sudo / with privileges"
  exit 1
fi

BASE_IMAGE="${MERTENSIA_PAYLOAD_IMAGE:-localhost/mertensiaos:base}"
INSTALLER_IMAGE="${MERTENSIA_INSTALLER_IMAGE:-localhost/mertensiaos:installer}"
BUILDER_IMAGE="${MERTENSIA_BUILDER_IMAGE:-ghcr.io/osbuild/image-builder@sha256:bb4bb67be80131bf149722b2e7dacc039434ee2b68abeb70c86c8221c8281f45}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="$ROOT_DIR/output/installer"
CACHE_DIR="${MERTENSIA_ISO_CACHE_DIR:-$ROOT_DIR/.build/image-builder}"
ISO="$OUTPUT_DIR/mertensiaos-installer.iso"

bash "$ROOT_DIR/scripts/installer-image.sh"

mkdir -p "$OUTPUT_DIR" "$CACHE_DIR/store" "$CACHE_DIR/rpmmd"

# Resolve tags after building so reuse follows their actual contents. Pull an
# absent builder just as podman run would, then fingerprint its immutable ID.
if ! podman image exists "$BUILDER_IMAGE"; then
  podman pull "$BUILDER_IMAGE"
fi
payload_id="$(podman image inspect --format '{{.Id}}' "$BASE_IMAGE")"
installer_id="$(podman image inspect --format '{{.Id}}' "$INSTALLER_IMAGE")"
builder_id="$(podman image inspect --format '{{.Id}}' "$BUILDER_IMAGE")"
inputs=(
  "$payload_id" "$installer_id" "$builder_id"
  "$BASE_IMAGE" "$INSTALLER_IMAGE"
  "$(sha256sum "$ROOT_DIR/scripts/iso-image.sh" "$ROOT_DIR/scripts/iso-cache.py")"
)
if [[ "${MERTENSIA_FORCE_ISO_BUILD:-0}" != 1 ]] && \
   python3 "$ROOT_DIR/scripts/iso-cache.py" check "$ISO" "${inputs[@]}"; then
  echo "> Reusing checksum-verified ISO: $ISO"
  exit 0
fi
python3 "$ROOT_DIR/scripts/iso-cache.py" invalidate "$ISO"

echo "> Building installer ISO"
assembly_start=$SECONDS
podman run --rm \
  --privileged \
  --security-opt label=type:unconfined_t \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  -v "$OUTPUT_DIR:/output" \
  -v "$CACHE_DIR/store:/var/cache/image-builder/store" \
  -v "$CACHE_DIR/rpmmd:/var/cache/image-builder/rpmmd" \
  "$BUILDER_IMAGE" \
    build \
      --cache /var/cache/image-builder/store \
      --rpmmd-cache /var/cache/image-builder/rpmmd \
      --with-buildlog --with-manifest \
      --bootc-ref "$INSTALLER_IMAGE" \
      --bootc-installer-payload-ref "$BASE_IMAGE" \
      --bootc-default-fs ext4 \
      --output-dir /output \
      --output-name mertensiaos-installer \
      bootc-generic-iso

echo "> ISO assembly: $((SECONDS - assembly_start))s"
python3 "$ROOT_DIR/scripts/iso-cache.py" record "$ISO" "${inputs[@]}"
echo "Done!"
