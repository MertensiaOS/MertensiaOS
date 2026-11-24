#!/usr/bin/env bash
# Resolve the embedded payload reference shared by the installer and ISO builds.
installer_payload_ref() {
  local mode="$1" image="$2"
  if [[ "$mode" == production ]]; then
    if [[ ! "$image" =~ ^ghcr\.io/mertensiaos/mertensiaos@sha256:([a-f0-9]{64})$ ]]; then
      echo "Production requires MERTENSIA_PAYLOAD_IMAGE=ghcr.io/mertensiaos/mertensiaos@sha256:DIGEST" >&2
      return 2
    fi
    # Image Builder may change the layer representation in the ISO's local store.
    printf 'localhost/mertensiaos:iso-sha256-%s\n' "${BASH_REMATCH[1]}"
  else
    printf '%s\n' "$image"
  fi
}
