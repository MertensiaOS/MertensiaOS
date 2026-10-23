#!/usr/bin/env bash
# Validate GitHub's release credential without printing it or publishing anything.
set -euo pipefail
set +x
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${COSIGN_PRIVATE_KEY:-}" ]]; then
  echo 'SIGNING_SECRET is missing; store the private key matching cosign.pub in GitHub Actions secrets.' >&2
  exit 2
fi
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
if ! cosign public-key --key env://COSIGN_PRIVATE_KEY >"$WORK/signing.pub" 2>/dev/null; then
  echo 'Cannot open SIGNING_SECRET as a cosign private key. Check the PEM key and COSIGN_PASSWORD passphrase.' >&2
  exit 2
fi
openssl pkey -pubin -in "$WORK/signing.pub" -outform DER -out "$WORK/signing.der"
openssl pkey -pubin -in "$ROOT_DIR/cosign.pub" -outform DER -out "$WORK/trusted.der"
if ! cmp -s "$WORK/signing.der" "$WORK/trusted.der"; then
  echo 'SIGNING_SECRET does not match cosign.pub; existing installations would reject its signatures.' >&2
  exit 2
fi
echo 'Release signing key matches cosign.pub.'
