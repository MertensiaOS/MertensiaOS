"""Exercise release fail-closed boundaries without touching a registry."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "ghcr.io/mertensiaos/mertensiaos"
DIGEST = "sha256:" + "a" * 64
IMMUTABLE_IMAGE = f"{REPOSITORY}@{DIGEST}"
LOCAL_IMAGE_ID = "b" * 64
RETAGGED_IMAGE_ID = "c" * 64

# Each fake records the real scripts' arguments. During policy verification it
# also snapshots the temporary policy before verify-release's trap removes it.
FAKE_TOOL = r'''
import json
import os
from pathlib import Path
import sys
import subprocess

command = Path(sys.argv[0]).name
args = sys.argv[1:]
record = {"command": command, "args": args}
def resolve_local(source):
    if source != "localhost/mertensia:release":
        return source.removeprefix("sha256:")
    state = Path(os.environ["FAKE_LOCAL_TAG_STATE"])
    return state.read_text() if state.exists() else os.environ["FAKE_LOCAL_IMAGE_ID"]
if command == "podman" and args[0] in {"run", "push"}:
    source = args[args.index("-euc") - 1] if args[0] == "run" else args[-2]
    record["resolved_image_id"] = resolve_local(source)
if command == "skopeo" and "--policy" in args:
    policy_path = Path(args[args.index("--policy") + 1])
    registries_path = Path(args[args.index("--registries.d") + 1])
    record["policy"] = json.loads(policy_path.read_text())
    record["registries"] = {
        path.name: path.read_text() for path in registries_path.iterdir()
    }
with open(os.environ["FAKE_TOOL_LOG"], "a") as log:
    log.write(json.dumps(record) + "\n")

if command == "cosign":
    if args[0] == "public-key":
        sys.stdout.write(Path(os.environ["FAKE_PUBLIC_KEY"]).read_text())
    elif args[0] == "verify":
        if os.environ.get("FAKE_COSIGN_VERIFY_FAIL") == "1":
            print("no matching signatures", file=sys.stderr)
            sys.exit(1)
        print("[{}]")
    elif args[0] == "sign" and os.environ.get("FAKE_SIGN_FAIL") == "1":
        print("signing unavailable", file=sys.stderr)
        sys.exit(1)
elif command == "podman":
    if args[:2] == ["image", "inspect"]:
        if os.environ.get("FAKE_LOCAL_INSPECT_FAIL") == "1":
            print("local image unavailable", file=sys.stderr)
            sys.exit(1)
        print(resolve_local(args[-1]))
    if args[0] == "run" and os.environ.get("FAKE_RETAG_AFTER_VALIDATION") == "1":
        Path(os.environ["FAKE_LOCAL_TAG_STATE"]).write_text(os.environ["FAKE_RETAGGED_IMAGE_ID"])
    if args[0] == "run" and os.environ.get("FAKE_VALIDATE_FAIL") == "1":
        print("development image rejected", file=sys.stderr)
        sys.exit(1)
    if args[0] == "run" and os.environ.get("FAKE_PAYLOAD_ROOT"):
        script_index = args.index("-euc") + 1
        script = args[script_index]
        root = os.environ["FAKE_PAYLOAD_ROOT"]
        for prefix in ("/usr/lib/", "/usr/libexec/", "/usr/share/", "/etc/"):
            script = script.replace(prefix, root + prefix)
        result = subprocess.run(["bash", "-euc", script, *args[script_index + 1:]])
        sys.exit(result.returncode)
    if args[0] == "push":
        Path(args[args.index("--digestfile") + 1]).write_text(
            os.environ["FAKE_DIGEST"] + "\n"
        )
elif command == "skopeo":
    if args[0] == "inspect":
        outcome = os.environ.get("FAKE_INSPECT", "absent")
        if outcome == "exists":
            print(json.dumps({"Digest": os.environ["FAKE_DIGEST"]}))
        elif outcome == "outage":
            print("registry connection timed out", file=sys.stderr)
            sys.exit(1)
        elif outcome == "unauthorized":
            print("unauthorized: authentication required", file=sys.stderr)
            sys.exit(1)
        else:
            print("manifest unknown", file=sys.stderr)
            sys.exit(1)
    elif "--policy" in args and os.environ.get("FAKE_POLICY_VERIFY_FAIL") == "1":
        print("image rejected by signature policy", file=sys.stderr)
        sys.exit(1)
elif command == "bootc" and os.environ.get("FAKE_LINT_FAIL") == "1":
    print("bootc image lint failed", file=sys.stderr)
    sys.exit(1)
elif command == "getent":
    if os.environ.get("FAKE_ACCOUNT_LOOKUP_FAIL") == "1":
        print("account database unavailable", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if os.environ.get("FAKE_ACCOUNT") == args[-1] else 2)
'''


class ReleaseScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl") or not shutil.which("bash"):
            raise unittest.SkipTest("release tests require bash and openssl")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.log = self.work / "commands.jsonl"
        for name in ("podman", "cosign", "skopeo", "bootc", "getent"):
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            executable.chmod(0o755)
        self.environment = dict(os.environ)
        self.environment.pop("GITHUB_OUTPUT", None)
        self.environment.update(
            PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            FAKE_TOOL_LOG=str(self.log),
            FAKE_PUBLIC_KEY=str(ROOT / "cosign.pub"),
            FAKE_DIGEST=DIGEST,
            FAKE_LOCAL_IMAGE_ID=LOCAL_IMAGE_ID,
            FAKE_RETAGGED_IMAGE_ID=RETAGGED_IMAGE_ID,
            FAKE_LOCAL_TAG_STATE=str(self.work / "local-tag"),
            MERTENSIA_SIGNING_KEY="test-signing-key",
        )

    def run_script(self, script, *arguments, **environment):
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / script), *arguments],
            env=self.environment | environment,
            text=True,
            capture_output=True,
            timeout=20,
        )

    def publish(self, **environment):
        return self.run_script(
            "publish-image.sh", "localhost/mertensia:release", "45.20261005.1", "latest",
            **environment,
        )

    def payload_fixture(self):
        payload = self.work / "payload"
        fixtures = {
            "etc/pki/containers/mertensia.pub": ROOT / "cosign.pub",
            "etc/containers/policy.json": ROOT / "system/config/containers/policy.json",
            "usr/share/mertensia/bootc-policy.json": ROOT / "system/config/containers/policy.json",
            "etc/containers/registries.d/mertensia.yaml": ROOT / "system/config/containers/registries.yaml",
        }
        for target, source in fixtures.items():
            destination = payload / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        self.environment["FAKE_PAYLOAD_ROOT"] = str(payload)
        return payload

    def commands(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def calls(self, command, operation=None):
        return [record for record in self.commands()
                if record["command"] == command
                and (operation is None or record["args"][0] == operation)]

    def assert_no_push_or_promotion(self):
        self.assertEqual(self.calls("podman", "push"), [])
        self.assertFalse(any(record["command"] == "skopeo" and "copy" in record["args"]
                             for record in self.commands()))

    def assert_no_promotion(self):
        self.assertEqual(self.calls("skopeo", "copy"), [])

    def mismatched_public_key(self):
        # Negating the public point yields a different valid P-256 public key.
        # This only uses the repository's public fixture; no private key exists.
        pem = (ROOT / "cosign.pub").read_text()
        der = bytearray(base64.b64decode("".join(pem.splitlines()[1:-1])))
        self.assertEqual(len(der), 91)
        self.assertEqual(der[-65], 4)  # uncompressed EC point
        prime = int("ffffffff00000001000000000000000000000000ffffffffffffffffffffffff", 16)
        y = int.from_bytes(der[-32:], "big")
        der[-32:] = (prime - y).to_bytes(32, "big")
        encoded = base64.b64encode(der).decode("ascii")
        alternate = self.work / "mismatched.pub"
        alternate.write_text("-----BEGIN PUBLIC KEY-----\n"
                             + "\n".join(encoded[i:i + 64] for i in range(0, len(encoded), 64))
                             + "\n-----END PUBLIC KEY-----\n")
        return alternate

    def test_mismatched_signing_key_is_rejected_before_push(self):
        result = self.publish(FAKE_PUBLIC_KEY=str(self.mismatched_public_key()))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("does not match", result.stderr)
        self.assert_no_push_or_promotion()
        self.assertEqual(self.calls("podman", "run"), [])

    def test_existing_version_is_rejected_before_push(self):
        result = self.publish(FAKE_INSPECT="exists")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("already exists", result.stderr)
        self.assert_no_push_or_promotion()

    def test_registry_outage_and_authentication_failure_do_not_look_like_absence(self):
        for outcome in ("outage", "unauthorized"):
            with self.subTest(outcome=outcome):
                self.log.unlink(missing_ok=True)
                result = self.publish(FAKE_INSPECT=outcome)
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_push_or_promotion()

    def test_validation_failure_prevents_push(self):
        result = self.publish(FAKE_VALIDATE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls("podman", "run")), 1)
        self.assert_no_push_or_promotion()

    def test_failed_cosign_verification_prevents_channel_promotion(self):
        result = self.publish(FAKE_COSIGN_VERIFY_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls("podman", "push")), 1)
        self.assertEqual(len(self.calls("cosign", "sign")), 1)
        self.assert_no_promotion()
        self.assertFalse(any("policy" in record for record in self.commands()))

    def test_failed_policy_verification_prevents_channel_promotion(self):
        result = self.publish(FAKE_POLICY_VERIFY_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls("cosign", "verify")), 1)
        self.assertEqual(sum("policy" in record for record in self.commands()), 1)
        self.assert_no_promotion()

    def test_signing_failure_prevents_verification_and_promotion(self):
        result = self.publish(FAKE_SIGN_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("cosign", "verify"), [])
        self.assert_no_promotion()

    def test_version_is_validated_signed_and_verified_before_digest_promotion(self):
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.commands()
        validation = self.calls("podman", "run")[0]
        self.assertIn(LOCAL_IMAGE_ID, validation["args"])
        validation_script = validation["args"][validation["args"].index("-euc") + 1]
        self.assertIn("bootc container lint --fatal-warnings", validation_script)
        self.assertIn("testing integration_admin integration_user", validation_script)
        push = self.calls("podman", "push")[0]
        self.assertEqual(push["args"][-2], LOCAL_IMAGE_ID)
        self.assertEqual(push["args"][-1], f"docker://{REPOSITORY}:45.20261005.1")
        signing = self.calls("cosign", "sign")[0]
        verification = self.calls("cosign", "verify")[0]
        self.assertEqual(signing["args"][-1], IMMUTABLE_IMAGE)
        self.assertEqual(verification["args"][-1], IMMUTABLE_IMAGE)
        self.assertIn("--new-bundle-format=false", signing["args"])
        self.assertIn("--use-signing-config=false", signing["args"])
        policy_copy = next(record for record in records if "policy" in record)
        promotion = self.calls("skopeo", "copy")[0]
        self.assertEqual(promotion["args"], ["copy", "--preserve-digests",
                                           f"docker://{IMMUTABLE_IMAGE}",
                                           f"docker://{REPOSITORY}:latest"])
        ordered = [validation, push, signing, verification, policy_copy, promotion]
        self.assertEqual([records.index(record) for record in ordered],
                         sorted(records.index(record) for record in ordered))

    def test_verify_release_uses_scoped_strict_policy_and_signature_attachments(self):
        destination = "oci:" + str(self.work / "verified-image") + ":verified"
        result = self.run_script("verify-release.sh", IMMUTABLE_IMAGE, destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        verification = self.calls("cosign", "verify")[0]
        self.assertEqual(verification["args"], ["verify", "--new-bundle-format=false",
                                              "--key", str(ROOT / "cosign.pub"), IMMUTABLE_IMAGE])
        policy_copy = next(record for record in self.commands() if "policy" in record)
        policy = policy_copy["policy"]
        self.assertEqual(policy["default"], [{"type": "reject"}])
        self.assertEqual(set(policy["transports"]["docker"]), {REPOSITORY})
        scoped = policy["transports"]["docker"][REPOSITORY]
        self.assertEqual(scoped, [{"type": "sigstoreSigned", "keyPath": str(ROOT / "cosign.pub"),
                                  "signedIdentity": {"type": "matchRepository"}}])
        registries = policy_copy["registries"]
        self.assertEqual(registries["mertensia.yaml"], (ROOT / "system/config/containers/registries.yaml").read_text())
        self.assertIn(REPOSITORY, registries["mertensia.yaml"])
        self.assertIn("use-sigstore-attachments: true", registries["mertensia.yaml"])
        self.assertEqual(policy_copy["args"][-2:], [f"docker://{IMMUTABLE_IMAGE}", destination])
        self.assertIn("--preserve-digests", policy_copy["args"])

    def test_local_retag_after_validation_cannot_change_the_pushed_payload(self):
        result = self.publish(FAKE_RETAG_AFTER_VALIDATION="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "local-tag").read_text(), RETAGGED_IMAGE_ID)
        inspection = self.calls("podman", "image")
        self.assertEqual(len(inspection), 1)
        self.assertEqual(inspection[0]["args"][-1], "localhost/mertensia:release")
        validation = self.calls("podman", "run")[0]
        push = self.calls("podman", "push")[0]
        self.assertEqual(validation["resolved_image_id"], LOCAL_IMAGE_ID)
        self.assertEqual(push["resolved_image_id"], LOCAL_IMAGE_ID)
        self.assertNotIn("localhost/mertensia:release", validation["args"])
        self.assertNotIn("localhost/mertensia:release", push["args"])

    def test_missing_or_invalid_local_image_id_prevents_push(self):
        for fault in ({"FAKE_LOCAL_INSPECT_FAIL": "1"}, {"FAKE_LOCAL_IMAGE_ID": "not-an-image-id"}):
            with self.subTest(fault=fault):
                self.log.unlink(missing_ok=True)
                result = self.publish(**fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls("podman", "run"), [])
                self.assert_no_push_or_promotion()

    def test_verification_rejects_mutable_or_foreign_references_before_tools_run(self):
        for image in (f"{REPOSITORY}:latest", f"ghcr.io/other/project@{DIGEST}"):
            with self.subTest(image=image):
                result = self.run_script("verify-release.sh", image)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.commands(), [])

    def test_invalid_registry_digest_is_not_signed_or_promoted(self):
        result = self.publish(FAKE_DIGEST="not-a-digest")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("cosign", "sign"), [])
        self.assert_no_promotion()

    def test_actual_payload_validation_accepts_repository_trust_fixture(self):
        self.payload_fixture()
        result = self.run_script("validate-image.sh", "localhost/mertensia:release")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changed_payload_key_prevents_publication(self):
        payload = self.payload_fixture()
        shutil.copyfile(self.mismatched_public_key(), payload / "etc/pki/containers/mertensia.pub")
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the release trust root", result.stderr)
        self.assert_no_push_or_promotion()

    def test_extra_accept_rule_in_payload_policy_prevents_publication(self):
        payload = self.payload_fixture()
        policy_file = payload / "etc/containers/policy.json"
        policy = json.loads(policy_file.read_text())
        policy["transports"]["docker"][REPOSITORY].append({"type": "insecureAcceptAnything"})
        policy_file.write_text(json.dumps(policy))
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signature policy differs", result.stderr)
        self.assert_no_push_or_promotion()

    def test_extra_permissive_repository_scope_prevents_publication(self):
        payload = self.payload_fixture()
        policy_file = payload / "etc/containers/policy.json"
        policy = json.loads(policy_file.read_text())
        policy["transports"]["docker"]["ghcr.io/mertensiaos"] = [{"type": "insecureAcceptAnything"}]
        policy_file.write_text(json.dumps(policy))
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_push_or_promotion()

    def test_changed_backup_policy_prevents_publication(self):
        payload = self.payload_fixture()
        policy_file = payload / "usr/share/mertensia/bootc-policy.json"
        policy = json.loads(policy_file.read_text())
        policy["default"] = [{"type": "insecureAcceptAnything"}]
        policy_file.write_text(json.dumps(policy))
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signature policy differs", result.stderr)
        self.assert_no_push_or_promotion()

    def test_disabled_signature_attachments_prevent_publication(self):
        payload = self.payload_fixture()
        registry_file = payload / "etc/containers/registries.d/mertensia.yaml"
        registry_file.write_text(registry_file.read_text().replace("true", "false"))
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registry configuration differs", result.stderr)
        self.assert_no_push_or_promotion()

    def test_integration_artifacts_including_dangling_enablement_links_prevent_publication(self):
        payload = self.payload_fixture()
        for relative in ("usr/libexec/mertensia-integration-agent",
                         "usr/lib/systemd/system/mertensia-integration.service",
                         "usr/share/mertensia-integration",
                         "etc/systemd/system/multi-user.target.wants/mertensia-integration.service"):
            with self.subTest(relative=relative):
                self.log.unlink(missing_ok=True)
                artifact = payload / relative
                artifact.parent.mkdir(parents=True, exist_ok=True)
                if relative.startswith("etc/"):
                    artifact.symlink_to("/missing/integration.service")
                else:
                    artifact.touch()
                result = self.publish()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("integration artifact", result.stderr)
                self.assert_no_push_or_promotion()
                artifact.unlink()

    def test_development_accounts_prevent_publication(self):
        self.payload_fixture()
        for account in ("testing", "integration_admin", "integration_user"):
            with self.subTest(account=account):
                self.log.unlink(missing_ok=True)
                result = self.publish(FAKE_ACCOUNT=account)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("development image with account", result.stderr)
                self.assert_no_push_or_promotion()

    def test_actual_bootc_lint_failure_prevents_publication(self):
        self.payload_fixture()
        result = self.publish(FAKE_LINT_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootc image lint failed", result.stderr)
        self.assert_no_push_or_promotion()

    def test_unavailable_account_database_prevents_publication(self):
        self.payload_fixture()
        result = self.publish(FAKE_ACCOUNT_LOOKUP_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not check", result.stderr)
        self.assert_no_push_or_promotion()


if __name__ == "__main__":
    unittest.main()
