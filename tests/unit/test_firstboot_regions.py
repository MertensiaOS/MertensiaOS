from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest import mock

from mertensia.firstboot import regions


class RegionalChoicesTests(unittest.TestCase):
    def test_regional_options_use_system_lists_and_exclude_nonregional_locales(self):
        outputs = ["C\nC.utf8\nPOSIX\nen_US.utf8\nfr_FR.utf8\nen_US.utf8\n",
                   "us\nde\n", "UTC\nPacific/Auckland\n"]
        with mock.patch.object(regions.subprocess, "run", side_effect=[
                SimpleNamespace(stdout=output) for output in outputs]) as run:
            options = regions.regional_options()
        self.assertEqual(options["locale"], ["en_US.utf8", "fr_FR.utf8"])
        self.assertEqual(options["timezone"], ["Pacific/Auckland", "UTC"])
        self.assertEqual(options["keymap"], ["de", "us"])
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [["localectl", "list-locales"], ["localectl", "list-keymaps"],
                          ["timedatectl", "list-timezones"]])

    def test_missing_options_are_reported_instead_of_inventing_values(self):
        with mock.patch.object(regions.subprocess, "run", return_value=SimpleNamespace(stdout="C\nPOSIX\n")):
            with self.assertRaises(ValueError):
                regions.regional_options()

    def test_locale_default_matches_system_encoding_spelling(self):
        self.assertEqual(regions.preferred_option(["de_DE.utf8", "en_US.utf8"], "en_US.UTF-8"), 1)

    def test_names_do_not_change_or_drop_system_identifiers(self):
        def names(standard):
            return {"fr": "French"} if standard == "639-3" else {"FR": "France"}
        with mock.patch.object(regions, "iso_names", side_effect=names), \
             mock.patch.object(regions, "native_language", return_value="Français"):
            choices = regions.regional_choices("locale", ["fr_FR.utf8", "fr_FR.ISO-8859-1"])
        self.assertEqual({c.value for c in choices}, {"fr_FR.utf8", "fr_FR.ISO-8859-1"})
        self.assertEqual(choices[0].title, "French (France)")
        self.assertIn("Français", choices[0].subtitle)
        self.assertNotEqual(choices[0].subtitle, choices[1].subtitle)

    def test_missing_metadata_keeps_options_usable(self):
        with mock.patch.object(regions, "keyboard_names", return_value={}), \
             mock.patch.object(regions, "iso_names", return_value={}):
            self.assertEqual(regions.regional_choices("keymap", ["unusual-layout"])[0].value, "unusual-layout")
            self.assertEqual(regions.regional_choices("locale", ["zz_ZZ.utf8"])[0].value, "zz_ZZ.utf8")

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
                self.assertIn(expected, regions.timezone_preview(value, now))
        self.assertEqual(regions.timezone_preview("missing/zone"), "missing/zone")

    def test_search_handles_accents_and_identifiers(self):
        self.assertEqual(regions.search_text("Français"), regions.search_text("francais"))
        self.assertEqual(regions.search_text("São_Paulo"), regions.search_text("sao paulo"))
        self.assertEqual(regions.search_text("en_US.UTF-8"), "en us utf 8")


if __name__ == "__main__":
    unittest.main()
