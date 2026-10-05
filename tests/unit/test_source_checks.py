"""Executable scripts must be syntax checked even without filename extensions."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_source", ROOT / "scripts/check-source.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class SourceSyntaxTests(unittest.TestCase):
    def test_invalid_extensionless_shell_helpers_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "helper"
            for shebang in ("#!/usr/bin/bash", "#!/bin/sh", "#!/usr/bin/env bash"):
                with self.subTest(shebang=shebang):
                    helper.write_text(shebang + "\nif then\n")
                    with self.assertRaises(subprocess.CalledProcessError):
                        checker.check_file(helper)

    def test_invalid_isolated_python_launcher_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "helper"
            helper.write_text("#!/usr/bin/python3 -I\nif True\n")
            with self.assertRaises(SyntaxError):
                checker.check_file(helper)


if __name__ == "__main__":
    unittest.main()
