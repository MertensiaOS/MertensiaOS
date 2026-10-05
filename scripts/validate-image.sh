#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:?Usage: validate-image.sh LOCAL_PAYLOAD_IMAGE}"
for command in podman openssl sha256sum python3; do
  command -v "$command" >/dev/null || { echo "Missing dependency: $command" >&2; exit 2; }
done
EXPECTED_KEY_SHA256="$(openssl pkey -pubin -in "$ROOT_DIR/cosign.pub" -outform DER | sha256sum)"
EXPECTED_KEY_SHA256="${EXPECTED_KEY_SHA256%% *}"
EXPECTED_POLICY="$(<"$ROOT_DIR/system/containers-policy.json")"
EXPECTED_REGISTRIES_SHA256="$(sha256sum "$ROOT_DIR/system/mertensia-registries.yaml")"
EXPECTED_REGISTRIES_SHA256="${EXPECTED_REGISTRIES_SHA256%% *}"
# Pass only public trust data as arguments, without binding host paths into the
# image. This also works on SELinux hosts and keeps the expected trust external.
podman run --rm --entrypoint /usr/bin/bash "$IMAGE" -euc "$(cat <<'BASH'
  set -o pipefail
  bootc container lint --fatal-warnings --no-truncate
  shopt -s nullglob
  for test_artifact in \
      /usr/lib/systemd/system/mertensia-integration* \
      /usr/libexec/mertensia-integration* \
      /etc/systemd/system/mertensia-integration* \
      /etc/systemd/system/*.wants/mertensia-integration* \
      /usr/lib/bootc/install/*mertensia-integration* \
      /usr/share/mertensia-integration; do
    if [[ -e "$test_artifact" || -L "$test_artifact" ]]; then
      echo "Refusing to release an integration artifact: $test_artifact" >&2
      exit 1
    fi
  done
  for test_user in testing integration_admin integration_user; do
    if getent passwd "$test_user" >/dev/null; then
      echo "Refusing to release a development image with account $test_user" >&2
      exit 1
    else
      account_status=$?
      if [[ "$account_status" != 2 ]]; then
        echo "Could not check the image for development account $test_user" >&2
        exit 1
      fi
    fi
  done
  payload_key_sha256="$(openssl pkey -pubin -in /etc/pki/containers/mertensia.pub -outform DER | sha256sum)"
  if [[ "${payload_key_sha256%% *}" != "$1" ]]; then
    echo "Payload signing key does not match the release trust root" >&2
    exit 1
  fi
  python3 - "$2" "$3" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

expected_policy = json.loads(sys.argv[1])
for policy_file in ("/etc/containers/policy.json", "/usr/share/mertensia/bootc-policy.json"):
    policy = json.loads(Path(policy_file).read_text())
    if policy != expected_policy:
        raise SystemExit(f"Payload signature policy differs from the release policy: {policy_file}")
registries = Path("/etc/containers/registries.d/mertensia.yaml").read_bytes()
if hashlib.sha256(registries).hexdigest() != sys.argv[2]:
    raise SystemExit("Payload signature registry configuration differs from the release configuration")
PY
BASH
)" mertensia-image-validation "$EXPECTED_KEY_SHA256" "$EXPECTED_POLICY" "$EXPECTED_REGISTRIES_SHA256"
