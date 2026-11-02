"""Compile and exercise the code linked into the downstream GNOME Settings RPM."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GnomeAccountsRedirectTests(unittest.TestCase):
    def test_native_routes_and_unprivileged_launcher(self):
        flags = shlex.split(subprocess.check_output(
            ["pkg-config", "--cflags", "--libs", "libadwaita-1"], text=True))
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "accounts-redirect"
            result = subprocess.run(
                ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                 "-I" + str(ROOT / "system/gnome"),
                 str(ROOT / "system/gnome/cc-mertensia-accounts.c"),
                 str(ROOT / "tests/native/gnome-accounts-redirect.c"),
                 "-Wl,--wrap=g_spawn_async", "-o", str(binary), *flags],
                text=True, capture_output=True, env=dict(os.environ, CCACHE_DISABLE="1"),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
