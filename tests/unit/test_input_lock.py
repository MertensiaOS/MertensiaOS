import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inputs = script("build-inputs")
release = script("release-channel")
upstream = script("check-upstream")


class LockTests(unittest.TestCase):
    def test_unavailable_base_falls_back_to_the_same_manifest_at_fedora(self):
        manifest = "sha256:" + "a" * 64
        quay = f"quay.io/fedora/fedora-bootc@{manifest}"
        mirror = f"registry.fedoraproject.org/fedora-bootc@{manifest}"
        uncached = SimpleNamespace(returncode=1)
        missing = SimpleNamespace(returncode=125, stderr="manifest unknown")
        available = SimpleNamespace(returncode=0, stderr="")
        with patch.object(inputs.subprocess, "run", side_effect=[uncached, uncached, missing, available]) as podman, \
             patch.object(inputs.sys, "stderr"):
            self.assertEqual(inputs.pull_base(quay), mirror)
        self.assertEqual([call.args[0] for call in podman.call_args_list], [
            ("podman", "image", "exists", quay), ("podman", "image", "exists", mirror),
            ("podman", "pull", quay), ("podman", "pull", mirror),
        ])

    def test_missing_and_modified_cached_artifacts_are_rejected(self):
        data = b"locked bytes"
        package = {"file": "example.rpm", "sha256": inputs.digest(data)}
        lock = {"inputs": {"transactions": {"demo": {"packages": [package]}}}}
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "demo").mkdir()
            with self.assertRaises(FileNotFoundError):
                inputs.verify_snapshot(lock, root)
            rpm = root / "demo" / package["file"]
            rpm.write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                inputs.verify_snapshot(lock, root)
            rpm.write_bytes(data)
            inputs.verify_snapshot(lock, root)

    def test_build_recipe_drift_stops_before_materializing_artifacts(self):
        lock = {"inputs": json.loads(inputs.CONFIG.read_text()) | {"recipe": {}}}
        with patch.object(inputs, "read_lock", return_value=lock), patch.object(inputs, "acquire") as acquire:
            with self.assertRaisesRegex(ValueError, "RPM build recipe changed"):
                inputs.normal_build(None)
        acquire.assert_not_called()

    def test_unchanged_resolution_does_not_publish_or_rewrite(self):
        with tempfile.TemporaryDirectory() as work:
            lock = Path(work) / "lock.json"
            lock.write_text("old lock")
            draft = Path(work) / "draft.json"
            draft.write_text('{"inputs":{"base":"same"}}')
            with patch.object(inputs, "LOCK", lock), patch.object(inputs, "read_lock", return_value={"inputs": {"base": "same"}}), patch.object(inputs, "run") as run:
                inputs.retain(SimpleNamespace(draft=draft))
            self.assertEqual(lock.read_text(), "old lock")
            run.assert_not_called()


class UpstreamTests(unittest.TestCase):
    def test_installed_source_build_matches_advisory_including_inherited_and_rebuilt_packages(self):
        published = upstream.inventory(["kernel-core\t0\t1\t1.fc45\tx86_64\tkernel-1-1.fc45.src.rpm",
                                        "skopeo\t1\t1\t1.fc45.mertensia1\tx86_64\tskopeo-1-1.fc45.mertensia1.src.rpm"])
        current = upstream.inventory(["kernel-core\t0\t2\t1.fc45\tx86_64\tkernel-2-1.fc45.src.rpm",
                                      "skopeo\t1\t2\t1.fc45.mertensia1\tx86_64\tskopeo-2-1.fc45.mertensia1.src.rpm"])
        update = {"alias": "FEDORA-2026-abcd", "type": "security",
                  "builds": [{"nvr": "kernel-2-1.fc45"}, {"nvr": "skopeo-2-1.fc45"}]}
        fixed, pending = upstream.correlate(published, current, [update])
        self.assertEqual(set(fixed), {update["alias"]})
        self.assertEqual(pending, {})
        fixed, pending = upstream.correlate(published, {}, [update])
        self.assertEqual(fixed, {})
        self.assertEqual(set(pending), {update["alias"]})

    def test_unavailable_bodhi_is_an_error(self):
        with patch.object(upstream, "get_json", side_effect=OSError("Bodhi unavailable")):
            with self.assertRaisesRegex(OSError, "Bodhi unavailable"):
                upstream.bodhi_updates(45, ["kernel"])


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.stable = "sha256:" + "a" * 64
        self.candidate = "sha256:" + "b" * 64
        self.other = "sha256:" + "c" * 64
        self.configs = {
            self.stable: {release.PREFIX + "release-order": "3.1"},
            self.candidate: {release.PREFIX + "release-order": "4.1",
                             release.PREFIX + "upgrade-baseline": f"latest@{self.stable}"},
            self.other: {release.PREFIX + "release-order": "5.1"},
        }

    def test_failed_gate_cannot_be_promoted_to_latest(self):
        with patch.object(release, "resolve", side_effect=[self.stable, self.other]), \
             patch.object(release, "labels", side_effect=self.configs.get), patch.object(release, "verify") as verify:
            with self.assertRaisesRegex(ValueError, "current passing testing digest"):
                release.promote("latest", self.candidate)
            self.assertTrue(all(len(call.args) == 1 for call in verify.call_args_list))

    def test_older_candidate_cannot_replace_newer_testing(self):
        with patch.object(release, "resolve", side_effect=[self.stable, self.other]), \
             patch.object(release, "labels", side_effect=self.configs.get), patch.object(release, "verify") as verify:
            with self.assertRaisesRegex(ValueError, "older than the testing release"):
                release.promote("testing", self.candidate)
            self.assertTrue(all(len(call.args) == 1 for call in verify.call_args_list))

    def test_promote_the_exact_passing_testing_digest(self):
        with patch.object(release, "resolve", side_effect=[self.stable, self.candidate]), \
             patch.object(release, "labels", side_effect=self.configs.get), patch.object(release, "verify") as verify:
            release.promote("latest", self.candidate)
            verify.assert_called_with(f"{release.REPO}@{self.candidate}", f"docker://{release.REPO}:latest")

    def test_registry_outage_does_not_select_bootstrap_baseline(self):
        with patch.object(release, "resolve", side_effect=ValueError("registry unavailable")) as resolve:
            with self.assertRaisesRegex(ValueError, "registry unavailable"):
                release.baseline()
            resolve.assert_called_once_with("latest", optional=True)


