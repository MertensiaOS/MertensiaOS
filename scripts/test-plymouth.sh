#!/usr/bin/env bash
# Exercise real Plymouth in an isolated X server, never on the host console.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$#" -ne 2 ]]; then
    echo "Usage: $0 PAYLOAD_IMAGE NEW_CAPTURE_DIRECTORY" >&2
    exit 2
fi
payload_image="$1"
capture_directory="$(realpath -m -- "$2")"
if [[ -e "$capture_directory" ]]; then
    echo "Capture directory must be new: $capture_directory" >&2
    exit 2
fi
preview_image=localhost/mertensiaos-plymouth-preview:local
podman build --build-arg "BASE_IMAGE=$payload_image" -f "$ROOT_DIR/tests/plymouth/Containerfile" \
    -t "$preview_image" "$ROOT_DIR"
mkdir -p -- "$capture_directory"
podman run --rm --network=none --security-opt label=disable \
    -v "$capture_directory:/captures" "$preview_image" --output /captures
