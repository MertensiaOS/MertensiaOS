#!/usr/bin/env bash
# Private D-Bus, home and dummy credentials; never mounts a host keyring.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:?Usage: test-keyring.sh LOCAL_PAYLOAD_IMAGE}"
TEST_IMAGE=localhost/mertensiaos:keyring-tests
podman build --build-arg "BASE_IMAGE=$IMAGE" -f "$ROOT_DIR/tests/keyring/Containerfile" -t "$TEST_IMAGE" "$ROOT_DIR"
podman run --rm --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
  --tmpfs /run:rw,nosuid,nodev "$TEST_IMAGE"
