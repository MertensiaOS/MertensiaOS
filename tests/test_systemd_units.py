"""Verify boot ordering transactions without starting any services."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]


class LoginWatcherOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("systemd-analyze"):
            raise unittest.SkipTest("unit transaction tests require systemd-analyze")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.units = Path(self.temporary.name)
        # These retain the relevant real target relationships. systemd supplies
        # the implicit Before=paths.target on .path units and After=basic.target
        # on services, so this catches interactions that text checks cannot.
        fixtures = {
            "sysinit.target": "[Unit]\nDefaultDependencies=no\n",
            "shutdown.target": "[Unit]\nDefaultDependencies=no\n",
            "paths.target": "[Unit]\nDescription=Path units\n",
            "basic.target": "[Unit]\nRequires=sysinit.target\nWants=paths.target\nAfter=sysinit.target paths.target\n",
            "multi-user.target": (
                "[Unit]\nRequires=basic.target\n"
                "Wants=systemd-homed.service mertensia-login-users.path\nAfter=basic.target\n"
            ),
            "systemd-homed.service": "[Service]\nExecStart=/usr/bin/true\n",
            "mertensia-login-users.service": (
                "[Unit]\nWants=systemd-homed.service\nAfter=systemd-homed.service\n"
                "[Service]\nType=oneshot\nExecStart=/usr/bin/true\n"
            ),
        }
        for name, contents in fixtures.items():
            (self.units / name).write_text(contents)

    def verify(self, watcher):
        path = self.units / "mertensia-login-users.path"
        path.write_text(watcher)
        return subprocess.run(
            ["systemd-analyze", "verify", "--man=no", "--generators=no",
             str(self.units / "multi-user.target"), str(path)],
            env=dict(os.environ, SYSTEMD_UNIT_PATH=str(self.units)),
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_login_watcher_can_start_with_homed_without_a_boot_cycle(self):
        result = self.verify((ROOT / "system/mertensia-login-users.path").read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ordering cycle", (result.stdout + result.stderr).lower())

    def test_ordering_watcher_after_homed_reproduces_the_boot_cycle(self):
        watcher = (ROOT / "system/mertensia-login-users.path").read_text()
        watcher = watcher.replace("[Unit]\n", "[Unit]\nAfter=systemd-homed.service\n", 1)
        result = self.verify(watcher)
        # verify can return success after resolving a cycle by deleting a
        # wanted job. That was the live ISO's skipped paths.target failure.
        self.assertIn("ordering cycle", (result.stdout + result.stderr).lower())
        # The job selected to break the cycle varies with systemd versions;
        # both paths.target and homed have been chosen on Fedora hosts.
        self.assertRegex(result.stdout + result.stderr, r"Job \S+/start deleted to break ordering cycle")


if __name__ == "__main__":
    unittest.main()
