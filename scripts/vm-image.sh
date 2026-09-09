#!/usr/bin/env bash
# requires root
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "!! This script must be run as sudo / with privileges"
  exit 1
fi

BASE_IMAGE="localhost/mertensiaos:base"
DEV_IMAGE="localhost/mertensiaos:dev"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="$ROOT_DIR/output"
DISK="$OUTPUT_DIR/mertensiaos.raw"

echo "> Building base image"
podman build -f "$ROOT_DIR/Containerfile" -t "$BASE_IMAGE" "$ROOT_DIR"

echo "> Building dev image"
podman build -f "$ROOT_DIR/Containerfile.dev" -t "$DEV_IMAGE" "$ROOT_DIR"

mkdir -p "$OUTPUT_DIR"
rm -f "$DISK"
truncate -s "12G" "$DISK"

echo "> Installing MertensiaOS to disk image"
podman run --rm \
  --privileged \
  --pid=host \
  --ipc=host \
  --security-opt label=type:unconfined_t \
  -v /dev:/dev \
  -v /var/lib/containers:/var/lib/containers \
  -v "$OUTPUT_DIR:/output" \
  "$DEV_IMAGE" \
    bootc install to-disk \
      --filesystem "ext4" \
      --generic-image \
      --via-loopback \
      --skip-fetch-check \
      /output/mertensiaos.raw

echo "Done!"

