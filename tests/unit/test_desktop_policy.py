"""Guard dependency boundaries and the image's noninteractive DNF policy."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("desktop_policy", ROOT / "scripts/clean-desktop.py")
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)
CHECK_SPEC = importlib.util.spec_from_file_location("check_desktop_policy", ROOT / "scripts/check-desktop-policy.py")
checks = importlib.util.module_from_spec(CHECK_SPEC)
CHECK_SPEC.loader.exec_module(checks)


class DesktopPolicyTests(unittest.TestCase):
    def test_nss_group_merging_accepts_authselect_output_and_rejects_hidden_homed_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            nsswitch = Path(temporary) / "nsswitch.conf"
            for entry, valid in (
                ("group: files [SUCCESS=merge] systemd [SUCCESS=merge]\n", True),
                ("group:\tfiles [SUCCESS=merge] systemd\n", True),
                ("group:      files [SUCCESS=merge] altfiles [SUCCESS=merge] systemd [SUCCESS=merge]\n", True),
                ("group: files [SUCCESS=merge] altfiles [SUCCESS=merge] systemd # bootc\n", True),
                ("group: files [SUCCESS=merge] altfiles systemd\n", False),
                ("group: files altfiles [SUCCESS=merge] systemd\n", False),
                ("group: files [SUCCESS=merge] altfiles [SUCCESS=return] systemd\n", False),
                ("group: files [SUCCESS=merge] altfiles [SUCCESS=merge]\n", False),
                ("group: files systemd\n", False),
                ("# group: files [SUCCESS=merge] systemd\n", False),
                ("passwd: files [SUCCESS=merge] systemd\n", False),
                ("group: files [SUCCESS=return] systemd\n", False),
                ("group: files systemd [SUCCESS=merge]\n", False),
            ):
                with self.subTest(entry=entry):
                    nsswitch.write_text(entry)
                    if valid:
                        checks.check_group_merging(nsswitch)
                    else:
                        with self.assertRaisesRegex(SystemExit, "Missing files/systemd group merging"):
                            checks.check_group_merging(nsswitch)

    def check_password_pam(self, system_auth, passwd="password substack system-auth\n"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_auth_path = root / "system-auth"
            passwd_path = root / "passwd"
            system_auth_path.write_text(system_auth)
            passwd_path.write_text(passwd)
            checks.check_password_pam(system_auth_path, passwd_path)

    def test_password_pam_accepts_current_and_older_authselect_syntax(self):
        for prefix in ("password", "-password", "  -password"):
            for control in ("sufficient", "[success=done default=ignore]"):
                for delegation in ("password substack system-auth\n", "-password include system-auth\n"):
                    with self.subTest(prefix=prefix, control=control, delegation=delegation):
                        self.check_password_pam(f"{prefix}\t{control}\tpam_systemd_home.so\n", delegation)

    def test_password_pam_rejects_missing_or_inactive_homed_password_module(self):
        for system_auth in (
            "password sufficient pam_unix.so\n",
            "# -password sufficient pam_systemd_home.so\n",
            "auth sufficient pam_systemd_home.so\n",
            "password sufficient pam_unix.so pam_systemd_home.so\n",
            "password sufficient pam_systemd_home.so.disabled\n",
            "password sufficient\nauth sufficient pam_systemd_home.so\n",
        ):
            with self.subTest(system_auth=system_auth):
                with self.assertRaisesRegex(SystemExit, "Missing homed password support"):
                    self.check_password_pam(system_auth)

    def test_password_pam_rejects_missing_password_delegation(self):
        for passwd in (
            "# password substack system-auth\n",
            "auth include system-auth\n",
            "password include password-auth\n",
            "password include system-auth-other\n",
            "password\ninclude system-auth\n",
        ):
            with self.subTest(passwd=passwd):
                with self.assertRaisesRegex(SystemExit, "Missing system-auth password delegation"):
                    self.check_password_pam("-password sufficient pam_systemd_home.so\n", passwd)

    def test_accounts_policy_authenticates_admin_unlock_and_preserves_self_service(self):
        rule = (ROOT / "system/config/polkit/49-mertensia-accounts.rules").read_text()
        script = '''let callback;
const polkit = {
    Result: {NO: "no", YES: "yes", AUTH_SELF_KEEP: "auth_self_keep"},
    addRule: rule => { callback = rule; },
    spawn: () => { throw Error("Setup authorization must not be used"); }
};
''' + rule + '''
for (let active of [false, true]) {
    for (let local of [false, true]) {
        for (let admin of [false, true]) {
            let subject = {user: "alice", active, local, isInGroup: group => admin && group === "wheel"};
            for (let id of ["org.freedesktop.accounts.user-administration",
                            "org.gnome.controlcenter.user-accounts.administration"]) {
                let expected = active && local && admin ? "auth_self_keep" : "no";
                if (callback({id}, subject) !== expected) throw Error(id);
            }
            for (let id of ["org.freedesktop.accounts.change-own-password",
                            "org.freedesktop.accounts.change-own-user-data",
                            "org.freedesktop.home1.passwd-home",
                            "org.freedesktop.NetworkManager.settings.modify.system"]) {
                if (callback({id}, subject) !== undefined) throw Error(id);
            }
            let result = callback({id: "org.mertensia.accounts.manage"}, subject);
            if (result !== (active && local && admin ? "auth_self_keep" : "no"))
                throw Error("Encrypted account creation authorization changed");
        }
    }
}
'''
        result = subprocess.run(["gjs", "-c", script], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_accounts_redirect_rejects_stock_package_and_desktop_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary) / "gnome-users-panel.desktop"
            desktop.write_text("[Desktop Entry]\nName=Mertensia Accounts\nExec=/usr/bin/mertensia-accounts\n")
            checks.check_accounts_redirect(desktop, "1.fc45.mertensia1")
            with self.assertRaisesRegex(SystemExit, "Missing Mertensia Accounts redirect"):
                checks.check_accounts_redirect(desktop, "1.fc45")
            desktop.write_text("[Desktop Entry]\nName=Users\nExec=gnome-control-center system users\n")
            with self.assertRaisesRegex(SystemExit, "does not redirect"):
                checks.check_accounts_redirect(desktop, "1.fc45.mertensia1")

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
