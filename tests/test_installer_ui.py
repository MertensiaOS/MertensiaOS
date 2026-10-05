import importlib.util
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class InstallerFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parents[1] / "installer/mertensia_installer.py"
        spec = importlib.util.spec_from_file_location("installer_ui", path)
        cls.ui = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(cls.ui)
        except (ImportError, ValueError) as error:
            raise unittest.SkipTest(f"GTK/Adwaita unavailable: {error}")

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
