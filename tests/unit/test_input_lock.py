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
            git("commit", "-qm", "Start")
            main = git("rev-parse", "HEAD")
            git("push", "origin", "main")
            gh = base / "gh"
            gh.write_text("""#!/bin/bash
printf '%s %s\\n' "$1" "$2" >> "$PR_LOG"
case "$1 $2" in
  'pr list') if [[ -f "$PR_STATE" ]]; then echo 16; fi ;;
  'pr create') touch "$PR_STATE" ;;
  'pr view') if [[ "$*" != *labels* ]]; then echo 16; fi ;;
esac
""")
            gh.chmod(0o755)
            env = os.environ | {"PATH": str(base) + os.pathsep + os.environ["PATH"],
                                "GITHUB_SHA": main, "GITHUB_REPOSITORY": "example/project",
                                "GITHUB_OUTPUT": str(base / "outputs"), "PR_LOG": str(log),
                                "PR_STATE": str(base / "state")}
            head = None
            for value in ("updated", "updated", "updated again"):
                git("reset", "--hard", main)
                (lock / "lock.json").write_text(value)
                result = subprocess.run(["bash", str(ROOT / "scripts/propose-input-refresh.sh"), str(report), "false"],
                                        cwd=checkout, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                observed = git("ls-remote", "--heads", "origin", "automation/refresh-build-inputs").split()[0]
                if head is None:
                    head = observed
                elif value == "updated":
                    self.assertEqual(head, observed)
                else:
                    self.assertNotEqual(head, observed)
            self.assertEqual(log.read_text().splitlines().count("pr create"), 1)
            self.assertEqual(log.read_text().splitlines().count("pr edit"), 2)
