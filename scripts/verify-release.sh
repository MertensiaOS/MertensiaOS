#!/usr/bin/env bash
# Verify artifacts from the protected release workflow.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/release-identity.sh"
verify_identity=(--certificate-oidc-issuer "$OIDC_ISSUER" --certificate-identity "$WORKFLOW_IDENTITY")
case "${1:-}" in
  --checksums)
    checksums="${2:?Supply the path to SHA256SUMS}"
    cosign verify-blob "${verify_identity[@]}" --bundle "$checksums.sigstore.json" "$checksums"
    exit
    ;;
  --fixture)
    repository="$RELEASE_REPOSITORY-integration"
    shift
    ;;
  *)
    repository="$RELEASE_REPOSITORY"
    ;;
esac
IMAGE="${1:?Supply an immutable image digest in the official repository}"
digest="${IMAGE#"$repository@"}"
if [[ ! "$digest" =~ ^sha256:[a-f0-9]{64}$ || "$IMAGE" != "$repository@$digest" ]]; then
  echo "Expected an immutable digest in $repository" >&2
  exit 2
fi

cosign verify --new-bundle-format=false "${verify_identity[@]}" "$IMAGE" >/dev/null
if [[ -n "${2:-}" ]]; then
  # The runner uses stock Skopeo. Allow only the exact manifest Cosign verified.
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  printf '{"default":[{"type":"reject"}],"transports":{"docker":{"%s":[{"type":"insecureAcceptAnything"}]}}}\n' "$IMAGE" >"$work/policy.json"
  skopeo --policy "$work/policy.json" copy --preserve-digests --remove-signatures "docker://$IMAGE" "$2" >/dev/null
fi
printf 'Verified release: %s\n' "$IMAGE"
