#!/usr/bin/env bash
# Sourced by the publisher and verifier. Share the identity shipped to bootc.
RELEASE_REPOSITORY=ghcr.io/mertensiaos/mertensiaos
release_identity="$(python3 - "$ROOT_DIR/system/config/containers/bootc-policy.json" "$RELEASE_REPOSITORY" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    policy = json.load(stream)
fulcio = policy["transports"]["docker"][sys.argv[2]][0]["fulcio"]
print(fulcio["oidcIssuer"], fulcio["buildSignerURI"])
PY
)"
read -r OIDC_ISSUER WORKFLOW_IDENTITY <<<"$release_identity"
