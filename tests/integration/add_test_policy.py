"""Trust one separately signed integration repository in a test overlay only."""

import argparse
import base64
import json
import subprocess
from pathlib import Path


KEY_PATH = "/etc/pki/containers/mertensia-integration.pub"


def repository(image):
    value = image.partition("@")[0]
    head, separator, tail = value.rpartition("/")
    if not separator or not head or not tail:
        raise ValueError("integration update must use a fully qualified registry repository")
    result = head + "/" + tail.partition(":")[0]
    if result == "ghcr.io/mertensiaos/mertensiaos" or "integration" not in tail.partition(":")[0]:
        raise ValueError("test signing policy is restricted to an integration repository")
    return result


def add_scope(policy, image):
    if policy.get("default") != [{"type": "reject"}]:
        raise ValueError("integration overlays require the production default-reject signature policy")
    scope = repository(image)
    existing = policy.setdefault("transports", {}).setdefault("docker", {})
    if scope in existing:
        raise ValueError("test signing policy cannot replace an existing repository trust rule")
    existing[scope] = [{"type": "sigstoreSigned", "keyPath": KEY_PATH,
                        "signedIdentity": {"type": "matchRepository"}}]
    return policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--public-key-base64", required=True)
    args = parser.parse_args()
    policy_path = Path("/etc/containers/policy.json")
    policy = add_scope(json.loads(policy_path.read_text()), args.image)
    public = base64.b64decode(args.public_key_base64, validate=True)
    if not public.startswith(b"-----BEGIN PUBLIC KEY-----") or b"PRIVATE KEY" in public:
        raise ValueError("provide a PEM public signing key")
    key = Path(KEY_PATH)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(public)
    key.chmod(0o644)
    subprocess.run(["openssl", "pkey", "-pubin", "-in", str(key), "-noout"], check=True)
    policy_path.write_text(json.dumps(policy, indent=2) + "\n")
    # Enable Sigstore attachment discovery for the explicitly scoped test repo.
    registry = repository(args.image)
    registries = Path("/etc/containers/registries.d/mertensia-integration.yaml")
    registries.parent.mkdir(parents=True, exist_ok=True)
    registries.write_text("docker:\n  " + json.dumps(registry) + ":\n    use-sigstore-attachments: true\n")


if __name__ == "__main__":
    main()
