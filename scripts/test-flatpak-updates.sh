#!/usr/bin/env bash
# All app installations occur inside a disposable container, with no host mounts.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:?Usage: test-flatpak-updates.sh LOCAL_PAYLOAD_IMAGE}"
TEST_IMAGE=localhost/mertensiaos:flatpak-update-tests
podman build --build-arg "BASE_IMAGE=$IMAGE" -f "$ROOT_DIR/tests/flatpak/Containerfile" -t "$TEST_IMAGE" "$ROOT_DIR"
podman run --rm --network none "$TEST_IMAGE"
