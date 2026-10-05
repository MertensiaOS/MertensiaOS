#!/usr/bin/env bash
# for building the installer container
# requires root
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "!! This script must be run as sudo / with privileges"
  exit 1
fi

BUILD_MODE="${MERTENSIA_BUILD_MODE:-development}"
BASE_IMAGE="${MERTENSIA_PAYLOAD_IMAGE:-localhost/mertensiaos:base}"
INSTALLER_IMAGE="${MERTENSIA_INSTALLER_IMAGE:-localhost/mertensiaos:installer}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$BUILD_MODE" in
  development)
    TARGET_IMAGE="${MERTENSIA_TARGET_IMAGE:-$BASE_IMAGE}"
    ;;
  production)
    TARGET_IMAGE="${MERTENSIA_TARGET_IMAGE:-ghcr.io/mertensiaos/mertensiaos:latest}"
    if [[ ! "$BASE_IMAGE" =~ ^ghcr\.io/mertensiaos/mertensiaos@sha256:[a-f0-9]{64}$ ]]; then
      echo "Production requires MERTENSIA_PAYLOAD_IMAGE=ghcr.io/mertensiaos/mertensiaos@sha256:DIGEST" >&2
      exit 2
    fi
    if [[ ! "$TARGET_IMAGE" =~ ^ghcr\.io/mertensiaos/mertensiaos(:[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}|@sha256:[a-f0-9]{64})$ ]]; then
      echo "Production target must use the official MertensiaOS repository" >&2
      exit 2
    fi
    ;;
  *)
    echo "MERTENSIA_BUILD_MODE must be 'development' or 'production'" >&2
    exit 2
    ;;
esac

if [[ "$BUILD_MODE" == production ]]; then
  echo "> Verifying and importing the signed production payload"
  bash "$ROOT_DIR/scripts/verify-release.sh" "$BASE_IMAGE" "containers-storage:$BASE_IMAGE"
elif [[ "${MERTENSIA_SKIP_BASE_BUILD:-0}" != 1 ]]; then
  echo "> Building payload image: $BASE_IMAGE"
  podman build --pull -f "$ROOT_DIR/Containerfile" -t "$BASE_IMAGE" "$ROOT_DIR"
fi

echo "> Building installer image"
podman build \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --build-arg "SOURCE_IMAGE=$BASE_IMAGE" \
  --build-arg "TARGET_IMAGE=$TARGET_IMAGE" \
  --build-arg "BUILD_MODE=$BUILD_MODE" \
  -f "$ROOT_DIR/Containerfile.installer" \
  -t "$INSTALLER_IMAGE" \
  "$ROOT_DIR"

echo "Done!"
