import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


class FirstbootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("firstboot_ui", Path(__file__).parents[1] / "system/mertensia_firstboot.py")
        cls.ui = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(cls.ui)
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

    def test_regional_options_use_system_lists_and_exclude_nonregional_locales(self):
        outputs = ["C\nC.utf8\nPOSIX\nen_US.utf8\nfr_FR.utf8\nen_US.utf8\n",
                   "us\nde\n", "UTC\nPacific/Auckland\n"]
        with mock.patch.object(self.ui.subprocess, "run", side_effect=[
                SimpleNamespace(stdout=output) for output in outputs]) as run:
            options = self.ui.regional_options()
        self.assertEqual(options["locale"], ["en_US.utf8", "fr_FR.utf8"])
        self.assertEqual(options["timezone"], ["Pacific/Auckland", "UTC"])
        self.assertEqual(options["keymap"], ["de", "us"])
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [["localectl", "list-locales"], ["localectl", "list-keymaps"],
                          ["timedatectl", "list-timezones"]])

    def test_missing_options_are_reported_instead_of_inventing_values(self):
        with mock.patch.object(self.ui.subprocess, "run", return_value=SimpleNamespace(stdout="C\nPOSIX\n")):
            with self.assertRaises(ValueError):
                self.ui.regional_options()

    def test_locale_default_matches_system_encoding_spelling(self):
        self.assertEqual(self.ui.preferred_option(["de_DE.utf8", "en_US.utf8"], "en_US.UTF-8"), 1)

    def test_selected_regional_identifiers_are_preserved(self):
        values = {"locale": "fr_FR.utf8", "timezone": "Europe/Paris", "keymap": "fr"}
        window = SimpleNamespace(selectors=dict.fromkeys(values), selected_regions={
            key: self.ui.RegionalChoice(value, "Friendly name", "Native name")
            for key, value in values.items()
        })
        self.assertEqual(self.ui.SetupWindow._regional_values(window), values)

    def test_names_do_not_change_or_drop_system_identifiers(self):
        def names(standard):
            return {"fr": "French"} if standard == "639-3" else {"FR": "France"}
        with mock.patch.object(self.ui, "iso_names", side_effect=names), \
             mock.patch.object(self.ui, "native_language", return_value="Français"):
            choices = self.ui.regional_choices("locale", ["fr_FR.utf8", "fr_FR.ISO-8859-1"])
        self.assertEqual({c.value for c in choices}, {"fr_FR.utf8", "fr_FR.ISO-8859-1"})
        self.assertEqual(choices[0].title, "French (France)")
        self.assertIn("Français", choices[0].subtitle)
        self.assertNotEqual(choices[0].subtitle, choices[1].subtitle)

    def test_missing_metadata_keeps_options_usable(self):
        with mock.patch.object(self.ui, "keyboard_names", return_value={}), \
             mock.patch.object(self.ui, "iso_names", return_value={}):
            self.assertEqual(self.ui.regional_choices("keymap", ["unusual-layout"])[0].value, "unusual-layout")
            self.assertEqual(self.ui.regional_choices("locale", ["zz_ZZ.utf8"])[0].value, "zz_ZZ.utf8")

    def test_timezone_preview_handles_dst_and_fractional_offsets(self):
        for value, date, expected in (
            ("Pacific/Auckland", "2026-01-01T00:00:00", "UTC+13:00"),
            ("Pacific/Auckland", "2026-07-01T00:00:00", "UTC+12:00"),
            ("Asia/Kathmandu", "2026-01-01T00:00:00", "UTC+05:45"),
            ("America/St_Johns", "2026-01-01T00:00:00", "UTC−03:30"),
            ("UTC", "2026-01-01T00:00:00", "UTC+00:00"),
        ):
            with self.subTest(value=value, date=date):
                now = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
                self.assertIn(expected, self.ui.timezone_preview(value, now))
        self.assertEqual(self.ui.timezone_preview("missing/zone"), "missing/zone")

    def test_search_handles_accents_and_identifiers(self):
        self.assertEqual(self.ui.search_text("Français"), self.ui.search_text("francais"))
        self.assertEqual(self.ui.search_text("São_Paulo"), self.ui.search_text("sao paulo"))
        self.assertEqual(self.ui.search_text("en_US.UTF-8"), "en us utf 8")

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
