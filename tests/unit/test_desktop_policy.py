"""Guard dependency boundaries and the image's noninteractive DNF policy."""

import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("desktop_policy", ROOT / "scripts/clean-desktop.py")
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class DesktopPolicyTests(unittest.TestCase):
    def test_failed_dependency_preflight_never_invokes_dnf(self):
        with patch.object(policy, "inventory", return_value={"openssh-server", "bootc"}), \
                patch.object(policy.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "rpm")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                policy.clean()
        self.assertEqual(run.call_args_list[0].args[0], ["rpm", "-e", "--test", "openssh-server"])
        self.assertEqual(run.call_count, 1)

    def test_unexpected_dnf_cascade_fails_the_build(self):
        with patch.object(policy, "inventory", side_effect=[{"sssd-common", "bootc"}, set()]), \
                patch.object(policy.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "outside the reviewed"):
                policy.clean()
        self.assertEqual(run.call_args_list[1].args[0],
                         ["dnf", "-y", "--setopt=clean_requirements_on_remove=False", "remove", "sssd-common"])
        self.assertEqual(run.call_count, 2)

    def test_desktop_and_bootc_dependencies_are_outside_removal_families(self):
        for name in ("bootc", "podman", "skopeo", "openssh-clients", "gnome-software",
                     "dnf5daemon-server", "dnf5daemon-server-polkit", "NetworkManager",
                     "google-noto-fonts-all", "glibc-all-langpacks", "fwupd", "systemd-pam",
                     "nfs-client-utils", "rpcbind", "avahi"):
            with self.subTest(name=name):
                self.assertFalse(policy.excluded(name))

    def test_polkit_denies_dnf_actions_without_affecting_other_desktop_actions(self):
        # Execute the actual rule with GJS (SpiderMonkey), not text matching.
        rule = (ROOT / "system/config/polkit/00-mertensia-no-host-packages.rules").read_text()
        script = '''let callback;
const polkit = { Result: {NO: "no"}, addRule: function(rule) { callback = rule; } };
''' + rule + '''
for (let suffix of ["rpm.execute_trusted_transaction", "rpm.execute_transaction",
                    "rpm.Repo.confirm_key", "rpm.Repo.conf_write", "base.Config.override", "future"]) {
    for (let subject of [{active: true, local: true, isInGroup: () => true}, {}]) {
        if (callback({id: "org.rpm.dnf.v0." + suffix}, subject) !== "no") throw Error(suffix);
    }
}
for (let id of ["org.freedesktop.Flatpak.app-install", "org.freedesktop.fwupd.update-device",
                "org.mertensia.accounts", "org.freedesktop.home1.create-home"]) {
    if (callback({id}, {}) !== undefined) throw Error(id);
}
'''
        result = subprocess.run(["gjs", "-c", script], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
