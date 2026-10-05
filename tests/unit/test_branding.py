"""Check that both applications retain their theme and accessibility colors."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from mertensia.ui import branding


class BrandingTests(unittest.TestCase):
    def test_checkout_branding_is_shared(self):
        self.assertEqual(branding.BRANDING, Path(__file__).resolve().parents[2] / "branding")
        self.assertTrue((branding.BRANDING / "installer.css").is_file())
        self.assertTrue((branding.BRANDING / "firstboot.css").is_file())

    def test_both_themes_use_tokens_with_application_secondary_colors(self):
        themes = json.loads((branding.BRANDING / "brand-tokens.json").read_text())["themes"]
        for dark in (False, True):
            with self.subTest(dark=dark):
                theme = themes["dark" if dark else "light"]
                firstboot = branding.theme_colors(dark)
                installer = branding.theme_colors(
                    dark, muted="#D0C3DC" if dark else "#65586F",
                    border="#695777" if dark else "#C9BFD3",
                )
                for css in (firstboot, installer):
                    self.assertIn(f"@define-color window_bg_color {theme['background']};", css)
                    self.assertIn(f"@define-color accent_bg_color {theme['action']};", css)
                    self.assertIn(f"@define-color accent_fg_color {theme['onAction']};", css)
                self.assertIn("@define-color brand_muted @window_fg_color;", firstboot)
                self.assertIn("@define-color brand_border @window_fg_color;", firstboot)
                self.assertIn("#D0C3DC" if dark else "#65586F", installer)
                self.assertIn("#695777" if dark else "#C9BFD3", installer)

    def test_high_contrast_keeps_native_colors_without_reading_theme_tokens(self):
        with TemporaryDirectory() as directory, mock.patch.object(branding, "BRANDING", Path(directory)):
            for dark in (False, True):
                css = branding.theme_colors(dark, True, muted="#D0C3DC", border="#695777")
                self.assertEqual(css, "@define-color brand_muted @window_fg_color;\n"
                                      "@define-color brand_border @window_fg_color;")


if __name__ == "__main__":
    unittest.main()
