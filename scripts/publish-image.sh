#!/usr/bin/env bash
# Publish a version first, then promote its verified digest to the update channel.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_IMAGE="${1:?Usage: publish-image.sh LOCAL_IMAGE VERSION [CHANNEL]}"
VERSION="${2:?Supply an immutable version such as 45.20261005.1}"
CHANNEL="${3:-latest}"
REPOSITORY=ghcr.io/mertensiaos/mertensiaos
SIGNING_KEY="${MERTENSIA_SIGNING_KEY:-env://COSIGN_PRIVATE_KEY}"
for tag in "$VERSION" "$CHANNEL"; do
  if [[ ! "$tag" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]; then
    echo "Invalid container tag" >&2
    exit 2
  fi
done
if [[ "$VERSION" == "$CHANNEL" ]]; then
  echo "The immutable version and update channel must differ" >&2
  exit 2
fi
for command in podman cosign skopeo openssl python3; do
  command -v "$command" >/dev/null || { echo "Missing dependency: $command" >&2; exit 2; }
done
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# Refuse to publish if the key does not match the trust root shipped in the OS.
cosign public-key --key "$SIGNING_KEY" >"$WORK/signing.pub"
openssl pkey -pubin -in "$WORK/signing.pub" -outform DER -out "$WORK/signing.der"
openssl pkey -pubin -in "$ROOT_DIR/cosign.pub" -outform DER -out "$WORK/trusted.der"
cmp -s "$WORK/signing.der" "$WORK/trusted.der" || { echo "Signing key does not match cosign.pub" >&2; exit 2; }
# Existing versions are never overwritten. Errors other than manifest absence fail closed.
if skopeo inspect "docker://$REPOSITORY:$VERSION" >"$WORK/existing.json" 2>"$WORK/inspect-error"; then
  echo "Version $VERSION already exists; choose a new version" >&2
  exit 2
else
  INSPECT_ERROR="$(cat "$WORK/inspect-error")"
  if [[ ! "${INSPECT_ERROR,,}" =~ manifest[[:space:]_]unknown|name[[:space:]_]unknown ]]; then
    cat "$WORK/inspect-error" >&2
    exit 1
  fi
fi
# A local tag can move during validation. Pin its image ID once so the exact
# payload checked here is also the payload pushed and subsequently signed.
LOCAL_IMAGE_ID="$(podman image inspect --format '{{.Id}}' "$LOCAL_IMAGE")"
LOCAL_IMAGE_ID="${LOCAL_IMAGE_ID#sha256:}"
[[ "$LOCAL_IMAGE_ID" =~ ^[a-f0-9]{64}$ ]] || { echo "Podman returned an invalid local image ID" >&2; exit 1; }
bash "$ROOT_DIR/scripts/validate-image.sh" "$LOCAL_IMAGE_ID"
podman push --digestfile "$WORK/digest" "$LOCAL_IMAGE_ID" "docker://$REPOSITORY:$VERSION"
DIGEST="$(cat "$WORK/digest")"
[[ "$DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "Registry returned an invalid digest" >&2; exit 1; }
IMAGE="$REPOSITORY@$DIGEST"
# containers/image consumes the traditional .sig attachments, not Cosign v3 bundles.
cosign sign --yes --new-bundle-format=false --use-signing-config=false --key "$SIGNING_KEY" "$IMAGE"
bash "$ROOT_DIR/scripts/verify-release.sh" "$IMAGE"
skopeo copy --preserve-digests "docker://$IMAGE" "docker://$REPOSITORY:$CHANNEL"
printf 'Published %s as %s and %s\n' "$IMAGE" "$VERSION" "$CHANNEL"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'image=%s\ndigest=%s\n' "$IMAGE" "$DIGEST" >>"$GITHUB_OUTPUT"
fi
