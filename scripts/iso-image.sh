#!/usr/bin/env bash
# for creating the installer ISO
# requires root
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "!! This script must be run as sudo / with privileges"
  exit 1
fi

BASE_IMAGE="${MERTENSIA_PAYLOAD_IMAGE:-localhost/mertensiaos:base}"
INSTALLER_IMAGE="${MERTENSIA_INSTALLER_IMAGE:-localhost/mertensiaos:installer}"
BUILDER_IMAGE="${MERTENSIA_BUILDER_IMAGE:-ghcr.io/osbuild/image-builder@sha256:bb4bb67be80131bf149722b2e7dacc039434ee2b68abeb70c86c8221c8281f45}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="$ROOT_DIR/output/installer"

bash "$ROOT_DIR/scripts/installer-image.sh"

mkdir -p "$OUTPUT_DIR"

echo "> Building installer ISO"
podman run --rm \
  --privileged \
  --security-opt label=type:unconfined_t \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  -v "$OUTPUT_DIR:/output" \
  "$BUILDER_IMAGE" \
    build \
      --bootc-ref "$INSTALLER_IMAGE" \
      --bootc-installer-payload-ref "$BASE_IMAGE" \
      --bootc-default-fs ext4 \
      --output-dir /output \
      --output-name mertensiaos-installer \
      bootc-generic-iso

echo "Done!"
