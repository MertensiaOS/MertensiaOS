from types import SimpleNamespace
import unittest
from unittest import mock


class FirstbootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import mertensia.firstboot.ui as ui
            cls.ui = ui
        except (ImportError, ValueError) as error:
            raise unittest.SkipTest(f"GTK/Adwaita unavailable: {error}")

    def values(self, **changes):
        return dict(real_name="Alex Morgan", username="alex", password="sample password", confirm="sample password") | changes

    def test_valid_account(self):
        self.assertIsNone(self.ui.account_error(self.values()))

    def test_password_limit_counts_utf8_bytes(self):
        for password, accepted in (("a" * 256, True), ("a" * 257, False),
                                   ("é" * 128, True), ("é" * 129, False),
                                   ("a" * 8 + "\ud800", False)):
            with self.subTest(password=repr(password)):
                error = self.ui.account_error(self.values(password=password, confirm=password))
                if accepted:
                    self.assertIsNone(error)
                else:
                    self.assertEqual(error[0], "password")

    def test_selected_regional_identifiers_are_preserved(self):
        values = {"locale": "fr_FR.utf8", "timezone": "Europe/Paris", "keymap": "fr"}
        window = SimpleNamespace(selectors=dict.fromkeys(values), selected_regions={
            key: self.ui.RegionalChoice(value, "Friendly name", "Native name")
            for key, value in values.items()
        })
        self.assertEqual(self.ui.SetupWindow._regional_values(window), values)

    def test_blank_or_invalid_device_name_cannot_advance(self):
        for hostname in ("", " ", "my laptop", "-laptop"):
            window = SimpleNamespace(entries={"hostname": mock.Mock(get_text=lambda: hostname)},
                                     device_message=mock.Mock(), stack=mock.Mock())
            self.ui.SetupWindow._next(window)
            window.stack.set_visible_child_name.assert_not_called()
            window.entries["hostname"].grab_focus.assert_called_once()

    def test_loading_or_failed_regions_cannot_advance_with_retained_values(self):
        window = SimpleNamespace(entries={"hostname": mock.Mock(get_text=lambda: "my-laptop")},
                                 continue_button=mock.Mock(get_sensitive=lambda: False),
                                 device_message=mock.Mock(), stack=mock.Mock())
        self.ui.SetupWindow._next(window)
        window.stack.set_visible_child_name.assert_not_called()
        window.device_message.set_visible.assert_called_once_with(True)

    def test_validation_identifies_the_field_to_correct(self):
        for key, value in (("real_name", " "), ("username", "mertensia-setup"),
                           ("username", "Alex Smith"), ("password", "short"),
                           ("password", "sample\npassword"), ("confirm", "different")):
            with self.subTest(key=key, value=value):
                self.assertEqual(self.ui.account_error(self.values(**{key: value}))[0], key)

    def test_duplicate_submission_is_ignored(self):
        window = SimpleNamespace(busy=True)
        with mock.patch.object(self.ui.threading, "Thread") as thread:
            self.ui.SetupWindow._submit(window)
        thread.assert_not_called()

    def test_firstboot_close_keeps_setup_accessible_and_blocks_busy_exit(self):
        for busy in (False, True):
            with self.subTest(busy=busy):
                window = SimpleNamespace(firstboot=True, busy=busy, _close_dialog=None,
                                         _close_response=mock.Mock())
                with mock.patch.object(self.ui.Adw, "AlertDialog") as alert:
                    self.assertTrue(self.ui.SetupWindow._close_requested(window))
                responses = [call.args[0] for call in alert.return_value.add_response.call_args_list]
                self.assertEqual(responses, ["return"] if busy else ["return", "restart", "shutdown"])
                alert.return_value.set_close_response.assert_called_once_with("return")
                alert.return_value.set_default_response.assert_called_once_with("return")
                alert.return_value.present.assert_called_once_with(window)
                with mock.patch.object(self.ui.Adw, "AlertDialog") as repeated:
                    self.assertTrue(self.ui.SetupWindow._close_requested(window))
                    repeated.assert_not_called()

    def test_add_user_window_can_close_unless_creating_an_account(self):
        for busy in (False, True):
            window = SimpleNamespace(firstboot=False, busy=busy)
            self.assertEqual(self.ui.SetupWindow._close_requested(window), busy)

    def test_firstboot_power_choices_recheck_account_creation(self):
        for busy in (False, True):
            for response in ("return", "restart", "shutdown"):
                with self.subTest(busy=busy, response=response):
                    window = SimpleNamespace(busy=busy, _close_dialog=mock.Mock())
                    with mock.patch.object(self.ui.subprocess, "Popen") as launch:
                        self.ui.SetupWindow._close_response(window, None, response)
                    if busy or response == "return":
                        launch.assert_not_called()
                    else:
                        launch.assert_called_once_with(["systemctl", "reboot" if response == "restart" else "poweroff"])
                    self.assertIsNone(window._close_dialog)

    def test_invalid_input_does_not_launch_helper(self):
        window = SimpleNamespace(busy=False, entries={key: mock.Mock(get_text=lambda value=value: value)
                                 for key, value in self.values(confirm="wrong").items()}, message=mock.Mock())
        with mock.patch.object(self.ui.threading, "Thread") as thread:
            self.ui.SetupWindow._submit(window)
        thread.assert_not_called()
        window.entries["confirm"].grab_focus.assert_called_once()
        window.message.set_visible.assert_called_once_with(True)

    def test_failure_returns_to_editable_form_without_losing_password(self):
        window = SimpleNamespace(busy=True, spinner=mock.Mock(), message=mock.Mock(),
                                 submit=mock.Mock(), stack=mock.Mock(), entries={"password": mock.Mock(), "confirm": mock.Mock()})
        self.ui.SetupWindow._done(window, {"event": "error", "message": "Try again"})
        self.assertFalse(window.busy)
        window.submit.set_sensitive.assert_called_once_with(True)
        window.stack.set_visible_child_name.assert_called_once_with("account")
        window.entries["password"].set_text.assert_not_called()

    def test_success_clears_both_password_fields(self):
        window = SimpleNamespace(busy=True, spinner=mock.Mock(), stack=mock.Mock(),
                                 entries={"password": mock.Mock(), "confirm": mock.Mock()})
        self.ui.SetupWindow._done(window, {"event": "complete"})
        self.assertFalse(window.busy)
        for entry in window.entries.values():
            entry.set_text.assert_called_once_with("")
        window.stack.set_visible_child_name.assert_called_once_with("complete")

    def test_committed_setup_warning_is_shown_without_offering_account_retry(self):
        window = SimpleNamespace(busy=True, spinner=mock.Mock(), stack=mock.Mock(),
                                 completion_message=mock.Mock(), submit=mock.Mock(),
                                 entries={"password": mock.Mock(), "confirm": mock.Mock()})
        warning = "The sign-in screen could not open automatically. Restart this computer to sign in."
        self.ui.SetupWindow._done(window, {"event": "complete", "ok": True, "warning": warning})
        window.completion_message.set_text.assert_called_once_with(warning)
        window.stack.set_visible_child_name.assert_called_once_with("complete")
        window.submit.set_sensitive.assert_not_called()
        for entry in window.entries.values():
            entry.set_text.assert_called_once_with("")


if __name__ == "__main__":
    unittest.main()
