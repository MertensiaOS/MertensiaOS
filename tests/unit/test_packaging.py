"""Exercise the application files assembled by the image COPY instructions."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = "/usr/lib/mertensia/python"


class ApplicationPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.image = Path(self.temporary.name) / "image"
        self.image.mkdir()
        self.stage("Containerfile")

    def stage(self, filename):
        # The application/configuration COPYs have literal paths. RPM outputs
        # from the upstream build stage are outside this source-layout check.
        for line in (ROOT / filename).read_text().splitlines():
            if not line.startswith("COPY "):
                continue
            fields = shlex.split(line)
            if any(field.startswith("--from=") for field in fields):
                continue
            self.assertEqual(len(fields), 3, line)
            source = ROOT / fields[1]
            target = self.image / fields[2].lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)

    def launcher(self, installed_path, *arguments, input_text=None):
        executable = self.image / installed_path.lstrip("/")
        # Relocate only the image's absolute module root into this fixture;
        # execute the real shebang so Python's isolation flags are exercised.
        source = executable.read_text()
        self.assertTrue(source.startswith("#!/usr/bin/python3 -I\n"))
        executable.write_text(source.replace(MODULE_ROOT, str(self.image / MODULE_ROOT.lstrip("/"))))
        executable.chmod(0o755)
        poison = Path(self.temporary.name) / "poison"
        poison.mkdir(exist_ok=True)
        (poison / "mertensia.py").write_text('raise RuntimeError("untrusted module imported")\n')
        return subprocess.run(
            [str(executable), *arguments], input=input_text, text=True,
            capture_output=True, timeout=15, cwd=poison,
            env=dict(os.environ, PYTHONPATH=str(poison), PYTHONDONTWRITEBYTECODE="1"),
        )

    def test_payload_launchers_import_the_installed_packages_in_isolated_mode(self):
        result = self.launcher("/usr/bin/mertensia-firstboot", "--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--add-user", result.stdout)
        result = self.launcher("/usr/libexec/mertensia-accounts-helper", input_text="{}\n")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["event"], "error")
        self.assertNotIn("untrusted module", result.stderr)
        self.assertFalse((self.image / MODULE_ROOT.lstrip("/") / "mertensia/installer").exists())

    def test_installer_overlay_imports_its_helper_and_uses_shared_branding(self):
        self.stage("Containerfile.installer")
        result = self.launcher("/usr/libexec/mertensia-installer-helper", "--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("discard-prepared", result.stdout)
        self.assertTrue((self.image / "usr/share/mertensia/branding/installer.css").is_file())
        self.assertTrue((self.image / "usr/share/mertensia/branding/firstboot.css").is_file())
        self.assertFalse((self.image / "usr/share/mertensia-installer/branding").exists())

    def test_configuration_keeps_the_system_paths_used_by_services_and_policies(self):
        for name in (
            "usr/lib/tmpfiles.d/mertensia.conf",
            "usr/lib/tmpfiles.d/mertensia-firstboot.conf",
            "usr/lib/sysusers.d/mertensia-firstboot.conf",
            "usr/lib/systemd/system/mertensia-firstboot-retire.path",
            "usr/lib/systemd/system/mertensia-firstboot-retire.service",
            "usr/lib/systemd/system/mertensia-firstboot-finish.timer",
            "usr/lib/systemd/system/mertensia-firstboot-finish.service",
            "usr/lib/systemd/system/mertensia-login-users.path",
            "usr/lib/systemd/system/mertensia-login-users.service",
            "usr/share/polkit-1/actions/org.mertensia.Accounts.policy",
            "etc/containers/policy.json",
            "usr/lib/os-release",
        ):
            with self.subTest(path=name):
                self.assertTrue((self.image / name).is_file())
        self.stage("Containerfile.installer")
        for name in (
            "etc/systemd/system/var-tmp.mount",
            "usr/lib/image-builder/bootc/iso.yaml",
            "usr/share/polkit-1/actions/org.mertensia.Installer.policy",
        ):
            with self.subTest(path=name):
                self.assertTrue((self.image / name).is_file())


if __name__ == "__main__":
    unittest.main()
