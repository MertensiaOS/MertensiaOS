"""Exercise the application files assembled by the image COPY instructions."""

import configparser
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = "/usr/lib/mertensia/python"
SPEC = importlib.util.spec_from_file_location("install_image_files", ROOT / "scripts/install-image-files.py")
image_files = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(image_files)


class ApplicationPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.image = Path(self.temporary.name) / "image"
        self.image.mkdir()
        self.stage("Containerfile")

    def stage(self, filename):
        # Follow scratch staging and final COPY --from instructions. RPM outputs
        # are built externally, but all source application/config files are real.
        stages = {}
        image = self.image
        for line in (ROOT / filename).read_text().splitlines():
            if line.startswith("FROM "):
                fields = shlex.split(line)
                if "AS" in fields:
                    name = fields[fields.index("AS") + 1]
                    image = Path(self.temporary.name) / (filename + "-" + name)
                    image.mkdir(exist_ok=True)
                    stages[name] = image
                else:
                    image = self.image
                continue
            if line.startswith("RUN --mount=type=bind,from=") and "-files,target=" in line:
                role = "payload" if filename == "Containerfile" else "installer"
                image_files.install(ROOT / f"scripts/{role}-files.json", stages[f"{role}-files"], image)
            if not line.startswith("COPY "):
                continue
            fields = shlex.split(line)
            if fields[1].startswith("--from="):
                name = fields.pop(1).split("=", 1)[1]
                if name == "accountsservice-build":
                    continue  # Compiled RPMs, not source application files.
                source_root = stages[name]
            else:
                source_root = ROOT
            for source_name in fields[1:-1]:
                source = source_root / source_name.lstrip("/")
                target = image / fields[-1].lstrip("/")
                if len(fields) > 3:
                    target /= source.name
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
        self.assertFalse((self.image / "build").exists())
        self.assertFalse((self.image / "configure-payload.sh").exists())
        result = self.launcher("/usr/bin/mertensia-firstboot", "--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--add-user", result.stdout)
        result = self.launcher("/usr/libexec/mertensia-boot-health", "--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("collect", result.stdout)
        result = self.launcher("/usr/bin/mertensia-update", "--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("resume", result.stdout)
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
            "usr/lib/systemd/system/bootc-fetch-apply-updates.service.d/50-mertensia-stage.conf",
            "usr/lib/systemd/system/bootc-fetch-apply-updates.timer.d/50-mertensia-daily.conf",
            "usr/lib/systemd/system/mertensia-update-status.service",
            "usr/lib/tmpfiles.d/mertensia-updates.conf",
            "usr/share/glib-2.0/schemas/zz-mertensia-software.gschema.override",
            "usr/lib/systemd/user/gnome-software.service.d/50-mertensia-session.conf",
            "usr/lib/systemd/system/mertensia-boot-health-status.service",
            "usr/lib/systemd/system/greenboot-healthcheck.service.d/50-mertensia-desktop.conf",
            "usr/lib/systemd/system/greenboot-set-rollback-trigger.service.d/50-mertensia-installed.conf",
            "usr/lib/greenboot/check/required.d/50-mertensia-desktop.sh",
            "usr/lib/systemd/journald.conf.d/50-mertensia-boot-health.conf",
            "usr/lib/tmpfiles.d/mertensia-boot-health.conf",
            "etc/greenboot/greenboot.conf",
            "etc/xdg/autostart/org.mertensia.BootHealth.desktop",
            "usr/share/applications/org.mertensia.BootHealth.desktop",
            "usr/share/flatpak/remotes.d/flathub.flatpakrepo",
            "usr/share/polkit-1/actions/org.mertensia.Accounts.policy",
            "etc/containers/policy.json",
            "usr/lib/os-release",
        ):
            with self.subTest(path=name):
                self.assertTrue((self.image / name).is_file())

    def test_native_splash_and_install_defaults_are_shared_with_the_live_image(self):
        theme = self.image / "usr/share/plymouth/themes/mertensia"
        config = configparser.ConfigParser()
        config.read(theme / "mertensia.plymouth")
        self.assertEqual(config["Plymouth Theme"]["ModuleName"], "two-step")
        self.assertEqual(config["two-step"]["ImageDir"], "/usr/share/plymouth/themes/mertensia")
        for filename in ("entry.png", "watermark.png"):
            self.assertEqual((theme / filename).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        daemon = self.image / "etc/plymouth/plymouthd.conf"
        config.read(daemon)
        self.assertEqual(config["Daemon"]["Theme"], "mertensia")
        defaults = self.image / "usr/lib/bootc/install/20-mertensia-plymouth.toml"
        self.assertEqual(tomllib.loads(defaults.read_text())["install"]["kargs"], ["quiet", "rhgb"])
        self.assertFalse((self.image / "usr/lib/bootc/kargs.d/20-mertensia-plymouth.toml").exists())
        before = {str(path.relative_to(theme)): path.read_bytes() for path in theme.iterdir()}
        self.stage("Containerfile.installer")
        self.assertEqual(before, {str(path.relative_to(theme)): path.read_bytes() for path in theme.iterdir()})
        self.assertTrue((self.image / "usr/lib/dracut/dracut.conf.d/50-mertensia-plymouth.conf").is_file())
        self.assertTrue((self.image / "usr/libexec/mertensia-check-plymouth-initrd").is_file())

    def test_updates_are_staged_without_reboots_and_disabled_in_disposable_images(self):
        units = ROOT / "system/config/systemd"
        service = (units / "bootc-fetch-apply-updates.service.d/50-mertensia-stage.conf").read_text()
        self.assertIn("ExecStart=\nExecStart=/usr/bin/mertensia-update stage", service)
        self.assertNotIn("--apply", service)
        timer = (units / "bootc-fetch-apply-updates.timer.d/50-mertensia-daily.conf").read_text()
        for setting in ("OnBootSec=\n", "OnUnitInactiveSec=\n", "OnCalendar=daily", "Persistent=true", "RandomizedDelaySec=1h"):
            self.assertIn(setting, timer)
        for name in ("Containerfile.installer", "Containerfile.dev"):
            text = (ROOT / name).read_text()
            if name == "Containerfile.installer":
                text += (ROOT / "scripts/configure-installer.sh").read_text()
            self.assertIn("bootc-fetch-apply-updates.timer bootc-fetch-apply-updates.service mertensia-update-status.service", text)
        session = (units / "user/gnome-software.service.d/50-mertensia-session.conf").read_text()
        self.assertIn("WantedBy=gnome-session.target", session)
        self.assertIn("ConditionUser=!mertensia-setup", session)
        self.assertIn("systemctl --global mask gnome-software.service", (ROOT / "scripts/configure-installer.sh").read_text())

    def test_only_supported_installed_payload_enables_recovery(self):
        payload = (ROOT / "scripts/configure-payload.sh").read_text()
        self.assertIn("systemctl enable greenboot-healthcheck.service mertensia-boot-health-status.service", payload)
        self.assertIn("systemctl --global mask grub-boot-success.timer grub-boot-success.service", payload)
        self.assertIn("! rpm -q greenboot-default-health-checks", payload)
        for name in ("Containerfile.installer", "Containerfile.dev"):
            text = (ROOT / name).read_text()
            if name == "Containerfile.installer":
                text += (ROOT / "scripts/configure-installer.sh").read_text()
            self.assertIn("systemctl mask greenboot-healthcheck.service greenboot-set-rollback-trigger.service", text)
            self.assertIn("/etc/xdg/autostart/org.mertensia.BootHealth.desktop", text)
        self.stage("Containerfile.installer")
        for name in (
            "usr/lib/image-builder/bootc/iso.yaml",
            "usr/share/polkit-1/actions/org.mertensia.Installer.policy",
        ):
            with self.subTest(path=name):
                self.assertTrue((self.image / name).is_file())
        self.assertFalse((self.image / "etc/systemd/system/var-tmp.mount").exists())

    def test_chromium_policies_and_default_handlers(self):
        root = self.image / "etc/chromium/policies"
        self.assertEqual(json.loads((root / "managed/mertensia.json").read_text()), {
            "SitePerProcess": True, "MetricsReportingEnabled": False,
            "UrlKeyedAnonymizedDataCollectionEnabled": False,
        })
        self.assertEqual(json.loads((root / "recommended/mertensia.json").read_text()), {
            "SafeBrowsingProtectionLevel": 1, "BackgroundModeEnabled": False,
            "SearchSuggestEnabled": False, "NetworkPredictionOptions": 2,
            "SpellCheckServiceEnabled": False,
        })
        defaults = configparser.ConfigParser()
        defaults.read(self.image / "usr/share/applications/gnome-mimeapps.list")
        self.assertEqual(dict(defaults["Default Applications"]), {
            mime: "chromium-browser.desktop;" for mime in (
                "x-scheme-handler/http", "x-scheme-handler/https", "text/html", "application/xhtml+xml")
        })

    def test_chromium_wayland_hook_preserves_flags_and_headless_selection(self):
        hook = self.image / "etc/chromium/mertensia-wayland.conf"
        for display, expected in (("wayland-0", "--existing --ozone-platform=wayland"), ("", "--existing")):
            with self.subTest(display=display):
                result = subprocess.run(
                    ["bash", "-euc", 'CHROMIUM_FLAGS=--existing; . "$1"; printf "%s" "$CHROMIUM_FLAGS"', "test", str(hook)],
                    env=dict(os.environ, WAYLAND_DISPLAY=display), text=True, capture_output=True, check=True,
                )
                self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
