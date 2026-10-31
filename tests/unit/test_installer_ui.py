import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock


class InstallerFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import mertensia.installer.ui as ui
            cls.ui = ui
        except (ImportError, ValueError) as error:
            raise unittest.SkipTest(f"GTK/Adwaita unavailable: {error}")

    def test_install_uses_the_confirmation_snapshot_even_if_selection_changes(self):
        disk = {"path": "/dev/test", "stable_path": "/dev/test", "model": "Original",
                "serial": "", "size": 40 * 1024**3, "disk_sequence": 1}
        confirmed = disk.copy()
        window = SimpleNamespace(
            _selected_disk=mock.Mock(return_value=disk), confirm_disk=mock.Mock(),
            erase_entry=mock.Mock(), stack=mock.Mock(), _read_events=mock.Mock(),
        )
        self.ui.InstallerWindow._show_confirmation(window)
        disk.update(model="Replacement", disk_sequence=2)
        window._selected_disk.return_value = {"stable_path": "/dev/other"}
        with mock.patch.object(self.ui.subprocess, "Popen") as launch, mock.patch.object(
            self.ui.threading, "Thread"
        ):
            self.ui.InstallerWindow._install(window)
        argv = launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--disk") + 1], confirmed["stable_path"])
        self.assertEqual(json.loads(argv[argv.index("--expected-disk") + 1]), confirmed)
        self.assertIn("Original", window.install_disk)

    def test_memory_warning_allows_installation_but_below_minimum_blocks_it(self):
        for allowed, recommended in ((False, False), (True, False), (True, True)):
            with self.subTest(allowed=allowed, recommended=recommended):
                icon, status = mock.Mock(), mock.Mock()
                window = SimpleNamespace(
                    requirement_rows={"memory_ok": (icon, status)},
                    requirements_heading=mock.Mock(), requirements_label=mock.Mock(),
                    requirements_next=mock.Mock(), _refresh_disks=mock.Mock(),
                )
                self.ui.InstallerWindow._probe_done(window, {
                    "event": "requirements", "ok": allowed, "memory_ok": allowed,
                    "memory_recommended": recommended, "memory_bytes": 2875476 * 1024,
                })
                window.requirements_next.set_sensitive.assert_called_once_with(allowed)
                self.assertEqual(window._refresh_disks.call_count, int(allowed))
                icon.set_from_icon_name.assert_called_once_with(
                    "mertensia-check-symbolic" if recommended else "mertensia-warning-symbolic"
                )
                self.assertIn("usable", status.set_text.call_args.args[0])
                if allowed and not recommended:
                    self.assertIn("4 GiB recommended", status.set_text.call_args.args[0])
                if not allowed:
                    self.assertIn("Add memory", window.requirements_label.set_text.call_args.args[0])

    def test_failure_is_persistent_and_preserves_original_details(self):
        window = SimpleNamespace(
            failure_report="", last_phase="Verifying unlock methods", install_disk="Test disk /dev/vda",
            failure_context=mock.Mock(), failure_steps=mock.Mock(), failure_details=mock.Mock(),
            recovery_continue=mock.Mock(), stack=mock.Mock(),
        )
        show = self.ui.InstallerWindow._show_failure
        show(window, "Device mertensia-root is still in use")
        window.stack.set_visible_child_name.assert_called_once_with("failure")
        window.recovery_continue.set_sensitive.assert_called_once_with(False)
        self.assertIn("Restart into the installation media", window.failure_steps.set_text.call_args.args[0])
        self.assertIn("erase the selected disk again", window.failure_steps.set_text.call_args.args[0])
        self.assertIn("Verifying unlock methods", window.failure_report)
        self.assertIn("/dev/vda", window.failure_report)
        show(window, "The installer stopped unexpectedly")
        window.failure_details.set_text.assert_called_once_with("Device mertensia-root is still in use")

    def test_close_requests_keep_window_open_and_block_exit_during_installation(self):
        for running in (True, False):
            with self.subTest(running=running):
                window = SimpleNamespace(
                    process=mock.Mock(), _close_dialog=None, _close_response=mock.Mock(),
                )
                window.process.poll.return_value = None if running else 0
                with mock.patch.object(self.ui.Adw, "AlertDialog") as alert:
                    self.assertTrue(self.ui.InstallerWindow._close_requested(window))
                dialog = alert.return_value
                responses = [call.args[0] for call in dialog.add_response.call_args_list]
                self.assertEqual(responses, ["return"] if running else ["return", "restart", "shutdown"])
                dialog.set_default_response.assert_called_once_with("return")
                dialog.set_close_response.assert_called_once_with("return")
                dialog.present.assert_called_once_with(window)
                # Repeated Alt+F4 must not stack dialogs or destroy the window.
                with mock.patch.object(self.ui.Adw, "AlertDialog") as repeated:
                    self.assertTrue(self.ui.InstallerWindow._close_requested(window))
                    repeated.assert_not_called()

    def test_close_before_installation_offers_a_way_back(self):
        window = SimpleNamespace(process=None, _close_dialog=None, _close_response=mock.Mock())
        with mock.patch.object(self.ui.Adw, "AlertDialog") as alert:
            self.assertTrue(self.ui.InstallerWindow._close_requested(window))
        alert.return_value.add_response.assert_any_call("return", "Return to installer")

    def test_exit_choices_recheck_helper_and_only_explicit_choices_power_off(self):
        for status in (None, 0):
            for response in ("return", "restart", "shutdown"):
                with self.subTest(status=status, response=response):
                    window = SimpleNamespace(process=mock.Mock(), _close_dialog=mock.Mock())
                    window.process.poll.return_value = status
                    with mock.patch.object(self.ui.subprocess, "Popen") as launch:
                        self.ui.InstallerWindow._close_response(window, None, response)
                    if status is None or response == "return":
                        launch.assert_not_called()
                    else:
                        launch.assert_called_once_with(["systemctl", "reboot" if response == "restart" else "poweroff"])
                    self.assertIsNone(window._close_dialog)

    def test_recovery_continue_tracks_manual_confirmation(self):
        window = SimpleNamespace(recovery_continue=mock.Mock())
        for confirmed in (False, True, False):
            with self.subTest(confirmed=confirmed):
                self.ui.InstallerWindow._recovery_toggled(
                    window, SimpleNamespace(get_active=lambda: confirmed)
                )
                window.recovery_continue.set_sensitive.assert_called_with(confirmed)

    def test_broken_confirmation_pipe_shows_failure_and_keeps_key(self):
        for error in (BrokenPipeError("closed"), OSError("write failed"), ValueError("closed stream")):
            with self.subTest(error=error):
                window = SimpleNamespace(
                    process=SimpleNamespace(stdin=mock.Mock()), recovery_key="saved-key",
                    stack=mock.Mock(), progress_label=mock.Mock(), _show_failure=mock.Mock(),
                )
                window.process.stdin.write.side_effect = error
                self.ui.InstallerWindow._confirm_recovery(window)
                window._show_failure.assert_called_once()
                self.assertIn("Recovery state", window._show_failure.call_args.args[0])
                self.assertEqual(window.recovery_key, "saved-key")

    def test_retained_recovery_failure_tells_user_to_keep_live_session(self):
        window = SimpleNamespace(
            failure_report="", last_phase="Verifying unlock methods", install_disk="Test disk /dev/vda",
            failure_context=mock.Mock(), failure_steps=mock.Mock(), failure_details=mock.Mock(),
            recovery_continue=mock.Mock(), stack=mock.Mock(),
        )
        self.ui.InstallerWindow._show_failure(window, "TPM error. Recovery state retained only in this live session.")
        advice = window.failure_steps.set_text.call_args.args[0]
        self.assertIn("Keep this computer running", advice)
        self.assertNotIn("Restart into the installation media", advice)

    def test_unused_prepared_state_allows_restart_guidance(self):
        window = SimpleNamespace(
            failure_report="", last_phase="Encrypting the system volume", install_disk="Test disk /dev/vda",
            failure_context=mock.Mock(), failure_steps=mock.Mock(), failure_details=mock.Mock(),
            recovery_continue=mock.Mock(), stack=mock.Mock(),
        )
        self.ui.InstallerWindow._show_failure(window, "No encrypted volume was created. Unused recovery state can be released before retrying.")
        advice = window.failure_steps.set_text.call_args.args[0]
        self.assertNotIn("Keep this computer running", advice)

    def test_process_exit_does_not_replace_backend_error(self):
        window = SimpleNamespace(
            process=SimpleNamespace(stdout=io.StringIO('{"event":"error","message":"TPM failed"}\n'),
                                    wait=lambda: 1),
            _event=mock.Mock(),
        )
        with mock.patch.object(self.ui.GLib, "idle_add") as schedule:
            self.ui.InstallerWindow._read_events(window)
        schedule.assert_called_once_with(window._event, {"event": "error", "message": "TPM failed"})

    def test_exit_without_completion_is_an_error_even_after_recovery(self):
        for code in (0, 1):
            with self.subTest(code=code):
                window = SimpleNamespace(
                    process=SimpleNamespace(stdout=io.StringIO('{"event":"recovery-key","key":"secret"}\n'),
                                            wait=lambda: code),
                    _event=mock.Mock(),
                )
                with mock.patch.object(self.ui.GLib, "idle_add") as schedule:
                    self.ui.InstallerWindow._read_events(window)
                error = schedule.call_args.args[1]
                self.assertEqual(error["event"], "error")
                self.assertIn(f"exit {code}", error["message"])
                self.assertNotIn("secret", error["message"])

    def test_completed_process_does_not_report_a_spurious_error(self):
        window = SimpleNamespace(
            process=SimpleNamespace(
                stdout=io.StringIO('{"event":"complete","message":"Installed"}\n'),
                wait=lambda: 0,
            ),
            _event=mock.Mock(),
        )
        with mock.patch.object(self.ui.GLib, "idle_add") as schedule:
            self.ui.InstallerWindow._read_events(window)
        schedule.assert_called_once_with(
            window._event, {"event": "complete", "message": "Installed"}
        )


if __name__ == "__main__":
    unittest.main()
