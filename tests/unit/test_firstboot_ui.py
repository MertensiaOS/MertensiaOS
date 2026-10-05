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
