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
BUILD_JOBS="${MERTENSIA_BUILD_JOBS:-4}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/installer-payload.sh"

case "$BUILD_MODE" in
  development)
    TARGET_IMAGE="${MERTENSIA_TARGET_IMAGE:-$BASE_IMAGE}"
    ;;
  production)
    TARGET_IMAGE="${MERTENSIA_TARGET_IMAGE:-ghcr.io/mertensiaos/mertensiaos:latest}"
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
PAYLOAD_REF="$(installer_payload_ref "$BUILD_MODE" "$BASE_IMAGE")"

if [[ "$BUILD_MODE" == production ]]; then
  echo "> Verifying and importing the signed production payload"
  bash "$ROOT_DIR/scripts/verify-release.sh" "$BASE_IMAGE" "containers-storage:$PAYLOAD_REF"
elif [[ "${MERTENSIA_SKIP_BASE_BUILD:-0}" != 1 ]]; then
  echo "> Building payload image: $BASE_IMAGE"
  payload_start=$SECONDS
  python3 "$ROOT_DIR/scripts/build-inputs.py" build payload "$BASE_IMAGE" --jobs "$BUILD_JOBS"
  echo "> Payload build: $((SECONDS - payload_start))s"
fi

echo "> Building installer image"
installer_start=$SECONDS
python3 "$ROOT_DIR/scripts/build-inputs.py" build installer "$INSTALLER_IMAGE" \
  --jobs "$BUILD_JOBS" --parent "$PAYLOAD_REF" --source "$PAYLOAD_REF" \
  --target "$TARGET_IMAGE" --mode "$BUILD_MODE"
echo "> Installer build: $((SECONDS - installer_start))s"

echo "Done!"
