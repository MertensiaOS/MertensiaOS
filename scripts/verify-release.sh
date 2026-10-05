#!/usr/bin/env bash
# Verify an immutable release using both Cosign and the installed OS policy.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:?Usage: verify-release.sh ghcr.io/mertensiaos/mertensiaos@sha256:DIGEST}"
if [[ ! "$IMAGE" =~ ^ghcr\.io/mertensiaos/mertensiaos@sha256:[a-f0-9]{64}$ ]]; then
  echo "Expected an immutable digest in the official MertensiaOS repository" >&2
  exit 2
fi
for command in cosign skopeo python3; do
  command -v "$command" >/dev/null || { echo "Missing dependency: $command" >&2; exit 2; }
done
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DESTINATION="${2:-oci:$WORK/image:verified}"
cosign verify --new-bundle-format=false --key "$ROOT_DIR/cosign.pub" "$IMAGE" >"$WORK/verification.json"
# An isolated policy/registry directory avoids changing the builder's trust.
python3 - "$ROOT_DIR" "$WORK" <<'PY'
import json
import pathlib
import sys
root, work = map(pathlib.Path, sys.argv[1:])
policy = json.loads((root / "system/containers-policy.json").read_text())
policy["transports"]["docker"]["ghcr.io/mertensiaos/mertensiaos"][0]["keyPath"] = str(root / "cosign.pub")
(work / "policy.json").write_text(json.dumps(policy))
(work / "registries.d").mkdir()
(work / "registries.d/mertensia.yaml").write_text((root / "system/mertensia-registries.yaml").read_text())
PY
skopeo --policy "$WORK/policy.json" --registries.d "$WORK/registries.d" copy \
  --preserve-digests "docker://$IMAGE" "$DESTINATION" >/dev/null
printf 'Verified release: %s\n' "$IMAGE"