class ProposalTests(unittest.TestCase):
    def test_repeated_updates_reuse_the_same_branch_and_pr(self):
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            checkout = base / "checkout"
            checkout.mkdir()
            log = base / "log"
            report = base / "report"
            report.write_text("Updated dependency report.\n")

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=checkout, text=True, stderr=subprocess.PIPE).strip()

            git("init", "-q", "-b", "main")
            git("init", "-q", "--bare", str(base / "upstream"))
            git("remote", "add", "origin", str(base / "upstream"))
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            lock = checkout / "build-inputs"
            lock.mkdir()
            (lock / "config.json").write_text("{}")
            (lock / "lock.json").write_text("original")
            git("add", ".")
            git("commit", "--no-gpg-sign", "-qm", "Start")
            main = git("rev-parse", "HEAD")
            git("push", "origin", "main")
            gh = base / "gh"
            gh.write_text("""#!/usr/bin/env python3
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

def git(*args):
    return subprocess.check_output(["git", *args])

def field(name):
    return next(arg.split("=", 1)[1] for arg in args if arg.startswith(name + "="))

args = sys.argv[1:]
if args == ["auth", "setup-git"]:
    sys.exit()
if "--paginate" in args:
    args.remove("--paginate")
method, route = (args[2], args[3]) if args[1] == "--method" else ("GET", args[1])
api = "repos/example/project"
with open(os.environ["PR_LOG"], "a") as log:
    print(method, route, file=log)
if route == f"{api}/git/refs" and method == "POST":
    git("push", "origin", f"{field('sha')}:{field('ref')}")
elif route.startswith(f"{api}/git/refs/heads/") and method == "DELETE":
    git("push", "origin", ":" + route.removeprefix(f"{api}/git/"))
elif route == "graphql":
    request = json.load(sys.stdin)["variables"]["input"]
    branch = "refs/heads/" + request["branch"]["branchName"]
    assert request["branch"]["repositoryNameWithOwner"] == "example/project"
    parent = request["expectedHeadOid"]
    assert git("ls-remote", "origin", branch).split()[0].decode() == parent
    additions = request["fileChanges"]["additions"]
    assert [entry["path"] for entry in additions] == ["build-inputs/lock.json"]
    for entry in additions:
        assert base64.b64decode(entry["contents"]) == Path(entry["path"]).read_bytes()
    tree = git("write-tree").decode().strip()
    commit = git("commit-tree", "--no-gpg-sign", tree, "-p", parent,
                 "-m", request["message"]["headline"]).decode().strip()
    git("push", "origin", f"{commit}:{branch}")
    if os.environ.get("MOVE_REFRESH_BRANCH"):
        git("push", "--force", "origin", f"{parent}:refs/heads/automation/refresh-build-inputs")
    print(commit)
elif route.startswith(f"{api}/commits/"):
    print(os.environ.get("SIGNATURE_VERIFIED", "true"))
elif route == f"{api}/pulls" and method == "GET":
    if Path(os.environ["PR_STATE"]).exists():
        print(16)
elif route == f"{api}/pulls" and method == "POST":
    Path(os.environ["PR_STATE"]).touch()
    print(16)
elif (method, route) not in {("PATCH", f"{api}/pulls/16"), ("GET", f"{api}/issues/16/labels")}:
    sys.exit(f"Unexpected API request: {method} {route}")
""")
            gh.chmod(0o755)
            env = os.environ | {"PATH": str(base) + os.pathsep + os.environ["PATH"],
                                "GITHUB_SHA": main, "GITHUB_REPOSITORY": "example/project",
                                "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
                                "GITHUB_OUTPUT": str(base / "outputs"), "PR_LOG": str(log),
                                "PR_STATE": str(base / "state")}

            def propose(value, **overrides):
                git("reset", "--hard", main)
                (lock / "lock.json").write_text(value)
                return subprocess.run(["bash", str(ROOT / "scripts/propose-input-refresh.sh"), str(report), "false"],
                                      cwd=checkout, env=env | overrides, text=True, capture_output=True)

            def refresh_head():
                return git("ls-remote", "--heads", "origin", "automation/refresh-build-inputs").split()[0]

            heads = []
            for value in ("updated", "updated", "updated again"):
                result = propose(value)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                heads.append(refresh_head())
            self.assertEqual(heads[0], heads[1])
            self.assertNotEqual(heads[1], heads[2])

            result = propose("unverified input", SIGNATURE_VERIFIED="false")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not verify the signature", result.stderr)
            self.assertEqual(refresh_head(), heads[2])

            result = propose("concurrent update", MOVE_REFRESH_BRANCH="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stale info", result.stderr)
            self.assertEqual(refresh_head(), main)
            self.assertEqual(git("ls-remote", "--heads", "origin", "automation/refresh-signing/*"), "")
            calls = log.read_text().splitlines()
            self.assertEqual(calls.count("POST repos/example/project/pulls"), 1)
            self.assertEqual(calls.count("PATCH repos/example/project/pulls/16"), 2)
            self.assertEqual(calls.count("GET graphql"), 4)
