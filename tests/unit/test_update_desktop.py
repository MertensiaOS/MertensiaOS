"""Desktop update controls must preserve the updater's authorization and policy."""

import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from mertensia.boot_health import notifier
from mertensia.updates import backend, client, helper


class DesktopUpdateTests(unittest.TestCase):
    def setUp(self):
        self.state = {"record": {}, "current": True, "supported": True, "running": False,
                      "failed": False, "timer_enabled": True, "timer_active": True}

    def test_empty_staged_slot_does_not_claim_the_channel_is_current(self):
        title, description, _ = client.presentation(self.state)
        self.assertEqual(title, "No update staged")
        self.assertIn("Check and download", description)
        self.assertNotIn("up to date", description.lower())

    def test_recovery_rollback_and_restart_take_precedence_over_download_state(self):
        for record, expected in (({"recovery_pending": "rollback-a", "staged_digest": "a"}, "Updates paused after recovery"),
                                 ({"rollback_queued": True, "staged_digest": "a"}, "Rollback ready"),
                                 ({"staged_digest": "a"}, "Ready to restart")):
            with self.subTest(record=record):
                self.assertEqual(client.presentation(self.state | {"record": record, "running": True})[0], expected)

    def test_interrupted_and_stale_attempts_do_not_claim_to_be_downloading(self):
        self.assertEqual(client.presentation(self.state | {"current": False})[0], "Waiting for system status")
        self.assertEqual(client.presentation(self.state | {"record": {"activity": "downloading"}})[0], "Update attempt interrupted")
        self.assertEqual(client.presentation(self.state | {"running": True})[0], "Checking and downloading")
        self.assertEqual(client.presentation(self.state | {"failed": True})[0], "Update attempt failed")

    def test_snapshot_discards_previous_boot_deployment_and_recovery_state(self):
        with tempfile.TemporaryDirectory() as directory:
            status, boot_id = Path(directory) / "status", Path(directory) / "boot_id"
            status.write_text(json.dumps({"version": 1, "boot_id": "old", "staged_digest": "a", "recovery_pending": "rollback-a"}))
            boot_id.write_text("new\n")
            with mock.patch.object(client, "STATUS", status), mock.patch.object(client, "BOOT_ID", boot_id), \
                 mock.patch.object(client, "unit_properties", return_value={"LoadState": "loaded"}):
                result = client.snapshot()
                self.assertFalse(result["current"])
                self.assertEqual(result["record"], {})
                status.write_text("[]")
                with self.assertRaises(backend.UpdateError):
                    client.snapshot()

    def test_pkexec_client_uses_only_fixed_helper_operations_and_reports_cancellation(self):
        with mock.patch.object(client.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            client.administrator_request("disable")
            self.assertEqual(run.call_args.args[0], ["/usr/bin/pkexec", "/usr/libexec/mertensia-update-helper", "disable"])
            with self.assertRaises(backend.UpdateError):
                client.administrator_request("start sshd")
            self.assertEqual(run.call_count, 1)
        with mock.patch.object(client.subprocess, "run", return_value=subprocess.CompletedProcess([], 126, "", "")):
            with self.assertRaisesRegex(backend.UpdateError, "cancelled"):
                client.administrator_request("resume")

    def test_helper_revalidates_original_caller_before_any_mutation(self):
        user = SimpleNamespace(pw_name="alex", pw_gid=1000)
        with mock.patch.object(helper.os, "geteuid", return_value=0), \
             mock.patch.dict(helper.os.environ, {"PKEXEC_UID": "1000"}), \
             mock.patch.object(helper.pwd, "getpwuid", return_value=user), \
             mock.patch.object(helper.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=10)), \
             mock.patch.object(helper.os, "getgrouplist", return_value=[1000, 10]), \
             mock.patch.object(backend, "installed_system", return_value=True), \
             mock.patch.object(backend, "command") as command:
            helper.perform("download")
            command.assert_called_once_with(["systemctl", "start", "--no-block", helper.SERVICE])
            command.reset_mock()
            with mock.patch.object(helper.os, "getgrouplist", return_value=[1000]):
                with self.assertRaisesRegex(backend.UpdateError, "Only an administrator"):
                    helper.perform("download")
            with mock.patch.dict(helper.os.environ, {"PKEXEC_UID": "invalid"}):
                with self.assertRaises(backend.UpdateError):
                    helper.perform("enable")
            with mock.patch.object(backend, "installed_system", return_value=False):
                with self.assertRaises(backend.UpdateError):
                    helper.perform("disable")
            user.pw_name = "mertensia-setup"
            with self.assertRaises(backend.UpdateError):
                helper.perform("enable")
            command.assert_not_called()

    def test_timer_operations_do_not_cancel_downloads_or_stage_an_image(self):
        with mock.patch.object(helper, "authorize"), mock.patch.object(backend, "command") as command:
            helper.perform("disable")
            helper.perform("enable")
            self.assertEqual(command.call_args_list, [
                mock.call(["systemctl", "disable", "--now", helper.TIMER]),
                mock.call(["systemctl", "enable", "--now", helper.TIMER]),
            ])
        with mock.patch.object(helper, "authorize") as authorize:
            with self.assertRaises(backend.UpdateError):
                helper.perform("rollback")
            authorize.assert_not_called()

    def test_resume_uses_the_same_recovery_acknowledgement_without_waiting_for_download(self):
        with mock.patch.object(helper, "authorize"), mock.patch.object(backend, "locked") as locked, \
             mock.patch.object(backend, "resume") as resume:
            helper.perform("resume")
            locked.assert_called_once_with(blocking=False)
            resume.assert_called_once_with()

    def test_clicking_owned_notifications_opens_updates_without_privilege(self):
        from gi.repository import Gio, GLib
        ids = {42}
        with mock.patch.object(Gio.Subprocess, "new") as launch:
            notifier.notification_action(ids, GLib.Variant("(us)", (99, "default")))
            notifier.notification_action(ids, GLib.Variant("(us)", (42, "unexpected")))
            launch.assert_not_called()
            notifier.notification_action(ids, GLib.Variant("(us)", (42, "default")))
            launch.assert_called_once_with(["/usr/bin/mertensia-system", "--page", "updates"], Gio.SubprocessFlags.NONE)
            self.assertEqual(ids, set())

    def test_notification_delivery_keeps_the_daemon_id_for_click_routing(self):
        from gi.repository import Gio, GLib
        ids = set()
        with mock.patch.object(Gio, "bus_get_sync") as bus:
            bus.return_value.call_sync.return_value = GLib.Variant("(u)", (42,))
            notifier.send_notification(notifier.UPDATE_MESSAGE, ids)
        self.assertEqual(ids, {42})


if __name__ == "__main__":
    unittest.main()
