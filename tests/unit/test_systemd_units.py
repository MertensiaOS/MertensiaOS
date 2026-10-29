"""Verify boot ordering transactions without starting any services."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


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
        result = self.verify((ROOT / "system/config/systemd/mertensia-login-users.path").read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ordering cycle", (result.stdout + result.stderr).lower())

    def test_ordering_watcher_after_homed_reproduces_the_boot_cycle(self):
        watcher = (ROOT / "system/config/systemd/mertensia-login-users.path").read_text()
        watcher = watcher.replace("[Unit]\n", "[Unit]\nAfter=systemd-homed.service\n", 1)
        result = self.verify(watcher)
        # verify can return success after resolving a cycle by deleting a
        # wanted job. That was the live ISO's skipped paths.target failure.
        self.assertIn("ordering cycle", (result.stdout + result.stderr).lower())
        # The job selected to break the cycle varies with systemd versions;
        # both paths.target and homed have been chosen on Fedora hosts.
        self.assertRegex(result.stdout + result.stderr, r"Job \S+/start deleted to break ordering cycle")


class GreenbootOrderingTests(unittest.TestCase):
    def test_graphical_health_check_and_status_collection_have_no_boot_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            units = root / "usr/lib/systemd/system"
            units.mkdir(parents=True)
            fixtures = {
                "sysinit.target": "[Unit]\nDefaultDependencies=no\n",
                "shutdown.target": "[Unit]\nDefaultDependencies=no\n",
                "basic.target": "[Unit]\nRequires=sysinit.target\nAfter=sysinit.target\n",
                "multi-user.target": "[Unit]\nRequires=basic.target\nAfter=basic.target\nWants=timers.target\n",
                "timers.target": "[Unit]\nWants=bootc-fetch-apply-updates.timer\n",
                "graphical.target": "[Unit]\nRequires=multi-user.target\nAfter=multi-user.target\nWants=gdm.service greenboot-healthcheck.service\n",
                "boot-complete.target": "[Unit]\n",
                "greenboot-success.target": "[Unit]\nAfter=greenboot-healthcheck.service\nRequires=boot-complete.target\nWants=mertensia-boot-health-status.service mertensia-update-status.service\n",
                "bootc-status-updated.target": "[Unit]\nStopWhenUnneeded=true\nConditionPathExists=/run/ostree-booted\n",
                "bootc-fetch-apply-updates.timer": "[Timer]\nOnBootSec=1h\nOnUnitInactiveSec=8h\nRandomizedDelaySec=2h\n",
                "bootc-fetch-apply-updates.service": "[Service]\nType=oneshot\nExecStart=/usr/bin/true\n",
                "network-online.target": "[Unit]\n",
                "greenboot-healthcheck.service": (
                    "[Unit]\nBefore=boot-complete.target\nWants=greenboot-success.target\n"
                    "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/true\n"
                    "[Install]\nRequiredBy=greenboot-success.target\nWantedBy=multi-user.target\nAlso=greenboot-set-rollback-trigger.service\n"
                ),
                "greenboot-set-rollback-trigger.service": (
                    "[Unit]\nDefaultDependencies=no\nAfter=ostree-finalize-staged.service\n"
                    "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStop=/usr/bin/true\n"
                    "[Install]\nRequiredBy=ostree-finalize-staged.service\n"
                ),
                "ostree-finalize-staged.service": "[Service]\nType=oneshot\nExecStart=/usr/bin/true\n",
                # Exercise a display manager that starts only after multi-user.
                "gdm.service": "[Unit]\nAfter=multi-user.target mertensia-login-users.service\n[Service]\nExecStart=/usr/bin/true\n",
                "systemd-homed.service": "[Service]\nExecStart=/usr/bin/true\n",
                "accounts-daemon.service": "[Service]\nExecStart=/usr/bin/true\n",
                "mertensia-login-users.service": "[Unit]\nAfter=systemd-homed.service accounts-daemon.service\nBefore=gdm.service\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n",
            }
            for name, source in fixtures.items():
                (units / name).write_text(source)
            for name in ("greenboot-healthcheck.service.d", "greenboot-set-rollback-trigger.service.d"):
                shutil.copytree(ROOT / "system/config/systemd" / name, units / name)
            source = (ROOT / "system/config/systemd/mertensia-boot-health-status.service").read_text()
            source = source.replace("ExecStart=/usr/libexec/mertensia-boot-health collect", "ExecStart=/usr/bin/true")
            (units / "mertensia-boot-health-status.service").write_text(source)
            source = (ROOT / "system/config/systemd/mertensia-update-status.service").read_text()
            (units / "mertensia-update-status.service").write_text(source.replace("ExecStart=/usr/bin/mertensia-update status", "ExecStart=/usr/bin/true"))
            for name in ("bootc-fetch-apply-updates.service.d", "bootc-fetch-apply-updates.timer.d"):
                shutil.copytree(ROOT / "system/config/systemd" / name, units / name)
            override = units / "bootc-fetch-apply-updates.service.d/50-mertensia-stage.conf"
            override.write_text(override.read_text().replace("ExecStart=/usr/bin/mertensia-update stage", "ExecStart=/usr/bin/true"))
            enabled = subprocess.run(["systemctl", "--root", directory, "enable", "greenboot-healthcheck.service",
                                      "mertensia-boot-health-status.service", "mertensia-update-status.service"],
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(enabled.returncode, 0, enabled.stderr)
            installed = root / "etc/systemd/system"
            self.assertTrue((installed / "graphical.target.wants/greenboot-healthcheck.service").is_symlink())
            self.assertFalse((installed / "multi-user.target.wants/greenboot-healthcheck.service").is_symlink())
            self.assertTrue((installed / "ostree-finalize-staged.service.requires/greenboot-set-rollback-trigger.service").is_symlink())
            for target in ("greenboot-success.target", "bootc-status-updated.target"):
                link = installed / f"{target}.wants/mertensia-update-status.service"
                self.assertTrue(link.is_symlink())
                self.assertEqual(os.readlink(link), "/usr/lib/systemd/system/mertensia-update-status.service")
            # Both boot-time and bootc event transactions must resolve the
            # actual enablement symlinks, without introducing an ordering cycle.
            result = subprocess.run(["systemd-analyze", "verify", "--man=no", "--generators=no",
                                     str(units / "graphical.target"), str(units / "bootc-status-updated.target")],
                                    env=dict(os.environ, SYSTEMD_UNIT_PATH=f"{installed}:{units}"),
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("ordering cycle", (result.stdout + result.stderr).lower())


if __name__ == "__main__":
    unittest.main()
