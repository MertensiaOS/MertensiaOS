"""Check native theme behavior and reproducible artwork, without starting Plymouth."""

import configparser
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

import cairo


ROOT = Path(__file__).resolve().parents[2]
THEME = ROOT / "branding/plymouth"
loader = importlib.machinery.SourceFileLoader(
    "check_plymouth", str(ROOT / "system/bin/mertensia-check-plymouth-initrd"))
spec = importlib.util.spec_from_loader(loader.name, loader)
checker = importlib.util.module_from_spec(spec)
loader.exec_module(checker)


class PlymouthTests(unittest.TestCase):
    def test_normal_boot_has_no_progress_bar_or_forced_message(self):
        config = configparser.ConfigParser()
        config.read(THEME / "mertensia.plymouth")
        palette = json.loads((ROOT / "branding/brand-tokens.json").read_text())["palette"]
        for key in ("BackgroundStartColor", "BackgroundEndColor"):
            self.assertEqual(int(config["two-step"][key], 16), int(palette["ink"][1:], 16))
        for mode in ("boot-up", "reboot", "shutdown"):
            with self.subTest(mode=mode):
                self.assertFalse(config.getboolean(mode, "UseEndAnimation"))
                self.assertFalse(config.getboolean(mode, "UseFirmwareBackground"))
                self.assertFalse(config.getboolean(mode, "UseProgressBar"))
                self.assertNotIn("Title", config[mode])
                self.assertFalse(config.getboolean(mode, "SuppressMessages", fallback=False))
        for mode in ("updates", "system-upgrade", "firmware-upgrade", "system-reset"):
            self.assertTrue(config.getboolean(mode, "UseProgressBar"))
            self.assertTrue(config.getboolean(mode, "ProgressBarShowPercentComplete"))
            self.assertFalse(config.getboolean(mode, "SuppressMessages", fallback=False))

    def test_assets_regenerate_identically_and_keep_transparency(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["/usr/bin/python3", str(ROOT / "scripts/generate-plymouth-assets.py"),
                            "--output", directory], check=True)
            for filename, size in (("watermark.png", (112, 112)), ("entry.png", (320, 44)),
                                   ("bullet.png", (10, 10)), ("lock.png", (35, 44))):
                with self.subTest(filename=filename):
                    content = (THEME / filename).read_bytes()
                    committed = cairo.ImageSurface.create_from_png(str(THEME / filename))
                    regenerated = cairo.ImageSurface.create_from_png(str(Path(directory) / filename))
                    self.assertEqual(bytes(committed.get_data()), bytes(regenerated.get_data()))
                    self.assertEqual(struct.unpack(">II", content[16:24]), size)
                    self.assertEqual(content[25], 6)  # RGBA, including the bloom's knockout.

    def test_initramfs_check_rejects_missing_plymouth_module(self):
        with mock.patch.object(checker.subprocess, "check_output", return_value="tpm2-tss\n"):
            with self.assertRaisesRegex(SystemExit, "missing dracut module plymouth"):
                checker.check(Path("test.img"))

    def test_initramfs_check_rejects_missing_graphics_and_prompt_assets(self):
        with mock.patch.object(checker.subprocess, "check_output", side_effect=["plymouth\ntpm2-tss\n", ""]):
            with self.assertRaisesRegex(SystemExit, "incomplete Plymouth initramfs") as raised:
                checker.check(Path("test.img"))
            self.assertIn("watermark.png", str(raised.exception))
            self.assertIn("label-pango.so", str(raised.exception))
            self.assertIn("Figtree-Variable.ttf", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
