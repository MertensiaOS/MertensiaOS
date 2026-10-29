"""Exercise automatic staging policy without changing the host OS."""

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from mertensia.boot_health import backend as health, notifier
from mertensia.updates import backend


DIGEST = "sha256:" + "a" * 64
DEPLOYMENT = {"image": {"image": {"image": "ghcr.io/mertensiaos/mertensiaos:latest",
                               "transport": "registry", "signature": "containerPolicy"},
                         "imageDigest": DIGEST}}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name, value in (("STATE", self.root / "updates"), ("STATUS", self.root / "updates/status.json"),
                            ("EVENT", self.root / "recovery.json"),
                            ("ACKNOWLEDGEMENT", self.root / "updates/acknowledgement.json"),
                            ("BOOT_ID", self.root / "boot_id")):
            patch = mock.patch.object(backend, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        backend.BOOT_ID.write_text("boot-a\n")
        self.status = {"booted": copy.deepcopy(DEPLOYMENT), "staged": None}
        self.recovery = {"version": 1, "event_id": "rollback-boot-a", "restored_image_digest": DIGEST}

    def test_stages_with_bootc_without_apply_and_publishes_after_success(self):
        after = self.status | {"staged": copy.deepcopy(DEPLOYMENT)}
        with mock.patch.object(backend, "bootc_status", side_effect=[self.status, after]), \
             mock.patch.object(backend, "healthy_boot", return_value=True), \
             mock.patch.object(backend, "unmetered_network", return_value=True), \
             mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            result = backend.stage()
        run.assert_called_once_with(["bootc", "upgrade"], timeout=3600, check=False)
        self.assertEqual(result["staged_digest"], DIGEST)
        self.assertEqual(result["boot_id"], "boot-a")
        self.assertEqual(backend.STATUS.stat().st_mode & 0o777, 0o644)

    def test_pinned_local_incompatible_and_staged_deployments_are_preserved(self):
        cases = []
        for reference, transport in (("ghcr.io/mertensiaos/mertensiaos@" + DIGEST, "registry"),
                                     ("localhost/mertensiaos:base", "containers-storage")):
            status = copy.deepcopy(self.status)
            status["booted"]["image"]["image"].update(image=reference, transport=transport)
            cases.append(status)
        cases += [self.status | {"staged": DEPLOYMENT},
                  self.status | {"rollbackQueued": True},
                  self.status | {"booted": self.status["booted"] | {"incompatible": True}}]
        for signature in (None, "insecure", {"ostreeRemote": "other"}):
            status = copy.deepcopy(self.status)
            status["booted"]["image"]["image"]["signature"] = signature
            cases.append(status)
        for status in cases:
            with self.subTest(status=status), mock.patch.object(backend, "bootc_status", return_value=status), \
                 mock.patch.object(backend.subprocess, "run") as run:
                self.assertTrue(backend.stage()["reason"])
                run.assert_not_called()

    def test_unhealthy_boot_or_network_defers_without_running_upgrade(self):
        for healthy, network in ((False, True), (True, False)):
            with self.subTest(healthy=healthy, network=network), \
                 mock.patch.object(backend, "bootc_status", return_value=self.status), \
                 mock.patch.object(backend, "healthy_boot", return_value=healthy), \
                 mock.patch.object(backend, "unmetered_network", return_value=network), \
                 mock.patch.object(backend.subprocess, "run") as run:
                self.assertTrue(backend.stage()["reason"])
                run.assert_not_called()

    def test_health_requires_both_successful_active_units_started_this_boot(self):
        good = "ActiveState=active\nResult=success\nExecMainStartTimestampMonotonic=123\n"
        for bad in ("ActiveState=inactive\nResult=success\n", "ActiveState=failed\n", good.replace("123", "0")):
            with mock.patch.object(backend, "command", side_effect=[good, bad]):
                self.assertFalse(backend.healthy_boot())
        with mock.patch.object(backend, "command", return_value=good) as command:
            self.assertTrue(backend.healthy_boot())
        self.assertEqual([call.args[0][2] for call in command.call_args_list],
                         ["greenboot-healthcheck.service", "mertensia-boot-health-status.service"])

    def test_network_accepts_only_global_unmetered_connections(self):
        for network_state in ("u 20", "u 50", "u 60", "u 70"):
            for metered in ("u 0", "u 1", "u 2", "u 3", "u 4", "garbage"):
                with self.subTest(state=network_state, metered=metered), \
                     mock.patch.object(backend, "command", side_effect=[network_state, metered]):
                    self.assertEqual(backend.unmetered_network(), network_state == "u 70" and metered in {"u 2", "u 4"})

    def test_failed_signature_disk_full_or_download_failure_never_claims_success(self):
        for code in (1, 28):
            with self.subTest(code=code), mock.patch.object(backend, "bootc_status", return_value=self.status), \
                 mock.patch.object(backend, "skip_reason", return_value=None), \
                 mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], code)):
                with self.assertRaises(backend.UpdateError):
                    backend.stage()
            public = json.loads(backend.STATUS.read_text())
            self.assertIsNone(public["staged_digest"])
            self.assertIn("failed", public["reason"])

    def test_interrupted_download_refreshes_actual_staged_state(self):
        with mock.patch.object(backend, "bootc_status", return_value=self.status), \
             mock.patch.object(backend, "skip_reason", return_value=None), \
             mock.patch.object(backend.subprocess, "run", side_effect=subprocess.TimeoutExpired("bootc", 3600)):
            with self.assertRaises(backend.UpdateError):
                backend.stage()
        self.assertIsNone(json.loads(backend.STATUS.read_text())["staged_digest"])

    def test_recovery_pauses_until_root_acknowledges_and_new_event_pauses_again(self):
        health.atomic_json(backend.EVENT, self.recovery)
        with mock.patch.object(backend, "bootc_status", return_value=self.status), \
             mock.patch.object(backend.subprocess, "run") as run:
            self.assertEqual(backend.stage()["recovery_pending"], "rollback-boot-a")
            run.assert_not_called()
        with mock.patch.object(backend, "healthy_boot", return_value=True), \
             mock.patch.object(backend, "bootc_status", return_value=self.status):
            self.assertIsNone(backend.resume()["recovery_pending"])
        self.assertEqual(backend.ACKNOWLEDGEMENT.stat().st_mode & 0o777, 0o600)
        health.atomic_json(backend.EVENT, self.recovery | {"event_id": "rollback-boot-b"})
        self.assertEqual(backend.recovery_pending(), "rollback-boot-b")

    def test_resume_requires_healthy_boot_and_does_not_upgrade(self):
        with mock.patch.object(backend, "healthy_boot", return_value=False), \
             mock.patch.object(backend.subprocess, "run") as run:
            with self.assertRaises(backend.UpdateError):
                backend.resume()
            run.assert_not_called()

    def test_corrupt_recovery_and_acknowledgement_fail_closed(self):
        for value in ({}, [], {"version": 1, "event_id": 7}, {"event_id": "invalid"}):
            health.atomic_json(backend.EVENT, value)
            with self.subTest(value=value), self.assertRaises(backend.UpdateError):
                backend.recovery_pending()
        health.atomic_json(backend.EVENT, self.recovery)
        health.atomic_json(backend.ACKNOWLEDGEMENT, [])
        with self.assertRaises(backend.UpdateError):
            backend.recovery_pending()

    def test_cli_enforces_privilege_and_never_runs_on_live_or_container_systems(self):
        with mock.patch.object(backend.os, "geteuid", return_value=1000), \
             mock.patch.object(backend, "stage") as stage, mock.patch.object(backend, "resume") as resume:
            self.assertEqual(backend.main(["stage"]), 1)
            self.assertEqual(backend.main(["resume"]), 1)
            self.assertEqual(backend.main(["status"]), 0)
            stage.assert_not_called()
            resume.assert_not_called()
        with mock.patch.object(backend.os, "geteuid", return_value=0), \
             mock.patch.object(backend, "installed_system", return_value=False), \
             mock.patch.object(backend, "stage") as stage:
            self.assertEqual(backend.main(["stage"]), 0)
            stage.assert_not_called()

    def test_commands_bound_runtime_and_hide_private_output(self):
        with mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "secret", "secret")):
            with self.assertRaisesRegex(backend.UpdateError, "bootc exited with status 1"):
                backend.command(["bootc", "status"])

    def test_notifications_are_once_per_user_and_clear_after_boot(self):
        status = backend.publish(self.status | {"staged": DEPLOYMENT})
        seen = self.root / "user-a/seen.json"
        send = mock.Mock()
        self.assertTrue(notifier.deliver_update(backend.STATUS, seen, send, "boot-a"))
        self.assertFalse(notifier.deliver_update(backend.STATUS, seen, send, "boot-a"))
        self.assertFalse(notifier.deliver_update(backend.STATUS, seen, send, "boot-b"))
        self.assertTrue(notifier.deliver_update(backend.STATUS, self.root / "user-b/seen.json", send, "boot-a"))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(seen.stat().st_mode & 0o777, 0o600)
        health.atomic_json(backend.STATUS, status | {"staged_digest": None})
        self.assertFalse(notifier.deliver_update(backend.STATUS, seen, send, "boot-a"))
        self.assertEqual(json.loads(seen.read_text()), {})

    def test_status_operation_refreshes_manual_staging_and_removal_for_notifications(self):
        seen = self.root / "seen.json"
        send = mock.Mock()
        with mock.patch.object(backend.os, "geteuid", return_value=0), \
             mock.patch.object(backend, "installed_system", return_value=True), \
             mock.patch.object(backend, "bootc_status", side_effect=[
                 self.status | {"staged": copy.deepcopy(DEPLOYMENT)}, self.status,
             ]), mock.patch.object(backend, "stage") as stage, mock.patch("builtins.print"):
            self.assertEqual(backend.main(["status"]), 0)
            self.assertEqual(json.loads(backend.STATUS.read_text())["staged_digest"], DIGEST)
            self.assertTrue(notifier.deliver_update(backend.STATUS, seen, send, "boot-a"))
            self.assertEqual(backend.main(["status"]), 0)
            self.assertIsNone(json.loads(backend.STATUS.read_text())["staged_digest"])
            self.assertFalse(notifier.deliver_update(backend.STATUS, seen, send, "boot-a"))
            stage.assert_not_called()
        send.assert_called_once()
        self.assertEqual(json.loads(seen.read_text()), {})

    def test_notification_failure_is_retried_and_pause_suppresses_restart(self):
        status = backend.publish(self.status | {"staged": DEPLOYMENT})
        seen = self.root / "seen.json"
        with self.assertRaises(OSError):
            notifier.deliver_update(backend.STATUS, seen, mock.Mock(side_effect=OSError("no bus")), "boot-a")
        self.assertFalse(seen.exists())
        health.atomic_json(backend.STATUS, status | {"recovery_pending": "rollback-boot-a"})
        send = mock.Mock()
        self.assertFalse(notifier.deliver_update(backend.STATUS, seen, send, "boot-a"))
        send.assert_not_called()

    def test_recovery_notification_explains_update_pause(self):
        health.atomic_json(backend.EVENT, self.recovery)
        backend.publish(self.status)
        send = mock.Mock()
        self.assertTrue(notifier.deliver(backend.EVENT, self.root / "seen.json", send, backend.STATUS))
        self.assertIn("sudo mertensia-update resume", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
