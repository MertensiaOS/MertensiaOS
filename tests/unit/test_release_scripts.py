"""Exercise release fail-closed boundaries without touching a registry."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import yaml


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "ghcr.io/mertensiaos/mertensiaos"
DIGEST = "sha256:" + "a" * 64
IMMUTABLE_IMAGE = f"{REPOSITORY}@{DIGEST}"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
WORKFLOW_REF = "MertensiaOS/MertensiaOS/.github/workflows/release.yml@refs/heads/main"
WORKFLOW_IDENTITY = f"https://github.com/{WORKFLOW_REF}"
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
if command == "podman" and (args[0] == "push" or "-euc" in args):
    source = args[args.index("-euc") - 1] if args[0] == "run" else args[-2]
    record["resolved_image_id"] = resolve_local(source)
if command == "skopeo" and "--policy" in args:
    policy_path = Path(args[args.index("--policy") + 1])
    record["policy"] = json.loads(policy_path.read_text())
with open(os.environ["FAKE_TOOL_LOG"], "a") as log:
    log.write(json.dumps(record) + "\n")

if command == "cosign":
    if args[0] in {"verify", "verify-blob"}:
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
    if args[0] == "run" and "/usr/bin/skopeo" in args:
        if os.environ.get("FAKE_POLICY_VERIFY_FAIL") == "1":
            print("image rejected by signature policy", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    if args[0] == "run" and os.environ.get("FAKE_RETAG_AFTER_VALIDATION") == "1":
        Path(os.environ["FAKE_LOCAL_TAG_STATE"]).write_text(os.environ["FAKE_RETAGGED_IMAGE_ID"])
    if args[0] == "run" and os.environ.get("FAKE_VALIDATE_FAIL") == "1":
        print("development image rejected", file=sys.stderr)
        sys.exit(1)
    if args[0] == "run" and os.environ.get("FAKE_PAYLOAD_ROOT"):
        script_index = args.index("-euc") + 1
        script = args[script_index]
        root = os.environ["FAKE_PAYLOAD_ROOT"]
        for prefix in ("/usr/lib/", "/usr/lib64/", "/usr/libexec/", "/usr/share/", "/etc/", "/opt/", "/usr/bin/mertensia-update", "/usr/bin/mertensia-system"):
            script = script.replace(prefix, root + prefix)
        script = script.replace("'ExecStart=" + root + "/usr/bin/mertensia-update", "'ExecStart=/usr/bin/mertensia-update")
        script = script.replace("test -x /usr/bin/brave-origin-stable",
                                "test -x " + root + "/usr/bin/brave-origin-stable")
        # Model container ownership using the relocated payload's UID and GID.
        metadata = Path(root).stat()
        script = script.replace("expected_owner = 0", f"expected_owner = {metadata.st_uid}")
        script = script.replace("metadata.st_gid != expected_owner", f"metadata.st_gid != {metadata.st_gid}")
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
elif command == "bootc" and os.environ.get("FAKE_LINT_FAIL") == "1":
    print("bootc image lint failed", file=sys.stderr)
    sys.exit(1)
elif command == "getent":
    if os.environ.get("FAKE_ACCOUNT_LOOKUP_FAIL") == "1":
        print("account database unavailable", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if os.environ.get("FAKE_ACCOUNT") == args[-1] else 2)
elif command == "gsettings":
    print("false" if args[-1] == "refresh-when-metered" else "true")
elif command == "rpm":
    if args in (["-q", "chromium"], ["-q", "brave-browser"]):
        sys.exit(1)
    print("1.fc45.mertensia1" if args[-1] == "skopeo" else "gnome-software-51.0-1.fc45.x86_64")
elif command == "plymouth-set-default-theme":
    print(os.environ.get("FAKE_PLYMOUTH_THEME", "mertensia"))
elif command == "fc-match":
    print(os.environ.get("FAKE_PAYLOAD_ROOT", "") + "/usr/share/fonts/mertensia/Figtree-Variable.ttf")
'''


class ReleaseScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("bash"):
            raise unittest.SkipTest("release tests require bash")

    def test_production_iso_embeds_verified_payload_under_a_digest_derived_tag(self):
        helper = ROOT / "scripts/installer-payload.sh"
        for mode, image, expected, code in (
            ("production", IMMUTABLE_IMAGE, "localhost/mertensiaos:iso-sha256-" + "a" * 64, 0),
            ("production", REPOSITORY + ":testing", "", 2),
            ("production", "ghcr.io/another/mertensiaos@" + DIGEST, "", 2),
            ("development", "localhost/mertensiaos:base", "localhost/mertensiaos:base", 0),
        ):
            with self.subTest(mode=mode, image=image):
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; installer_payload_ref "$2" "$3"', "_", str(helper), mode, image],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.log = self.work / "commands.jsonl"
        for name in ("podman", "cosign", "skopeo", "bootc", "getent", "rpm", "gsettings",
                     "plymouth-set-default-theme", "fc-match"):
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            executable.chmod(0o755)
        self.environment = dict(os.environ)
        self.environment.pop("GITHUB_OUTPUT", None)
        self.environment.update(
            PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            FAKE_TOOL_LOG=str(self.log),
            FAKE_DIGEST=DIGEST,
            FAKE_LOCAL_IMAGE_ID=LOCAL_IMAGE_ID,
            FAKE_RETAGGED_IMAGE_ID=RETAGGED_IMAGE_ID,
            FAKE_LOCAL_TAG_STATE=str(self.work / "local-tag"),
            GITHUB_WORKFLOW_REF=WORKFLOW_REF,
            ACTIONS_ID_TOKEN_REQUEST_URL="https://token.example.test",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN="test-oidc-token",
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
            "publish-image.sh", "localhost/mertensia:release", "45.20261005.1",
            **environment,
        )

    def payload_fixture(self):
        payload = self.work / "payload"
        fixtures = {
            "etc/pki/containers/mertensia-fulcio.crt": ROOT / "system/config/containers/fulcio.crt",
            "etc/pki/containers/mertensia-rekor.pub": ROOT / "system/config/containers/rekor.pub",
            "etc/containers/policy.json": ROOT / "system/config/containers/policy.json",
            "etc/containers/mertensia-bootc-policy.json": ROOT / "system/config/containers/bootc-policy.json",
            "etc/containers/registries.d/mertensia.yaml": ROOT / "system/config/containers/registries.yaml",
            "etc/greenboot/greenboot.conf": ROOT / "system/config/greenboot/greenboot.conf",
            "usr/libexec/mertensia-boot-health": ROOT / "system/bin/mertensia-boot-health",
            "usr/libexec/mertensia-boot-health-notify": ROOT / "system/bin/mertensia-boot-health-notify",
            "usr/bin/mertensia-update": ROOT / "system/bin/mertensia-update",
            "usr/bin/mertensia-system": ROOT / "system/bin/mertensia-system",
            "usr/libexec/mertensia-update-helper": ROOT / "system/bin/mertensia-update-helper",
            "usr/share/applications/org.mertensia.System.desktop": ROOT / "system/config/desktop/org.mertensia.System.desktop",
            "usr/share/polkit-1/actions/org.mertensia.Updates.policy": ROOT / "system/config/polkit/org.mertensia.Updates.policy",
            "usr/share/polkit-1/rules.d/49-mertensia-updates.rules": ROOT / "system/config/polkit/49-mertensia-updates.rules",
            "usr/lib/systemd/system/bootc-fetch-apply-updates.service.d/50-mertensia-stage.conf": ROOT / "system/config/systemd/bootc-fetch-apply-updates.service.d/50-mertensia-stage.conf",
            "usr/lib/greenboot/check/required.d/50-mertensia-desktop.sh": ROOT / "system/config/greenboot/50-mertensia-desktop.sh",
            "etc/brave/policies/managed/mertensia.json": ROOT / "system/config/brave/policies/managed/mertensia.json",
            "etc/brave/policies/recommended/mertensia.json": ROOT / "system/config/brave/policies/recommended/mertensia.json",
            "usr/share/applications/gnome-mimeapps.list": ROOT / "system/config/desktop/gnome-mimeapps.list",
        }
        for target, source in fixtures.items():
            destination = payload / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            if target.startswith(("usr/libexec/", "usr/lib/greenboot/", "usr/bin/")):
                destination.chmod(0o755)
            elif target.startswith("etc/brave/policies/"):
                destination.parent.parent.chmod(0o755)
                destination.parent.chmod(0o755)
                destination.chmod(0o644)
        plugin = payload / "usr/lib64/gnome-software/plugins-23/libgs_plugin_flatpak.so"
        plugin.parent.mkdir(parents=True)
        plugin.touch()
        for name in ("usr/bin/brave-origin-stable", "opt/brave.com/brave-origin/brave",
                     "opt/brave.com/brave-origin/chrome-sandbox",
                     "usr/libexec/mertensia-check-plymouth-initrd"):
            executable = payload / name
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        (payload / "opt/brave.com/brave-origin/LICENSE").write_text("License fixture\n")
        for name in ("brave-origin.desktop", "com.brave.Origin.desktop"):
            (payload / "usr/share/applications" / name).write_text(
                "[Desktop Entry]\nExec=/usr/bin/brave-origin-stable --skip-origin-startup-dialog %U\n"
                "[Desktop Action new-window]\nExec=/usr/bin/brave-origin-stable --skip-origin-startup-dialog\n"
                "[Desktop Action new-private-window]\n"
                "Exec=/usr/bin/brave-origin-stable --skip-origin-startup-dialog --incognito\n")
        fragment = payload / "usr/lib/bootupd/grub2-static/configs.d/08_greenboot.cfg"
        fragment.parent.mkdir(parents=True, exist_ok=True)
        fragment.write_text("# packaged Greenboot GRUB fragment\n")
        for link, target in {
            "etc/systemd/system/graphical.target.wants/greenboot-healthcheck.service": "/usr/lib/systemd/system/greenboot-healthcheck.service",
            "etc/systemd/system/ostree-finalize-staged.service.requires/greenboot-set-rollback-trigger.service": "/usr/lib/systemd/system/greenboot-set-rollback-trigger.service",
            "etc/systemd/system/greenboot-success.target.wants/mertensia-boot-health-status.service": "/usr/lib/systemd/system/mertensia-boot-health-status.service",
            "etc/systemd/system/greenboot-success.target.wants/mertensia-update-status.service": "/usr/lib/systemd/system/mertensia-update-status.service",
            "etc/systemd/system/bootc-status-updated.target.wants/mertensia-update-status.service": "/usr/lib/systemd/system/mertensia-update-status.service",
            "etc/systemd/system/timers.target.wants/bootc-fetch-apply-updates.timer": "/usr/lib/systemd/system/bootc-fetch-apply-updates.timer",
            "etc/systemd/user/gnome-session.target.wants/gnome-software.service": "/usr/lib/systemd/user/gnome-software.service",
            "etc/systemd/user/grub-boot-success.timer": "/dev/null",
        }.items():
            path = payload / link
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        self.environment["FAKE_PAYLOAD_ROOT"] = str(payload)
        return payload

    def commands(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def calls(self, command, operation=None):
        return [record for record in self.commands()
                if record["command"] == command
                and (operation is None or record["args"][0] == operation
                     or (command == "skopeo" and record["args"][0] == "--policy"
                         and record["args"][2] == operation))]

    def assert_no_push_or_promotion(self):
        self.assertEqual(self.calls("podman", "push"), [])
        self.assertFalse(any(record["command"] == "skopeo" and "copy" in record["args"]
                             for record in self.commands()))

    def assert_no_promotion(self):
        self.assertEqual(self.calls("skopeo", "copy"), [])

    def test_publishing_rejects_missing_oidc_and_wrong_workflow_before_push(self):
        for overrides in (
            {"ACTIONS_ID_TOKEN_REQUEST_TOKEN": ""},
            {"ACTIONS_ID_TOKEN_REQUEST_URL": ""},
            {"GITHUB_WORKFLOW_REF": WORKFLOW_REF.replace("/main", "/feature")},
            {"GITHUB_WORKFLOW_REF": WORKFLOW_REF.replace("/release.yml", "/other.yml")},
        ):
            with self.subTest(overrides=overrides):
                result = self.publish(**overrides)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("trusted GitHub Actions workflow", result.stderr)
                self.assertEqual(self.commands(), [])

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
        self.assertEqual(self.calls("cosign", "verify"), [])
        self.assertIn("/usr/bin/skopeo", self.calls("podman", "run")[1]["args"])
        self.assert_no_promotion()

    def test_signing_failure_prevents_verification_and_promotion(self):
        result = self.publish(FAKE_SIGN_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("cosign", "verify"), [])
        self.assert_no_promotion()

    def test_version_is_validated_signed_and_verified_without_advancing_channels(self):
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
        self.assertNotIn("--key", signing["args"])
        native_verification = self.calls("podman", "run")[1]
        self.assertIn("/usr/bin/skopeo", native_verification["args"])
        self.assertIn(LOCAL_IMAGE_ID, native_verification["args"])
        self.assertIn(f"docker://{IMMUTABLE_IMAGE}", native_verification["args"])
        self.assert_no_promotion()

    def test_verify_release_checks_workflow_identity_and_only_imports_verified_digest(self):
        destination = "oci:" + str(self.work / "verified-image") + ":verified"
        result = self.run_script("verify-release.sh", IMMUTABLE_IMAGE, destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        verification = self.calls("cosign", "verify")[0]
        self.assertEqual(verification["args"], ["verify", "--new-bundle-format=false",
                                              "--certificate-oidc-issuer", OIDC_ISSUER,
                                              "--certificate-identity", WORKFLOW_IDENTITY, IMMUTABLE_IMAGE])
        policy_copy = self.calls("skopeo", "copy")[0]
        self.assertEqual(policy_copy["policy"], {
            "default": [{"type": "reject"}],
            "transports": {"docker": {IMMUTABLE_IMAGE: [{"type": "insecureAcceptAnything"}]}},
        })
        self.assertEqual(policy_copy["args"][-2:], [f"docker://{IMMUTABLE_IMAGE}", destination])
        self.assertIn("--preserve-digests", policy_copy["args"])

    def test_verify_installer_checksums_pins_workflow_identity(self):
        checksum = str(self.work / "SHA256SUMS")
        result = self.run_script("verify-release.sh", "--checksums", checksum)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("cosign", "verify-blob")[0]["args"], [
            "verify-blob", "--certificate-oidc-issuer", OIDC_ISSUER,
            "--certificate-identity", WORKFLOW_IDENTITY, "--bundle", checksum + ".sigstore.json", checksum,
        ])

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

    def test_payload_validation_rejects_the_wrong_default_plymouth_theme(self):
        self.payload_fixture()
        result = self.publish(FAKE_PLYMOUTH_THEME="bgrt")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_push_or_promotion()

    def test_release_uses_locked_inputs_and_runs_on_lock_changes(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        steps = workflow["jobs"]["release"]["steps"]
        build = next(step["run"] for step in steps if step.get("name") == "Build clean release payload")
        self.assertIn("scripts/build-inputs.py build payload", build)
        events = workflow.get("on", workflow.get(True, {}))
        self.assertEqual(events["push"]["paths"], ["build-inputs/lock.json"])
        self.assertIn("workflow_dispatch", events)

    def test_payload_validation_rejects_host_update_plugins(self):
        payload = self.payload_fixture()
        for name in ("dnf5", "packagekit", "rpm-ostree", "fwupd"):
            path = payload / f"usr/lib64/gnome-software/plugins-23/libgs_plugin_{name}.so"
            path.touch()
            result = self.run_script("validate-image.sh", "localhost/mertensia:release")
            self.assertNotEqual(result.returncode, 0, name)
            path.unlink()

    def test_payload_validation_requires_bootc_status_change_hook(self):
        payload = self.payload_fixture()
        (payload / "etc/systemd/system/bootc-status-updated.target.wants/mertensia-update-status.service").unlink()
        result = self.run_script("validate-image.sh", "localhost/mertensia:release")
        self.assertNotEqual(result.returncode, 0)

    def test_payload_validation_rejects_browser_policy_changes_and_writable_files(self):
        payload = self.payload_fixture()
        policy = payload / "etc/brave/policies/managed/mertensia.json"
        original = policy.read_text()
        policy.write_text('{"SitePerProcess": false}')
        result = self.run_script("validate-image.sh", "localhost/mertensia:release")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Brave managed policies differ", result.stderr)
        values = json.loads(original)
        values["SitePerProcess"] = 1  # Python considers 1 == True; Browser policy types differ.
        policy.write_text(json.dumps(values))
        result = self.run_script("validate-image.sh", "localhost/mertensia:release")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Brave managed policies differ", result.stderr)
        policy.write_text(original)
        policy.chmod(0o666)
        result = self.run_script("validate-image.sh", "localhost/mertensia:release")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsafe Brave policy", result.stderr)

    def test_payload_validation_requires_skip_dialog_on_every_browser_action(self):
        payload = self.payload_fixture()
        for name in ("brave-origin.desktop", "com.brave.Origin.desktop"):
            desktop = payload / "usr/share/applications" / name
            original = desktop.read_text()
            for suffix in (" %U", "", " --incognito"):
                with self.subTest(name=name, suffix=suffix):
                    desktop.write_text(original.replace(
                        f" --skip-origin-startup-dialog{suffix}\n", f"{suffix}\n"))
                    result = self.run_script("validate-image.sh", "localhost/mertensia:release")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"Incorrect Brave Origin launch command in {name}", result.stderr)
                desktop.write_text(original)

    def test_payload_validation_rejects_missing_browser_and_wrong_handler(self):
        payload = self.payload_fixture()
        defaults = payload / "usr/share/applications/gnome-mimeapps.list"
        defaults.write_text(defaults.read_text().replace("brave-origin.desktop", "other.desktop"))
        result = self.run_script("validate-image.sh", "localhost/mertensia:release")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing Brave Origin default handler", result.stderr)
        (payload / "usr/bin/brave-origin-stable").unlink()
        self.assertNotEqual(self.run_script("validate-image.sh", "localhost/mertensia:release").returncode, 0)

    def test_payload_validation_rejects_missing_update_timer_or_rebooting_override(self):
        payload = self.payload_fixture()
        override = payload / "usr/lib/systemd/system/bootc-fetch-apply-updates.service.d/50-mertensia-stage.conf"
        original = override.read_text()
        override.write_text(original + "\nExecStartPost=/usr/bin/bootc upgrade --apply\n")
        self.assertNotEqual(self.run_script("validate-image.sh", "localhost/mertensia:release").returncode, 0)
        override.write_text(original)
        (payload / "etc/systemd/system/timers.target.wants/bootc-fetch-apply-updates.timer").unlink()
        self.assertNotEqual(self.run_script("validate-image.sh", "localhost/mertensia:release").returncode, 0)

    def test_changed_sigstore_trust_roots_prevent_publication(self):
        payload = self.payload_fixture()
        for name in ("mertensia-fulcio.crt", "mertensia-rekor.pub"):
            with self.subTest(name=name):
                trust_root = payload / "etc/pki/containers" / name
                original = trust_root.read_text()
                trust_root.write_text(original + "\n")
                result = self.publish()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("trust root differs", result.stderr)
                self.assert_no_push_or_promotion()
                trust_root.write_text(original)

    def test_extra_accept_rule_in_payload_policy_prevents_publication(self):
        payload = self.payload_fixture()
        policy_file = payload / "etc/containers/mertensia-bootc-policy.json"
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
        policy["transports"]["docker"] = {"ghcr.io/mertensiaos": [{"type": "insecureAcceptAnything"}]}
        policy_file.write_text(json.dumps(policy))
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_push_or_promotion()

    def test_changed_workflow_identity_prevents_publication(self):
        payload = self.payload_fixture()
        policy_file = payload / "etc/containers/mertensia-bootc-policy.json"
        policy = json.loads(policy_file.read_text())
        policy["transports"]["docker"][REPOSITORY][0]["fulcio"]["buildSignerURI"] += "-wrong-signer"
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
                         "usr/lib/systemd/system/greenboot-healthcheck.service.d/99-mertensia-integration-console.conf",
                         "usr/lib/greenboot/check/required.d/00-mertensia-integration-failure.sh",
                         "etc/greenboot/check/required.d/00-mertensia-integration-local.sh",
                         "usr/share/mertensia-integration",
                         "usr/share/mertensia-flatpak-tests",
                         "usr/share/mertensia-keyring-tests",
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

    def test_missing_health_check_and_wrong_retry_policy_prevent_publication(self):
        payload = self.payload_fixture()
        hook = payload / "usr/lib/greenboot/check/required.d/50-mertensia-desktop.sh"
        hook.unlink()
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_push_or_promotion()
        hook.write_text("#!/usr/bin/bash\nexit 0\n")
        hook.chmod(0o755)
        (payload / "etc/greenboot/greenboot.conf").write_text("GREENBOOT_MAX_BOOT_ATTEMPTS=0\n")
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Greenboot retry policy", result.stderr)
        self.assert_no_push_or_promotion()

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

    def test_workflow_resolves_one_version_for_publication_and_artifacts(self):
        workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["version"]["required"], "false")
        steps = workflow["jobs"]["release"]["steps"]
        resolve = next(step for step in steps if step.get("id") == "version")
        publish = next(step for step in steps if step.get("id") == "publish")
        upload = next(step for step in steps if step.get("name") == "Save installer and signed checksums")
        self.assertEqual(publish["env"]["RELEASE_VERSION"], "${{ steps.version.outputs.version }}")
        self.assertEqual(upload["with"]["name"], "mertensiaos-${{ steps.version.outputs.version }}")

        date = self.bin / "date"
        date.write_text('#!/bin/bash\n[[ "$*" == "-u +%Y%m%d" ]] && printf "20261123\\n"\n')
        date.chmod(0o755)
        os_release = ROOT / "system/config/identity/os-release"
        version_id = next(line.split("=", 1)[1] for line in os_release.read_text().splitlines()
                          if line.startswith("VERSION_ID="))
        output = self.work / "github-output"

        for override, attempt, expected in (
            ("", "1", f"{version_id}.20261123.12.1"),
            ("", "2", f"{version_id}.20261123.12.2"),
            ("45.20261123.1", "1", "45.20261123.1"),
            ("testing", "1", None),
            ("45.1\nversion=latest", "1", None),
        ):
            with self.subTest(override=override, attempt=attempt):
                output.unlink(missing_ok=True)
                result = subprocess.run(
                    ["bash", "-e", "-c", resolve["run"]], cwd=ROOT,
                    env=self.environment | {
                        "VERSION_OVERRIDE": override,
                        "GITHUB_RUN_NUMBER": "12",
                        "GITHUB_RUN_ATTEMPT": attempt,
                        "GITHUB_OUTPUT": str(output),
                    }, capture_output=True, text=True, timeout=20,
                )
                if expected is None:
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertFalse(output.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_text(), f"version={expected}\n")

    def test_release_gates_and_publishes_the_rechunked_payload(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        job = workflow["jobs"]["release"]
        steps = job["steps"]
        build = next(step for step in steps if "scripts/build-inputs.py build payload" in step.get("run", ""))
        rechunk = next(step for step in steps if "scripts/rechunk-image.sh" in step.get("run", ""))
        checks = next(step for step in steps if "/actions/workflows/ci.yml/runs" in step.get("run", ""))
        login = next(step for step in steps if "podman login" in step.get("run", ""))
        fixture = next(step for step in steps if step.get("id") == "fixture")
        gate = next(step for step in steps if "scripts/test-iso.sh" in step.get("run", ""))
        publish = next(step for step in steps if step.get("id") == "publish")
        self.assertIn("build payload localhost/mertensiaos:release-unpacked", build["run"])
        self.assertEqual(shlex.split(rechunk["run"])[-2:],
                         ["localhost/mertensiaos:release-unpacked", "localhost/mertensiaos:release"])
        self.assertIn("BASE_IMAGE=localhost/mertensiaos:release", fixture["run"])
        self.assertIn("--base-image localhost/mertensiaos:release", gate["run"])
        self.assertIn("scripts/publish-image.sh localhost/mertensiaos:release ", publish["run"])
        self.assertEqual(job["permissions"]["actions"], "read")
        self.assertIn('-f branch=main -f event=push -f "head_sha=$GITHUB_SHA"', checks["run"])
        production_gate = next(step for step in steps if "--production-upgrade-from" in step.get("run", ""))
        testing = next(step for step in steps if "release-channel.py testing" in step.get("run", ""))
        order = [steps.index(step) for step in (build, rechunk, checks, login, fixture, gate, publish, production_gate, testing)]
        self.assertEqual(order, sorted(order))
        for step in (rechunk, checks, gate, production_gate, testing):
            self.assertNotIn("continue-on-error", step)
            self.assertNotIn("if", step)
        for step in steps:
            if "run" in step:
                checked = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_release_signs_all_health_fixtures_and_requires_automatic_recovery(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        job = workflow["jobs"]["release"]
        self.assertEqual(job["permissions"]["id-token"], "write")
        self.assertIn("github.ref == 'refs/heads/main'", job["if"])
        steps = job["steps"]
        fixture = next(step for step in steps if step.get("id") == "fixture")
        gate = next(step for step in steps if "--require-greenboot-recovery" in step.get("run", ""))
        self.assertIn("for mode in none always transient", fixture["run"])
        self.assertIn('INTEGRATION_HEALTH_MODE=$mode', fixture["run"])
        self.assertIn('cosign sign --yes', fixture["run"])
        self.assertIn('--greenboot-failure-image "$FAILURE_IMAGE"', gate["run"])
        self.assertIn('--greenboot-transient-image "$TRANSIENT_IMAGE"', gate["run"])
        self.assertIn('--automatic-update-image "$AUTOMATIC_UPDATE_IMAGE"', gate["run"])
        self.assertLess(steps.index(gate), next(index for index, step in enumerate(steps) if step.get("id") == "publish"))
        for step in (fixture, gate):
            checked = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()
