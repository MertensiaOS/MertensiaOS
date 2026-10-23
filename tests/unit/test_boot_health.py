"""Exercise offline health policy and recovery events without system mutations."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from mertensia.boot_health import backend, notifier


class BootHealthTests(unittest.TestCase):
    def test_completed_login_sync_is_healthy_but_never_started_is_not(self):
        state = {"ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0",
                 "ExecMainStartTimestampMonotonic": "123"}
        self.assertTrue(backend.synchronization_complete(state))
        for changes in ({"ExecMainStartTimestampMonotonic": "0"}, {"ExecMainStatus": "1"},
                        {"Result": "exit-code"}, {"ActiveState": "activating"}, {"ActiveState": "failed"}):
            self.assertFalse(backend.synchronization_complete(state | changes))

    def test_bus_calls_allow_empty_accounts_and_locked_homes(self):
        def command(args, deadline):
            if args[0] == "systemctl":
                active = "inactive" if args[2] == "mertensia-login-users.service" else "active"
                return f"ActiveState={active}\nResult=success\nExecMainStatus=0\nExecMainStartTimestampMonotonic=123\n"
            self.assertEqual(args[0], "busctl")
            return "a(ususso) 0" if args[-1] == "ListHomes" else "ao 0"
        with mock.patch.object(backend, "command", side_effect=command) as run:
            self.assertEqual(backend.desktop_problems(100), [])
        methods = [call.args[0][-1] for call in run.call_args_list if call.args[0][0] == "busctl"]
        self.assertEqual(methods, ["ListHomes", "ListCachedUsers"])
        self.assertFalse(any("network" in str(call).lower() for call in run.call_args_list))

    def test_service_failure_reports_state_without_account_data(self):
        with mock.patch.object(backend, "service_state", return_value={"ActiveState": "failed", "Result": "exit-code"}), \
             mock.patch.object(backend, "command", side_effect=backend.HealthError("busctl timed out")):
            problems = backend.desktop_problems(100)
        self.assertTrue(any("gdm.service: ActiveState=failed" in problem for problem in problems))
        self.assertTrue(any("homed is not responding" in problem for problem in problems))

    def test_readiness_recovers_from_a_transient_failure(self):
        with mock.patch.object(backend, "storage_checks"), \
             mock.patch.object(backend, "desktop_problems", side_effect=[["not ready"], []]), \
             mock.patch.object(backend.time, "sleep") as sleep:
            backend.check_desktop()
        sleep.assert_called_once()

    def test_total_deadline_expires_and_retains_failure_details(self):
        with mock.patch.object(backend, "storage_checks"), \
             mock.patch.object(backend, "desktop_problems", return_value=["gdm.service failed"]), \
             mock.patch.object(backend.time, "monotonic", side_effect=[0, 120]):
            with self.assertRaisesRegex(backend.HealthError, "timed out: gdm.service failed"):
                backend.check_desktop()

    def test_commands_are_bounded_and_hide_output(self):
        with mock.patch.object(backend.time, "monotonic", return_value=10), \
             mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "account-secret", "private")) as run:
            with self.assertRaisesRegex(backend.HealthError, "busctl exited with status 1"):
                backend.command(["busctl", "call"], 12)
        self.assertEqual(run.call_args.kwargs["timeout"], 2)
        with mock.patch.object(backend.time, "monotonic", return_value=10), \
             mock.patch.object(backend.subprocess, "run") as run:
            with self.assertRaises(backend.HealthError):
                backend.command(["busctl"], 10)
            run.assert_not_called()

    def test_plain_device_mapper_root_is_not_accepted_as_encrypted(self):
        def command(args, deadline):
            return "/dev/mapper/plain" if args[0] == "findmnt" else "type: PLAIN"
        with mock.patch.object(backend, "command", side_effect=command):
            with self.assertRaisesRegex(backend.HealthError, "LUKS backing"):
                backend.storage_checks(100)

    def test_storage_checks_encrypted_sysroot_boot_and_writable_var(self):
        def command(args, deadline):
            if args[0] == "cryptsetup":
                return "  type: LUKS2\n"
            return "/dev/mapper/root" if "SOURCE" in args else "/boot"
        original = tempfile.TemporaryFile
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(backend, "command", side_effect=command) as run, \
             mock.patch.object(backend.tempfile, "TemporaryFile", side_effect=lambda **kwargs: original(dir=directory)) as temporary:
            backend.storage_checks(100)
        self.assertTrue(any(call.args[0] == ["grub2-editenv", "/boot/grub2/grubenv", "list"] for call in run.call_args_list))
        self.assertEqual(temporary.call_args.kwargs["dir"], "/var")

    def test_container_and_live_guards_do_not_run_checks(self):
        with mock.patch.object(backend.Path, "exists", return_value=False), \
             mock.patch.object(backend.subprocess, "run") as run:
            self.assertFalse(backend.installed_system())
            run.assert_not_called()
        with mock.patch.object(backend.Path, "exists", side_effect=[True, True]):
            self.assertFalse(backend.installed_system())
        with mock.patch.object(backend.Path, "exists", side_effect=[True, False]), \
             mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertFalse(backend.installed_system())


class RecoveryEventTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.event = Path(self.temporary.name) / "public/recovery.json"
        self.seen = Path(self.temporary.name) / "home/seen.json"
        self.record = {"version": 1, "event_id": "rollback-boot-a", "boot_id": "boot-a",
                       "timestamp": "2026-10-23T00:00:00+00:00", "restored_image_digest": "sha256:" + "a" * 64}

    def test_only_confirmed_automatic_fallback_becomes_an_event(self):
        marker = "FALLBACK BOOT DETECTED! Default bootc deployment has been rolled back."
        self.assertTrue(backend.confirmed_fallback([marker], ["Rollback successful"]))
        self.assertTrue(backend.confirmed_fallback([marker], [" INFO greenboot > Rollback successful"]))
        self.assertTrue(backend.confirmed_fallback(["Rollback to previous deployment completed successfully"], []))
        self.assertFalse(backend.confirmed_fallback([marker], ["Rollback failed"]))
        self.assertFalse(backend.confirmed_fallback(["Making GRUB fallback permanent by rolling back"], []))
        self.assertFalse(backend.confirmed_fallback([], ["Rollback successful"]))

    def test_collector_writes_readable_event_only_after_health_succeeds(self):
        digest = self.record["restored_image_digest"]
        with mock.patch.object(backend, "service_state", return_value={"ActiveState": "active", "Result": "success"}), \
             mock.patch.object(backend, "journal_messages", side_effect=[["Rollback to previous deployment completed successfully"], []]), \
             mock.patch.object(backend, "command", return_value=json.dumps({"status": {"booted": {"image": {"imageDigest": digest}}}})), \
             mock.patch.object(backend.Path, "read_text", return_value="boot-a"):
            backend.collect_status(self.event)
        event = json.loads(self.event.read_text())
        self.assertEqual(event["event_id"], "rollback-boot-a")
        self.assertEqual(event["restored_image_digest"], digest)
        self.assertEqual(self.event.stat().st_mode & 0o777, 0o644)
        with mock.patch.object(backend, "service_state", return_value={"ActiveState": "failed"}), \
             mock.patch.object(backend, "journal_messages") as journal:
            backend.collect_status(self.event)
        journal.assert_not_called()

    def test_normal_and_first_boot_do_not_publish_events(self):
        with mock.patch.object(backend, "service_state", return_value={"ActiveState": "active", "Result": "success"}), \
             mock.patch.object(backend, "journal_messages", side_effect=[[], backend.HealthError("no previous boot")]):
            backend.collect_status(self.event)
        self.assertFalse(self.event.exists())

    def test_motd_handles_late_journal_delivery_after_userspace_rollback(self):
        marker = "FALLBACK BOOT DETECTED! Default bootc deployment has been rolled back."
        self.assertTrue(backend.confirmed_fallback([], [" INFO greenboot > Rollback successful"], marker))
        self.assertFalse(backend.confirmed_fallback([], ["Rollback failed"], marker))

    def test_kernel_fallback_waits_for_confirmation_in_the_journal(self):
        marker = "FALLBACK BOOT DETECTED! Default bootc deployment has been rolled back."
        with mock.patch.object(backend, "service_state", return_value={"ActiveState": "active", "Result": "success"}), \
             mock.patch.object(backend, "journal_messages", side_effect=[[], [], ["Rollback to previous deployment completed successfully"]]), \
             mock.patch.object(backend.Path, "read_text", side_effect=[marker, "boot-b"]), \
             mock.patch.object(backend, "command", return_value=json.dumps({"status": {"booted": {"image": {"imageDigest": "sha256:restored"}}}})), \
             mock.patch.object(backend.time, "sleep") as sleep:
            backend.collect_status(self.event)
        sleep.assert_called_once_with(1)
        self.assertEqual(json.loads(self.event.read_text())["boot_id"], "boot-b")

    def test_notification_once_per_user_per_event_and_new_recovery_delivers_again(self):
        backend.atomic_json(self.event, self.record)
        send = mock.Mock()
        self.assertTrue(notifier.deliver(self.event, self.seen, send))
        self.assertFalse(notifier.deliver(self.event, self.seen, send))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.seen.stat().st_mode & 0o777, 0o600)
        backend.atomic_json(self.event, self.record | {"event_id": "rollback-boot-b"})
        self.assertTrue(notifier.deliver(self.event, self.seen, send))
        other_user = self.seen.with_name("other-user.json")
        self.assertTrue(notifier.deliver(self.event, other_user, send))

    def test_unavailable_notification_service_is_retried(self):
        backend.atomic_json(self.event, self.record)
        with self.assertRaises(OSError):
            notifier.deliver(self.event, self.seen, mock.Mock(side_effect=OSError("no bus")))
        self.assertFalse(self.seen.exists())
        self.assertTrue(notifier.deliver(self.event, self.seen, mock.Mock()))

    def test_corrupt_per_user_state_does_not_hide_recovery(self):
        backend.atomic_json(self.event, self.record)
        backend.atomic_json(self.seen, [])
        self.assertIsNotNone(notifier.pending_event(self.event, self.seen))

    def test_notification_uses_the_session_service_and_desktop_identity(self):
        from gi.repository import Gio
        with mock.patch.object(Gio, "bus_get_sync") as bus:
            notifier.send_notification(notifier.MESSAGE)
        args = bus.return_value.call_sync.call_args.args
        self.assertEqual(args[:4], ("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                                    "org.freedesktop.Notifications", "Notify"))
        values = args[4].unpack()
        self.assertEqual(values[4], notifier.MESSAGE)
        self.assertEqual(values[6]["desktop-entry"], "org.mertensia.BootHealth")
        self.assertEqual(args[-2], 5000)

    def test_setup_account_never_connects_to_the_notification_service(self):
        with mock.patch.object(notifier.pwd, "getpwuid") as lookup, \
             mock.patch.object(notifier, "send_notification") as send:
            lookup.return_value.pw_name = "mertensia-setup"
            self.assertEqual(notifier.main(), 0)
        send.assert_not_called()

    def test_absent_and_malformed_events_are_quiet(self):
        self.assertIsNone(notifier.pending_event(self.event, self.seen))
        for event in ({}, [], {"version": 1, "event_id": 42}, {"version": 2}, self.record | {"event_id": ""}):
            backend.atomic_json(self.event, event)
            self.assertIsNone(notifier.pending_event(self.event, self.seen))

    def test_atomic_write_preserves_previous_event_on_failed_replace(self):
        backend.atomic_json(self.event, self.record)
        with mock.patch.object(backend.os, "replace", side_effect=OSError("failure")):
            with self.assertRaises(OSError):
                backend.atomic_json(self.event, {"new": "event"})
        self.assertEqual(json.loads(self.event.read_text()), self.record)
        self.assertEqual(list(self.event.parent.iterdir()), [self.event])


if __name__ == "__main__":
    unittest.main()
